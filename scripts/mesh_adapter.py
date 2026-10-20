"""Normalize downloaded Mesh session-log events into the common comparison schema."""
from __future__ import annotations

from common import add_event, digest, finish, metric, new_run, read_jsonl, usage_record
from mesh_protocol import TERMINAL_EVENTS, mesh_usage
from mesh_timing import MeshTiming


def parse_mesh(path):
    from adapters import call_event, pair_calls, result_event

    path, sha, rows = read_jsonl(path)
    metadata = next((o for _, o in rows if o.get("type") == "mesh.session"), {})
    session = metadata.get("session_id")
    if not session:
        raise ValueError("Mesh 日志缺少明确的 mesh.session 身份头")
    runs, seen, current = {}, {}, None
    timings, metrics_rows = {}, []
    client_prompt = next(((f"L{n}", o.get("prompt", "")) for n, o in rows if o.get("type") == "agent_run_compare.prompt"), None)

    def ensure(run_id):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Mesh 日志缺少明确 run_id")
        if run_id not in runs:
            run = new_run("mesh", path, sha, {"run_id": run_id}, session, "run")
            run["source_format"] = "mesh_api_events"
            run["warnings"].extend([
                "Token 来自主会话 assistant/message 的已报告 usage；未自动并入子代理或失败请求用量",
                "Mesh inputTokens 是未缓存输入；已按报告总量减输出，或显式缓存读写字段，换算为包含缓存的输入",
                "LLM 计时沿用 Mesh sessionStats 的步骤口径（step/start 至 assistant/message，可能含重试）；不等同于单次 HTTP 请求计时",
                "首 Token 包含文本、思考或工具调用的首次流事件；仅保留时间戳，不保存思考内容",
            ])
            runs[run_id] = run
            timings[run_id] = MeshTiming()
        return runs[run_id]

    if metadata.get("run_id"):
        current = ensure(metadata["run_id"])
    for line, obj in rows:
        if obj.get("type") == "mesh.metrics":
            if obj.get("session_id") != session:
                raise ValueError("Mesh metrics 混入了其他会话")
            metrics_rows.append((f"L{line}", obj))
            continue
        if obj.get("type") != "mesh.event":
            continue
        if obj.get("session_id") != session:
            raise ValueError("同一 Mesh 日志混入了其他会话")
        event = obj.get("event")
        if not isinstance(event, dict) or event.get("session_generation") != metadata.get("generation", 0):
            raise ValueError("Mesh 事件代次与身份头不一致")
        seq = event.get("seq")
        if type(seq) is not int or seq < 0:
            raise ValueError("Mesh 事件 seq 无效")
        key = (event["session_generation"], seq)
        fingerprint = digest(event)
        if key in seen:
            if seen[key] != fingerprint:
                raise ValueError("同一 Mesh 事件序号对应冲突内容")
            continue
        seen[key] = fingerprint
        kind, data, ts = event.get("type"), event.get("data") or {}, event.get("time_ms")
        if kind == "mesh/run-started":
            current = ensure(data.get("run_id"))
            current["start_ms"] = ts
            current["execution_status"] = "running"
        if current is None:
            continue
        if data.get("run_id") and data["run_id"] != current["selector"]["run_id"]:
            raise ValueError("Mesh 事件 run_id 与当前运行不一致")
        ref = f"L{line}:seq={seq}"
        timings[current["selector"]["run_id"]].apply(kind, data, ts, ref)
        if kind in ("request/context", "request/header") and isinstance(data.get("model"), str):
            current["models"].append(data["model"])
        elif kind == "user/message" and data.get("source_kind", "user") == "user":
            text = data.get("text", "")
            if text and not any(m["text"] == text for m in current["user_messages"]):
                current["user_messages"].append({"text": text, "ref": ref})
                add_event(current, ref, "user", text)
        elif kind == "assistant/message":
            current["usage_records"].append(usage_record(mesh_usage(data.get("usage")), ref, str(seq)))
            if data.get("text") and not data.get("has_tool_calls"):
                current["final_answer"], current["final_ref"] = data["text"], ref
                current["final_kind"] = "last_assistant_candidate"
            elif data.get("has_tool_calls"):
                current["final_answer"], current["final_ref"] = "", None
        elif kind == "tool/call":
            call_event(current, str(data.get("call_id") or f"seq-{seq}"), data.get("name"), ref, ts)
            current["final_answer"], current["final_ref"] = "", None
        elif kind == "tool/result":
            result_event(current, str(data.get("call_id")), ref, data.get("output", ""), ts, data.get("is_error"))
        elif kind == "mesh/interaction-requested":
            current["execution_status"] = "needs_input"
            current["warnings"].append("Mesh 请求用户交互；在原 Mesh 客户端处理，不自动代答或扩大全局权限")
        elif kind == "mesh/interaction-resolved":
            current["execution_status"] = "running"
        elif kind in TERMINAL_EVENTS:
            current["execution_status"] = "aborted" if TERMINAL_EVENTS[kind] == "cancelled" else TERMINAL_EVENTS[kind]
            current["end_ms"] = ts
            if isinstance(ts, (int, float)) and current["start_ms"] is not None and ts >= current["start_ms"]:
                current["duration"] = metric(ts-current["start_ms"], "run_event_span", [ref])
            if current["execution_status"] == "completed" and current["final_ref"]:
                current["final_kind"] = "completed_assistant_answer"
    for run in runs.values():
        if not run["user_messages"] and client_prompt:
            ref, text = client_prompt
            run["user_messages"].append({"ref": ref, "text": text})
            add_event(run, ref, "user", text)
            run["warnings"].append("原生日志尚无用户正文；当前提示词来自发布器提交记录")
        pair_calls(run)
        if run["final_ref"]:
            add_event(run, run["final_ref"], "final_answer", run["final_answer"])
        timing = timings[run["selector"]["run_id"]]
        timing.prepare_run(run)
        finish(run)
        for ref, envelope in metrics_rows:
            timing.apply_metrics(run, envelope, metadata, len(runs),
                                 max((key[1] for key in seen), default=-1), ref)
    if not runs:
        raise ValueError("Mesh 尚无可定位的运行记录")
    return list(runs.values())
