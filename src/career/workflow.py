"""Fixed recruitment sequence; LLM output is data, never an action plan."""
from __future__ import annotations

import json
import hashlib
import re
import threading
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from src.career import bridge, model, store
from src.core.system.runtime_settings import load_runtime_settings
from src.tools.desktop_chat_tool import qq_owner_read_visible, send_owner_notification


_CYCLE_LOCK = threading.RLock()


def _analysis_hash(config: dict[str, Any], resume_hash: str) -> str:
    runtime = load_runtime_settings()
    relevant = {"decision_policy_version": 3,
                "resume_hash": resume_hash, "facts": config["profile_facts"],
                "rules": config["job_rules"],
                "min_score": config["min_match_score"], "model": runtime["model_name"],
                "api_base": runtime["api_base"]}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()


def _quick_screen_policy(rules: list[str]) -> dict[str, float | bool | None]:
    """Recognize only explicit user rules that can be checked from a job card."""
    full_time_only = any(re.search(r"不要实习岗|不考虑实习岗|排除实习岗位", rule)
                         for rule in rules)
    minimums = [float(match.group(1)) for rule in rules
                if (match := re.search(r"固定月薪下限不低于\s*(\d+(?:\.\d+)?)\s*K", rule, re.I))]
    maximums = [float(match.group(1)) for rule in rules
                if (match := re.search(r"月薪区间上限达到\s*(\d+(?:\.\d+)?)\s*K\s*及以上", rule, re.I))]
    return {"full_time_only": full_time_only,
            "minimum_k": max(minimums) if minimums else None,
            "maximum_k": min(maximums) if maximums else None}


def _quick_skip_card(card: dict[str, str], policy: dict[str, float | bool | None]) -> str:
    """Skip only cases proved by the visible title or unambiguous pay range."""
    title = str(card.get("title") or "")
    text = str(card.get("list_text") or "")
    if policy["full_time_only"] and re.search(r"实习生|实习岗|(?:^|[（(\s])实习(?:[）)\s]|$)", title):
        return "标题明确为实习岗位，与全职偏好不符"
    minimum = policy["minimum_k"]
    maximum = policy["maximum_k"]
    if minimum is None and maximum is None:
        return ""
    # Multiple or unclear amounts are left for the JD and model to resolve.
    monthly = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[-–—~～至]\s*"
                         r"(\d+(?:\.\d+)?)\s*K\b", text, re.I)
    if len(monthly) == 1 and not re.search(r"[/／]\s*(?:天|周|年|小时)", text):
        low, high = map(float, monthly[0])
        if low <= high:
            if minimum is not None and low < minimum:
                return f"列表月薪下限 {low:g}K 低于设置的 {minimum:g}K"
            if maximum is not None and high >= maximum:
                return f"列表月薪上限 {high:g}K 达到设置的 {maximum:g}K"
    daily = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[-–—~～至]\s*"
                       r"(\d+(?:\.\d+)?)\s*元\s*[/／]\s*天", text)
    if minimum is not None and len(daily) == 1:
        low, high = map(float, daily[0])
        if low <= high and high * 31 < minimum * 1000:
            return f"列表日薪上限 {high:g} 元，即使按每月 31 天计算也低于 {minimum:g}K"
    return ""


def _salary_needs_review(salary: str, policy: dict[str, float | bool | None]) -> bool:
    if policy["minimum_k"] is None and policy["maximum_k"] is None:
        return False
    text = str(salary or "").strip()
    if not text or re.search(r"[\uE000-\uF8FF]", text):
        return True
    return re.fullmatch(r"\d+(?:\.\d+)?\s*(?:[-–—~～至]\s*\d+(?:\.\d+)?)?"
                        r"\s*K(?:[·.]\s*\d+\s*薪)?", text, re.I) is None


def _notify_once(event_key: str, message: str, config: dict[str, Any]) -> None:
    if not config.get("notify_qq"):
        return
    contact = str(config.get("qq_owner_qq") or "").strip()
    if not contact:
        return
    action = store.reserve_action("qq_notice", event_key, {"contact": contact, "message": message})
    if action is None:
        return
    store.mark_action(action["action_id"], "ATTEMPTING")
    try:
        result = json.loads(send_owner_notification(contact, "【求职巡航】" + message[:450]))
        state = "SENT_VISIBLE" if result.get("status") == "SENT_VISIBLE" else "UNKNOWN"
        store.mark_action(action["action_id"], state, result)
    except Exception as exc:
        store.mark_action(action["action_id"], "UNKNOWN", {"error": str(exc)[:300]})


def _review_hash(kind: str, item: dict[str, Any]) -> str:
    fields = (("analysis_hash", "match_state", "apply_state", "resume_hash",
               "salary", "salary_source", "match_reason") if kind == "job"
              else ("message_id", "body", "draft", "reply_state", "conversation_url"))
    data = [str(item.get(field) or "") for field in fields]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode("utf-8")).hexdigest()


def _queue_review(kind: str, item: dict[str, Any], config: dict[str, Any]) -> None:
    key = item["job_key"] if kind == "job" else item["message_key"]
    review = store.reserve_review(kind, key, _review_hash(kind, item))
    if review["status"] != "PENDING":
        return
    code = review["review_code"]
    if kind == "job":
        description = f"岗位 {str(item['company'])[:60]} · {str(item['title'])[:60]} 待审核。"
        options = "回复 1 编号确认投递，2 编号跳过。"
    else:
        description = f"{str(item['sender'])[:80]} 的消息已有待审核草稿。"
        options = "回复 1 编号发送当前草稿，2 编号标记不回复。"
    _notify_once(f"review:{kind}:{key}:{code}",
                 f"{description} 编号 {code}。{options}", config)


def apply_one(key: str, *, manual: bool = False) -> dict[str, Any]:
    """Send one reviewed application only when exact page controls are configured."""
    with _CYCLE_LOCK:
        config = store.settings()
        job = store.get_job(key)
        if not job:
            raise ValueError("岗位记录不存在。")
        if job["match_state"] not in {"MATCHED", "REVIEW"} or job["apply_state"] not in {"NOT_APPLIED", "APPROVED"}:
            raise ValueError("此岗位的匹配或投递状态不允许提交。")
        if not manual and not config["auto_apply"]:
            raise PermissionError("自动投递未启用。")
        if not config["platform_resume_ready"]:
            raise PermissionError("请先确认 BOSS 账号的在线简历已设置为想投递的版本。")
        resume_text, current_resume_hash = model.read_career_files(
            config["resume_file"], config["supporting_files"])
        if (job["resume_hash"] != current_resume_hash or
                job["analysis_hash"] != _analysis_hash(config, current_resume_hash)):
            raise PermissionError("简历或匹配设置在评分后发生变化，请先重新巡航评估。")
        if store.applications_today() >= config["max_applications_per_day"]:
            raise PermissionError("今日投递上限已达到。")
        selectors = config["selectors"]
        button, receipt = selectors["apply_button"], selectors["application_receipt"]
        if not button or not receipt:
            raise ValueError("投递页面选择器未配置。")
        if not all(selectors.get(name) for name in
                   ("conversation_rows", "message_input", "message_send", "outgoing_rows")):
            raise ValueError("开场白发送所需的会话控件未配置完整。")
        opening = (str(config["opening_message"] or "").strip() or
                   model.draft_opening_message(job, resume_text, config["profile_facts"]))
        image_path = (str(model.image_file_path(config["image_resume_file"]))
                      if config["image_resume_file"] else "")
        bridge.inspect_apply_target(job["url"], button, receipt)
        action = store.reserve_action("apply", key, {
            "job_url": job["url"], "title": job["title"], "company": job["company"],
            "resume_file": config["resume_file"], "manual": manual,
            "opening_message": opening, "image_resume_file": config["image_resume_file"],
        })
        if action is None:
            raise ValueError("此岗位已有投递动作记录；请先核对平台，不能重复点击。")
        store.mark_action(action["action_id"], "ATTEMPTING")
        try:
            observed = bridge.click_apply_and_observe(job["url"], button, receipt)
        except Exception as exc:
            store.mark_action(action["action_id"], "UNKNOWN", {"error": str(exc)[:500]})
            store.set_apply_state(key, "UNKNOWN")
            _notify_once("apply-unknown:" + key, f"岗位 {job['company']} · {job['title']} 投递结果不明，请到平台核对。", config)
            raise
        store.mark_action(action["action_id"], "SENT_VISIBLE", observed)
        store.set_apply_state(key, "SENT_VISIBLE")
        outcome: dict[str, Any] = {"application": observed, "opening": None,
                                   "image": None, "errors": [], "notices": []}
        greeting_action = store.reserve_action("opening", key, {"draft": opening})
        if greeting_action is not None:
            store.mark_action(greeting_action["action_id"], "ATTEMPTING")
            try:
                greeting = bridge.send_application_greeting(job, opening,
                                                             config["message_url"], selectors)
                state = "SENT_VISIBLE" if greeting["status"] == "SENT_VISIBLE" else "CANCELLED"
                store.mark_action(greeting_action["action_id"], state, greeting)
                outcome["opening"] = greeting
                if state == "CANCELLED":
                    outcome["notices"].append(str(greeting.get("detail") or "开场白未重复发送。"))
            except Exception as exc:
                store.mark_action(greeting_action["action_id"], "UNKNOWN",
                                  {"error": str(exc)[:500]})
                outcome["errors"].append("开场白结果不明：" + str(exc))
        if image_path and not outcome["errors"]:
            image_action = store.reserve_action("image_resume", key,
                                                {"file": config["image_resume_file"]})
            if image_action is not None:
                store.mark_action(image_action["action_id"], "ATTEMPTING")
                try:
                    image_result = bridge.send_application_image(job, image_path,
                                                                  config["message_url"], selectors)
                    state = "SENT_VISIBLE" if image_result["status"] == "SENT_VISIBLE" else "CANCELLED"
                    store.mark_action(image_action["action_id"], state, image_result)
                    outcome["image"] = image_result
                    if state == "CANCELLED":
                        outcome["notices"].append(str(image_result.get("detail") or "图片未重复发送。"))
                except Exception as exc:
                    store.mark_action(image_action["action_id"], "UNKNOWN",
                                      {"error": str(exc)[:500]})
                    outcome["errors"].append("图片简历结果不明：" + str(exc))
        return outcome


def skip_job(key: str) -> None:
    with _CYCLE_LOCK:
        job = store.get_job(key)
        if not job or job["apply_state"] not in {"NOT_APPLIED", "APPROVED"}:
            raise ValueError("这个岗位已经不能标记为跳过。")
        if store.action("apply", key) is not None:
            raise ValueError("岗位已有投递动作，先核对平台结果。")
        store.set_apply_state(key, "SKIPPED")


def scan_jobs(*, apply_mode: bool | None = None) -> dict[str, Any]:
    with _CYCLE_LOCK:
        run_id = store.start_run("jobs_manual" if apply_mode is True else "jobs")
        count = {"candidates": 0, "visible": 0, "quick_skipped": 0,
                 "salary_ocr_read": 0, "salary_unreadable": 0,
                 "paused_for_salary": False,
                 "quick_skip_examples": [], "new_or_changed": 0, "matched": 0,
                 "review": 0, "skipped": 0, "auto_applied": 0,
                 "auto_attempts": 0, "errors": [], "notices": []}
        try:
            config = store.settings()
            if apply_mode is True and (not config["platform_resume_ready"] or
                                       not config["selectors"]["application_receipt"]):
                raise ValueError("开始投递前，请确认 BOSS 在线简历并配置投递成功标记。")
            resume_text, resume_hash = model.read_career_files(
                config["resume_file"], config["supporting_files"])
            analysis_hash = _analysis_hash(config, resume_hash)
            quick_policy = _quick_screen_policy(config["job_rules"])
            should_apply = config["auto_apply"] if apply_mode is None else apply_mode
            if should_apply and store.applications_today() >= config["max_applications_per_day"]:
                count["notices"].append("今日投递尝试上限已达到，本轮没有开始浏览岗位。")
                store.finish_run(run_id, "COMPLETED", json.dumps(count, ensure_ascii=False))
                return {"run_id": run_id, **count}
            # A missing browser service is a failed start, not a completed
            # zero-job search hidden inside an individual keyword's errors.
            bridge.select_boss_tab(config["search_url"])
            # The browser list is the queue. Inspect the complete JD and act on
            # each job before moving down the list or to the next search term.
            seen_keys: set[str] = set()
            stop = False
            for keyword in config["search_keywords"]:
                if stop:
                    break
                seen_paths: set[str] = set()
                applied_keyword = 0
                cards: list[dict[str, str]] = []
                try:
                    result_url = bridge.search_jobs(keyword, config["search_url"])["url"]
                    while not stop:
                        if should_apply and count["auto_applied"] >= config["max_applications_per_run"]:
                            stop = True
                            break
                        if (should_apply and config["keyword_switch_mode"] == "quota" and
                                applied_keyword >= config["applications_per_keyword"]):
                            break
                        if should_apply and store.applications_today() >= config["max_applications_per_day"]:
                            count["notices"].append("今日投递尝试上限已达到，岗位巡航已停止。")
                            stop = True
                            break
                        if count["candidates"] >= config["max_inspections_per_run"]:
                            count["notices"].append("已达到本轮查看岗位的安全上限，后续关键词未继续。")
                            stop = True
                            break
                        if not cards:
                            result_url = bridge.restore_job_results(result_url, keyword)
                            cards = bridge.discover_visible_jobs(result_url, navigate=False)
                        if not cards:
                            break
                        card = next((row for row in cards
                                     if urlsplit(row["url"]).path not in seen_paths), None)
                        if card is None:
                            advanced_url = bridge.advance_job_results(seen_paths, keyword)
                            if advanced_url is None:
                                break
                            result_url = advanced_url
                            cards = []
                            continue
                        seen_paths.add(urlsplit(card["url"]).path)
                        key = store.job_key(card["url"])
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        count["candidates"] += 1
                        if store.apply_states([key]).get(key) in {"SENT_VISIBLE", "UNKNOWN", "SKIPPED"}:
                            continue
                        quick_reason = _quick_skip_card(card, quick_policy)
                        if quick_reason:
                            count["skipped"] += 1
                            count["quick_skipped"] += 1
                            if len(count["quick_skip_examples"]) < 30:
                                count["quick_skip_examples"].append({
                                    "title": card["title"], "url": card["url"],
                                    "reason": quick_reason,
                                })
                            continue
                        # A detail view or application can change the page.
                        # Refresh the list before selecting another card.
                        cards = []
                        try:
                            detail = bridge.read_job_detail(card["url"], card["title"])
                            count["visible"] += 1
                            if detail.get("salary_source") == "ocr":
                                count["salary_ocr_read"] += 1
                            key, changed = store.upsert_job(detail)
                            if changed:
                                count["new_or_changed"] += 1
                            job = store.get_job(key)
                            if job is None:
                                raise RuntimeError("岗位保存后无法读取。")
                            if job["apply_state"] in {"SENT_VISIBLE", "UNKNOWN", "SKIPPED"}:
                                continue
                            if job["match_state"] == "PENDING" or job["analysis_hash"] != analysis_hash:
                                detail_reason = _quick_skip_card(
                                    {"title": job["title"], "list_text": job["salary"]},
                                    quick_policy)
                                if detail_reason:
                                    store.set_match(key, 0, "SKIPPED", detail_reason, [],
                                                    resume_hash, analysis_hash)
                                elif _salary_needs_review(job["salary"], quick_policy):
                                    salary_note = str(detail.get("salary_note") or "").strip()
                                    store.set_match(
                                        key, None, "REVIEW",
                                        "BOSS 页面薪资未能读成可核实的月薪；按当前薪资规则留待人工审核，未调用模型。"
                                        + ("原因：" + salary_note if salary_note else ""),
                                        [], resume_hash, analysis_hash)
                                    count["salary_unreadable"] += 1
                                    if salary_note and count["salary_unreadable"] == 1:
                                        count["notices"].append("首个薪资识别问题：" + salary_note)
                                    if count["salary_unreadable"] >= 10 and count["auto_applied"] == 0:
                                        count["notices"].append(
                                            "已有 10 岗薪资无法读取且尚无投递，本轮已暂停，避免继续消耗 Token。"
                                            "请先检查薪资读取或人工审核这些岗位。")
                                        count["paused_for_salary"] = True
                                        stop = True
                                else:
                                    verdict = model.score_job(job, resume_text, config["profile_facts"],
                                                              config["job_rules"])
                                    all_rules_pass = all(check["result"] == "pass"
                                                         for check in verdict["rule_results"])
                                    state = ("MATCHED" if verdict["recommendation"] == "match"
                                             and verdict["score"] >= config["min_match_score"]
                                             and all_rules_pass
                                             else "SKIPPED" if verdict["recommendation"] == "skip"
                                             else "REVIEW")
                                    store.set_match(key, verdict["score"], state,
                                                    verdict["reason"] +
                                                    ("；待核实：" + "、".join(verdict["gaps"])
                                                     if verdict["gaps"] else ""),
                                                    verdict["evidence"], resume_hash, analysis_hash,
                                                    verdict["token_usage"], verdict["rule_results"])
                                job = store.get_job(key)
                            state = job["match_state"]
                            bucket = {"MATCHED": "matched", "REVIEW": "review",
                                      "SKIPPED": "skipped"}.get(state, "review")
                            count[bucket] += 1
                            if (should_apply and state == "MATCHED" and
                                    job["apply_state"] == "NOT_APPLIED"):
                                if store.action("apply", key) is not None:
                                    count["notices"].append(f"{job['title']} 已有投递动作记录，未重复发送。")
                                else:
                                    count["auto_attempts"] += 1
                                    try:
                                        result = apply_one(key, manual=apply_mode is True)
                                        count["auto_applied"] += 1
                                        applied_keyword += 1
                                        count["errors"].extend(f"{job['title']}：{error}" for error in result["errors"])
                                        count["notices"].extend(f"{job['title']}：{notice}" for notice in result["notices"])
                                    except Exception as exc:
                                        count["errors"].append(f"{job['title']} 投递暂停：{exc}")
                                        stop = True
                            latest = store.get_job(key)
                            if (latest and latest["match_state"] in {"MATCHED", "REVIEW"} and
                                    latest["apply_state"] == "NOT_APPLIED" and
                                    store.action("apply", key) is None):
                                _queue_review("job", latest, config)
                        except Exception as exc:
                            count["errors"].append(f"{card.get('title') or card.get('url')}：{exc}")
                except Exception as exc:
                    count["errors"].append(f"搜索“{keyword}”失败：{exc}")
                    # The remaining positions of this keyword are unknown.
                    # Moving to the next term would violate the chosen order.
                    stop = True
            if (should_apply and not stop and
                    count["auto_applied"] < config["max_applications_per_run"]):
                count["notices"].append("已遍历所有关键词，仍未达到本次投递目标；部分关键词可能按每词数量提前切换。")
            status = "PARTIAL" if count["errors"] or count["paused_for_salary"] else "COMPLETED"
            store.finish_run(run_id, status, json.dumps(count, ensure_ascii=False))
            return {"run_id": run_id, **count}
        except Exception as exc:
            store.finish_run(run_id, "FAILED", str(exc))
            if "config" in locals():
                fingerprint = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:12]
                _notify_once(f"jobs-scan-error:{datetime.now().date()}:{fingerprint}",
                             f"岗位巡航暂停：{str(exc)[:180]}", config)
            raise


def reply_one(key: str, *, manual: bool = False) -> dict[str, Any]:
    with _CYCLE_LOCK:
        config = store.settings()
        message = store.get_message(key)
        if not message or message["reply_state"] not in {"REVIEW", "READY"} or not message["draft"]:
            raise ValueError("此 HR 消息没有可发送的草稿。")
        if not manual and (not config["auto_reply"] or message["reply_state"] != "READY"
                           or message["category"] != "routine"):
            raise PermissionError("此消息只能人工审核后发送。")
        before_count = bridge.inspect_reply_target(message, config["selectors"])
        action = store.reserve_action("hr_reply", key, {
            "conversation_url": message["conversation_url"], "sender": message["sender"],
            "message_id": message["message_id"], "draft": message["draft"], "manual": manual,
        })
        if action is None:
            raise ValueError("此 HR 消息已有发送动作记录，不能自动重试。")
        store.mark_action(action["action_id"], "ATTEMPTING")
        try:
            receipt = bridge.send_reply_and_observe(message, config["selectors"], before_count)
        except Exception as exc:
            store.mark_action(action["action_id"], "UNKNOWN", {"error": str(exc)[:500]})
            store.set_reply_state(key, "UNKNOWN")
            _notify_once("reply-unknown:" + key, f"与 {message['sender']} 的回复结果不明，请检查招聘网站会话。", config)
            raise
        store.mark_action(action["action_id"], "SENT_VISIBLE", receipt)
        store.set_reply_state(key, "SENT_VISIBLE")
        return receipt


def dismiss_message(key: str) -> None:
    with _CYCLE_LOCK:
        message = store.get_message(key)
        if not message or message["reply_state"] not in {"REVIEW", "READY"}:
            raise ValueError("这条消息已经不能标记为无需回复。")
        if store.action("hr_reply", key) is not None:
            raise ValueError("消息已有发送动作，先核对平台结果。")
        store.set_reply_state(key, "NO_REPLY")


def update_message_draft(key: str, draft: str) -> None:
    with _CYCLE_LOCK:
        store.update_message_draft(key, draft)
        message = store.get_message(key)
        if message:
            _queue_review("hr", message, store.settings())


def scan_messages() -> dict[str, Any]:
    with _CYCLE_LOCK:
        run_id = store.start_run("messages")
        count = {"visible": 0, "new": 0, "review": 0, "auto_replied": 0, "errors": []}
        try:
            config = store.settings()
            resume_text, _ = model.read_career_files(config["resume_file"],
                                                      config["supporting_files"])
            observed = bridge.discover_visible_messages(config["message_url"], config["selectors"])
            count["visible"] = len(observed)
            seen = {(item["conversation_url"], item["message_id"]) for item in observed}
            for pending in store.retryable_messages():
                identity = (pending["conversation_url"], pending["message_id"])
                if identity not in seen:
                    observed.append(pending)
                    seen.add(identity)
            for item in observed:
                key = None
                try:
                    key, created = store.upsert_message(item)
                    stored = store.get_message(key)
                    if stored is None:
                        raise RuntimeError("HR 消息保存后无法读取。")
                    if not created:
                        # A previous model/API failure is retryable. A prepared
                        # routine reply can also resume if no send was attempted.
                        # Sent/unknown actions and human-review items stay put.
                        if stored["reply_state"] == "READY":
                            if (config["auto_reply"] and stored["category"] == "routine"
                                    and store.action("hr_reply", key) is None):
                                try:
                                    reply_one(key)
                                    count["auto_replied"] += 1
                                except Exception as exc:
                                    count["errors"].append(f"{item['sender']} 自动回复暂停：{exc}")
                            continue
                        if stored["reply_state"] == "REVIEW":
                            _queue_review("hr", stored, config)
                            continue
                        if stored["reply_state"] not in {"PENDING", "ERROR"}:
                            continue
                    else:
                        count["new"] += 1
                    job = store.get_job(stored["job_key"]) if stored and stored["job_key"] else None
                    job_error = ""
                    if job is None and stored["job_title"] and stored["company"]:
                        job = store.find_job_by_title_company(stored["job_title"], stored["company"])
                        if job:
                            store.link_message_job(key, job)
                    if job is None and stored and stored["job_url"]:
                        try:
                            detail = bridge.read_job_detail(stored["job_url"])
                            store.upsert_job(detail)
                            job = store.get_job(stored["job_key"])
                        except Exception as exc:
                            job_error = f"关联 JD 未能读取：{exc}"
                    if job is None and not job_error:
                        job_error = "未找到唯一匹配的岗位 JD，回复需人工审核"
                    plan = model.draft_hr_reply(stored, job, resume_text,
                                                config["profile_facts"],
                                                config["reply_policy_prompt"])
                    if not plan["should_reply"]:
                        state = "NO_REPLY"
                    elif plan["review_required"] or job_error or not config["auto_reply"]:
                        state = "REVIEW"
                    else:
                        state = "READY"
                    reason = plan["reason"] + ("；" + job_error if job_error else "")
                    store.set_message_plan(key, plan["category"], plan["draft"], reason, state)
                    if state == "REVIEW":
                        count["review"] += 1
                        _queue_review("hr", store.get_message(key), config)
                    elif plan["notify_owner"]:
                        _notify_once("hr:" + key, f"{item['sender']} 发来消息，请到工作台查看处理结果。", config)
                    if state == "READY":
                        try:
                            reply_one(key)
                            count["auto_replied"] += 1
                        except Exception as exc:
                            count["errors"].append(f"{item['sender']} 自动回复暂停：{exc}")
                except Exception as exc:
                    if key and store.get_message(key):
                        try:
                            store.set_message_plan(key, "unknown", "", str(exc), "ERROR")
                            _notify_once("hr-error:" + key,
                                         f"HR 消息 {item.get('sender', '')} 需要人工检查，自动分析失败。", config)
                        except Exception:
                            pass
                    count["errors"].append(f"{item.get('sender') or 'HR 消息'}：{exc}")
            status = "PARTIAL" if count["errors"] else "COMPLETED"
            store.finish_run(run_id, status, json.dumps(count, ensure_ascii=False))
            return {"run_id": run_id, **count}
        except Exception as exc:
            store.finish_run(run_id, "FAILED", str(exc))
            if "config" in locals():
                fingerprint = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:12]
                _notify_once(f"messages-scan-error:{datetime.now().date()}:{fingerprint}",
                             f"HR 消息检查暂停，请人工查看：{str(exc)[:160]}", config)
            raise


_QQ_COMMAND = re.compile(r"([12])\s+([0-9A-F]{12})", re.I)


def scan_owner_approvals() -> dict[str, Any]:
    """Act only on an exact one-use code visible in the configured owner chat."""
    config = store.settings()
    if not config["qq_command_enabled"] or not config["notify_qq"]:
        return {"processed": 0, "errors": []}
    pending = {item["review_code"]: item for item in store.pending_reviews()}
    if not pending:
        return {"processed": 0, "errors": []}
    run_id = store.start_run("qq_approval")
    result: dict[str, Any] = {"processed": 0, "errors": []}
    try:
        # Reading QQ can proceed while the BOSS browser is busy. Any approval
        # action still takes the shared BOSS lock and rechecks the exact target.
        observed = json.loads(qq_owner_read_visible(config["qq_owner_qq"],
                                                    config["qq_owner_name"]))
        if observed.get("status") != "VISIBLE_TEXT":
            raise RuntimeError("QQ 会话文字不可读取，远程审核没有执行。")
        for item in observed.get("items", []):
            match = _QQ_COMMAND.fullmatch(str(item.get("text") or "").strip())
            if not match:
                continue
            decision = "approve" if match.group(1) == "1" else "reject"
            code = match.group(2).upper()
            if code not in pending:
                continue
            with _CYCLE_LOCK:
                review = store.claim_review(code, decision)
                if review is None:
                    continue
                try:
                    kind, key = review["kind"], review["target_key"]
                    current = store.get_job(key) if kind == "job" else store.get_message(key)
                    expected_state = (current and
                                      (current["match_state"] in {"MATCHED", "REVIEW"} and
                                       current["apply_state"] == "NOT_APPLIED" if kind == "job"
                                       else current["reply_state"] == "REVIEW"))
                    if (not expected_state or
                            _review_hash(kind, current) != review["target_hash"]):
                        store.finish_review(code, "STALE", "审核对象或草稿已经改变，没有执行。")
                        continue
                    if kind == "job":
                        apply_one(key, manual=True) if decision == "approve" else skip_job(key)
                    else:
                        reply_one(key, manual=True) if decision == "approve" else dismiss_message(key)
                    store.finish_review(code, "APPROVED" if decision == "approve" else "REJECTED")
                    result["processed"] += 1
                    _notify_once("review-result:" + code,
                                 f"编号 {code} 已按你的 QQ 指令处理。", config)
                except Exception as exc:
                    store.finish_review(code, "UNKNOWN", str(exc))
                    result["errors"].append(f"编号 {code}：{exc}")
        store.finish_run(run_id, "PARTIAL" if result["errors"] else "COMPLETED",
                         json.dumps(result, ensure_ascii=False))
        return result
    except Exception as exc:
        store.finish_run(run_id, "FAILED", str(exc))
        raise


class CareerRunner:
    """Independent timers and workers; BOSS page actions share _CYCLE_LOCK."""

    def __init__(self) -> None:
        self.next_job_run_at: datetime | None = None
        self.next_hr_run_at: datetime | None = None
        self.next_qq_run_at: datetime | None = None
        self.last_job_error = ""
        self.last_hr_error = ""
        self.last_qq_error = ""
        self.last_manual_result: dict[str, Any] | None = None
        self.last_manual_error = ""
        self._state_lock = threading.RLock()
        self._active: set[str] = set()
        self._job_signature: tuple[Any, ...] | None = None
        self._hr_signature: tuple[Any, ...] | None = None
        self._qq_enabled: bool | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="career-poll", daemon=True)
        self._thread.start()

    @staticmethod
    def _next_daily(now: datetime, times: list[str]) -> datetime:
        for day_offset in (0, 1):
            day = now.date() + timedelta(days=day_offset)
            for value in sorted(times):
                hour, minute = (int(part) for part in value.split(":"))
                candidate = datetime.combine(day, datetime.min.time(), now.tzinfo).replace(
                    hour=hour, minute=minute)
                if candidate > now:
                    return candidate
        raise RuntimeError("每天巡航时间配置无效。")

    @property
    def running_kind(self) -> str:
        """Compatibility summary for older callers."""
        with self._state_lock:
            return ",".join(sorted(self._active))

    def is_running(self, kind: str) -> bool:
        with self._state_lock:
            return kind in self._active

    def start_manual_jobs(self) -> None:
        """Run one user-started application round without blocking Streamlit."""
        with self._state_lock:
            if "job" in self._active:
                raise RuntimeError("已有一轮岗位巡航正在运行，请等待结束后再启动。")
            self._active.add("job")
            self.last_manual_result = None
            self.last_manual_error = ""
            self.last_job_error = ""
        try:
            threading.Thread(target=self._run_manual_jobs, name="career-manual-job",
                             daemon=True).start()
        except Exception:
            with self._state_lock:
                self._active.discard("job")
            raise

    def _run_manual_jobs(self) -> None:
        result = None
        error = ""
        try:
            result = scan_jobs(apply_mode=True)
            error = "；".join(str(item) for item in result.get("errors", []))[:500]
        except Exception as exc:
            error = str(exc)[:500]
        finished = datetime.now().astimezone()
        with self._state_lock:
            self.last_manual_result = result
            self.last_manual_error = error
            self.last_job_error = error
            try:
                if self.next_job_run_at and self.next_job_run_at <= finished:
                    config = store.settings()
                    self.next_job_run_at = (
                        self._next_daily(finished, config["job_run_times"])
                        if config["job_schedule_mode"] == "daily" else
                        finished + timedelta(minutes=config["poll_minutes"])
                    )
            except Exception as exc:
                self.last_manual_error = (self.last_manual_error + "；" + str(exc)).strip("；")[:500]
                self.last_job_error = self.last_manual_error
            finally:
                self._active.discard("job")

    def _run_due(self, kind: str, step: Any, config: dict[str, Any],
                 signature: Any) -> None:
        try:
            result = step()
            error = "；".join(str(item) for item in result.get("errors", []))[:500]
        except Exception as exc:
            error = str(exc)[:500]
        finished = datetime.now().astimezone()
        with self._state_lock:
            try:
                if kind == "job":
                    self.last_job_error = error
                    if (self._job_signature == signature and self.next_job_run_at and
                            self.next_job_run_at <= finished):
                        self.next_job_run_at = (
                            self._next_daily(finished, config["job_run_times"])
                            if config["job_schedule_mode"] == "daily" else
                            finished + timedelta(minutes=config["poll_minutes"])
                        )
                elif kind == "hr":
                    self.last_hr_error = error
                    if (self._hr_signature == signature and self.next_hr_run_at and
                            self.next_hr_run_at <= finished):
                        self.next_hr_run_at = finished + timedelta(minutes=config["hr_poll_minutes"])
                else:
                    self.last_qq_error = error
                    if self._qq_enabled == signature and self.next_qq_run_at and self.next_qq_run_at <= finished:
                        self.next_qq_run_at = finished + timedelta(minutes=1)
            finally:
                self._active.discard(kind)

    def _loop(self) -> None:
        while not self._stop.wait(5):
            try:
                config = store.settings()
                now = datetime.now().astimezone()
                current_job = (bool(config["automation_enabled"]), config["job_schedule_mode"],
                               int(config["poll_minutes"]), tuple(config["job_run_times"]))
                current_hr = (bool(config["monitor_messages"]), int(config["hr_poll_minutes"]))
                current_qq = bool(config["qq_command_enabled"])
                with self._state_lock:
                    if current_job != self._job_signature:
                        self._job_signature = current_job
                        self.next_job_run_at = (
                            self._next_daily(now, config["job_run_times"])
                            if current_job[0] and config["job_schedule_mode"] == "daily" else
                            now + timedelta(minutes=config["poll_minutes"]) if current_job[0] else None
                        )
                    if current_hr != self._hr_signature:
                        self._hr_signature = current_hr
                        self.next_hr_run_at = (now + timedelta(minutes=config["hr_poll_minutes"])
                                               if current_hr[0] else None)
                    if current_qq != self._qq_enabled:
                        self._qq_enabled = current_qq
                        self.next_qq_run_at = (now + timedelta(minutes=1) if current_qq else None)
                    due = []
                    for kind, deadline, enabled, step, signature in (
                        ("job", self.next_job_run_at, current_job[0], scan_jobs, current_job),
                        ("hr", self.next_hr_run_at, current_hr[0], scan_messages, current_hr),
                        ("qq", self.next_qq_run_at, current_qq, scan_owner_approvals, current_qq),
                    ):
                        if enabled and deadline and now >= deadline and kind not in self._active:
                            self._active.add(kind)
                            due.append((kind, step, signature))
                for kind, step, signature in due:
                    try:
                        threading.Thread(target=self._run_due, args=(kind, step, config, signature),
                                         name="career-" + kind, daemon=True).start()
                    except Exception:
                        with self._state_lock:
                            self._active.discard(kind)
                        raise
            except Exception as exc:
                with self._state_lock:
                    self.last_job_error = str(exc)[:500]
                    self.last_hr_error = str(exc)[:500]
                    self.last_qq_error = str(exc)[:500]

    def stop(self) -> None:
        self._stop.set()


_RUNNER_LOCK = threading.Lock()
_RUNNER: CareerRunner | None = None


def get_runner() -> CareerRunner:
    """Keep one timer thread per Python process across Streamlit page reruns."""
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None or not _RUNNER._thread.is_alive():
            _RUNNER = CareerRunner()
        return _RUNNER


def inspect_browser() -> dict[str, Any]:
    with _CYCLE_LOCK:
        search_url = store.settings()["search_url"]
        bridge.select_boss_tab(search_url)
        return bridge.inspect_job_search_page(search_url)
