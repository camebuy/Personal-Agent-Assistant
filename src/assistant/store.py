"""Transactional state. No model or UI dependency; all times are UTC ISO strings."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.system.path_manager import PathManager


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


@contextmanager
def connect():
    PathManager.ASSISTANT_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(PathManager.ASSISTANT_DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript('''
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES(1);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
          messages TEXT NOT NULL DEFAULT '[]', answer TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          steps INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
          source TEXT NOT NULL DEFAULT 'manual', parent_id TEXT REFERENCES tasks(id));
        CREATE TABLE IF NOT EXISTS actions(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
          call_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
          status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', page_context TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, UNIQUE(task_id,call_id));
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
          level TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schedules(
          id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
          next_run TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 0,
          enabled INTEGER NOT NULL DEFAULT 1, last_task TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memories(
          key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS task_status ON tasks(status,created_at);
        CREATE INDEX IF NOT EXISTS action_status ON actions(status,created_at);
        CREATE INDEX IF NOT EXISTS event_task ON events(task_id,id);
        ''')


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute(sql, params)]


def task(task_id: str) -> dict:
    found = rows("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not found:
        raise ValueError("任务不存在。")
    return found[0]


def create_task(prompt: str, source: str = "manual", db=None, parent_id: str | None = None) -> str:
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 16000:
        raise ValueError("任务内容需为 1–16000 字。")
    if db is None:
        with connect() as connection:
            return create_task(prompt, source, connection, parent_id)
    task_id = uuid.uuid4().hex
    if parent_id:
        previous = db.execute("SELECT status FROM tasks WHERE id=?", (parent_id,)).fetchone()
        if not previous or previous["status"] != "COMPLETED":
            raise ValueError("只能继续已完成的对话。")
    db.execute("INSERT INTO tasks(id,prompt,status,created_at,updated_at,source,parent_id) VALUES(?,?,'QUEUED',?,?,?,?)",
               (task_id, prompt, now(), now(), source, parent_id))
    return task_id


def update_task(task_id: str, **values) -> None:
    allowed = {"status", "messages", "answer", "error", "steps", "cancel_requested"}
    if not values or not set(values) <= allowed:
        raise ValueError("无效的任务字段。")
    values["updated_at"] = now()
    with connect() as db:
        db.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                   (*values.values(), task_id))


def event(task_id: str | None, message: str, level: str = "INFO") -> None:
    with connect() as db:
        db.execute("INSERT INTO events(task_id,level,message,created_at) VALUES(?,?,?,?)",
                   (task_id, level, message[:4000], now()))


def claim_task() -> dict | None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tasks WHERE status='QUEUED' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE tasks SET status='RUNNING',updated_at=? WHERE id=?", (now(), row["id"]))
            return dict(row)
    return None


def recover() -> None:
    """Called only by the process holding the OS worker lock; never replay actions."""
    with connect() as db:
        db.execute("UPDATE tasks SET status='INTERRUPTED',error='进程中断；请检查执行记录后新建任务。',updated_at=? WHERE status IN ('RUNNING','QUEUED')", (now(),))
        db.execute("UPDATE actions SET status='UNKNOWN',updated_at=? WHERE status='RUNNING'", (now(),))
        db.execute("UPDATE tasks SET status='INTERRUPTED',error='审批已过期，请核对页面后重新发起任务。',updated_at=? WHERE status='WAITING_APPROVAL'", (now(),))
        db.execute("UPDATE actions SET status='EXPIRED',updated_at=? WHERE status IN ('PENDING','APPROVED')", (now(),))
    tick_schedules(startup=True)


def cancel(task_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE tasks SET cancel_requested=1,status=CASE WHEN status IN ('QUEUED','WAITING_APPROVAL') THEN 'CANCELLED' ELSE status END,updated_at=? WHERE id=?", (now(), task_id))
        db.execute("UPDATE actions SET status='REJECTED',updated_at=? WHERE task_id=? AND status IN ('PENDING','APPROVED')", (now(), task_id))


def decide(action_id: str, approve: bool) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT a.*,t.status AS task_status,t.cancel_requested FROM actions a JOIN tasks t ON a.task_id=t.id WHERE a.id=?", (action_id,)).fetchone()
        if not row or row["status"] != "PENDING" or row["task_status"] != "WAITING_APPROVAL" or row["cancel_requested"]:
            raise ValueError("审批已处理或任务已失效。")
        if approve and datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) > timedelta(minutes=30):
            raise ValueError("审批已超过 30 分钟，请取消并重新发起任务。")
        db.execute("UPDATE actions SET status=?,updated_at=? WHERE id=?",
                   ("APPROVED" if approve else "REJECTED", now(), action_id))
        db.execute("UPDATE tasks SET status='QUEUED',updated_at=? WHERE id=?", (now(), row["task_id"]))


def add_schedule(title: str, prompt: str, run_at: str, interval_minutes: int = 0) -> str:
    stamp = datetime.fromisoformat(run_at)
    if stamp.tzinfo is None or stamp <= datetime.now(timezone.utc):
        raise ValueError("请选择未来时间，并包含时区。")
    if not title.strip() or not prompt.strip() or len(title) > 150 or len(prompt) > 16000:
        raise ValueError("日程名称或内容无效。")
    if interval_minutes != 0 and not 5 <= interval_minutes <= 525600:
        raise ValueError("循环间隔需为 5–525600 分钟，0 表示单次。")
    key = uuid.uuid4().hex
    with connect() as db:
        db.execute("INSERT INTO schedules(id,title,prompt,next_run,interval_minutes,created_at) VALUES(?,?,?,?,?,?)",
                   (key, title.strip(), prompt.strip(), stamp.astimezone(timezone.utc).isoformat(timespec="seconds"), interval_minutes, now()))
    return key


def tick_schedules(startup: bool = False) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = datetime.now(timezone.utc)
        for row in db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now(),)).fetchall():
            due = datetime.fromisoformat(row["next_run"])
            active = db.execute("SELECT status FROM tasks WHERE id=?", (row["last_task"],)).fetchone()
            busy = active and active["status"] in {"QUEUED", "RUNNING", "WAITING_APPROVAL"}
            # A sleeping/offline device does not replay old occurrences.
            task_id = None
            if not startup and not busy and (current - due).total_seconds() <= 90:
                task_id = create_task(row["prompt"], "schedule", db)
            if row["interval_minutes"]:
                step = timedelta(minutes=row["interval_minutes"])
                future = due + step * (int((current - due) // step) + 1)
                db.execute("UPDATE schedules SET next_run=?,last_task=COALESCE(?,last_task) WHERE id=?",
                           (future.isoformat(timespec="seconds"), task_id, row["id"]))
            else:
                db.execute("UPDATE schedules SET enabled=0,last_task=? WHERE id=?", (task_id, row["id"]))
            if task_id is None:
                db.execute("INSERT INTO events(level,message,created_at) VALUES('INFO',?,?)",
                           ("已跳过过期或重叠日程：" + row["title"], now()))


DEFAULTS = {"max_steps": 10, "token_budget": 24000, "timeout_seconds": 45,
            "max_output_tokens": 1500, "enabled_groups": ["files", "notes", "calendar", "utilities"],
            "system_prompt": "请用中文简洁回答，遇到信息不足时向用户询问。"}


def preferences() -> dict:
    result = dict(DEFAULTS)
    for row in rows("SELECT key,value FROM preferences"):
        if row["key"] in DEFAULTS:
            result[row["key"]] = json.loads(row["value"])
    return result


def save_preferences(values: dict) -> None:
    ranges = {"max_steps": (1, 30), "token_budget": (2000, 100000),
              "timeout_seconds": (5, 120), "max_output_tokens": (128, 4096)}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError("未知配置项。")
        if key in ranges and (type(value) is not int or not ranges[key][0] <= value <= ranges[key][1]):
            raise ValueError("运行限制超出范围。")
        if key == "enabled_groups" and (not isinstance(value, list) or not set(value) <= {"files", "notes", "calendar", "utilities", "browser", "email", "desktop_chat"}):
            raise ValueError("工具分组无效。")
        if key == "system_prompt" and (not isinstance(value, str) or len(value) > 6000):
            raise ValueError("提示词长度超过限制。")
    with connect() as db:
        db.executemany("INSERT INTO preferences VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       [(k, encode(v)) for k, v in values.items()])
