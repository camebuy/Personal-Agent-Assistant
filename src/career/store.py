"""Durable state for job discovery, HR messages and one-shot external actions."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from src.core.system.path_manager import PathManager


_LOCK = threading.RLock()
_DB = PathManager.CAREER_DB
_INITIALIZED = False

DEFAULTS: dict[str, Any] = {
    "search_url": "https://www.zhipin.com/web/geek/jobs",
    "message_url": "https://www.zhipin.com/web/geek/chat",
    "resume_file": "",
    "profile_facts": "",
    "reply_policy_prompt": "先准确回答 HR 的具体问题；涉及面试时间、薪资、出差、加班或个人承诺时先生成草稿，等我审核。",
    "search_keywords": ["AI应用开发"],
    "job_rules": [],
    "opening_message": "",
    "image_resume_file": "",
    "supporting_files": [],
    "min_match_score": 70,
    "max_inspections_per_run": 2000,
    "keyword_switch_mode": "exhaust",
    "applications_per_keyword": 5,
    "max_applications_per_run": 2,
    "max_applications_per_day": 5,
    "poll_minutes": 20,
    "job_schedule_mode": "interval",
    "job_run_times": ["09:00"],
    "hr_poll_minutes": 5,
    "auto_apply": False,
    "auto_reply": False,
    "automation_enabled": False,
    "monitor_messages": False,
    "platform_resume_ready": False,
    "notify_qq": False,
    "qq_command_enabled": False,
    "qq_owner_qq": "",
    "qq_owner_name": "",
    "selectors": {
        "apply_button": ".job-detail-box .op-btn-chat",
        "application_receipt": 'a.btn-startchat[ka^="go_chat_done_"][data-isfriend="true"]',
        "conversation_rows": ".user-list-content ul[role='thead'] > li, .user-list-content ul[role='group'] > li",
        "incoming_rows": ".im-list > li.message-item.item-friend[data-mid]",
        "message_input": ".chat-conversation #chat-input",
        "message_send": ".chat-conversation button.btn-send",
        "outgoing_rows": ".im-list > li.message-item.item-myself[data-mid] .text-content",
    },
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    global _INITIALIZED
    _DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        _INITIALIZED = False
        raise
    finally:
        connection.close()


def init_store() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK, _connect() as db:
        if _INITIALIZED:
            return
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_key TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
                company TEXT NOT NULL, salary TEXT NOT NULL DEFAULT '',
                salary_source TEXT NOT NULL DEFAULT 'unreadable',
                location TEXT NOT NULL DEFAULT '', requirements TEXT NOT NULL DEFAULT '',
                jd_text TEXT NOT NULL DEFAULT '', jd_hash TEXT NOT NULL DEFAULT '',
                resume_hash TEXT NOT NULL DEFAULT '', analysis_hash TEXT NOT NULL DEFAULT '',
                match_state TEXT NOT NULL DEFAULT 'PENDING', match_score INTEGER,
                match_reason TEXT NOT NULL DEFAULT '', match_evidence TEXT NOT NULL DEFAULT '[]',
                match_tokens INTEGER NOT NULL DEFAULT 0,
                rule_results TEXT NOT NULL DEFAULT '[]',
                apply_state TEXT NOT NULL DEFAULT 'NOT_APPLIED',
                first_seen TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_key TEXT PRIMARY KEY, conversation_url TEXT NOT NULL,
                message_id TEXT NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL,
                contact_identity TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '', job_title TEXT NOT NULL DEFAULT '',
                job_key TEXT NOT NULL DEFAULT '', job_url TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '', reply_state TEXT NOT NULL DEFAULT 'PENDING',
                received_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_key TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
                receipt TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                attempted_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                UNIQUE(kind, target_key)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_code TEXT PRIMARY KEY, kind TEXT NOT NULL, target_key TEXT NOT NULL,
                target_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                decision TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(kind, target_key)
            );
            CREATE INDEX IF NOT EXISTS reviews_pending ON reviews(status, created_at);
            CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at DESC);
            CREATE INDEX IF NOT EXISTS messages_updated ON messages(updated_at DESC);
            CREATE INDEX IF NOT EXISTS actions_status ON actions(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS mail_notes (
                post_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS mail_notes_updated ON mail_notes(updated_at DESC);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "message_id" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")
        if "job_url" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN job_url TEXT NOT NULL DEFAULT ''")
        for column in ("contact_identity", "company", "job_title"):
            if column not in columns:
                db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "resume_hash" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN resume_hash TEXT NOT NULL DEFAULT ''")
        if "analysis_hash" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN analysis_hash TEXT NOT NULL DEFAULT ''")
        if "match_tokens" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN match_tokens INTEGER NOT NULL DEFAULT 0")
        if "rule_results" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN rule_results TEXT NOT NULL DEFAULT '[]'")
        if "salary_source" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN salary_source TEXT NOT NULL DEFAULT 'unreadable'")
            for row in db.execute("SELECT job_key,salary FROM jobs WHERE salary<>''").fetchall():
                if not re.search(r"[\uE000-\uF8FF]", row["salary"]):
                    db.execute("UPDATE jobs SET salary_source='dom' WHERE job_key=?",
                               (row["job_key"],))
        _INITIALIZED = True


def settings() -> dict[str, Any]:
    init_store()
    with _connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='career'").fetchone()
    try:
        saved = json.loads(row[0]) if row else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("求职配置文件无法读取；已保留原数据库，未覆盖设置。") from exc
    if not isinstance(saved, dict):
        raise ValueError("求职配置格式无效；已保留原数据库。")
    # Ignore settings removed from earlier releases, such as fixed keyword filters.
    result = {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}}
    old_contact = str(saved.get("qq_owner_contact") or "").strip()
    if not result["qq_owner_qq"] and old_contact:
        if re.fullmatch(r"[1-9][0-9]{4,11}", old_contact):
            result["qq_owner_qq"] = old_contact
        else:
            result["qq_owner_name"] = result["qq_owner_name"] or old_contact
            # A display name alone cannot verify the recipient account.
            result["notify_qq"] = False
            result["qq_command_enabled"] = False
    if "search_keywords" not in saved and saved.get("target_keywords"):
        old_terms = str(saved["target_keywords"]).replace("，", ",").split(",")
        result["search_keywords"] = [term.strip() for term in old_terms if term.strip()][:12]
    if "job_rules" not in saved and saved.get("job_policy_prompt"):
        result["job_rules"] = [str(saved["job_policy_prompt"])]
    if result["search_url"] == "https://www.zhipin.com/web/geek/job":
        result["search_url"] = DEFAULTS["search_url"]
    saved_selectors = saved.get("selectors") or {}
    if not isinstance(saved_selectors, dict):
        raise ValueError("网页控件配置格式无效；已保留原数据库。")
    result["selectors"] = {
        **DEFAULTS["selectors"],
        **{key: value for key, value in saved_selectors.items() if value or not DEFAULTS["selectors"].get(key)},
    }
    return result


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    current = settings()
    allowed = set(DEFAULTS)
    if set(changes) - allowed:
        raise ValueError("存在未知配置项。")
    merged = {**current, **changes}
    if "selectors" in changes:
        if not isinstance(changes["selectors"], dict) or set(changes["selectors"]) - set(DEFAULTS["selectors"]):
            raise ValueError("网页选择器配置无效。")
        merged["selectors"] = {**current["selectors"], **changes["selectors"]}
    for name, low, high in (
        ("min_match_score", 0, 100), ("max_inspections_per_run", 1, 2000),
        ("applications_per_keyword", 1, 30),
        ("max_applications_per_run", 1, 30),
        ("max_applications_per_day", 1, 30), ("poll_minutes", 5, 1440),
        ("hr_poll_minutes", 1, 1440),
    ):
        number = merged[name]
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError(f"{name} 超出允许范围。")
    for key in ("auto_apply", "auto_reply", "automation_enabled", "monitor_messages",
                "platform_resume_ready", "notify_qq", "qq_command_enabled"):
        if not isinstance(merged[key], bool):
            raise ValueError(f"{key} 必须为开关值。")
    if (not isinstance(merged["keyword_switch_mode"], str) or
            merged["keyword_switch_mode"] not in {"exhaust", "quota"}):
        raise ValueError("关键词切换方式无效。")
    if merged["job_schedule_mode"] not in {"interval", "daily"}:
        raise ValueError("岗位巡航定时方式无效。")
    times = merged["job_run_times"]
    if (not isinstance(times, list) or not 1 <= len(times) <= 12 or
            any(not isinstance(value, str) or len(value) != 5 or
                value[2] != ":" or not value[:2].isdigit() or
                not value[3:].isdigit() or int(value[:2]) > 23 or
                int(value[3:]) > 59 for value in times) or
            len(set(times)) != len(times)):
        raise ValueError("每天巡航时间须为不重复的 HH:MM，最多 12 个。")
    for key in ("search_url", "message_url"):
        parts = urlsplit(str(merged[key]))
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() != "https" or (host != "zhipin.com" and not host.endswith(".zhipin.com")):
            raise ValueError(f"{key} 必须使用 BOSS 直聘的 HTTPS 地址。")
    for key in ("reply_policy_prompt",):
        if not isinstance(merged[key], str) or len(merged[key]) > 4000:
            raise ValueError(f"{key} 最多 4000 字。")
    for key, max_items in (("search_keywords", 12), ("job_rules", 20),
                           ("supporting_files", 10)):
        values = merged[key]
        if (not isinstance(values, list) or len(values) > max_items or
                any(not isinstance(item, str) or not item.strip() or len(item) > 250
                    for item in values)):
            raise ValueError(f"{key} 列表无效或超过上限。")
    if not merged["search_keywords"]:
        raise ValueError("请至少添加一个网页搜索关键词。")
    if any(len(term) > 80 for term in merged["search_keywords"]):
        raise ValueError("每个网页搜索关键词最多 80 字。")
    for name in [merged["resume_file"], merged["image_resume_file"], *merged["supporting_files"]]:
        if name and (not isinstance(name, str) or Path(name).name != name or
                     '/' in name or '\\' in name):
            raise ValueError("文件配置只能使用 data/users 中的文件名。")
    if not isinstance(merged["opening_message"], str) or len(merged["opening_message"]) > 500:
        raise ValueError("固定开场白最多 500 字。")
    if merged["qq_owner_qq"] and not re.fullmatch(r"[1-9][0-9]{4,11}", str(merged["qq_owner_qq"])):
        raise ValueError("大号 QQ 号须为 5 到 12 位数字。")
    if not isinstance(merged["qq_owner_name"], str) or len(merged["qq_owner_name"]) > 100:
        raise ValueError("QQ 会话显示名称最多 100 字。")
    if merged["notify_qq"] and not str(merged["qq_owner_qq"]).strip():
        raise ValueError("启用 QQ 通知前必须填写自己的大号 QQ 号。")
    if merged["qq_command_enabled"] and not merged["notify_qq"]:
        raise ValueError("启用 QQ 远程审核前，须先开启 QQ 通知。")
    if merged["auto_apply"] and (not merged["platform_resume_ready"] or
                                  not str(merged["resume_file"]).strip() or
                                  not merged["selectors"]["apply_button"] or
                                  not merged["selectors"]["application_receipt"]):
        raise ValueError("启用自动投递前，须确认平台简历已配置并填写投递按钮和成功回执选择器。")
    if (merged["automation_enabled"] or merged["monitor_messages"]) and not str(merged["resume_file"]).strip():
        raise ValueError("开启岗位巡航或 HR 消息检查前，须先选择供 AI 阅读的主简历。")
    if merged["auto_reply"] and not all(merged["selectors"][key] for key in
                                        ("conversation_rows", "incoming_rows", "message_input",
                                         "message_send", "outgoing_rows")):
        raise ValueError("启用自动回复前，须完成 HR 会话页面选择器适配。")
    if merged["auto_reply"] and not str(merged["resume_file"]).strip():
        raise ValueError("启用自动回复前，须选择一份简历。")
    if merged["auto_reply"] and not merged["monitor_messages"]:
        raise ValueError("启用自动回复前，须先开启 HR 消息定时检查。")
    with _LOCK, _connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES('career',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_json(merged),))
    return merged


def job_key(url: str) -> str:
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or (host != "zhipin.com" and not host.endswith(".zhipin.com")):
        raise ValueError("只接受 BOSS 直聘岗位链接。")
    path = parts.path.rstrip("/")
    if "/job_detail/" not in path:
        raise ValueError("链接不是可核实的岗位详情页。")
    return hashlib.sha256((host + path).encode("utf-8")).hexdigest()[:24]


def upsert_job(observed: dict[str, str]) -> tuple[str, bool]:
    key = job_key(observed.get("url", ""))
    title = str(observed.get("title") or "").strip()
    company = str(observed.get("company") or "").strip()
    if not title or not company:
        raise ValueError("岗位名称和公司必须来自可见页面。")
    jd = str(observed.get("jd_text") or "").strip()[:30000]
    salary = str(observed.get("salary") or "")
    salary_source = str(observed.get("salary_source") or "unreadable")
    if salary_source not in {"dom", "ocr", "unreadable"}:
        raise ValueError("薪资来源无效。")
    if salary_source == "unreadable":
        salary = ""
    digest = hashlib.sha256(jd.encode("utf-8")).hexdigest() if jd else ""
    now = _now()
    with _LOCK, _connect() as db:
        prior = db.execute("SELECT jd_hash,salary,apply_state FROM jobs WHERE job_key=?", (key,)).fetchone()
        changed = (prior is None or prior["salary"] != salary or
                   (bool(digest) and prior["jd_hash"] != digest))
        if prior is None:
            db.execute("""INSERT INTO jobs(job_key,url,title,company,salary,salary_source,location,requirements,
                        jd_text,jd_hash,first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (key, observed["url"], title, company, salary, salary_source,
                        str(observed.get("location") or ""), str(observed.get("requirements") or ""),
                        jd, digest, now, now))
        else:
            db.execute("""UPDATE jobs SET url=?,title=?,company=?,salary=?,salary_source=?,location=?,requirements=?,
                        jd_text=CASE WHEN ?<>'' THEN ? ELSE jd_text END,
                        jd_hash=CASE WHEN ?<>'' THEN ? ELSE jd_hash END,
                        match_state=CASE WHEN ? THEN 'PENDING' ELSE match_state END,
                        match_score=CASE WHEN ? THEN NULL ELSE match_score END,
                        match_tokens=CASE WHEN ? THEN 0 ELSE match_tokens END,
                        rule_results=CASE WHEN ? THEN '[]' ELSE rule_results END,
                        analysis_hash=CASE WHEN ? THEN '' ELSE analysis_hash END,
                        updated_at=? WHERE job_key=?""",
                       (observed["url"], title, company, salary, salary_source,
                        str(observed.get("location") or ""), str(observed.get("requirements") or ""),
                        digest, jd, digest, digest, int(changed), int(changed), int(changed),
                        int(changed), int(changed), now, key))
    return key, changed


def get_job(key: str) -> dict[str, Any] | None:
    init_store()
    with _connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    return dict(row) if row else None


def find_job_by_title_company(title: str, company: str) -> dict[str, Any] | None:
    """Associate a chat with a JD only when exactly one recorded job matches."""
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT * FROM jobs WHERE title=? AND company=? LIMIT 2",
                          (title.strip(), company.strip())).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def jobs_brief(limit: int = 200) -> list[dict[str, Any]]:
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT job_key,title,company,match_state,match_score,match_tokens,"
                          "apply_state,updated_at "
                          "FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def apply_states(keys: list[str]) -> dict[str, str]:
    """Read the current apply state for one visible search page in one query."""
    if not keys:
        return {}
    init_store()
    placeholders = ",".join("?" for _ in keys)
    with _connect() as db:
        rows = db.execute(
            f"SELECT job_key,apply_state FROM jobs WHERE job_key IN ({placeholders})", keys
        ).fetchall()
    return {row["job_key"]: row["apply_state"] for row in rows}


def dashboard_counts() -> dict[str, int]:
    init_store()
    with _connect() as db:
        job_count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        job_review = db.execute("SELECT count(*) FROM reviews WHERE kind='job' "
                                "AND status='PENDING'").fetchone()[0]
        message_review = db.execute("SELECT count(*) FROM messages WHERE reply_state='REVIEW'").fetchone()[0]
    return {"jobs": int(job_count), "job_review": int(job_review),
            "message_review": int(message_review)}


def set_match(key: str, score: int | None, state: str, reason: str,
              evidence: list[str], resume_hash: str = "", analysis_hash: str = "",
              match_tokens: int = 0, rule_results: list[dict[str, str]] | None = None) -> None:
    if state not in {"MATCHED", "REVIEW", "SKIPPED", "ERROR"}:
        raise ValueError("匹配状态无效。")
    with _LOCK, _connect() as db:
        db.execute("UPDATE jobs SET match_score=?,match_state=?,match_reason=?,match_evidence=?,"
                   "resume_hash=?,analysis_hash=?,match_tokens=?,rule_results=?,updated_at=? WHERE job_key=?",
                   (score, state, reason[:1000], _json(evidence[:12]), resume_hash,
                    analysis_hash, max(0, int(match_tokens)), _json(rule_results or []), _now(), key))


def set_apply_state(key: str, state: str) -> None:
    if state not in {"NOT_APPLIED", "APPROVED", "SENT_VISIBLE", "UNKNOWN", "SKIPPED"}:
        raise ValueError("投递状态无效。")
    with _LOCK, _connect() as db:
        db.execute("UPDATE jobs SET apply_state=?,updated_at=? WHERE job_key=?", (state, _now(), key))


def message_key(conversation_url: str, message_id: str) -> str:
    if not message_id.strip():
        raise ValueError("缺少网页可核实的消息 ID，不能自动处理。")
    raw = conversation_url.split("#", 1)[0] + "|" + message_id
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def upsert_message(observed: dict[str, str]) -> tuple[str, bool]:
    url = str(observed.get("conversation_url") or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or (host != "zhipin.com" and not host.endswith(".zhipin.com")):
        raise ValueError("HR 会话链接不属于 BOSS 直聘。")
    key = message_key(url, str(observed.get("message_id") or ""))
    sender = str(observed.get("sender") or "").strip()
    body = str(observed.get("body") or "").strip()
    contact_identity = str(observed.get("contact_identity") or "").strip()
    company = str(observed.get("company") or "").strip()
    job_title = str(observed.get("job_title") or "").strip()
    if not sender or not body or not contact_identity or not company:
        raise ValueError("消息缺少可核实的联系人、公司或正文。")
    related_url = str(observed.get("job_url") or "").strip()
    try:
        related_key = job_key(related_url) if related_url else ""
    except ValueError:
        related_url, related_key = "", ""
    now = _now()
    with _LOCK, _connect() as db:
        if not related_key and job_title:
            jobs = db.execute("SELECT job_key,url FROM jobs WHERE title=? AND company=? LIMIT 2",
                              (job_title, company)).fetchall()
            if len(jobs) == 1:
                related_key, related_url = jobs[0]["job_key"], jobs[0]["url"]
        cursor = db.execute("""INSERT OR IGNORE INTO messages
            (message_key,conversation_url,message_id,sender,body,contact_identity,company,
             job_title,job_key,job_url,received_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, url, str(observed["message_id"]), sender, body[:10000],
             contact_identity, company, job_title, related_key, related_url, now, now))
        if not cursor.rowcount:
            existing = db.execute(
                "SELECT contact_identity,company,body FROM messages WHERE message_key=?", (key,)
            ).fetchone()
            if (existing is None or existing["contact_identity"] != contact_identity
                    or existing["company"] != company or existing["body"] != body[:10000]):
                raise ValueError("网页消息 ID 与已有联系人或正文冲突，已停止处理以免误关联。")
    return key, bool(cursor.rowcount)


def link_message_job(message_key: str, job: dict[str, Any]) -> None:
    with _LOCK, _connect() as db:
        db.execute("UPDATE messages SET job_key=?,job_url=?,updated_at=? WHERE message_key=? AND job_key=''",
                   (job["job_key"], job["url"], _now(), message_key))


def messages_brief(limit: int = 200) -> list[dict[str, Any]]:
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT message_key,sender,reply_state,received_at,updated_at "
                          "FROM messages ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def retryable_messages(limit: int = 50) -> list[dict[str, Any]]:
    """Resume analysis or a pre-send draft after a transient process failure."""
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT * FROM messages WHERE reply_state IN ('PENDING','ERROR','READY') "
                          "ORDER BY updated_at ASC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_message(key: str) -> dict[str, Any] | None:
    init_store()
    with _connect() as db:
        row = db.execute("SELECT * FROM messages WHERE message_key=?", (key,)).fetchone()
    return dict(row) if row else None


def set_message_plan(key: str, category: str, draft: str, reason: str, state: str) -> None:
    if state not in {"REVIEW", "READY", "NO_REPLY", "ERROR"}:
        raise ValueError("消息状态无效。")
    with _LOCK, _connect() as db:
        db.execute("UPDATE messages SET category=?,draft=?,reason=?,reply_state=?,updated_at=? "
                   "WHERE message_key=? AND reply_state IN ('PENDING','ERROR','REVIEW')",
                   (category, draft[:2000], reason[:1000], state, _now(), key))


def set_reply_state(key: str, state: str) -> None:
    if state not in {"REVIEW", "READY", "NO_REPLY", "SENT_VISIBLE", "UNKNOWN"}:
        raise ValueError("消息状态无效。")
    with _LOCK, _connect() as db:
        db.execute("UPDATE messages SET reply_state=?,updated_at=? WHERE message_key=?",
                   (state, _now(), key))


def update_message_draft(key: str, draft: str) -> None:
    text = str(draft or "").strip()
    if not text or len(text) > 300:
        raise ValueError("回复草稿必须为 1–300 字。")
    with _LOCK, _connect() as db:
        cursor = db.execute("UPDATE messages SET draft=?,reply_state='REVIEW',updated_at=? "
                            "WHERE message_key=? AND reply_state IN ('REVIEW','READY')",
                            (text, _now(), key))
        if not cursor.rowcount:
            raise ValueError("消息已发送或结果未知，不能修改草稿。")


def reserve_action(kind: str, target_key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """One unique reservation. An ambiguous action can never be retried automatically."""
    if kind not in {"apply", "opening", "image_resume", "hr_reply", "qq_notice", "mail_send"}:
        raise ValueError("动作类型无效。")
    init_store()
    now = _now()
    action_id = uuid.uuid4().hex
    with _LOCK, _connect() as db:
        cursor = db.execute("""INSERT OR IGNORE INTO actions
            (action_id,kind,target_key,status,payload,created_at,updated_at)
            VALUES(?,?,?,'RESERVED',?,?,?)""", (action_id, kind, target_key, _json(payload), now, now))
    return {"action_id": action_id, "kind": kind, "target_key": target_key} if cursor.rowcount else None


def action(kind: str, target_key: str) -> dict[str, Any] | None:
    init_store()
    with _connect() as db:
        row = db.execute("SELECT * FROM actions WHERE kind=? AND target_key=?", (kind, target_key)).fetchone()
    return dict(row) if row else None


def mark_action(action_id: str, status: str, receipt: dict[str, Any] | None = None) -> None:
    if status not in {"ATTEMPTING", "SENT_VISIBLE", "UNKNOWN", "PRE_CLICK_FAILED", "CANCELLED"}:
        raise ValueError("动作状态无效。")
    now = _now()
    with _LOCK, _connect() as db:
        db.execute("""UPDATE actions SET status=?,receipt=?,
            attempted_at=CASE WHEN ?='ATTEMPTING' THEN ? ELSE attempted_at END,
            updated_at=? WHERE action_id=?""",
            (status, _json(receipt or {}), status, now, now, action_id))


def recent_actions(kind: str = "", limit: int = 100) -> list[dict[str, Any]]:
    init_store()
    with _connect() as db:
        rows = (db.execute("SELECT * FROM actions WHERE kind=? ORDER BY created_at DESC LIMIT ?", (kind, limit))
                if kind else db.execute("SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,))).fetchall()
    return [dict(row) for row in rows]


def reserve_review(kind: str, target_key: str, target_hash: str) -> dict[str, Any]:
    if kind not in {"job", "hr"} or not target_key or not target_hash:
        raise ValueError("审核对象无效。")
    init_store()
    now = _now()
    with _LOCK, _connect() as db:
        row = db.execute("SELECT * FROM reviews WHERE kind=? AND target_key=?",
                         (kind, target_key)).fetchone()
        if row:
            if row["target_hash"] == target_hash or row["status"] == "PROCESSING":
                return dict(row)
            if row["status"] in {"PENDING", "STALE", "UNKNOWN"}:
                db.execute("DELETE FROM reviews WHERE review_code=?", (row["review_code"],))
            else:
                return dict(row)
        code = secrets.token_hex(6).upper()
        db.execute("INSERT INTO reviews(review_code,kind,target_key,target_hash,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?)", (code, kind, target_key, target_hash, now, now))
        row = db.execute("SELECT * FROM reviews WHERE review_code=?", (code,)).fetchone()
    return dict(row)


def pending_reviews(limit: int = 100) -> list[dict[str, Any]]:
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT * FROM reviews WHERE status='PENDING' "
                          "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def review_for_target(kind: str, target_key: str) -> dict[str, Any] | None:
    init_store()
    with _connect() as db:
        row = db.execute("SELECT * FROM reviews WHERE kind=? AND target_key=?",
                         (kind, target_key)).fetchone()
    return dict(row) if row else None


def claim_review(code: str, decision: str) -> dict[str, Any] | None:
    if decision not in {"approve", "reject"}:
        raise ValueError("审核指令无效。")
    init_store()
    with _LOCK, _connect() as db:
        cursor = db.execute("UPDATE reviews SET status='PROCESSING',decision=?,updated_at=? "
                            "WHERE review_code=? AND status='PENDING'",
                            (decision, _now(), code))
        if not cursor.rowcount:
            return None
        row = db.execute("SELECT * FROM reviews WHERE review_code=?", (code,)).fetchone()
    return dict(row)


def finish_review(code: str, status: str, detail: str = "") -> None:
    if status not in {"APPROVED", "REJECTED", "STALE", "UNKNOWN"}:
        raise ValueError("审核结果无效。")
    with _LOCK, _connect() as db:
        db.execute("UPDATE reviews SET status=?,detail=?,updated_at=? "
                   "WHERE review_code=? AND status='PROCESSING'",
                   (status, detail[:500], _now(), code))


def start_run(kind: str) -> str:
    init_store()
    run_id = uuid.uuid4().hex[:12]
    with _LOCK, _connect() as db:
        db.execute("INSERT INTO runs(run_id,kind,status,started_at) VALUES(?,?,'RUNNING',?)",
                   (run_id, kind, _now()))
    return run_id


def finish_run(run_id: str, status: str, summary: str) -> None:
    with _LOCK, _connect() as db:
        db.execute("UPDATE runs SET status=?,summary=?,finished_at=? WHERE run_id=?",
                   (status, summary[:2000], _now(), run_id))


def runs(limit: int = 50) -> list[dict[str, Any]]:
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def applications_today() -> int:
    init_store()
    today = datetime.now().astimezone().date().isoformat()
    with _connect() as db:
        row = db.execute("SELECT count(*) FROM actions WHERE kind='apply' AND substr(created_at,1,10)=? "
                         "AND status IN ('ATTEMPTING','SENT_VISIBLE','UNKNOWN')", (today,)).fetchone()
    return int(row[0])


def applications_sent_count() -> int:
    init_store()
    with _connect() as db:
        row = db.execute("SELECT count(*) FROM actions WHERE kind='apply' "
                         "AND status='SENT_VISIBLE'").fetchone()
    return int(row[0])


def _mail_post_id(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() != "https" or parts.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        raise ValueError("备注只能关联小红书 HTTPS 帖子链接。")
    match = re.fullmatch(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)/?", parts.path)
    if not match:
        raise ValueError("备注链接不是有效的小红书帖子。")
    return match.group(1)


def mail_note(url: str) -> dict[str, Any] | None:
    key = _mail_post_id(url)
    init_store()
    with _connect() as db:
        row = db.execute("SELECT * FROM mail_notes WHERE post_id=?", (key,)).fetchone()
    return dict(row) if row else None


def save_mail_note(url: str, title: str, note: str) -> None:
    key = _mail_post_id(url)
    note = str(note or "").strip()
    title = str(title or "").strip()
    if len(note) > 2000 or len(title) > 300:
        raise ValueError("备注最多 2000 字，标题最多 300 字。")
    init_store()
    now = _now()
    with _LOCK, _connect() as db:
        db.execute("""INSERT INTO mail_notes(post_id,url,title,note,created_at,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
            url=excluded.url,title=excluded.title,note=excluded.note,updated_at=excluded.updated_at""",
                   (key, url, title, note, now, now))


def recent_mail_notes(limit: int = 30) -> list[dict[str, Any]]:
    init_store()
    with _connect() as db:
        rows = db.execute("SELECT * FROM mail_notes WHERE note<>'' ORDER BY updated_at DESC LIMIT ?",
                          (max(1, min(int(limit), 100)),)).fetchall()
    return [dict(row) for row in rows]
