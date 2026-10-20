"""Parse the owned SSE journal captured by the Epsilon HTTP publisher."""
from common import add_event, digest, finish, metric, new_run, read_jsonl, usage_record
from epsilon_protocol import epsilon_usage


def parse_epsilon(path):
    from adapters import call_event, pair_calls, result_event
    path, sha, rows = read_jsonl(path)
    header = next((o for _, o in rows if o.get("type") == "epsilon.session"), {})
    sid = header.get("session_id")
    if not sid:
        raise ValueError("Epsilon 日志缺少会话身份头")
    run = new_run("epsilon", path, sha, {"session_id": sid}, sid, "run")
    run["source_format"] = "epsilon_sse"
    run["tool_usage_records"] = []
    run["tool_usage_models"] = []
    run["start_ms"] = header.get("started_ms")
    run["execution_status"] = "running"
    prompt = next(((n, o["prompt"]) for n, o in rows if o.get("type") == "agent_run_compare.prompt"), None)
    if prompt:
        ref = f"L{prompt[0]}"
        run["user_messages"].append({"text": prompt[1], "ref": ref})
        add_event(run, ref, "user", prompt[1])
    run["warnings"] = [
        "Epsilon Token 为每个 turn_completed 的 usage 求和，输入包含缓存读写；不重复累加 details",
        "工具额外 Token 单独统计 tool_exec_completed.usage，不加进主模型总 Token；只统计接口报告值，不估算外部工具账单",
        "任务和工具时间取本机单调时钟接收 SSE 的跨度，含传输/调度开销",
        "首 Token 字段为请求发出至首个公开 text_delta；服务未公开完整 LLM 请求边界，LLM 耗时留空",
    ]
    seen, first_text, fatal_error = {}, False, False
    for line, obj in rows:
        if obj.get("type") not in ("epsilon.event", "epsilon.done"):
            continue
        if obj.get("session_id") != sid:
            raise ValueError("Epsilon 日志混入其他会话")
        ref, stamp = f"L{line}", obj.get("time_ms")
        if obj["type"] == "epsilon.done":
            state = obj.get("status")
            run["execution_status"] = {"cancelled": "aborted"}.get(state, state)
            run["end_ms"] = stamp
            if isinstance(stamp, (int, float)) and isinstance(run["start_ms"], (int, float)) and stamp >= run["start_ms"]:
                run["duration"] = metric(stamp-run["start_ms"], "epsilon_sse_response_span", [ref])
            continue
        seq = obj.get("seq")
        if type(seq) is not int or seq < 1:
            raise ValueError("Epsilon SSE 缺少有效序号")
        fingerprint = digest(obj)
        if seq in seen:
            if seen[seq] != fingerprint:
                raise ValueError("Epsilon 同一 SSE 序号出现冲突")
            continue
        seen[seq] = fingerprint
        e = obj["event"];p = e["payload"];kind = e["type"]
        if kind == "run_started":
            if run.get("native_run_id") and run["native_run_id"] != p.get("run_id"):
                raise ValueError("Epsilon SSE 混入其他 run_id")
            run["native_run_id"] = p.get("run_id")
        elif kind == "text_delta" and p.get("has_text") and not first_text:
            first_text = True
            if isinstance(stamp, (int, float)) and isinstance(run["start_ms"], (int, float)) and stamp >= run["start_ms"]:
                run["first_token"] = metric(stamp-run["start_ms"], "epsilon_first_public_text_span", [ref])
        elif kind == "turn_completed":
            if not fatal_error:
                run.pop("execution_error", None)
            run["usage_records"].append(usage_record(epsilon_usage(p.get("usage", {})), ref, str(seq)))
            run["models"].extend(p.get("models", []))
            run["final_answer"], run["final_ref"] = (p.get("text", ""), ref) if not p.get("has_tool_calls") else ("", None)
        elif kind == "tool_exec_started":
            call_event(run, p.get("tool_use_id") or f"seq-{seq}", p.get("tool_name"), ref, stamp)
            run["final_answer"], run["final_ref"] = "", None
        elif kind == "tool_exec_completed":
            result_event(run, p.get("tool_use_id"), ref, p.get("output", ""), stamp, p.get("is_error"))
            if p.get("usage_reported") is True or isinstance(p.get("usage"), dict):
                record = usage_record(epsilon_usage(p.get("usage", {})), ref, str(seq))
                record.update(tool_call_id=p.get("tool_use_id"), tool_name=p.get("tool_name"))
                run["tool_usage_records"].append(record)
                usage = p.get("usage", {})
                run["tool_usage_models"].extend(
                    m for m in [usage.get("last_model")] + list((usage.get("details") or {}).keys())
                    if isinstance(m, str) and m and m != "unknown")
        elif kind == "agent_suspended":
            run["execution_status"] = "needs_input"
        elif kind == "error":
            fatal_error = fatal_error or not p.get("recoverable")
            run["execution_error"] = p.get("message", "Epsilon 返回错误")
            run["warnings"].append("Epsilon 运行错误：" + run["execution_error"])
            add_event(run, ref, "execution_error", run["execution_error"])
        elif kind == "response_cancelled":
            run["execution_status"] = "aborted"
    pair_calls(run)
    if run["final_ref"]:
        run["final_kind"] = "completed_assistant_answer" if run["execution_status"] == "completed" else "last_assistant_candidate"
        add_event(run, run["final_ref"], "final_answer", run["final_answer"])
    finish(run)
    records = run["tool_usage_records"]
    captured = (header.get("capture_capabilities") or {}).get("tool_usage") is True
    known = [r for r in records if r["usage"].get("total_tokens") is not None]
    if known:
        run["tool_extra_tokens"] = metric(
            sum(r["usage"]["total_tokens"] for r in known),
            "reported_tool_usage_sum" if captured and len(known) == len(records) else "partial_tool_usage_sum",
            [r["ref"] for r in known],
            "与主模型调用用量分开；按 SSE 序号去重，不重复累加 usage.details")
    elif records:
        run["tool_extra_tokens"] = metric(note="工具报告了不完整用量，不能当成零")
    elif captured:
        run["tool_extra_tokens"] = metric(0, "reported_no_tool_usage", [],
                                         "已开启工具 usage 采集；接口未报告额外用量，不代表外部工具实际账单为零")
    else:
        run["tool_extra_tokens"] = metric(None, "tool_usage_not_captured", [],
                                         "历史采集器未保留工具 usage；无法从主模型用量恢复")
    run["tool_extra_tokens"].update(known_records=len(known), observed_records=len(records),
                                    capture_supported=captured)
    run["tool_usage_models"] = sorted(set(run["tool_usage_models"]))
    run["tool_latency"]["method"] = "epsilon_sse_tool_span" if run["tool_latency"]["count"] else "missing"
    return [run]
