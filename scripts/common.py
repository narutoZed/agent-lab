"""Shared, dependency-free evidence and metric utilities."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def first(*values):
    return next((v for v in values if v is not None), None)


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    """Epoch milliseconds; numeric epoch seconds/milliseconds or ISO8601."""
    if isinstance(value, (int, float)):
        return float(value) if value > 1e11 else float(value) * 1000
    if isinstance(value, str):
        try:
            return timestamp(float(value))
        except ValueError:
            try:
                date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    return None  # Never silently assign a timezone.
                return date.timestamp() * 1000
            except ValueError:
                return None
    return None


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(x) for x in value if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text", "Text"))
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return ""


def redact(value):
    """Conservative known credential patterns; not a guarantee of anonymization."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.fullmatch(r"(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|secret)", k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", value)
    return value


def read_jsonl(path):
    """Read one consistent byte snapshot; fail visibly on malformed records."""
    p = Path(path).expanduser().resolve()
    hasher = hashlib.sha256()
    rows = []
    with p.open("rb") as stream:
        for line_no, raw in enumerate(stream, 1):
            hasher.update(raw)
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"{p}:{line_no}: 无效/尚未写完的 JSONL；请导出完整快照后重试") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{p}:{line_no}: 记录必须是 JSON 对象")
            rows.append((line_no, obj))
    return p, hasher.hexdigest(), rows


TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")


def usage_record(usage, ref, request_id=None):
    out = {k: number(usage.get(k)) for k in TOKEN_KEYS}
    if out["total_tokens"] is None and out["input_tokens"] is not None and out["output_tokens"] is not None:
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return {"usage": out, "ref": ref, "request_id": request_id}


def metric(value=None, method="missing", refs=None, note=""):
    return {"value": value, "method": method, "evidence": refs or [], "note": note}


def distribution(values, method, expected=None):
    known = sorted(v for v in values if number(v) is not None)
    def percentile(q):
        if not known:
            return None
        pos = (len(known) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(known) - 1)
        return known[lo] + (known[hi] - known[lo]) * (pos - lo)
    return {"count": len(known), "expected": expected, "sum_ms": sum(known) if known else None,
            "mean_ms": sum(known) / len(known) if known else None,
            "p50_ms": percentile(.5), "p95_ms": percentile(.95), "method": method if known else "missing"}


def new_run(platform, path, sha, selector, session, scope="turn"):
    return {"platform": platform, "source": str(path), "source_sha256": sha, "selector": selector,
            "session_id": session, "scope": scope, "models": [], "user_messages": [], "final_answer": "",
            "final_ref": None, "final_kind": "missing", "execution_status": "unknown", "calls": [],
            "usage_records": [], "warnings": [], "start_ms": None, "end_ms": None,
            "duration": metric(), "first_token": metric(), "llm_latencies": [], "llm_latency_method": "missing",
            "assistant_output_spans_ms": [], "events": [], "source_quality_label": None}


def add_event(run, ref, kind, text="", **extra):
    event = {"ref": ref, "kind": kind, "text": redact(text), **extra}
    run["events"].append(event)


def finish(run):
    records = run["usage_records"]
    tokens = {}
    for key in TOKEN_KEYS:
        vals = [r["usage"].get(key) for r in records]
        known = [v for v in vals if v is not None]
        # A partial sum is displayed explicitly, never silently called a full total.
        tokens[key] = metric(sum(known) if known else None,
                             "recorded_sum" if known and len(known) == len(vals) else "partial_sum" if known else "missing",
                             [r["ref"] for r in records if r["usage"].get(key) is not None])
        tokens[key]["known_records"] = len(known)
        tokens[key]["observed_records"] = len(records)
    run["tokens"] = tokens
    run["usage_record_count"] = len(records)
    run["usage_completeness"] = "日志记录覆盖，未证明包含所有请求/子代理"
    run["tool_call_count"] = len(run["calls"])
    run["tool_results_count"] = sum(c.get("result_ref") is not None for c in run["calls"])
    run["tool_errors"] = sum(c.get("is_error") is True or c.get("error_observed") is True for c in run["calls"])
    known_errors = sum(type(c.get("is_error")) is bool or c.get("error_observed") is True for c in run["calls"])
    run["tool_error_count"] = metric(
        run["tool_errors"] if known_errors or not run["calls"] else None,
        "explicit_tool_errors" if known_errors == len(run["calls"]) else "partial_tool_errors" if known_errors else "missing",
        [ref for c in run["calls"] for ref in (c.get("error_refs") or
         ([c["result_ref"]] if c.get("result_ref") and c.get("is_error") is True else []))])
    run["tool_error_count"].update(known_records=known_errors, observed_records=len(run["calls"]))
    incoming, cached = tokens["input_tokens"], tokens["cached_input_tokens"]
    complete_input = incoming["known_records"] == incoming["observed_records"] and incoming["observed_records"] > 0
    rate = cached["value"] / incoming["value"] * 100 if (complete_input and incoming["value"]
           and cached["value"] is not None and 0 <= cached["value"] <= incoming["value"]) else None
    complete_cache = cached["known_records"] == cached["observed_records"]
    run["cache_rate"] = metric(rate, ("cached_input_ratio" if complete_cache else "cached_input_ratio_lower_bound")
                               if rate is not None else "missing",
                               cached["evidence"] + incoming["evidence"],
                               "缓存读取 Token / 含缓存读写的输入 Token × 100%；缓存记录不全时仅为下界")
    run["tool_latency"] = distribution([c.get("duration_ms") for c in run["calls"]], "call_to_result_event_span", len(run["calls"]))
    run["llm_latency"] = distribution(run["llm_latencies"], run["llm_latency_method"], len(records) or None)
    run["assistant_output_span"] = distribution(run["assistant_output_spans_ms"], "output_item_span_not_request_latency")
    marked = [m for m in run["user_messages"] if re.match(r"^\[评测任务编号:\s*arc-[0-9a-f]+\]", m["text"].strip())]
    selected = marked[0] if len(marked) == 1 else run["user_messages"][0] if run["user_messages"] else None
    run["prompt"] = selected["text"] if selected else ""
    if selected and run["user_messages"] and selected is not run["user_messages"][0]:
        run["warnings"].append("前置运行时注入的 user 消息不作为评测题目；业务提示词按唯一评测编号消息识别，Token仍包含运行时上下文")
    run["prompt_hash"] = digest(re.sub(r"\s+", " ", run["prompt"]).strip()) if run["prompt"] else None
    run["models"] = sorted(set(run["models"]))
    run["warnings"] = list(dict.fromkeys(run["warnings"]))
    run["fingerprint"] = digest({k: run[k] for k in ("source_sha256", "selector", "platform", "scope", "models",
                                                     "user_messages", "final_answer", "final_ref", "events", "calls",
                                                     "usage_records", "duration", "first_token")})
    return run
