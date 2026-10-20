"""Public Epsilon SSE evidence; hidden message blocks and tool inputs are excluded."""
from common import number, redact, text_content

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def safe_usage(raw):
    """Keep only reported numeric usage; details are a breakdown, not another total."""
    if not isinstance(raw, dict):
        return {}
    result = {k: raw[k] for k in USAGE_FIELDS if number(raw.get(k)) is not None}
    if isinstance(raw.get("last_model"), str):
        result["last_model"] = raw["last_model"]
    details = raw.get("details")
    if isinstance(details, dict):
        result["details"] = {
            model: {k: item[k] for k in USAGE_FIELDS if number(item.get(k)) is not None}
            for model, item in details.items() if isinstance(model, str) and isinstance(item, dict)}
    return result


def sanitize_event(event):
    if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
        raise ValueError("Epsilon SSE 事件格式无效")
    kind, p = event.get("type"), event["payload"]
    safe = {}
    if kind in ("run_started", "response_cancelled"):
        safe = {k: p[k] for k in ("run_id", "reason") if isinstance(p.get(k), str)}
    elif kind == "text_delta":
        safe = {"has_text": bool(p.get("text"))}
    elif kind == "turn_completed":
        message = p.get("message") or {}
        blocks = message.get("content", []) if isinstance(message, dict) else []
        safe = {"text": redact(p.get("text") or text_content(blocks)),
                "has_tool_calls": any(isinstance(b, dict) and b.get("type") in
                                      ("tool_use", "tool-call", "tool_call") for b in blocks)}
        usage = p.get("usage") or {}
        safe["usage"] = safe_usage(usage)
        safe["models"] = [m for m in dict.fromkeys([usage.get("last_model")] + list((usage.get("details") or {}).keys()))
                          if isinstance(m, str) and m and m != "unknown"]
    elif kind in ("tool_exec_started", "tool_exec_completed"):
        safe = {k: p[k] for k in ("tool_name", "tool_use_id") if isinstance(p.get(k), str)}
        if kind == "tool_exec_completed":
            safe["output"] = redact(text_content(p.get("output")))
            safe["is_error"] = p.get("is_error") if type(p.get("is_error")) is bool else None
            safe["usage_reported"] = p.get("usage") is not None
            if safe["usage_reported"]:
                safe["usage"] = safe_usage(p["usage"])
    elif kind == "error":
        safe = {k: redact(p[k]) for k in ("message", "code") if isinstance(p.get(k), str)}
        safe["recoverable"] = p.get("recoverable") is True
    elif kind == "agent_suspended":
        safe = {k: p[k] for k in ("reason", "resume_token") if isinstance(p.get(k), str)}
    else:
        return None
    return {"type": kind, "payload": safe}


def epsilon_usage(raw):
    # UsageSnapshot.input_tokens excludes both cache buckets (verified in Epsilon source).
    values = [number(raw.get(k)) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")]
    inclusive = sum(values) if all(v is not None for v in values) else None
    output = number(raw.get("output_tokens"))
    return {"input_tokens": inclusive, "output_tokens": output,
            "total_tokens": inclusive + output if inclusive is not None and output is not None else None,
            "cached_input_tokens": values[1], "cache_write_input_tokens": values[2]}
