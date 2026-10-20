"""Public Mesh event allowlist and provider-reported token normalization."""
from __future__ import annotations

from common import first, number, redact, text_content
from mesh_timing import first_token_time

TERMINAL_EVENTS = {"mesh/run-completed": "completed", "mesh/run-failed": "failed", "mesh/run-cancelled": "cancelled"}
USAGE_FIELDS = ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens",
                "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def mesh_usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    get = lambda camel, snake: number(first(raw.get(camel), raw.get(snake)))
    uncached = get("inputTokens", "input_tokens")
    output = get("outputTokens", "output_tokens")
    total = get("totalTokens", "total_tokens")
    cached = get("cacheReadTokens", "cache_read_tokens")
    written = get("cacheWriteTokens", "cache_write_tokens")
    # Mesh's inputTokens is uncached input. The comparison input includes cached input.
    # Prefer the reported total minus output when optional cache fields are absent.
    inclusive = total - output if total is not None and output is not None and total >= output else None
    if inclusive is None and all(v is not None for v in (uncached, cached, written)):
        inclusive = uncached + cached + written
    return {"input_tokens": inclusive, "output_tokens": output, "total_tokens": total,
            "cached_input_tokens": cached, "cache_write_input_tokens": written,
            "reasoning_output_tokens": get("reasoningTokens", "reasoning_tokens")}


def sanitize_event(event):
    """Exclude reasoning, system instructions, request bodies, credentials and stream chunks."""
    if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] < 0:
        raise ValueError("Mesh 事件缺少有效 seq")
    if type(event.get("session_generation")) is not int or event["session_generation"] < 0:
        raise ValueError("Mesh 事件缺少有效 session_generation")
    if number(event.get("time_ms")) is None:
        raise ValueError("Mesh 事件缺少有效 time_ms")
    kind, data = event.get("type"), event.get("data")
    if not isinstance(kind, str) or not isinstance(data, dict):
        raise ValueError("Mesh 事件格式无效")
    safe = {k: data[k] for k in ("run_id", "request_id", "turn", "step") if isinstance(data.get(k), (str, int))}
    if kind in TERMINAL_EVENTS or kind == "mesh/run-started":
        safe.update({k: redact(data[k]) for k in ("reason", "error_type", "message") if isinstance(data.get(k), str)})
    elif kind in ("request/context", "request/header"):
        model = data.get("model") if kind == "request/context" else (data.get("header") or {}).get("config", {}).get("model")
        safe["model"] = model if isinstance(model, str) else None
    elif kind in ("user/message", "assistant/message"):
        message = data.get("message", {})
        if not isinstance(message, dict):
            message = {}
        source = message.get("source", {})
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        safe.update(text=redact(text_content(message.get("content"))),
                    source_kind=source.get("kind", "user") if isinstance(source, dict) else "user",
                    message_id=message.get("id") if isinstance(message.get("id"), str) else None,
                    has_tool_calls=any(isinstance(b, dict) and b.get("type") in ("tool-call", "tool_use") for b in blocks))
        if kind == "assistant/message":
            raw = data.get("usage")
            safe["usage"] = {k: raw[k] for k in USAGE_FIELDS if number(raw.get(k)) is not None} if isinstance(raw, dict) else {}
            safe["first_token_at_ms"] = first_token_time(data.get("stream"))
    elif kind == "assistant/attempt":
        safe["first_token_at_ms"] = first_token_time(data.get("stream"))
    elif kind == "tool/call":
        safe.update(call_id=first(data.get("callId"), data.get("call_id")), name=data.get("name", "unknown"))
    elif kind == "tool/result":
        message = data.get("message") or {}
        source = message.get("source") or {}
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        safe.update(call_id=first(data.get("callId"), data.get("call_id"), message.get("toolCallId"), source.get("callId")),
                    output=redact(text_content(blocks)),
                    is_error=bool(data.get("error")) or any(x is True for x in (data.get("isError"), data.get("is_error"), message.get("isError"), message.get("is_error")))
                    or any(isinstance(b, dict) and (b.get("isError") is True or b.get("is_error") is True) for b in blocks))
    elif kind in ("step/start", "step/end", "turn/start", "turn/end", "llm/retry"):
        if isinstance(data.get("reason"), dict):
            safe["reason_kind"] = data["reason"].get("kind")
    elif kind in ("mesh/interaction-requested", "mesh/interaction-resolved"):
        safe.update({k: data[k] for k in ("interaction_id", "kind", "type") if isinstance(data.get(k), str)})
    else:
        return None
    return {"schema_version": 1, "session_generation": event["session_generation"], "seq": event["seq"],
            "time_ms": event["time_ms"], "type": kind, "data": safe}
