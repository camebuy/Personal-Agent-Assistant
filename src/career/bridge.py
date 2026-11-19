"""BOSS page adapter using the user's Kimi WebBridge browser session.

Search and filter controls are used by the fixed workflow. Submissions require
an observable receipt and durable authorization checks.
"""
from __future__ import annotations

import http.client
import hashlib
import io
import json
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from config.settings import settings
from src.career.store import job_key, settings as career_settings


_LOCK = threading.RLock()


class BrowserUnavailable(RuntimeError):
    pass


class BrowserObservationError(RuntimeError):
    pass


def _is_boss(url: str) -> bool:
    parts = urlsplit(str(url))
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() == "https" and (host == "zhipin.com" or host.endswith(".zhipin.com"))


def _command(action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({"action": action, "args": args or {},
                       "session": "aacwork-career"}, ensure_ascii=False).encode("utf-8")
    with _LOCK:
        connection = http.client.HTTPConnection(settings.KIMI_WEBBRIDGE_HOST,
                                                settings.KIMI_WEBBRIDGE_PORT,
                                                timeout=settings.KIMI_WEBBRIDGE_TIMEOUT)
        try:
            connection.request("POST", "/command", body,
                               {"Content-Type": "application/json; charset=utf-8"})
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024)
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise BrowserUnavailable(
                f"无法连接 Kimi WebBridge 本地服务 "
                f"{settings.KIMI_WEBBRIDGE_HOST}:{settings.KIMI_WEBBRIDGE_PORT}。"
                "请在 PowerShell 检查 kimi-webbridge status；未运行时尝试 kimi-webbridge start。"
                "如果进程仍在但端口无法连接，请手动运行 kimi-webbridge restart。"
            ) from exc
        finally:
            connection.close()
    try:
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BrowserUnavailable("WebBridge 返回格式无效。") from exc
    if not isinstance(result, dict):
        raise BrowserUnavailable("WebBridge 返回的响应不是对象。")
    if status // 100 != 2 or result.get("ok") is not True:
        error = result.get("error") or {}
        detail = error.get("message") if isinstance(error, dict) else str(error)
        if detail and ("No last-focused window" in detail or "No current window" in detail):
            raise BrowserUnavailable(
                "WebBridge 服务与扩展已连接，但没有可操作的浏览器窗口。"
                "请在安装扩展的 Edge 中打开 BOSS 直聘并切到该标签页。"
            )
        raise BrowserUnavailable(f"WebBridge {action} 失败：{detail or status}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise BrowserObservationError(f"WebBridge {action} 未返回有效数据。")
    if data.get("success") is False:
        raise BrowserUnavailable(f"WebBridge {action} 未确认成功：{data.get('message') or data}")
    return data


def _evaluate(code: str) -> Any:
    data = _command("evaluate", {"code": code})
    value = data.get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _page() -> dict[str, str]:
    value = _evaluate("JSON.stringify({url:location.href,title:document.title})")
    if not isinstance(value, dict) or not _is_boss(value.get("url", "")):
        raise BrowserObservationError("当前浏览器页面不是 BOSS 直聘，已停止。")
    return {"url": str(value["url"]), "title": str(value.get("title") or "")}


_PRIVATE_GLYPH = re.compile(r"[\uE000-\uF8FF]")
_SALARY_RANGE = re.compile(r"([1-9]\d{0,2})(?:-([1-9]\d{0,2}))?K(?:[·.]([1-9]\d?)薪)?")


def _salary_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    text = re.sub(r"\s+", "", text)
    for separator in ("–", "—", "~", "～", "至"):
        text = text.replace(separator, "-")
    return text.replace("•", "·")


def _parsed_visual_salary(value: str, raw: str) -> str:
    text = _salary_text(value)
    match = _SALARY_RANGE.fullmatch(text)
    if not match:
        return ""
    low, high = int(match[1]), int(match[2] or match[1])
    months = int(match[3]) if match[3] else None
    if high < low or high > 500 or (months is not None and not 12 <= months <= 24):
        return ""
    # The private-font characters each render as one digit. Reject OCR that
    # changes the number of digits or any visible salary delimiter.
    expected_shape = re.sub(r"\d", "#", _PRIVATE_GLYPH.sub("#", _salary_text(raw)))
    observed_shape = re.sub(r"\d", "#", text)
    return text if expected_shape == observed_shape else ""


def _recognize_salary_image(image_bytes: bytes, raw: str) -> str:
    from PIL import Image, ImageOps
    import numpy as np
    from src.career.mail_post import ocr_reader

    with Image.open(io.BytesIO(image_bytes)) as source:
        if not (20 <= source.width <= 350 and 12 <= source.height <= 100):
            return ""
        source.load()
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        enlarged = source.convert("RGB").resize((source.width * 4, source.height * 4), resampling)
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(enlarged))
    variants = (enlarged, grayscale)
    recognized: list[str] = []
    for variant in variants:
        padded = ImageOps.expand(variant, border=20, fill="white")
        lines = ocr_reader().readtext(
            np.asarray(padded), detail=1, paragraph=False, min_size=5,
            allowlist="0123456789Kk-–—~·.薪")
        if not 1 <= len(lines) <= 3 or any(float(item[2]) < 0.78 for item in lines):
            return ""
        lines.sort(key=lambda item: min(float(point[0]) for point in item[0]))
        candidate = _parsed_visual_salary("".join(str(item[1]) for item in lines), raw)
        if not candidate:
            return ""
        recognized.append(candidate)
    return recognized[0] if recognized[0] == recognized[1] else ""


def _salary_region_ready(selector: str, expected_path: str, raw: str,
                         mode: str) -> bool:
    observed = _evaluate("""(() => { const selector=%s, path=%s, raw=%s, mode=%s;
      const nodes=Array.from(document.querySelectorAll(selector));
      if(nodes.length!==1) return JSON.stringify(false);
      const x=nodes[0], rect=x.getBoundingClientRect(), style=getComputedStyle(x);
      const detail=document.querySelector('.job-detail-box a.more-job-btn[href*="/job_detail/"]');
      const detailPath=detail?new URL(detail.href).pathname:'';
      const cardLink=x.closest('.job-card-box')?.querySelector('a.job-name[href*="/job_detail/"]');
      const cardPath=cardLink?new URL(cardLink.href).pathname:'';
      const sameJob=mode==='list' ? detailPath===path && cardPath===path : location.pathname===path;
      const visible=rect.width>=20 && rect.width<=350 && rect.height>=12 && rect.height<=100
        && rect.left>=0 && rect.top>=0 && rect.right<=innerWidth && rect.bottom<=innerHeight
        && style.display!=='none' && style.visibility!=='hidden' && style.opacity!=='0';
      const clear=visible && [0.2,0.5,0.8].every(ratio=>{
        const top=document.elementFromPoint(rect.left+rect.width*ratio,rect.top+rect.height/2);
        return top===x || x.contains(top);
      });
      return JSON.stringify(sameJob && x.innerText.trim()===raw && clear);
    })()""" % (json.dumps(selector), json.dumps(expected_path),
                json.dumps(raw, ensure_ascii=False), json.dumps(mode)))
    return observed is True


def _read_visual_salary(selector: str, expected_path: str, raw: str,
                        mode: str) -> tuple[str, str]:
    if not raw or not _PRIVATE_GLYPH.search(raw):
        return "", "页面没有可识别的薪资字形。"
    try:
        with tempfile.TemporaryDirectory(prefix="aacwork-salary-") as directory:
            destination = Path(directory) / "salary.png"
            with _LOCK:
                if not _salary_region_ready(selector, expected_path, raw, mode):
                    return "", "当前岗位的薪资区域未显示、被遮挡或不唯一。"
                receipt = _command("screenshot", {"format": "png", "selector": selector,
                                                   "path": str(destination)})
                if (receipt.get("format") != "png" or receipt.get("mimeType") != "image/png"
                        or Path(str(receipt.get("path") or "")).resolve() != destination.resolve()
                        or not _salary_region_ready(selector, expected_path, raw, mode)):
                    return "", "截图前后岗位或薪资区域发生变化。"
                if not destination.is_file() or not 0 < destination.stat().st_size <= 300_000:
                    return "", "薪资截图为空或大小异常。"
                image_bytes = destination.read_bytes()
        salary = _recognize_salary_image(image_bytes, raw)
        return (salary, "") if salary else ("", "OCR 未能两次一致地读出可信的薪资。")
    except ImportError:
        return "", "本机缺少薪资 OCR 依赖。"
    except Exception:
        # Browser capture and OCR are optional observations. A failure must
        # leave the salary unknown, never create an automatic match.
        return "", "薪资截图或 OCR 识别出错。"


def select_boss_tab(url: str) -> dict[str, str]:
    if not _is_boss(url):
        raise ValueError("只能打开 BOSS 直聘页面。")
    try:
        _command("find_tab", {"url": url, "active": True})
    except BrowserUnavailable as exc:
        if ("没有可操作的浏览器窗口" in str(exc)
                or "无法连接 Kimi WebBridge 本地服务" in str(exc)):
            raise
        try:
            _command("find_tab", {"url": url, "active": False})
        except BrowserUnavailable as exc:
            if ("没有可操作的浏览器窗口" in str(exc)
                    or "无法连接 Kimi WebBridge 本地服务" in str(exc)):
                raise
            _command("navigate", {"url": url, "newTab": True,
                                  "group_title": "求职巡航"})
    return _page()


def navigate_boss(url: str) -> dict[str, str]:
    if not _is_boss(url):
        raise ValueError("只能访问 BOSS 直聘页面。")
    try:
        current = _page()
    except (BrowserUnavailable, BrowserObservationError):
        current = select_boss_tab(url)
    current_parts, target_parts = urlsplit(current["url"]), urlsplit(url)
    same_page = (current_parts.hostname == target_parts.hostname
                 and current_parts.path.rstrip("/") == target_parts.path.rstrip("/")
                 and (not target_parts.query or current_parts.query == target_parts.query))
    if not same_page:
        _command("navigate", {"url": url, "newTab": False})
    page = _page()
    if not _is_boss(page["url"]):
        raise BrowserObservationError("导航后离开了招聘网站。")
    return page


def inspect_job_search_page(search_url: str) -> dict[str, Any]:
    """Report whether the visible jobs page has usable search controls."""
    page = navigate_boss(search_url)
    result = _evaluate("""(() => JSON.stringify({
      search_fields:document.querySelectorAll('input[placeholder="搜索职位、公司"]').length,
      search_buttons:document.querySelectorAll('a[ka="job_search_btn_click"]').length,
      visible_jobs:document.querySelectorAll('.job-card-box a.job-name[href*="/job_detail/"]').length
    }))()""")
    if not isinstance(result, dict):
        raise BrowserObservationError("无法检查 BOSS 岗位页的搜索控件。")
    return {"page": page, "search_fields": int(result.get("search_fields") or 0),
            "search_buttons": int(result.get("search_buttons") or 0),
            "visible_jobs": int(result.get("visible_jobs") or 0)}


def _visible_search_control(selector: str, marker: str) -> dict[str, int]:
    """Mark one rendered search control so a hidden duplicate is not used."""
    result = _evaluate("""(() => { const nodes=Array.from(document.querySelectorAll(%s));
      nodes.forEach(x=>x.removeAttribute(%s));
      const visible=nodes.filter(x=>{const s=getComputedStyle(x);
        return x.getClientRects().length>0 && s.display!=='none' && s.visibility!=='hidden';});
      if(visible.length===1) visible[0].setAttribute(%s,'1');
      return JSON.stringify({total:nodes.length,visible:visible.length});
    })()""" % (json.dumps(selector), json.dumps(marker), json.dumps(marker)))
    if not isinstance(result, dict):
        raise BrowserObservationError("无法核对 BOSS 搜索控件。")
    return {"total": int(result.get("total") or 0),
            "visible": int(result.get("visible") or 0)}


def search_jobs(keyword: str, search_url: str) -> dict[str, str]:
    """Use the visible BOSS search field and verify the resulting query URL."""
    term = str(keyword or "").strip()
    if not term or len(term) > 80:
        raise ValueError("搜索关键词须为 1 到 80 字。")
    page = navigate_boss(search_url)
    if parse_qs(urlsplit(page["url"]).query).get("query", [""])[0] == term:
        return page
    field = 'input[placeholder="搜索职位、公司"]'
    button = 'a[ka="job_search_btn_click"]'
    field_marker, button_marker = "data-aacwork-search-field", "data-aacwork-search-button"
    fields = buttons = {"total": 0, "visible": 0}
    for _ in range(8):
        fields = _visible_search_control(field, field_marker)
        buttons = _visible_search_control(button, button_marker)
        if fields["visible"] == buttons["visible"] == 1:
            break
        time.sleep(0.5)
    if fields["visible"] != 1 or buttons["visible"] != 1:
        raise BrowserObservationError(
            f"BOSS 搜索控件未稳定（可见搜索框 {fields['visible']} 个、"
            f"可见按钮 {buttons['visible']} 个），已停止。")
    marked_field = f'[{field_marker}="1"]'
    _command("fill", {"selector": marked_field, "value": term})
    if _visible_search_control(field, field_marker)["visible"] != 1:
        raise BrowserObservationError("搜索框在输入后发生变化，未提交搜索。")
    current_value = _evaluate("(() => JSON.stringify(document.querySelector(%s)?.value||''))()" %
                              json.dumps(marked_field))
    if current_value != term:
        raise BrowserObservationError("网页搜索词填写后无法核对，未提交搜索。")
    if _visible_search_control(button, button_marker)["visible"] != 1:
        raise BrowserObservationError("搜索按钮在输入后发生变化，未提交搜索。")
    _command("click", {"selector": f'[{button_marker}="1"]'})
    for _ in range(8):
        page = _page()
        query = parse_qs(urlsplit(page["url"]).query).get("query", [""])[0]
        if query == term:
            return page
        time.sleep(0.5)
    raise BrowserObservationError("提交搜索后，网页地址未出现对应关键词。")


def discover_visible_jobs(search_url: str, limit: int = 2000, *, navigate: bool = True) -> list[dict[str, str]]:
    """Read job links and text from the rendered list; no hidden API replay."""
    if navigate:
        navigate_boss(search_url)
    elif urlsplit(_page()["url"]).path.rstrip("/") != "/web/geek/jobs":
        raise BrowserObservationError("当前页不是岗位搜索结果，已停止采集。")
    for _ in range(5):
        value = _evaluate("""(() => {
          const anchors = Array.from(document.querySelectorAll(
            '.job-card-box a.job-name[href*="/job_detail/"]'))
            .filter(a => a.getClientRects().length>0 &&
              getComputedStyle(a).visibility!=='hidden');
          const seen = new Set();
          return JSON.stringify(anchors.map(a => {
            const u = new URL(a.getAttribute('href'), location.href).href;
            if (seen.has(u)) return null; seen.add(u);
            const card = a.closest('.job-card-box');
            const text = (card?.innerText || a.innerText || '').trim().slice(0,3000);
            return {url:u, title:(a.innerText||'').trim().slice(0,120), list_text:text};
          }).filter(Boolean));
        })()""")
        if isinstance(value, list) and value:
            output = []
            for row in value[:max(1, min(limit, 2000))]:
                if isinstance(row, dict):
                    try:
                        job_key(row.get("url", ""))
                        output.append({k: str(row.get(k) or "") for k in ("url", "title", "list_text")})
                    except ValueError:
                        continue
            if output:
                return output
        time.sleep(1)
    empty = _evaluate("""(() => { const region=document.querySelector(
      '.job-list-box,.job-list,.search-job-list,.job-list-container');
      const text=(region?.innerText||'').replace(/\\s+/g,'');
      return JSON.stringify(/暂无相关职位|没有找到相关职位|未找到相关职位|暂无岗位|无搜索结果|没有符合条件的职位/.test(text));
    })()""")
    if empty is True:
        return []
    raise BrowserObservationError("岗位列表未出现可核实的详情链接；可能需要登录、手动选城市或处理页面提示。")


def restore_job_results(result_url: str, keyword: str) -> str:
    """Return to the active keyword's search results after reading a job or chat."""
    try:
        current = _page()
    except (BrowserUnavailable, BrowserObservationError):
        current = None
    if current is not None and current["url"] == result_url:
        return result_url
    page = navigate_boss(result_url)
    if (urlsplit(page["url"]).path.rstrip("/") != "/web/geek/jobs" or
            parse_qs(urlsplit(page["url"]).query).get("query", [""])[0] != keyword):
        raise BrowserObservationError("无法恢复原搜索结果；未继续检查其他岗位。")
    return page["url"]


def advance_job_results(seen_paths: set[str], keyword: str) -> str | None:
    """Scroll the rendered list, then use a visible next-page control if present."""
    page = _page()
    if (urlsplit(page["url"]).path.rstrip("/") != "/web/geek/jobs" or
            parse_qs(urlsplit(page["url"]).query).get("query", [""])[0] != keyword):
        raise BrowserObservationError("翻页前已离开当前关键词的岗位列表。")
    for _ in range(8):
        _evaluate("""(() => { const cards=Array.from(document.querySelectorAll(
          '.job-card-box a.job-name[href*="/job_detail/"]'))
          .filter(a=>a.getClientRects().length>0 &&
            getComputedStyle(a).visibility!=='hidden');
          const last=cards[cards.length-1];
          if(last) last.scrollIntoView({block:'end',inline:'nearest'});
          return JSON.stringify({count:cards.length});
        })()""")
        time.sleep(1)
        cards = discover_visible_jobs(page["url"], navigate=False)
        if any(urlsplit(card["url"]).path not in seen_paths for card in cards):
            return _page()["url"]
    marker = "data-aacwork-next-job-page"
    state = _evaluate("""(() => { const marker=%s;
      const nodes=Array.from(document.querySelectorAll('a,button,[role="button"]'));
      nodes.forEach(e=>e.removeAttribute(marker));
      const next=nodes.filter(e=>e.getClientRects().length>0 &&
        /^(下一页|下页|下一页[>›»])$/.test((e.innerText||e.getAttribute('aria-label')||'').replace(/\\s+/g,'')) &&
        !e.disabled && e.getAttribute('aria-disabled')!=='true' &&
        !e.classList.contains('disabled'));
      if(next.length===1) next[0].setAttribute(marker,'1');
      return JSON.stringify({count:next.length});
    })()""" % json.dumps(marker))
    if not isinstance(state, dict):
        raise BrowserObservationError("无法检查 BOSS 岗位翻页控件。")
    if state.get("count") == 0:
        return None
    if state.get("count") != 1:
        raise BrowserObservationError("BOSS 页面有多个下一页控件，无法确认目标页。")
    _command("click", {"selector": f'[{marker}="1"]'})
    for _ in range(8):
        time.sleep(0.5)
        current = _page()
        if parse_qs(urlsplit(current["url"]).query).get("query", [""])[0] != keyword:
            raise BrowserObservationError("翻页后搜索词发生变化，已停止。")
        cards = discover_visible_jobs(current["url"], navigate=False)
        if any(urlsplit(card["url"]).path not in seen_paths for card in cards):
            return current["url"]
    raise BrowserObservationError("点击下一页后没有出现新岗位，已停止以避免循环。")


def _select_job_detail(url: str) -> dict[str, str]:
    """Prefer the list's detail pane over reopening a standalone detail URL."""
    expected = job_key(url)
    token = urlsplit(url).path.rsplit("/", 1)[-1].removesuffix(".html")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise BrowserObservationError("岗位链接的标识无法安全匹配页面控件。")
    try:
        page = _page()
    except (BrowserUnavailable, BrowserObservationError):
        page = select_boss_tab(url)
    current_path = urlsplit(page["url"]).path.rstrip("/")
    if current_path != "/web/geek/jobs":
        if "/job_detail/" in current_path and job_key(page["url"]) == expected:
            return {"mode": "standalone", "page_url": page["url"]}
        _command("navigate", {"url": career_settings()["search_url"], "newTab": False})
        page = _page()
        if urlsplit(page["url"]).path.rstrip("/") != "/web/geek/jobs":
            raise BrowserObservationError("无法打开岗位列表，未尝试直接重开详情链接。")

    path = urlsplit(url).path
    selected = None
    for _ in range(8):
        selected = _evaluate("""(() => { const p=%s;
          const links=Array.from(document.querySelectorAll('.job-card-box a.job-name'))
            .filter(a=>new URL(a.href).pathname===p);
          const detail=document.querySelector('.job-detail-box');
          const more=detail?.querySelector('a.more-job-btn[href*="/job_detail/"]');
          return JSON.stringify({count:links.length,
            detailPath:more ? new URL(more.href).pathname : '',
            chatKa:detail?.querySelector('.op-btn-chat')?.getAttribute('ka')||''});
        })()""" % json.dumps(path))
        if isinstance(selected, dict) and selected.get("count") == 1:
            break
        time.sleep(0.5)
    if not isinstance(selected, dict) or selected.get("count") != 1:
        return _open_standalone_job(url, expected)
    if selected.get("detailPath") != path:
        _command("click", {"selector": f'.job-card-box a.job-name[href*="/{token}.html"]'})
    for _ in range(8):
        page = _page()
        if "/job_detail/" in urlsplit(page["url"]).path and job_key(page["url"]) == expected:
            return {"mode": "standalone", "page_url": page["url"]}
        selected = _evaluate("""(() => { const d=document.querySelector('.job-detail-box');
          const more=d?.querySelector('a.more-job-btn[href*="/job_detail/"]');
          return JSON.stringify({detailPath:more ? new URL(more.href).pathname : '',
            chatKa:d?.querySelector('.op-btn-chat')?.getAttribute('ka')||''});
        })()""")
        if (isinstance(selected, dict) and selected.get("detailPath") == path
                and str(selected.get("chatKa") or "").endswith(token)):
            return {"mode": "list_pane", "page_url": _page()["url"]}
        time.sleep(0.5)
    return _open_standalone_job(url, expected)


def _open_standalone_job(url: str, expected: str) -> dict[str, str]:
    """Read a visible job page when the search list has changed underneath us."""
    _command("navigate", {"url": url, "newTab": False})
    page = _page()
    if job_key(page["url"]) != expected:
        raise BrowserObservationError("独立详情页不是目标岗位，已停止采集。")
    return {"mode": "standalone", "page_url": page["url"]}


def read_job_detail(url: str, listed_title: str = "") -> dict[str, str]:
    expected = job_key(url)
    selection = _select_job_detail(url)
    if selection["mode"] == "list_pane":
        value = _evaluate("""(() => { const p=%s;
          const link=Array.from(document.querySelectorAll('.job-card-box a.job-name'))
            .find(a=>new URL(a.href).pathname===p);
          const card=link?.closest('.job-card-box');
          const detail=document.querySelector('.job-detail-box');
          const head=detail?.querySelector('.job-detail-header');
          const firm=(detail?.querySelector('.job-boss-info .boss-info-attr')?.innerText||'')
            .trim().split(/\\s*[·•]\\s*/)[0];
          const salary=(head?.querySelector('.job-salary')?.innerText||'').trim();
          return JSON.stringify({title:(head?.querySelector('.job-name')?.innerText||'').trim(),
            card_title:(link?.innerText||'').trim(),
            company:firm,card_company:(card?.querySelector('.boss-name')?.innerText||'').trim(),
            salary:salary,card_salary:(card?.querySelector('.job-salary')?.innerText||'').trim(),
            location:(card?.querySelector('.company-location')?.innerText||'').trim(),
            requirements:(head?.querySelector('.tag-list')?.innerText||'').trim().slice(0,300),
            jd_text:(detail?.querySelector('.job-detail-body .desc')?.innerText||'').trim().slice(0,30000),
            detail_path:new URL(detail?.querySelector('a.more-job-btn')?.href||location.href).pathname});
        })()""" % json.dumps(urlsplit(url).path))
        if (not isinstance(value, dict) or value.get("detail_path") != urlsplit(url).path
                or value.get("title") != value.get("card_title")
                or not value.get("company") or value.get("company") != value.get("card_company")
                or len(str(value.get("jd_text") or "")) < 30):
            _open_standalone_job(url, expected)
        else:
            salary = str(value.get("salary") or "")
            card_salary = str(value.get("card_salary") or "")
            salary_source = "dom" if salary and not _PRIVATE_GLYPH.search(salary) else "unreadable"
            salary_note = ""
            if _PRIVATE_GLYPH.search(salary or card_salary):
                filename = urlsplit(url).path.rsplit("/", 1)[-1]
                salary = ""
                if (re.fullmatch(r"[A-Za-z0-9_-]+\.html", filename)
                        and card_salary and
                        (not value.get("salary") or card_salary == value["salary"])):
                    selector = ('.job-card-box:has(a.job-name[href*="/' + filename + '"]) '
                                '.job-salary')
                    salary, salary_note = _read_visual_salary(
                        selector, urlsplit(url).path, card_salary, "list")
                else:
                    salary_note = "列表和详情的薪资字形无法对应同一岗位。"
                salary_source = "ocr" if salary else "unreadable"
            return {"url": url, "title": str(value["title"])[:150],
                    "company": str(value["company"])[:150],
                    "salary": salary, "salary_source": salary_source,
                    "salary_note": salary_note,
                    "location": str(value.get("location") or ""),
                    "requirements": str(value.get("requirements") or ""),
                    "jd_text": str(value["jd_text"])}

    for _ in range(3):
        value = _evaluate("""(() => {
          const title=document.querySelector('.info-primary h1');
          const company=document.querySelector('.sider-company a[ka^="job-detail-company_"]:not([ka*="logo"])');
          const salary=document.querySelector('.info-primary .salary');
          const area=document.querySelector('.info-primary .text-city');
          const experience=document.querySelector('.info-primary .text-experiece');
          const degree=document.querySelector('.info-primary .text-degree');
          const detail=document.querySelector('.job-detail-section .job-sec-text');
          const body=(detail?.innerText||'').trim();
          const head=(title?.innerText||'').trim();
          const firm=(company?.innerText||company?.getAttribute('title')||'').trim();
          return JSON.stringify({url:location.href,title:head,company:firm,
            salary:(salary?.innerText||'').trim().slice(0,100),
            location:(area?.innerText||'').trim().slice(0,100),
            requirements:[experience?.innerText,degree?.innerText].filter(Boolean).join(' / ').slice(0,300),
            jd_text:body.slice(0,30000),page_text:document.body.innerText.slice(0,5000)});
        })()""")
        if (isinstance(value, dict) and len(str(value.get("jd_text") or "")) >= 30
                and value.get("title")):
            if job_key(str(value.get("url"))) != expected:
                raise BrowserObservationError("页面跳转到其他岗位，未采集。")
            company = str(value.get("company") or "").strip()
            if not company:
                raise BrowserObservationError("岗位公司名称没有出现在页面，未采集。")
            page_text = str(value.get("page_text") or "")
            if any(x in page_text[:1000] for x in ("访问被拒绝", "已被阻止", "异常访问")):
                raise BrowserObservationError("平台显示限制页面；请手动处理。")
            salary = str(value.get("salary") or "")
            salary_source = "dom" if salary and not _PRIVATE_GLYPH.search(salary) else "unreadable"
            salary_note = ""
            if _PRIVATE_GLYPH.search(salary):
                salary, salary_note = _read_visual_salary(
                    '.info-primary .salary', urlsplit(url).path, salary, "standalone")
                salary_source = "ocr" if salary else "unreadable"
            return {"url": url, "title": str(value["title"]).strip()[:150],
                    "company": company[:150], "salary": salary,
                    "salary_source": salary_source, "salary_note": salary_note,
                    "location": str(value.get("location") or ""),
                    "requirements": str(value.get("requirements") or ""),
                    "jd_text": str(value["jd_text"])}
        time.sleep(1)
    raise BrowserObservationError(f"未能读取岗位详情：{listed_title or url}")


def inspect_selected_salary() -> dict[str, str]:
    """Read the current BOSS job salary without scoring or applying."""
    page = _page()
    path = urlsplit(page["url"]).path
    if "/job_detail/" in path:
        target = page["url"]
    elif path.rstrip("/") == "/web/geek/jobs":
        selected = _evaluate("""(() => { const d=document.querySelector('.job-detail-box');
          const a=d?.querySelector('a.more-job-btn[href*="/job_detail/"]');
          return JSON.stringify({url:a?.href||''}); })()""")
        target = str(selected.get("url") or "") if isinstance(selected, dict) else ""
    else:
        raise BrowserObservationError("当前页面没有已选中的 BOSS 岗位。")
    if not _is_boss(target) or "/job_detail/" not in urlsplit(target).path:
        raise BrowserObservationError("当前页面没有可核对的岗位详情链接。")
    detail = read_job_detail(target)
    return {key: detail.get(key, "") for key in
            ("title", "company", "salary", "salary_source", "salary_note")}


def _selector_text(selector: str) -> dict[str, Any]:
    if not selector.strip():
        raise BrowserObservationError("还没有配置当前页面的准确控件选择器。")
    code = """(() => { const xs=Array.from(document.querySelectorAll(%s));
      return JSON.stringify({count:xs.length,items:xs.slice(0,5).map(x=>({text:(x.innerText||x.value||'').trim(),
        disabled:!!x.disabled||x.classList.contains('disabled')||x.getAttribute('aria-disabled')==='true',
        tag:x.tagName}))}); })()""" % json.dumps(selector, ensure_ascii=False)
    result = _evaluate(code)
    if not isinstance(result, dict):
        raise BrowserObservationError("无法读取目标控件。")
    return result


def _visible_count(selector: str, exact_text: str) -> int:
    code = """(() => { const xs=Array.from(document.querySelectorAll(%s));
      return JSON.stringify(xs.filter(x=>(x.innerText||'').trim()===%s).length); })()""" % (
          json.dumps(selector, ensure_ascii=False), json.dumps(exact_text, ensure_ascii=False))
    value = _evaluate(code)
    return int(value) if isinstance(value, int) else 0


def inspect_apply_target(url: str, button_selector: str, receipt_selector: str) -> dict[str, Any]:
    expected = job_key(url)
    selection = _select_job_detail(url)
    page = _page()
    if selection["mode"] == "standalone" and job_key(page["url"]) != expected:
        raise BrowserObservationError("投递前页面岗位不一致。")
    actual_button = (button_selector if selection["mode"] == "list_pane" else
                     'a.btn-startchat[ka^="go_chat_done_"][data-isfriend="false"]')
    button = _selector_text(actual_button)
    if button["count"] != 1 or button["items"][0]["disabled"]:
        raise BrowserObservationError("投递按钮没有唯一、可用的匹配。")
    label = button["items"][0]["text"]
    if not any(token in label for token in ("立即沟通", "立即投递", "投递简历", "申请职位")):
        raise BrowserObservationError("匹配的按钮不是可确认的投递或沟通入口。")
    if _selector_text(receipt_selector)["count"]:
        raise BrowserObservationError("投递前已显示结果标记，不能确定是否之前投递过。")
    return {"url": page["url"], "button_label": label,
            "button_selector": actual_button}


def click_apply_and_observe(url: str, button_selector: str,
                            receipt_selector: str) -> dict[str, Any]:
    """Exactly one final click; unknown result is never retried by this module."""
    before = inspect_apply_target(url, button_selector, receipt_selector)
    _command("click", {"selector": before["button_selector"]})
    # The list pane has no stable CSS attribute for its "继续沟通" state.
    # The standalone page exposes data-isfriend=true on the job's chat button.
    # Reuse the current tab after the one click; repeated jobs must not create tabs.
    _command("navigate", {"url": url, "newTab": False})
    for _ in range(6):
        page = _page()
        if job_key(page["url"]) != job_key(url):
            raise BrowserObservationError("回执页面不是刚刚操作的岗位，请手动核对平台记录。")
        receipt = _selector_text(receipt_selector)
        if receipt["count"] == 1:
            return {"before": before, "after_url": page["url"],
                    "receipt": receipt["items"][0]["text"][:300]}
        if receipt["count"] > 1:
            raise BrowserObservationError("回执匹配到多个控件，请手动核对平台记录。")
        time.sleep(1)
    raise BrowserObservationError("未观察到该岗位的唯一已沟通标记，请手动核对平台记录。")


def _application_conversation(job: dict[str, Any], message_url: str,
                              rows_selector: str) -> dict[str, Any]:
    """Find exactly one already-read conversation for this company and role."""
    navigate_boss(message_url)
    matches: list[tuple[str, dict[str, Any]]] = []
    for row in _conversation_rows(rows_selector):
        if row["company"] != job["company"] or row["unread"]:
            continue
        identity = _contact_identity(row)
        _open_conversation(identity, rows_selector)
        observed = _evaluate("""(() => JSON.stringify({
          title:(document.querySelector('.chat-position-content .position-name')?.innerText||'').trim(),
          outgoingText:document.querySelectorAll(
            '.chat-conversation .im-list > li.message-item.item-myself .message-content .text .text-content').length,
          outgoingImages:document.querySelectorAll(
            '.chat-conversation .im-list > li.message-item.item-myself .message-content img.message-image').length
        }))()""")
        if (isinstance(observed, dict) and
                str(observed.get("title") or "").casefold() == str(job["title"]).casefold()):
            matches.append((identity, observed))
    if len(matches) != 1:
        raise BrowserObservationError("未找到唯一对应公司和岗位的已读会话；已停止开场白和图片发送。")
    identity, observed = matches[0]
    opened = _open_conversation(identity, rows_selector)
    return {"identity": identity, "contact_name": opened["name"], **observed}


def _verify_application_conversation(job: dict[str, Any], contact_name: str) -> None:
    observed = _evaluate("""(() => JSON.stringify({
      title:(document.querySelector('.chat-position-content .position-name')?.innerText||'').trim(),
      header:(document.querySelector('.chat-conversation .user-info')?.innerText||'').trim()
    }))()""")
    if (not isinstance(observed, dict) or
            str(observed.get("title") or "").casefold() != str(job["title"]).casefold() or
            str(job["company"]) not in str(observed.get("header") or "") or
            contact_name not in str(observed.get("header") or "")):
        raise BrowserObservationError("发送前无法核对会话岗位和公司，已停止。")


def send_application_greeting(job: dict[str, Any], draft: str, message_url: str,
                              selectors: dict[str, str]) -> dict[str, Any]:
    context = _application_conversation(job, message_url, selectors["conversation_rows"])
    if context["outgoingText"]:
        return {"status": "EXISTING_GREETING", "detail": "会话已有发出的文字，未重复发送开场白。"}
    _verify_application_conversation(job, context["contact_name"])
    editor = _selector_text(selectors["message_input"])
    if editor["count"] != 1 or editor["items"][0]["text"]:
        raise BrowserObservationError("开场白输入框不唯一或已有草稿，未发送。")
    before = _visible_count(selectors["outgoing_rows"], draft)
    _command("fill", {"selector": selectors["message_input"], "value": draft})
    if _selector_text(selectors["message_input"])["items"][0]["text"] != draft:
        raise BrowserObservationError("开场白填写后无法核对，未发送。")
    button = _selector_text(selectors["message_send"])
    if button["count"] != 1 or button["items"][0]["disabled"]:
        raise BrowserObservationError("开场白发送按钮不可用，未发送。")
    _verify_application_conversation(job, context["contact_name"])
    _command("click", {"selector": selectors["message_send"]})
    for _ in range(8):
        if _visible_count(selectors["outgoing_rows"], draft) == before + 1:
            return {"status": "SENT_VISIBLE", "draft": draft}
        time.sleep(0.5)
    raise BrowserObservationError("开场白发送后没有看到新增的精确文字，请人工核对。")


def send_application_image(job: dict[str, Any], image_path: str, message_url: str,
                           selectors: dict[str, str]) -> dict[str, Any]:
    context = _application_conversation(job, message_url, selectors["conversation_rows"])
    if not context["outgoingText"]:
        raise BrowserObservationError("会话里还没有可见的开场白，未发送图片简历。")
    if context["outgoingImages"]:
        return {"status": "EXISTING_IMAGE", "detail": "会话已有发出的图片，未重复上传。"}
    _verify_application_conversation(job, context["contact_name"])
    file_selector = '.chat-conversation [aria-label="发送图片"] input[type="file"]'
    if _selector_text(file_selector)["count"] != 1:
        raise BrowserObservationError("BOSS 图片上传控件不唯一，未上传。")
    _command("upload", {"selector": file_selector, "files": [image_path]})
    for _ in range(16):
        observed = _evaluate("""(() => JSON.stringify(document.querySelectorAll(
          '.chat-conversation .im-list > li.message-item.item-myself .message-content img.message-image').length))()""")
        if observed == context["outgoingImages"] + 1:
            return {"status": "SENT_VISIBLE", "image": image_path.rsplit("\\", 1)[-1]}
        time.sleep(0.5)
    raise BrowserObservationError("上传图片后没有看到新增的已发送图片，请人工核对。")


def discover_visible_messages(message_url: str, selectors: dict[str, str],
                              limit: int = 60) -> list[dict[str, str]]:
    """Read unread, visible text messages from BOSS's real conversation DOM."""
    rows_css = str(selectors.get("conversation_rows") or "").strip()
    incoming_css = str(selectors.get("incoming_rows") or "").strip()
    if not rows_css or not incoming_css:
        raise BrowserObservationError("HR 会话控件选择器未配置。")
    navigate_boss(message_url)
    rows = _conversation_rows(rows_css, max(1, min(limit, 60)))
    collected: list[dict[str, str]] = []
    for row in rows:
        unread = str(row.get("unread") or "").strip()
        if not unread:
            continue
        if not unread.isdigit() or int(unread) > 20:
            raise BrowserObservationError("HR 未读数量无法准确识别，请人工查看该会话。")
        active = _open_conversation(_contact_identity(row), rows_css)
        observed = _evaluate("""(() => JSON.stringify({
          job_title:(document.querySelector('.chat-position-content .position-name')?.innerText||'').trim(),
          messages:Array.from(document.querySelectorAll(%s)).map(x=>({
            message_id:x.getAttribute('data-mid')||'',
            body:(x.querySelector('.message-content .text .text-content')?.innerText||'').trim()
          })).filter(x=>x.message_id&&x.body).slice(-30)
        }))()""" % json.dumps(incoming_css, ensure_ascii=False))
        if not isinstance(observed, dict) or not isinstance(observed.get("messages"), list):
            raise BrowserObservationError("当前 HR 会话消息结构无法读取。")
        messages = observed["messages"]
        if len(messages) < int(unread):
            raise BrowserObservationError("有未读消息不是可核实的文字，请人工查看会话。")
        newest = messages[-int(unread):]
        if not newest or str(newest[-1].get("body") or "").strip() != str(active["preview"] or "").strip():
            raise BrowserObservationError("会话预览与收到的消息不一致，已停止自动处理。")
        identity = _contact_identity(active)
        for item in newest:
            collected.append({"conversation_url": message_url,
                              "contact_identity": identity,
                              "message_id": str(item["message_id"]),
                              "sender": str(active["name"])[:150],
                              "company": str(active["company"])[:150],
                              "job_title": str(observed.get("job_title") or "")[:150],
                              "body": str(item["body"])[:10000],
                              "job_url": ""})
    return collected


def _conversation_rows(selector: str, limit: int = 60) -> list[dict[str, str]]:
    rows = _evaluate("""(() => JSON.stringify(Array.from(document.querySelectorAll(%s))
      .filter(x=>x.querySelector('.friend-content')).slice(0,%d).map(x=>{
        const avatar=x.querySelector('.figure img.image-circle')?.src||'';
        const parts=Array.from(x.querySelectorAll('.name-box span')).map(s=>s.innerText.trim());
        return {list_role:x.parentElement?.getAttribute('role')||'',
          position:Array.from(x.parentElement?.children||[]).indexOf(x)+1,
          name:(x.querySelector('.name-text')?.innerText||'').trim(),
          company:parts[1]||'',role:parts[2]||'',
          avatar:avatar?new URL(avatar).pathname:'',
          unread:(x.querySelector('.notice-badge')?.innerText||'').trim(),
          preview:(x.querySelector('.last-msg-text')?.innerText||'').trim()};
      })))()""" % (json.dumps(selector, ensure_ascii=False), limit))
    if not isinstance(rows, list):
        raise BrowserObservationError("HR 会话列表无法读取。")
    return rows


def _contact_identity(row: dict[str, Any]) -> str:
    fields = [str(row.get(key) or "").strip() for key in ("name", "company", "role", "avatar")]
    if not fields[0] or not fields[1] or not fields[3]:
        raise BrowserObservationError("会话缺少可核实的联系人、公司或头像标识。")
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()[:24]


def _open_conversation(identity: str, rows_css: str) -> dict[str, str]:
    rows = _conversation_rows(rows_css)
    matches = []
    for row in rows:
        try:
            if _contact_identity(row) == identity:
                matches.append(row)
        except BrowserObservationError:
            continue
    if len(matches) != 1:
        raise BrowserObservationError("目标 HR 会话未在可见列表中唯一出现。")
    row = matches[0]
    role, position = row.get("list_role"), int(row.get("position") or 0)
    if role not in {"group", "thead"} or position < 1:
        raise BrowserObservationError("HR 会话列表位置无法核对。")
    target = f'.user-list-content ul[role="{role}"] > li:nth-child({position}) .friend-content'
    _command("click", {"selector": target})
    for _ in range(6):
        state = _evaluate("""(() => JSON.stringify({
          header:(document.querySelector('.chat-conversation .user-info')?.innerText||'').trim(),
          has_messages:!!document.querySelector('.chat-conversation .im-list')
        }))()""")
        if (isinstance(state, dict) and state.get("has_messages")
                and row["name"] in str(state.get("header") or "")
                and row["company"].removesuffix("...") in str(state.get("header") or "")):
            return row
        time.sleep(0.5)
    raise BrowserObservationError("打开后无法核对 HR 会话联系人。")


def inspect_reply_target(message: dict[str, str], selectors: dict[str, str]) -> int:
    navigate_boss(message["conversation_url"])
    identity = str(message.get("contact_identity") or "")
    if not identity:
        raise BrowserObservationError("消息缺少稳定的会话标识。")
    _open_conversation(identity, str(selectors.get("conversation_rows") or ""))
    incoming = str(selectors.get("incoming_rows") or "")
    outgoing = str(selectors.get("outgoing_rows") or "")
    edit = str(selectors.get("message_input") or "")
    send = str(selectors.get("message_send") or "")
    if not all((incoming, outgoing, edit, send)):
        raise BrowserObservationError("HR 回复控件尚未配置完整。")
    incoming_id = str(message.get("message_id") or "")
    if not incoming_id:
        raise BrowserObservationError("缺少收到消息的稳定 ID。")
    code = """(() => JSON.stringify(Array.from(document.querySelectorAll(%s))
      .some(x=>x.getAttribute('data-mid')===%s
        && (x.querySelector('.message-content .text .text-content')?.innerText||'').trim()===%s)))()""" % (
            json.dumps(incoming, ensure_ascii=False),
            json.dumps(incoming_id, ensure_ascii=False),
            json.dumps(str(message.get("body") or ""), ensure_ascii=False))
    if _evaluate(code) is not True:
        raise BrowserObservationError("当前会话里未核对到原始 HR 消息。")
    editor = _selector_text(edit)
    button = _selector_text(send)
    if editor["count"] != 1 or editor["items"][0]["text"]:
        raise BrowserObservationError("回复输入框不唯一或已有草稿。")
    if button["count"] != 1:
        raise BrowserObservationError("发送按钮不唯一。")
    return _visible_count(outgoing, str(message.get("draft") or ""))


def _reply_context_matches(message: dict[str, str], incoming_selector: str) -> bool:
    observed = _evaluate("""(() => JSON.stringify({
      header:(document.querySelector('.chat-conversation .user-info')?.innerText||'').trim(),
      incoming:Array.from(document.querySelectorAll(%s)).some(x=>
        x.getAttribute('data-mid')===%s &&
        (x.querySelector('.message-content .text .text-content')?.innerText||'').trim()===%s)
    }))()""" % (
        json.dumps(incoming_selector, ensure_ascii=False),
        json.dumps(str(message.get("message_id") or ""), ensure_ascii=False),
        json.dumps(str(message.get("body") or ""), ensure_ascii=False),
    ))
    header = str(observed.get("header") or "") if isinstance(observed, dict) else ""
    company_hint = str(message.get("company") or "").removesuffix("...")
    return (isinstance(observed, dict) and observed.get("incoming") is True
            and str(message.get("sender") or "") in header
            and bool(company_hint) and company_hint in header)


def send_reply_and_observe(message: dict[str, str], selectors: dict[str, str],
                           before_count: int) -> dict[str, Any]:
    draft = str(message.get("draft") or "").strip()
    if not _reply_context_matches(message, selectors["incoming_rows"]):
        raise BrowserObservationError("发送前会话或原始消息已变化，未填写回复。")
    _command("fill", {"selector": selectors["message_input"], "value": draft})
    if _selector_text(selectors["message_input"])["items"][0]["text"] != draft:
        raise BrowserObservationError("回复草稿填写后无法核对，未发送。")
    button = _selector_text(selectors["message_send"])
    if button["count"] != 1 or button["items"][0]["disabled"]:
        raise BrowserObservationError("填写草稿后发送按钮仍不可用，未发送。")
    if not _reply_context_matches(message, selectors["incoming_rows"]):
        raise BrowserObservationError("发送前会话或原始消息已变化，未点击发送。")
    _command("click", {"selector": selectors["message_send"]})
    time.sleep(0.7)
    after = _visible_count(selectors["outgoing_rows"], draft)
    if after != before_count + 1:
        raise BrowserObservationError("点击后未观察到新增的精确回复；请手动核对。")
    return {"conversation_url": _page()["url"], "draft": draft, "visible_count": after}
