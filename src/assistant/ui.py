"""Streamlit presentation; background execution and storage stay in services."""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

import streamlit as st

from config.settings import settings
from src.assistant import __version__, store
from src.assistant.runtime import get_runtime
from src.assistant.tools import REGISTRY, list_files, save_note, upload
from src.core.system.path_manager import PathManager
from src.core.system.runtime_settings import load_runtime_settings, save_runtime_settings

LABELS = {"QUEUED": "等待执行", "RUNNING": "执行中", "WAITING_APPROVAL": "等待确认",
          "COMPLETED": "已完成", "FAILED": "失败", "CANCELLED": "已取消",
          "INTERRUPTED": "执行中断", "LIMIT_REACHED": "达到预算限制", "NEEDS_ATTENTION": "需要检查",
          "PENDING": "待审批", "APPROVED": "已批准", "REJECTED": "已拒绝", "EXPIRED": "已过期",
          "UNKNOWN": "结果未知", "SUCCEEDED": "工具已返回"}
GROUPS = {"files": "文件与文档", "notes": "个人记忆", "calendar": "日程",
          "utilities": "计算与时间", "browser": "浏览器", "email": "邮箱草稿", "desktop_chat": "QQ / 微信"}


def local_time(value):
    return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M") if value else "—"


def boot():
    st.set_page_config(page_title="个人 Agent 助手", page_icon="◈", layout="wide", initial_sidebar_state="collapsed")
    st.markdown('''<style>
    .block-container{max-width:1360px;padding-top:3.4rem;padding-bottom:3rem}
    [data-testid="stHeader"]{background:transparent}
    h1{font-size:2rem!important;letter-spacing:-.035em} h2{font-size:1.3rem!important}
    [data-testid="stMetric"]{background:#30343b;border:1px solid #454b55;padding:18px;border-radius:14px}
    .eyebrow{color:#91b9a6;font:12px monospace;letter-spacing:.16em;margin-bottom:8px}
    .terminal{background:#080b0d;color:#8ad6ad;border:1px solid #303b34;border-radius:12px;
      padding:18px;white-space:pre-wrap;font:13px/1.8 Consolas,monospace;max-height:320px;overflow:auto}
    .stButton button{border-radius:9px} [data-testid="stChatMessage"]{border:1px solid #3c424c;border-radius:12px}
    </style>''', unsafe_allow_html=True)
    runtime = get_runtime()
    runtime.start()
    left, right = st.columns([5, 2], vertical_alignment="center")
    with left:
        st.markdown('<div class="eyebrow">PERSONAL AGENT ASSISTANT</div>', unsafe_allow_html=True)
        st.title("个人 Agent 助手")
    with right:
        st.caption(f"本地工作空间 · v{__version__}")
        st.caption("任务服务已启动" if runtime.running else "任务服务不可用")
    if runtime.error:
        st.warning(runtime.error)
    page = st.radio("工作区", ["概览", "对话", "任务与日程", "审批中心", "文件与记忆", "工具与设置"], horizontal=True, label_visibility="collapsed")
    st.divider()
    routes = {"概览": overview, "对话": chat, "任务与日程": tasks, "审批中心": approvals,
              "文件与记忆": files, "工具与设置": configuration}
    try:
        routes[page]()
    except Exception as exc:
        st.error(f"当前操作未完成（{type(exc).__name__}）。")
        if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
            st.caption(str(exc))


@st.fragment(run_every=3)
def overview():
    summary = store.rows("SELECT COUNT(*) AS total,COALESCE(SUM(input_tokens),0) AS input,COALESCE(SUM(output_tokens),0) AS output FROM tasks")[0]
    active = store.rows("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('QUEUED','RUNNING','WAITING_APPROVAL')")[0]["n"]
    pending = store.rows("SELECT COUNT(*) AS n FROM actions WHERE status='PENDING'")[0]["n"]
    for column, label, value in zip(st.columns(4), ["累计任务", "进行中", "等待确认", "累计 Token"],
                                    [summary["total"], active, pending, summary["input"] + summary["output"]]):
        column.metric(label, f"{value:,}")
    st.caption(f"输入 {summary['input']:,} / 输出 {summary['output']:,} · 服务未返回 usage 时采用保守估算。")
    a, b = st.columns([3, 2])
    with a:
        st.subheader("近期工作")
        for item in store.rows("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 5"):
            with st.container(border=True):
                st.write(item["prompt"][:120])
                st.caption(LABELS.get(item["status"], item["status"]) + " · " + local_time(item["created_at"]))
        if not summary["total"]:
            st.info("从“对话”开始：查找文件、整理笔记、生成文档，或安排未来任务。")
    with b:
        st.subheader("接下来")
        for item in store.rows("SELECT * FROM schedules WHERE enabled=1 ORDER BY next_run LIMIT 5"):
            st.write(item["title"])
            st.caption(local_time(item["next_run"]))
        st.caption("日程依赖应用运行和电脑唤醒；错过的时间不会补跑。")
    st.subheader("运行动态")
    events = store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 16")
    content = "\n".join(f"{local_time(e['created_at'])}  {e['message']}" for e in reversed(events)) or "就绪，等待你的任务。"
    st.markdown('<div class="terminal">' + html.escape(content) + '</div>', unsafe_allow_html=True)


@st.fragment(run_every=2)
def task_detail(task_id):
    item = store.task(task_id)
    st.caption("任务 " + task_id[:8] + " · " + LABELS.get(item["status"], item["status"]))
    for event in store.rows("SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 8", (task_id,))[::-1]:
        st.caption(event["message"])
    for action in store.rows("SELECT * FROM actions WHERE task_id=? ORDER BY created_at", (task_id,)):
        with st.container(border=True):
            tool = REGISTRY.get(action["tool"])
            st.write((tool.label if tool else action["tool"]) + " · " + LABELS.get(action["status"], action["status"]))
            with st.expander("参数与结果"):
                st.json(json.loads(action["args"]))
                if action["result"]:
                    st.text(action["result"][:8000])
    if item["answer"]:
        with st.chat_message("assistant"):
            st.markdown(item["answer"])
        if not store.rows("SELECT id FROM actions WHERE task_id=?", (task_id,)):
            st.caption("本次为模型文本回答，没有执行工具。")
    if item["error"]:
        st.warning(item["error"])
    if item["status"] == "WAITING_APPROVAL":
        st.info("请到审批中心检查具体内容并决定是否执行。")
    if item["status"] in {"QUEUED", "RUNNING", "WAITING_APPROVAL"}:
        if st.button("停止任务", key="stop_" + task_id):
            store.cancel(task_id)
            st.rerun(scope="fragment")
        st.caption("停止在当前调用结束后生效；已发生的外部动作无法自动撤销。")


def chat():
    st.subheader("告诉助手，你想完成什么")
    st.caption("可继续最近一次已完成的对话；每次任务最多携带前四轮文本。")
    if st.button("开始新对话"):
        st.session_state.pop("selected_task", None)
        st.rerun()
    selected = st.session_state.get("selected_task")
    if selected:
        with st.chat_message("user"):
            st.write(store.task(selected)["prompt"])
        task_detail(selected)
    prompt = st.chat_input("例如：读取我的项目说明，生成一份待办清单并保存为 Word")
    if prompt:
        if not get_runtime().running:
            st.error("任务服务尚未启动，请检查进程锁。")
            return
        parent = selected if selected and store.task(selected)["status"] == "COMPLETED" else None
        st.session_state["selected_task"] = store.create_task(prompt, parent_id=parent)
        st.rerun()


@st.dialog("添加日程")
def schedule_dialog():
    with st.form("schedule_create"):
        title = st.text_input("名称", max_chars=150)
        prompt = st.text_area("执行内容", help="到时创建任务；写入和发送操作仍需审批。")
        day = st.date_input("日期", value=datetime.now().date())
        time = st.time_input("时间", value=(datetime.now() + timedelta(hours=1)).time().replace(second=0, microsecond=0))
        interval = st.number_input("重复间隔（分钟，0 为单次）", 0, 525600, 0)
        st.caption("使用当前电脑的本地时区。")
        if st.form_submit_button("保存", type="primary"):
            try:
                store.add_schedule(title, prompt, datetime.combine(day, time).astimezone().isoformat(), int(interval))
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def tasks():
    st.subheader("日程")
    if st.button("添加日程", type="primary"):
        schedule_dialog()
    day = st.date_input("选择日期")
    schedules = [item for item in store.rows("SELECT * FROM schedules ORDER BY next_run")
                 if datetime.fromisoformat(item["next_run"]).astimezone().date() == day]
    if not schedules:
        st.caption("当日没有待运行日程。循环日程只显示最近一次待运行时间。")
    for item in schedules:
        with st.container(border=True):
            st.write(item["title"])
            st.caption(local_time(item["next_run"]) + (f" · 每 {item['interval_minutes']} 分钟" if item["interval_minutes"] else " · 单次"))
            st.write(item["prompt"])
            if st.button("停用" if item["enabled"] else "已停用", key=item["id"], disabled=not item["enabled"]):
                with store.connect() as db:
                    db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (item["id"],))
                st.rerun()
    st.divider()
    st.subheader("任务记录")
    items = store.rows("SELECT id,prompt,status,created_at,input_tokens,output_tokens FROM tasks ORDER BY created_at DESC LIMIT 100")
    if items:
        chosen = st.selectbox("查看任务", [i["id"] for i in items],
                              format_func=lambda key: next(i["prompt"][:60] + " · " + LABELS.get(i["status"], i["status"]) for i in items if i["id"] == key))
        task_detail(chosen)
        st.download_button("导出任务摘要 JSON", store.encode(items), "task-summary.json", "application/json")


@st.fragment(run_every=3)
def approvals():
    st.subheader("由你决定，再执行")
    st.caption("审批绑定工具和参数，30 分钟有效。拒绝会停止任务；程序重启后旧审批失效。")
    pending = store.rows("SELECT a.*,t.prompt FROM actions a JOIN tasks t ON a.task_id=t.id WHERE a.status='PENDING' AND t.status='WAITING_APPROVAL' ORDER BY a.created_at")
    if not pending:
        st.success("目前没有待确认操作。")
    for item in pending:
        with st.container(border=True):
            tool = REGISTRY.get(item["tool"])
            st.write(tool.label if tool else item["tool"])
            st.caption(item["prompt"][:200])
            st.json(json.loads(item["args"]))
            if item["tool"].startswith("browser_"):
                st.warning("确认前请核对浏览器当前页面；审批期间请勿更换目标页面。")
            yes, no = st.columns(2)
            try:
                if yes.button("确认执行", key="yes_" + item["id"], type="primary"):
                    store.decide(item["id"], True)
                    st.rerun(scope="fragment")
                if no.button("拒绝", key="no_" + item["id"]):
                    store.decide(item["id"], False)
                    st.rerun(scope="fragment")
            except ValueError as exc:
                st.error(str(exc))


def files():
    st.subheader("你的资料，只保留一份原件")
    st.caption("文件存放于 data/users；调用读取工具时，相关文本会进入模型上下文。")
    uploads = st.file_uploader("添加资料（单份最大 10 MiB）", type=["txt", "md", "pdf", "docx", "csv", "json"], accept_multiple_files=True)
    if st.button("保存上传文件", disabled=not uploads):
        for entry in uploads:
            st.success("已保存：" + upload(entry.name, entry.getvalue()))
    entries = list_files()
    if entries:
        st.dataframe(entries, hide_index=True, use_container_width=True)
    with st.expander("已生成文档"):
        root = PathManager.DOCUMENT_OUTPUT_DIR
        paths = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
        for path in paths[:30]:
            if path.is_file() and not path.is_symlink() and path.suffix in {".md", ".docx"} and path.stat().st_size <= 10 * 1024 * 1024:
                st.download_button(path.name, path.read_bytes(), file_name=path.name, key="doc_" + path.name)
    st.subheader("已确认的记忆与偏好")
    with st.form("note_form"):
        key = st.text_input("名称", max_chars=100)
        value = st.text_area("内容", max_chars=3000)
        if st.form_submit_button("保存") and key.strip() and value.strip():
            save_note(key.strip(), value.strip())
            st.rerun()
    for item in store.rows("SELECT * FROM memories ORDER BY updated_at DESC"):
        with st.expander(item["key"]):
            st.write(item["value"])
            if st.button("删除此记忆", key="memory_" + item["key"]):
                with store.connect() as db:
                    db.execute("DELETE FROM memories WHERE key=?", (item["key"],))
                st.rerun()


def configuration():
    st.subheader("模型与执行设置")
    runtime = load_runtime_settings(strict=True)
    limits = store.preferences()
    with st.form("assistant_settings"):
        base = st.text_input("OpenAI 兼容 API 地址", runtime["api_base"])
        model_name = st.text_input("模型名称", runtime["model_name"])
        st.caption("密钥从本机 .env / 环境变量读取。修改密钥后重启应用。")
        a, b = st.columns(2)
        steps = a.number_input("单任务最大模型调用次数", 1, 30, limits["max_steps"])
        budget = b.number_input("单任务 Token 预算", 2000, 100000, limits["token_budget"], step=1000)
        output = a.number_input("单次最大输出 Token", 128, 4096, limits["max_output_tokens"])
        timeout = b.number_input("模型请求超时（秒）", 5, 120, limits["timeout_seconds"])
        groups = st.multiselect("允许的工具分组", list(GROUPS), default=limits["enabled_groups"], format_func=lambda k: GROUPS[k])
        prompt = st.text_area("助手表达偏好", limits["system_prompt"], max_chars=6000)
        if st.form_submit_button("保存设置", type="primary"):
            save_runtime_settings({"api_base": base, "model_name": model_name})
            store.save_preferences({"max_steps": int(steps), "token_budget": int(budget),
                                    "max_output_tokens": int(output), "timeout_seconds": int(timeout),
                                    "enabled_groups": groups, "system_prompt": prompt})
            st.success("已保存，新的任务生效；已发出的请求不受影响。")
    st.subheader("工具目录")
    st.dataframe([{"工具": t.label, "分组": GROUPS[t.group], "执行确认": "需要" if t.approval else "读取",
                   "状态": "启用" if t.group in limits["enabled_groups"] else "停用", "说明": t.description}
                  for t in REGISTRY.values()], hide_index=True, use_container_width=True)
    with st.expander("环境与扩展"):
        st.write("API Key：" + ("已配置" if settings.MODEL_API_KEY else "未配置"))
        st.write("数据目录：" + str(settings.DATA_DIR))
        st.write("网页操作依赖 Kimi WebBridge；QQ / 微信依赖 Windows 桌面可访问性控件。")
        st.caption("这里只展示配置要求，未主动连接外部服务，不表示连接正常。")
        st.code("streamlit run career_app.py --server.port 8502", language="powershell")
        st.caption("历史求职流程作为独立扩展保留。避免两套工作台同时操纵同一网页。")
