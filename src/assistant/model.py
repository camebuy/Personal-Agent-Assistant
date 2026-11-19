"""Bounded OpenAI-compatible chat completion transport; no implicit retries."""
from __future__ import annotations

import json
import os
from urllib import error, request
from urllib.parse import urlsplit

from config.settings import settings
from src.core.system.runtime_settings import load_runtime_settings


class ModelError(RuntimeError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError("模型服务重定向已拒绝，请配置最终 API 地址。")


def complete(messages: list[dict], tools: list[dict], limits: dict) -> tuple[dict, dict]:
    config = load_runtime_settings(strict=True)
    key = os.getenv("MODEL_API_KEY", "").strip() or settings.MODEL_API_KEY
    if not key:
        raise ModelError("请在本机 .env 配置 MODEL_API_KEY，然后重启应用。")
    base = config["api_base"].rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
        raise ModelError("API 地址必须使用 HTTPS；本机模型可使用 localhost HTTP。")
    body = {"model": config["model_name"], "messages": messages,
            "max_tokens": limits["max_output_tokens"], "temperature": 0.2}
    if tools:
        body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
    req = request.Request(base + "/chat/completions", data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=limits["timeout_seconds"]) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ModelError("模型响应超过 2 MiB 限制。")
            data = json.loads(raw)
    except error.HTTPError as exc:
        raise ModelError(f"模型服务返回 HTTP {exc.code}；请检查接口、配额及工具调用支持。") from exc
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ModelError("模型请求失败或响应格式异常，请检查网络及服务配置。") from exc
    try:
        if data["choices"][0].get("finish_reason") == "length":
            raise ModelError("模型输出达到长度上限，未将截断内容视为完成。请缩小任务或提高输出限制。")
        message = data["choices"][0]["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("invalid message")
        result = {"role": "assistant", "content": message.get("content") or ""}
        if not isinstance(result["content"], str):
            raise ValueError("invalid content")
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        usage = data.get("usage") or {}
        counts = {}
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            counts[key] = value if type(value) is int and value >= 0 else None
        return result, counts
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ModelError("模型响应缺少有效消息。") from exc
