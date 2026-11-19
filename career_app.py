"""Focused Streamlit workbench for the deterministic career assistant."""
from __future__ import annotations

import html
import json
import socket

import streamlit as st

from config.settings import settings as app_settings
from src.career import bridge, model, store, workflow
from src.core.system.runtime_settings import load_runtime_settings, save_runtime_settings
from src.tools.token_tracker import get_today_stats


st.set_page_config(page_title="个人 Agent 助手 · 求职扩展", page_icon="G", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
:root{--ink:#d6d8dc;--muted:#9da2a8;--line:#484b50;--canvas:#2b2d30;
  --surface:#33363a;--field:#3b3e42;--accent:#589878;color-scheme:dark}
html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"],
[data-testid="stMainBlockContainer"]{background:#2b2d30!important;color:var(--ink)!important}
[data-testid="stHeader"]{background:#2b2d30!important}
[data-testid="stSidebar"], [data-testid="collapsedControl"]{display:none!important}
[data-testid="stMainBlockContainer"]{max-width:1380px;padding-top:5rem!important}
h1,h2,h3,h4,.stMarkdown,.stMarkdown p{color:var(--ink)!important}
[data-testid="stVerticalBlockBorderWrapper"]{border-color:var(--line)!important;
  border-radius:12px;background:var(--surface)!important}
[data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input,[data-baseweb="select"]>div{
  background:var(--field)!important;color:var(--ink)!important;border-color:var(--line)!important}
div.stButton>button{background:var(--surface)!important;color:var(--ink)!important;
  border-color:var(--line)!important;border-radius:9px}
div.stButton>button:hover{border-color:var(--accent)!important;color:#b8e0c9!important}
div.stButton>button[kind="primary"]{background:var(--accent)!important;color:#f2f7f4!important}
.note{color:var(--muted);font-size:.9rem}
.terminal{background:#050706;border:1px solid #17241c;border-radius:12px;padding:16px;
  color:#55dc84;font:13px/1.7 Consolas,monospace;white-space:pre-wrap;max-height:360px;overflow:auto}
.chip{display:inline-block;max-width:100%;background:#3a4540;border:1px solid #638874;
  color:#d3eadb;border-radius:8px;padding:8px 11px;line-height:1.45;margin:2px 0;
  overflow-wrap:anywhere}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=8, show_spinner=False)
def bridge_online() -> bool:
    try:
        with socket.create_connection((app_settings.KIMI_WEBBRIDGE_HOST,
                                       app_settings.KIMI_WEBBRIDGE_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _status(value: str) -> str:
    return {
        "PENDING": "待分析", "MATCHED": "匹配", "REVIEW": "待你判断", "SKIPPED": "已筛除",
        "ERROR": "分析失败", "NOT_APPLIED": "未投递", "APPROVED": "已批准",
        "SENT_VISIBLE": "页面可见已发送", "UNKNOWN": "结果未知，需人工核对",
        "READY": "可按规则回复", "NO_REPLY": "无需回复",
        "COMPLETED": "已完成", "PARTIAL": "部分完成", "FAILED": "失败", "RUNNING": "运行中",
    }.get(str(value), str(value))


def _safe(value: object) -> str:
    return html.escape(str(value or ""))


def _add_chip(field: str) -> None:
    draft_key = "draft_" + field
    input_key = "new_" + field
    item = str(st.session_state.get(input_key) or "").strip()
    values = list(st.session_state.get(draft_key) or [])
    if item and item not in values:
        values.append(item)
        st.session_state[draft_key] = values
    st.session_state[input_key] = ""


def _chip_editor(field: str, label: str, hint: str) -> list[str]:
    draft_key = "draft_" + field
    values = list(st.session_state[draft_key])
    for index, item in enumerate(values):
        text_col, remove_col = st.columns([11, 1], gap="small")
        text_col.markdown('<div class="chip">' + _safe(item) + '</div>', unsafe_allow_html=True)
        if remove_col.button("×", key=f"remove_{field}_{index}", help="移除这一条"):
            values.pop(index)
            st.session_state[draft_key] = values
            st.rerun()
    st.text_input(label, key="new_" + field, placeholder=hint,
                  on_change=_add_chip, args=(field,))
    return list(st.session_state[draft_key])


config = store.settings()
llm_config = load_runtime_settings()
timer = workflow.get_runner()
for field in ("search_keywords", "job_rules"):
    st.session_state.setdefault("draft_" + field, list(config[field]))
pages = ("投递设置", "自动回复", "审核回复", "邮件投递", "投递分析")
if st.session_state.get("career_page") not in pages:
    st.session_state["career_page"] = pages[0]
nav = st.columns([1, 1, 1, 1, 1, 1.5], gap="small")
for column, label in zip(nav[:5], pages):
    if column.button(label, key="nav_" + label,
                     type="primary" if st.session_state["career_page"] == label else "secondary",
                     use_container_width=True):
        st.session_state["career_page"] = label
        st.rerun()
nav[5].caption("BOSS 直聘 · " + ("服务可连接" if bridge_online() else "服务未连接"))
st.divider()
page = st.session_state["career_page"]


@st.fragment(run_every="5s")
def _show_job_status() -> None:
    if timer.is_running("job"):
        st.info("岗位巡航已启动，正在进行或等待 BOSS 页面操作。")
    last_run = timer.last_manual_result
    if last_run:
        summary = (f"最近一次手动巡航：查看 {last_run['candidates']} 岗，"
                   f"列表快速排除 {last_run.get('quick_skipped', 0)} 岗，"
                   f"读取 JD {last_run['visible']} 岗，"
                   f"截图读薪资 {last_run.get('salary_ocr_read', 0)} 岗，"
                   f"薪资无法核实 {last_run.get('salary_unreadable', 0)} 岗，"
                   f"匹配 {last_run['matched']} 岗，"
                   f"页面可见投递 {last_run['auto_applied']} 岗。")
        (st.warning if last_run["errors"] or last_run["auto_applied"] == 0
         else st.success)(summary)
        if last_run["errors"]:
            st.error("本轮未完成：" + str(last_run["errors"][0]))
            with st.expander(f"{len(last_run['errors'])} 项需要查看"):
                for item in last_run["errors"]:
                    st.write(item)
        if last_run.get("notices"):
            if last_run["auto_applied"] == 0:
                st.info(str(last_run["notices"][0]))
            with st.expander(f"{len(last_run['notices'])} 项投递提示"):
                for item in last_run["notices"]:
                    st.write(item)
        if last_run.get("quick_skip_examples"):
            with st.expander("查看列表快速排除的原因（最多显示 30 项）"):
                for item in last_run["quick_skip_examples"]:
                    st.write(item)
    elif timer.last_manual_error and not timer.is_running("job"):
        st.error("最近一次手动巡航失败：" + timer.last_manual_error)


if page == "投递设置":
    sent_col, token_col, check_col, start_col = st.columns([1, 1, 1, 1], gap="medium")
    sent_col.metric("已投岗位", store.applications_sent_count())
    token_col.metric("Token 消耗（今日）", get_today_stats()["total_tokens"])
    check_col.caption("网页连接")
    if check_col.button("检查 BOSS 网页", use_container_width=True):
        try:
            inspected = workflow.inspect_browser()
            page_info = inspected.get("page") or {}
            ready = inspected["search_fields"] == 1 and inspected["search_buttons"] == 1
            st.session_state["boss_page_status"] = (
                f"{'搜索控件可用' if ready else '搜索控件未就绪'} · "
                f"可见岗位 {inspected['visible_jobs']} · {page_info.get('title') or 'BOSS'}")
            if ready:
                st.success("BOSS 搜索框与按钮可用。登录状态请以网页实际内容为准。")
            else:
                st.warning("已连接网页，但未找到唯一的搜索框和按钮；请检查登录与页面加载情况。")
        except Exception as exc:
            st.session_state["boss_page_status"] = "检查失败"
            st.error(str(exc))
    start_col.caption("投递操作")
    start_requested = start_col.button("开始投递", type="primary", use_container_width=True)
    last_error = st.session_state.get("career_last_run_error")
    if last_error:
        st.error(last_error)
    else:
        _show_job_status()
    st.caption("按顺序配置搜索、AI 判断和投递资料。点击开始投递会先保存当前设置。")
    st.caption("网页状态：" + st.session_state.get("boss_page_status", "尚未检查") +
               " · 本地 WebBridge：" + ("可连接" if bridge_online() else "未连接"))
    if timer.next_job_run_at:
        st.caption("下次岗位巡航：" + timer.next_job_run_at.strftime("%Y-%m-%d %H:%M"))
    if timer.last_job_error and timer.last_job_error != timer.last_manual_error:
        st.warning("上次岗位巡航遇到问题：" + timer.last_job_error)

def _render_jobs() -> None:
    st.subheader("岗位候选")
    st.caption("页面采集后按 JD 与简历评分；结果未知的投递不会自动重试。")
    items = store.jobs_brief(200)
    if not items:
        st.info("还没有岗位。先在“投递设置”选择简历，再点击顶部“开始投递”。")
    else:
        labels = {f"{j['company']} · {j['title']} · {_status(j['match_state'])} · {j['job_key'][:6]}": j
                  for j in items}
        job = store.get_job(labels[st.selectbox("选择岗位", list(labels))]["job_key"])
        st.markdown(f"**匹配：** {_status(job['match_state'])} · "
                    f"**评分：** {job['match_score'] if job['match_score'] is not None else '未评估'} · "
                    f"**投递：** {_status(job['apply_state'])}")
        shown_salary = job["salary"] if job.get("salary_source") in {"dom", "ocr"} else ""
        st.caption(" · ".join(part for part in
                              (shown_salary, job["location"], job["requirements"]) if part))
        st.caption("薪资来源：" + {"dom": "网页文字", "ocr": "页面截图识别",
                             "unreadable": "无法核实"}.get(job.get("salary_source"), "无法核实"))
        st.write(job["match_reason"] or "暂无分析结论。")
        review = store.review_for_target("job", job["job_key"])
        if review and review["status"] == "PENDING":
            st.caption("QQ 审核编号：" + review["review_code"])
        st.caption("本次岗位评估 Token：" +
                   (str(job["match_tokens"]) if job["match_tokens"] else "接口未返回用量"))
        try:
            rule_results = json.loads(job.get("rule_results") or "[]")
        except json.JSONDecodeError:
            rule_results = []
        for check in rule_results:
            label = {"pass": "符合", "fail": "不符合", "uncertain": "待核实"}.get(
                check.get("result"), "未判断")
            st.caption(f"规则 · {check.get('rule', '')}：{label}。{check.get('reason', '')}")
        try:
            evidence = json.loads(job["match_evidence"] or "[]")
        except json.JSONDecodeError:
            evidence = []
        if evidence:
            st.caption("简历依据：" + "；".join(evidence))
        st.link_button("打开岗位详情", job["url"])
        with st.expander("查看已采集 JD"):
            st.text(job["jd_text"] or "尚未采集详情。")
        if job["apply_state"] in {"NOT_APPLIED", "APPROVED"} and job["match_state"] in {"MATCHED", "REVIEW"}:
            receipt_ready = bool(config["selectors"].get("application_receipt"))
            if not receipt_ready:
                st.info("投递成功标记尚未适配；请先在投递设置中核对页面回执。")
            apply_col, skip_col = st.columns(2)
            if apply_col.button("确认并投递这一个岗位", type="primary", disabled=not receipt_ready):
                try:
                    result = workflow.apply_one(job["job_key"], manual=True)
                    st.success("页面显示投递成功标记。")
                    if result["errors"]:
                        st.warning("开场白或图片简历仍需人工核对：" + "；".join(result["errors"]))
                    if result["notices"]:
                        st.info("；".join(result["notices"]))
                    st.json(result)
                except Exception as exc:
                    st.error(str(exc))
            if skip_col.button("跳过这个岗位"):
                try:
                    workflow.skip_job(job["job_key"])
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        prior = store.action("apply", job["job_key"])
        if prior:
            st.caption(f"投递动作：{_status(prior['status'])} · {prior['updated_at']}")

def _render_messages() -> None:
    st.subheader("HR 消息")
    st.caption("简单确认语可在开启后自动回复；其他消息会按模型判断生成草稿或标记无需回复。")
    items = store.messages_brief(200)
    if not items:
        st.info("还没有可核实的 HR 消息。消息页需要先完成页面控件适配。")
    else:
        labels = {f"{m['sender']} · {_status(m['reply_state'])} · {m['received_at']} · {m['message_key'][:6]}": m
                  for m in items}
        msg = store.get_message(labels[st.selectbox("选择消息", list(labels))]["message_key"])
        st.markdown("**收到：**")
        st.write(msg["body"])
        st.caption("分类：" + (msg["category"] or "待分类") + " · " + (msg["reason"] or ""))
        review = store.review_for_target("hr", msg["message_key"])
        if review and review["status"] == "PENDING":
            st.caption("QQ 审核编号：" + review["review_code"])
        st.link_button("打开会话", msg["conversation_url"])
        if msg["reply_state"] in {"REVIEW", "READY"}:
            with st.form("reply_form_" + msg["message_key"]):
                draft = st.text_area("回复草稿", value=msg["draft"], max_chars=300)
                save = st.form_submit_button("保存修改")
            if save:
                try:
                    workflow.update_message_draft(msg["message_key"], draft)
                    st.success("草稿已保存。")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            send_col, ignore_col = st.columns(2)
            if send_col.button("审核后发送这一条", type="primary"):
                try:
                    st.json(workflow.reply_one(msg["message_key"], manual=True))
                    st.success("页面显示新增的精确回复。")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            if ignore_col.button("不回复，标记已处理"):
                try:
                    workflow.dismiss_message(msg["message_key"])
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        prior = store.action("hr_reply", msg["message_key"])
        if prior:
            st.caption(f"回复动作：{_status(prior['status'])} · {prior['updated_at']}")

def _render_mail() -> None:
    st.caption("粘贴小红书招聘帖链接并按回车。脚本读取正文和每张帖子图片的文字，AI 结合投递设置中选定的简历生成邮件草稿。")
    if "mail_note_pending" in st.session_state:
        st.session_state["mail_note_input"] = st.session_state.pop("mail_note_pending")
    if st.session_state.pop("mail_note_saved", False):
        st.success("备注已按帖子链接保存在本机。")
    saved_posts = store.recent_mail_notes(10)
    if saved_posts:
        with st.expander("最近的帖子备注"):
            for item in saved_posts:
                label = item["title"] or item["post_id"]
                if st.button("填入链接 · " + label[:45], key="mail_history_" + item["post_id"]):
                    st.session_state["mail_post_url"] = item["url"]
                    st.session_state["mail_note_pending"] = item["note"]
                    st.session_state["mail_note_for_url"] = item["url"]
                    st.session_state["mail_note_loaded_text"] = item["note"]
                    st.rerun()
                st.caption(item["note"][:300])
    with st.form("mail_post_link"):
        post_url = st.text_input("小红书招聘帖链接", key="mail_post_url",
                                 placeholder="https://www.xiaohongshu.com/explore/…")
        note_input = st.text_area("这篇帖子的备注", key="mail_note_input", height=90,
                                  max_chars=2000,
                                  help="按帖子保存在本机，仅供你查看；不会发给 AI 或写进邮件。")
        load_post = st.form_submit_button("读取帖子并生成草稿", type="primary")
        save_note = st.form_submit_button("只保存备注")
    if load_post or save_note:
        try:
            saved_note = store.mail_note(post_url)
            note = note_input
            if (st.session_state.get("mail_note_for_url") != post_url and
                    note_input == st.session_state.get("mail_note_loaded_text", "")):
                note = saved_note["note"] if saved_note else ""
            if save_note:
                title = (saved_note or {}).get("title") or ""
                store.save_mail_note(post_url, title, note)
                if (st.session_state.get("mail_post") or {}).get("url") != post_url:
                    for key in ("mail_post", "mail_analysis", "mail_recipient",
                                "mail_subject", "mail_body"):
                        st.session_state.pop(key, None)
                st.session_state["mail_note_pending"] = note
                st.session_state["mail_note_for_url"] = post_url
                st.session_state["mail_note_loaded_text"] = note
                st.session_state["mail_note_saved"] = True
                st.rerun()
            if load_post:
                # Keep the user's note even if page reading or OCR fails later.
                store.save_mail_note(post_url, (saved_note or {}).get("title") or "", note)
        except Exception as exc:
            st.error(str(exc))
            return
    if load_post:
        for key in ("mail_post", "mail_analysis", "mail_recipient", "mail_subject", "mail_body"):
            st.session_state.pop(key, None)
        try:
            if not config["resume_file"]:
                raise ValueError("请先在“投递设置”选择供 AI 阅读的主简历。")
            from src.career.mail_post import read_post, draft_email
            with st.spinner("读取帖子正文和图片文字，并对照简历生成邮件…"):
                post = read_post(post_url)
                resume_text, _ = model.read_career_files(
                    config["resume_file"], config["supporting_files"])
                analysis = draft_email(post, resume_text, config["profile_facts"], config["job_rules"])
            st.session_state["mail_post"] = post
            st.session_state["mail_analysis"] = analysis
            st.session_state["mail_recipient"] = analysis["recipient"]
            st.session_state["mail_subject"] = analysis["subject"]
            st.session_state["mail_body"] = analysis["body"]
            store.save_mail_note(post["url"], post["title"], note)
            st.session_state["mail_note_pending"] = note
            st.session_state["mail_note_for_url"] = post["url"]
            st.session_state["mail_note_loaded_text"] = note
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    post = st.session_state.get("mail_post")
    analysis = st.session_state.get("mail_analysis")
    if not post or not analysis:
        st.info("填写帖子链接和备注后可生成草稿；请先在“投递设置”选择主简历。")
        return
    st.subheader("帖子内容与匹配判断")
    st.link_button("查看原帖", post["url"])
    st.write(f"**{post['title'] or '未填写标题'}**")
    st.write("结论：" + {"match": "适合考虑", "review": "需要核实", "skip": "不建议投递"}[analysis["recommendation"]]
             + " · " + analysis["reason"])
    st.caption(f"建议岗位：{analysis['role'] or '未确定'} · 公司：{analysis['company'] or '未确定'}"
               f" · 读取图片 {len(post['images'])} 张 · 模型 Token {analysis['token_usage'] or '接口未返回'}")
    if analysis["gaps"]:
        st.warning("待核实：" + "；".join(analysis["gaps"]))
    with st.expander("查看提取的帖子正文与图片文字"):
        st.text(post["description"] or "正文为空。")
        for item in post["images"]:
            st.markdown(f"**第 {item['index']} 张图片**")
            st.text(item["text"] or "未识别到文字。")
    st.subheader("审核邮件草稿")
    st.caption("收件邮箱只能从帖子正文或图片识别；未出现邮箱时请自行核实并填写。写入 QQ 邮箱不会自动发送。")
    with st.form("mail_draft"):
        recipient = st.text_input("收件邮箱", key="mail_recipient")
        subject = st.text_input("主题", key="mail_subject")
        body = st.text_area("正文", height=260, key="mail_body")
        draft_mail = st.form_submit_button("写入已登录的 QQ 邮箱")
    if draft_mail:
        try:
            from src.tools.email_tool import qq_mail_open_compose, qq_mail_write_draft
            qq_mail_open_compose()
            result = json.loads(qq_mail_write_draft(subject=subject, body=body,
                                                   recipient_email=recipient))
            st.success("邮件已填入 QQ 邮箱的写信页。请在邮箱检查内容与附件后自行发送。")
            st.caption("收件人状态：" + str(result.get("recipient_status")) +
                       "；草稿箱保存状态尚未核实。")
        except Exception as exc:
            st.error(str(exc))

def _render_runs() -> None:
    st.subheader("巡航记录")
    for row in store.runs(100):
        with st.expander(f"{row['started_at']} · {row['kind']} · {_status(row['status'])}"):
            st.code(row["summary"] or "暂无详情。", language="text")
    st.subheader("对外动作")
    for row in store.recent_actions(limit=100):
        with st.expander(f"{row['created_at']} · {row['kind']} · {_status(row['status'])}"):
            st.json({"target": row["target_key"], "payload": json.loads(row["payload"]),
                     "receipt": json.loads(row["receipt"])})

if page == "审核回复":
    st.title("审核与回复")
    st.subheader("QQ 通知与远程审核")
    st.caption("在这台电脑登录 QQ 小号，并只保留一个 QQ 主窗口。按大号 QQ 号搜索联系人；若聊天标题显示昵称，再填写精确显示名称用于复核。")
    qq_a, qq_b, qq_c = st.columns([1, 1.4, 1.4])
    notify_qq = qq_a.checkbox("待审核时通知我的 QQ", value=config["notify_qq"])
    qq_command = qq_a.checkbox("接收 QQ 审核指令", value=config["qq_command_enabled"],
                               disabled=not notify_qq)
    qq_number = qq_b.text_input("我的大号 QQ 号", value=config["qq_owner_qq"])
    qq_name = qq_c.text_input("会话显示名称（昵称，选填）", value=config["qq_owner_name"])
    st.caption("QQ 审核使用一次性编号：回复“1 编号”确认投递或发送草稿，回复“2 编号”跳过；单独回复 1 无法安全对应到某张卡片。")
    qq_save, qq_check = st.columns(2)
    if qq_save.button("保存 QQ 审核设置", type="primary", use_container_width=True):
        try:
            store.save_settings({"notify_qq": notify_qq,
                                 "qq_command_enabled": bool(qq_command and notify_qq),
                                 "qq_owner_qq": qq_number.strip(),
                                 "qq_owner_name": qq_name.strip()})
            st.success("QQ 审核设置已保存。")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if qq_check.button("检查 QQ 客户端与待审核指令", use_container_width=True):
        try:
            from src.tools.desktop_chat_tool import desktop_chat_windows
            windows = json.loads(desktop_chat_windows("qq"))["windows"]
            st.write(f"已找到 {len(windows)} 个 QQ 客户端窗口。")
            if config["qq_command_enabled"] and windows:
                st.json(workflow.scan_owner_approvals())
        except Exception as exc:
            st.error(str(exc))
    if timer.next_qq_run_at:
        st.caption("下次 QQ 指令检查：" + timer.next_qq_run_at.strftime("%Y-%m-%d %H:%M"))
    if timer.last_qq_error:
        st.warning("QQ 指令检查异常：" + timer.last_qq_error)
    if not config["qq_owner_qq"]:
        st.info("尚未填写大号 QQ 号；QQ 通知和远程审核目前不会发送或执行。")
    notices = store.recent_actions("qq_notice", limit=1)
    if notices and notices[0]["status"] == "UNKNOWN":
        st.warning("最近一次 QQ 通知结果不明，请到桌面 QQ 核对。")
    pending = store.pending_reviews()
    if pending:
        with st.expander(f"待审核编号（{len(pending)}）"):
            for item in pending:
                st.caption(f"{item['review_code']} · {'岗位' if item['kind'] == 'job' else 'HR 回复'} · {item['created_at']}")
    job_tab, message_tab = st.tabs(("岗位审核", "HR 回复"))
    with job_tab:
        _render_jobs()
    with message_tab:
        _render_messages()
elif page == "自动回复":
    st.title("HR 消息自动回复")
    st.caption("独立于岗位巡航，按这里的间隔检查 BOSS 消息页；两个开关可以同时开启。")
    a, b, c = st.columns(3)
    monitor_messages = a.checkbox("开启定时检查", value=config["monitor_messages"])
    hr_interval = b.number_input("每隔几分钟检查 HR 消息", min_value=1, max_value=1440,
                                 value=config["hr_poll_minutes"])
    auto_reply = c.checkbox("简单确认语自动回复", value=config["auto_reply"],
                            disabled=not monitor_messages)
    st.caption("目前自动发送只适用于“收到、谢谢、好的”等简单确认语；面试、薪资、时间、取舍及其他复杂消息会生成草稿交给你审核。")
    reply_policy = st.text_area("HR 回复规则", value=config["reply_policy_prompt"], height=140)
    if st.button("保存自动回复设置", type="primary", use_container_width=True):
        try:
            store.save_settings({"monitor_messages": monitor_messages,
                                 "hr_poll_minutes": int(hr_interval),
                                 "auto_reply": bool(auto_reply and monitor_messages),
                                 "reply_policy_prompt": reply_policy})
            st.success("HR 检查与回复设置已保存。")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if st.button("现在检查一次 HR 消息"):
        try:
            with st.spinner("读取 BOSS 消息并生成处理结果…"):
                st.json(workflow.scan_messages())
        except Exception as exc:
            st.error(str(exc))
    if timer.next_hr_run_at:
        st.caption("下次 HR 检查：" + timer.next_hr_run_at.strftime("%Y-%m-%d %H:%M"))
    if timer.is_running("hr"):
        st.info("正在检查 HR 消息。")
    if timer.last_hr_error:
        st.warning("上次 HR 检查遇到问题：" + timer.last_hr_error)
elif page == "邮件投递":
    st.title("邮件投递")
    _render_mail()
elif page == "投递分析":
    st.title("投递分析")
    st.caption("每个岗位的模型 Token 见岗位审核；总量包含其他模型请求。")
    counts = store.dashboard_counts()
    summary = st.columns(4)
    summary[0].metric("已记录岗位", counts["jobs"])
    summary[1].metric("今日投递尝试", store.applications_today())
    summary[2].metric("待审岗位", counts["job_review"])
    summary[3].metric("今日 Token", get_today_stats()["total_tokens"])
    jobs = store.jobs_brief(200)
    st.subheader("岗位明细")
    if jobs:
        st.dataframe([{"公司": row["company"], "岗位": row["title"],
                       "匹配": _status(row["match_state"]),
                       "评分": str(row["match_score"]) if row["match_score"] is not None else "—",
                       "投递": _status(row["apply_state"]),
                       "评估 Token": str(row["match_tokens"]) if row["match_tokens"] else "—"} for row in jobs],
                     hide_index=True, use_container_width=True)
    else:
        st.info("还没有岗位分析记录。")
    st.divider()
    _render_runs()
elif page == "投递设置":
    if st.session_state.pop("career_settings_saved", False):
        st.success("投递设置已保存。")
    st.divider()
    st.subheader("岗位投递设置")
    a, b, c = st.columns(3)
    per_run = a.number_input("本轮最多查看岗位（安全上限）", min_value=1, max_value=2000,
                             value=config["max_inspections_per_run"])
    apply_per_run = b.number_input("本次投递目标", min_value=1, max_value=30,
                                   value=config["max_applications_per_run"])
    per_day = c.number_input("每日投递尝试上限", min_value=1, max_value=30,
                             value=config["max_applications_per_day"])
    switch_options = {"当前关键词处理完再切换": "exhaust",
                      "每个关键词投满指定数量后切换": "quota"}
    switch_labels = list(switch_options)
    switch_mode = st.selectbox(
        "关键词切换方式", switch_labels,
        index=(list(switch_options.values()).index(config["keyword_switch_mode"])
               if config["keyword_switch_mode"] in switch_options.values() else 0))
    per_keyword = config["applications_per_keyword"]
    if switch_options[switch_mode] == "quota":
        per_keyword = st.number_input("每个关键词投递成功后切换（岗位数）",
                                      min_value=1, max_value=30, value=per_keyword)
    st.caption("按搜索词顺序、岗位列表从上到下逐个读取完整 JD；符合规则就立即投递，不再每词截取候选或跨词择优。达到本次投递目标后停止。")
    st.caption("当前关键词没有更多可发现岗位时会切换到下一个；若选择按数量切换，网页确认投递成功达到该数量也会提前切换。本轮查看上限和每日投递尝试上限仍会生效。")
    if switch_options[switch_mode] == "quota":
        st.caption("按数量切换只在实际投递时生效；关闭自动投递的定时巡航会把当前关键词处理完再切换。")
    st.caption("每个新岗位都要读取 JD 并调用模型，查看越多 Token 消耗越高。")
    if per_day < apply_per_run:
        st.info("每日投递尝试上限小于本次目标，今日会先按每日上限停止。")
    st.subheader("岗位巡航时间")
    a, b, c = st.columns(3)
    automation = a.checkbox("开启定时岗位巡航", value=config["automation_enabled"])
    schedule_mode = b.selectbox("巡航方式", ("每隔固定分钟", "每天指定时间"),
                                index=0 if config["job_schedule_mode"] == "interval" else 1)
    auto_apply = c.checkbox("巡航时自动投递", value=config["auto_apply"])
    interval = config["poll_minutes"]
    daily_times = ", ".join(config["job_run_times"])
    if schedule_mode == "每隔固定分钟":
        interval = st.number_input("岗位巡航间隔（分钟）", min_value=5, max_value=1440,
                                   value=config["poll_minutes"])
    else:
        daily_times = st.text_input("每天运行时间（24 小时制，可填多个，逗号分隔）",
                                    value=daily_times, placeholder="09:00, 18:30")
    threshold = st.number_input("AI 匹配最低分", min_value=0, max_value=100,
                                value=config["min_match_score"])
    st.caption("点击顶部“开始投递”会先保存本页设置，再立即运行一轮；定时开关只控制之后的自动巡航。")

    st.divider()
    st.subheader("网页搜索关键词")
    st.caption("严格按下方顺序搜索。每个词从上到下逐岗判断，按照上方切换方式进入下一个词。")
    searches = _chip_editor("search_keywords", "输入搜索词后按回车",
                            "例如：AI应用开发")
    if (switch_options[switch_mode] == "quota" and
            len(searches) * int(per_keyword) < int(apply_per_run)):
        st.info("当前关键词数量 × 每词投递数小于本次投递目标；即使每个词都投满，也达不到总目标。")

    st.divider()
    st.subheader("AI 岗位判断规则")
    st.caption("每条规则独立判断。明确不符合则跳过；信息不足则留待你审核。")
    rules = _chip_editor("job_rules", "输入一条判断规则后按回车",
                         "例如：不投外包岗位；只考虑上海的岗位")

    st.divider()
    st.subheader("开场白与投递资料")
    opening = st.text_area("固定开场白（留空则由 AI 按 JD 和简历拟写）",
                           value=config["opening_message"], height=110, max_chars=500)
    st.caption("发送前会核对岗位对应会话；若平台已经自动发出招呼语，脚本不会再重复发一条。")
    image_names = [path.name for path in model.image_files()]
    image_choice = st.selectbox("已保存的图片简历", ["", *image_names],
                                index=(["", *image_names].index(config["image_resume_file"])
                                       if config["image_resume_file"] in image_names else 0))
    image_upload = st.file_uploader("上传新的图片简历", type=["jpg", "jpeg", "png"],
                                    key="career_image_upload")
    st.caption("图片简历会保存在本机 data/users；发送时先发开场白，再尝试从已核对的会话上传图片。")
    document_names = [path.name for path in model.resume_files()]
    resume_choice = st.selectbox("供 AI 阅读的主简历", ["", *document_names],
                                 index=(["", *document_names].index(config["resume_file"])
                                        if config["resume_file"] in document_names else 0))
    resume_upload = st.file_uploader("上传新的主简历", type=["pdf", "docx", "txt", "md"],
                                     key="career_resume_upload")
    extras = st.multiselect("其它供 AI 参考的附件", document_names,
                            default=[name for name in config["supporting_files"]
                                     if name in document_names])
    extra_uploads = st.file_uploader("上传其它参考文件（可多选）",
                                     type=["pdf", "docx", "txt", "md"],
                                     accept_multiple_files=True, key="career_extra_uploads")
    facts = st.text_area("已核实的个人资料和求职偏好", value=config["profile_facts"],
                         height=110, help="只填写真实、可向 HR 说明的事实。")
    st.caption("AI 只读取你选中的主简历与参考附件；图片简历用于网页发送，不会自动识别图片文字。")

    with st.expander("BOSS 页面地址与控件配置"):
        search_url = st.text_input("BOSS 岗位列表地址", value=config["search_url"])
        message_url = st.text_input("BOSS 消息页地址", value=config["message_url"])
        resume_ready = st.checkbox("已确认 BOSS 在线简历可以投递",
                                   value=config["platform_resume_ready"])
        sel = {}
        for key, label in {
            "apply_button": "投递按钮", "application_receipt": "投递成功标记",
            "conversation_rows": "HR 会话列表项", "incoming_rows": "收到的消息项",
            "message_input": "回复输入框", "message_send": "发送按钮",
            "outgoing_rows": "已发送文字项",
        }.items():
            help_text = ("已在 BOSS 岗位详情页核对：已沟通时按钮的 data-isfriend 为 true。"
                         if key == "application_receipt" else None)
            widget_key = "sel_application_receipt_v2" if key == "application_receipt" else "sel_" + key
            sel[key] = st.text_input(label, value=config["selectors"].get(key, ""),
                                     key=widget_key, help=help_text)
        if st.button("只读核对当前岗位薪资", key="career_check_visible_salary"):
            try:
                observed_salary = bridge.inspect_selected_salary()
                source = {"dom": "网页文字", "ocr": "页面截图识别"}.get(
                    observed_salary["salary_source"], "未核实")
                label = f"{observed_salary['company']} · {observed_salary['title']}"
                if observed_salary["salary"]:
                    st.success(f"{label}：{observed_salary['salary']}（{source}）")
                else:
                    st.warning(f"{label}：薪资未核实。{observed_salary['salary_note']}")
            except Exception as exc:
                st.error("当前岗位薪资核对失败：" + str(exc))
        st.caption("先在 Edge 的 BOSS 页面选中一个岗位；此操作只读取薪资，不调用模型或投递。")
    with st.expander("模型接口"):
        model_name = st.text_input("模型名称", value=llm_config["model_name"])
        api_base = st.text_input("OpenAI 兼容 API 地址", value=llm_config["api_base"])
        st.caption("API Key 从本机 MODEL_API_KEY 读取；未设置时沿用 SILICONFLOW_API_KEY。")
    st.caption("HR 检查与回复请到“自动回复”配置；QQ 审核请到“审核回复”配置。")
    save_requested = st.button("保存投递设置", type="primary", use_container_width=True)
    if save_requested or start_requested:
        try:
            if not model_name.strip() or not api_base.strip().startswith("https://"):
                raise ValueError("模型名称不能为空，API 地址必须使用 HTTPS。")
            parsed_times = [value.strip() for value in daily_times.replace("，", ",").split(",")
                            if value.strip()]
            selected_resume = (model.save_user_upload(resume_upload,
                               {".pdf", ".docx", ".txt", ".md"})
                               if resume_upload else resume_choice)
            if start_requested and not selected_resume:
                raise ValueError("开始投递前请先选择供 AI 阅读的主简历。")
            selected_image = (model.save_user_upload(image_upload,
                              {".jpg", ".jpeg", ".png"})
                              if image_upload else image_choice)
            selected_extras = list(extras)
            for uploaded in extra_uploads or []:
                name = model.save_user_upload(uploaded, {".pdf", ".docx", ".txt", ".md"})
                if name not in selected_extras and name != selected_resume:
                    selected_extras.append(name)
            save_runtime_settings({"model_name": model_name, "api_base": api_base})
            store.save_settings({
                "resume_file": selected_resume, "supporting_files": selected_extras,
                "image_resume_file": selected_image, "opening_message": opening,
                "search_keywords": searches, "job_rules": rules,
                "profile_facts": facts, "min_match_score": int(threshold),
                "max_inspections_per_run": int(per_run),
                "keyword_switch_mode": switch_options[switch_mode],
                "applications_per_keyword": int(per_keyword),
                "max_applications_per_run": int(apply_per_run),
                "max_applications_per_day": int(per_day),
                "poll_minutes": int(interval), "automation_enabled": automation,
                "job_schedule_mode": "interval" if schedule_mode == "每隔固定分钟" else "daily",
                "job_run_times": parsed_times,
                "auto_apply": auto_apply,
                "search_url": search_url, "message_url": message_url,
                "platform_resume_ready": resume_ready, "selectors": sel,
            })
            if start_requested:
                with st.spinner("正在连接 BOSS 网页…"):
                    inspected = workflow.inspect_browser()
                if inspected["search_fields"] < 1 or inspected["search_buttons"] < 1:
                    raise RuntimeError("已打开 BOSS 网页，但没有找到岗位搜索框或搜索按钮。请检查登录状态和页面加载情况。")
                timer.start_manual_jobs()
                st.session_state.pop("career_last_run_error", None)
            else:
                st.session_state["career_settings_saved"] = True
            st.rerun()
        except Exception as exc:
            if start_requested:
                st.session_state["career_last_run_error"] = "设置已保存，但投递未启动：" + str(exc)
                st.rerun()
            else:
                st.error(str(exc))
