"""Read only adapters for Codex rollouts, WorkBuddy JSONL and Jahead exports.

Log instructions, tool arguments and outputs are evidence, never executable input.
"""
from __future__ import annotations

import re

from common import (add_event, canonical, digest, finish, first, metric, new_run,
                    number, read_jsonl, redact, text_content, timestamp, usage_record)


def pair_calls(run):
    for call in run["calls"]:
        if call.get("start_ms") is not None and call.get("end_ms") is not None:
            delta = call["end_ms"] - call["start_ms"]
            if delta >= 0:
                call["duration_ms"] = delta
            else:
                run["warnings"].append("工具结果时间早于调用；该耗时留空")
        if not call.get("result_ref"):
            run["warnings"].append("存在没有匹配结果的工具调用；工具耗时覆盖不完整")


def call_event(run, call_id, name, ref, start=None):
    for call in run["calls"]:
        if call["id"] == call_id:
            return call
    call = {"id": call_id, "name": name or "unknown", "ref": ref, "result_ref": None,
            "start_ms": start, "end_ms": None, "duration_ms": None, "is_error": None}
    run["calls"].append(call)
    add_event(run, ref, "tool_call", name or "unknown", call_id=call_id)
    return call


def result_event(run, call_id, ref, output, end=None, error=None):
    call = next((c for c in run["calls"] if c["id"] == call_id), None)
    if call is None:
        run["warnings"].append("存在无对应调用的工具结果；未把结果重复计为调用")
        return
    if call["result_ref"] is not None:
        run["warnings"].append("同一工具 ID 出现多条结果；保留最后一次结果时间")
    call.update(result_ref=ref, end_ms=end, is_error=error,
                error_observed=call.get("error_observed") is True or error is True)
    if error is True and ref not in call.setdefault("error_refs", []):
        call["error_refs"].append(ref)
    # Excerpts are explicitly marked; full original evidence remains in the source.
    rendered = redact(output if isinstance(output, str) else canonical(output))
    add_event(run, ref, "tool_result", rendered[:2000], call_id=call_id,
              excerpt=len(rendered) > 2000, original_chars=len(rendered), is_error=error)


def codex_result_error(run, payload):
    for key in ("is_error", "isError"):
        if type(payload.get(key)) is bool:
            return payload[key]
    output = payload.get("output")
    if isinstance(output, dict):
        for key in ("is_error", "isError"):
            if type(output.get(key)) is bool:
                return output[key]
    call = next((c for c in run["calls"] if c["id"] == str(payload.get("call_id"))), {})
    # Only recognize the command tool's own wrapper; words like "error" in content are not flags.
    if isinstance(output, str) and call.get("name", "").split(".")[-1] in ("exec_command", "write_stdin"):
        match = re.search(r"^Process exited with code (-?\d+)\s*$", output, re.MULTILINE)
        if match and re.search(r"^Chunk ID:", output, re.MULTILINE):
            return int(match[1]) != 0
    return None


def parse_jahead(path):
    path, sha, rows = read_jsonl(path)
    if any(o.get("type") == "jahead.desktop.snapshot" or o.get("transport") == "jahead_desktop" for _, o in rows):
        return parse_jahead_desktop_rows(path, sha, rows)
    runs = []
    ids = set()
    for line, obj in rows:
        if not isinstance(obj.get("messages"), list) or "metadata" not in obj:
            raise ValueError(f"{path}:{line}: 不符合即先 reporting_session_trajectory 格式")
        rid = str(obj.get("id") or f"line-{line}")
        if rid in ids:
            raise ValueError(f"{path}:{line}: 重复即先记录 id {rid}，请先处理导出重复")
        ids.add(rid)
        meta = obj.get("metadata") or {}
        run = new_run("jahead", path, sha, {"record_id": rid}, meta.get("session_id", rid), "session")
        run["source_quality_label"] = meta.get("quality")
        run["source_task_ids"] = meta.get("task_ids", [])
        run["warnings"].extend(["此即先轨迹格式未提供 Token、时间戳、模型请求耗时；指标留空",
                                "metadata.quality 是来源标签，不代表任务验收结论"])
        last_assistant = None
        last_action = None
        for idx, msg in enumerate(obj["messages"], 1):
            ref = f"L{line}:messages[{idx-1}]"
            role = msg.get("role")
            if role == "user":
                txt = redact(text_content(msg.get("content")))
                run["user_messages"].append({"text": txt, "ref": ref})
                add_event(run, ref, "user", txt)
                last_action = "user"
            elif role == "assistant":
                txt = redact(text_content(msg.get("content")))
                tools = msg.get("tool_calls") or []
                if not tools:
                    last_assistant = (txt, ref) if txt else None
                for tool_idx, tool in enumerate(tools):
                    fn = tool.get("function") or {}
                    cid = str(tool.get("id") or f"{ref}:tool[{tool_idx}]")
                    call_event(run, cid, fn.get("name"), f"{ref}:tool_calls[{tool_idx}]")
                last_action = "tool_call" if tools else "assistant"
            elif role == "tool":
                result_event(run, str(msg.get("tool_call_id")), ref, msg.get("content", ""), error=msg.get("is_error"))
                last_action = "tool_result"
        if last_action == "assistant" and last_assistant:
            run["final_answer"], run["final_ref"] = last_assistant
            run["final_kind"] = "last_assistant_candidate"
            add_event(run, run["final_ref"], "final_answer", run["final_answer"])
        else:
            run["warnings"].append("轨迹末尾没有无工具调用的 assistant 回答；最终回答缺失")
        if len(run["user_messages"]) > 1:
            run["warnings"].append("包含多条用户消息；本条按整段会话统计，配对时核对人工干预和任务边界")
        claimed = (obj.get("stats") or {}).get("tool_calls")
        if claimed is not None and claimed != len(run["calls"]):
            run["warnings"].append(f"来源 stats.tool_calls={claimed} 与按调用 ID 重算值 {len(run['calls'])} 不同")
        run["reported_stats"] = obj.get("stats", {})
        pair_calls(run)
        runs.append(finish(run))
    return runs


def parse_jahead_desktop_rows(path, sha, rows):
    """CLI snapshots are cumulative; parse each session's latest snapshot once."""
    latest, receipts, terminal_observations = {}, {}, {}
    for line, obj in rows:
        sid = obj.get("session_id")
        if sid and obj.get("status") in ("completed", "failed", "cancelled") and timestamp(obj.get("observed_at")) is not None:
            terminal_observations.setdefault(sid, (timestamp(obj["observed_at"]), line))
        if obj.get("transport") == "jahead_desktop" and sid:
            receipts[sid] = obj
        if sid and isinstance(obj.get("events"), list) and isinstance(obj.get("messages"), list):
            latest[sid] = (line, obj)
    runs = []
    for sid, (line, obj) in latest.items():
        run = new_run("jahead", path, sha, {"session_id": sid}, sid, "session")
        run["source_format"] = "jahead_desktop_snapshot"
        run["module"] = obj.get("module", "report")
        if run["module"] == "developer":
            run["scope"] = "record"
            if (obj.get("record") or {}).get("model"):
                run["models"].append(obj["record"]["model"])
        run["execution_status"] = {"cancelled": "aborted"}.get(obj.get("status"), obj.get("status", "unknown"))
        for i, msg in enumerate(obj["messages"]):
            if msg.get("role") == "user":
                ref = f"L{line}:messages[{i}]"
                txt = redact(text_content(msg.get("content")))
                run["user_messages"].append({"text": txt, "ref": ref})
                add_event(run, ref, "user", txt)
        for i, event in enumerate(obj["events"]):
            if event.get("type") == "tool_exec_started":
                call_event(run, f"event-{i}", event.get("tool_name"), f"L{line}:events[{i}]", timestamp(event.get("at")))
            elif event.get("type") == "tool_exec_completed":
                # Source lacks call IDs; do not invent latency or pair overlapping same-name calls.
                add_event(run, f"L{line}:events[{i}]", "tool_result", event.get("tool_name", "unknown"))
        run["final_answer"] = redact(obj.get("final_answer") or obj.get("answer_candidate") or "")
        if run["final_answer"]:
            run["final_ref"] = f"L{line}:final_answer" if obj.get("final_answer") else f"L{line}:answer_candidate"
            run["final_kind"] = "completed_assistant_answer" if obj.get("status") == "completed" else "last_assistant_candidate"
            add_event(run, run["final_ref"], "final_answer", run["final_answer"])
        started = timestamp(receipts.get(sid, {}).get("created_at"))
        ended, end_line = terminal_observations.get(sid, (timestamp(obj.get("observed_at")), line))
        if obj.get("status") in ("completed", "failed", "cancelled") and started is not None and ended is not None and ended >= started:
            run["duration"] = metric(ended-started, "client_observed_submission_span", [f"L{end_line}"])
        run["billed_token_count"] = metric(obj.get("billed_token_count"), "reported_billing_tokens", [f"L{line}"])
        run["warnings"].extend(["即先计费 Token 仅保留为原始证据，不作为横向指标展示或混入模型 Token", "工具数量为已观察到的事件；没有调用 ID，不推算工具/模型延迟", "即先报告使用云端环境，本地 workspace 不自动同步"])
        run["warnings"].extend(obj.get("execution_warnings", []))
        finish(run)
        run["tool_results_count"] = sum(e.get("type") == "tool_exec_completed" for e in obj["events"])
        if not run["calls"] and not run["tool_results_count"]:
            run["tool_call_count"] = None
            run["tool_results_count"] = None
        run["tool_errors"] = sum(e.get("type") == "tool_exec_completed" and e.get("is_error") is True for e in obj["events"])
        results = [e for e in obj["events"] if e.get("type") == "tool_exec_completed"]
        known = sum(type(e.get("is_error")) is bool for e in results)
        run["tool_error_count"] = metric(run["tool_errors"] if known else None,
                                          "error_result_events" if known == len(results) and known else "partial_error_result_events" if known else "missing",
                                          [f"L{line}:events[{i}]" for i, e in enumerate(obj["events"])
                                           if e.get("type") == "tool_exec_completed" and e.get("is_error") is True],
                                          "来源没有调用 ID；按错误结果事件统计，不推断配对")
        run["tool_error_count"].update(known_records=known, observed_records=len(results))
        runs.append(run)
    if not runs:
        raise ValueError("即先已收到回执，等待首个会话快照")
    return runs


def wb_usage(pd):
    raw, usage = pd.get("rawUsage") or {}, pd.get("usage") or {}
    details = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    return {"input_tokens": first(raw.get("prompt_tokens"), usage.get("inputTokens")),
            "output_tokens": first(raw.get("completion_tokens"), usage.get("outputTokens")),
            "total_tokens": first(raw.get("total_tokens"), usage.get("totalTokens")),
            "cached_input_tokens": first(raw.get("prompt_cache_hit_tokens"), raw.get("cache_read_input_tokens"), details.get("cached_tokens")),
            "cache_write_input_tokens": first(raw.get("prompt_cache_write_tokens"), raw.get("cache_creation_input_tokens"), details.get("cached_creation_tokens"), details.get("cache_write_tokens")),
            "reasoning_output_tokens": first(raw.get("completion_thinking_tokens"), completion.get("reasoning_tokens"))}


def workbuddy_user_text(content):
    text = text_content(content)
    queries = re.findall(r"<user_query>([\s\S]*?)</user_query>", text)
    if queries:
        return "\n".join(queries).strip()
    return re.sub(r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>", "", text).strip()


def parse_workbuddy(path):
    path, sha, rows = read_jsonl(path)
    runs, mapping, seen, usage_seen = [], {}, set(), set()
    current = None
    session = next((o.get("sessionId") for _, o in rows if o.get("sessionId")), path.stem)
    for line, obj in rows:
        ref, kind = f"L{line}", obj.get("type")
        pd = obj.get("providerData") or {}
        cid = pd.get("conversationRequestId")
        ts = timestamp(obj.get("timestamp"))
        is_user = obj.get("role") == "user" and pd.get("startsNewUserRequest", True)
        event_key = (kind, obj.get("id"), obj.get("status")) if obj.get("id") else None
        if event_key and event_key in seen:
            continue
        if event_key:
            seen.add(event_key)
        if is_user:
            selector = {"request_id": str(cid or obj.get("id") or ref)}
            current = new_run("workbuddy", path, sha, selector, session)
            current["start_ms"] = ts
            current["warnings"].extend(["耗时为用户事件至最后业务事件的观测跨度，不是服务端任务精确耗时",
                                        "未自动并入子代理文件；若任务使用子代理，请显式添加对应日志并核对边界",
                                        "未记录 LLM 请求开始时间；不以总耗时减工具耗时冒充 LLM 耗时"])
            runs.append(current)
            if cid:
                mapping[cid] = current
        run = mapping.get(cid, current)
        if run is None:
            continue
        if obj.get("role") == "user":
            txt = redact(workbuddy_user_text(obj.get("content")))
            run["user_messages"].append({"text": txt, "ref": ref})
            add_event(run, ref, "user", txt)
        if kind in ("message", "function_call", "function_call_result") and ts is not None:
            run["end_ms"] = max(ts, run["end_ms"] or ts)
        model = first(pd.get("model"), pd.get("requestModelId"), pd.get("requestModelName"))
        if model:
            run["models"].append(str(model))
        u = wb_usage(pd)
        if any(number(v) is not None for v in u.values()):
            uid = str(first(pd.get("messageId"), obj.get("id"), f"{cid}:{pd.get('stepSeq')}:{ref}"))
            if uid not in usage_seen:
                usage_seen.add(uid)
                run["usage_records"].append(usage_record(u, ref, uid))
        if kind == "function_call":
            call_event(run, str(obj.get("callId") or obj.get("id") or ref), obj.get("name"), ref, ts)
            run["final_answer"], run["final_ref"], run["final_kind"] = "", None, "missing"
        elif kind == "function_call_result":
            error = obj.get("status") in ("error", "failed", "rejected")
            result_event(run, str(obj.get("callId")), ref, obj.get("output", ""), ts, error)
        elif obj.get("role") == "assistant" and obj.get("status") == "completed":
            txt = redact(text_content(obj.get("content")))
            if txt:
                run["final_answer"], run["final_ref"] = txt, ref
                run["final_kind"] = "last_assistant_candidate"
    for run in runs:
        pair_calls(run)
        if run["start_ms"] is not None and run["end_ms"] is not None and run["end_ms"] >= run["start_ms"]:
            run["duration"] = metric(run["end_ms"] - run["start_ms"], "observed_event_span", [run["user_messages"][0]["ref"]])
        if run["final_ref"]:
            add_event(run, run["final_ref"], "final_answer", run["final_answer"])
            run["execution_status"] = "answer_observed"
        finish(run)
    return runs


def parse_codex(path):
    path, sha, rows = read_jsonl(path)
    if any(o.get("type") in ("thread.started", "agent_run_compare.prompt") for _, o in rows):
        return parse_codex_exec_rows(path, sha, rows)
    session = next((o.get("payload", {}).get("id") for _, o in rows if o.get("type") == "session_meta"), path.stem)
    runs, mapping = [], {}
    current = None
    response_seen, item_seen, usage_seen = set(), set(), set()
    previous_total = None
    fallback = {}
    for line, obj in rows:
        ref, kind, ts = f"L{line}", obj.get("type"), timestamp(obj.get("timestamp"))
        pl = obj.get("payload") or {}
        if not isinstance(pl, dict):
            continue
        typ = pl.get("type")
        if kind == "event_msg" and typ == "task_started":
            tid = str(pl.get("turn_id") or ref)
            if tid in mapping:
                current = mapping[tid]
            else:
                current = new_run("codex", path, sha, {"turn_id": tid}, session)
                current["start_ms"] = first(timestamp(pl.get("started_at")), ts)
                current["warnings"].extend(["工具调用次数按模型侧顶层 call_id；exec 等批处理封装内部动作不重复相加",
                                            "LLM 完整请求延迟未由 rollout 明确记录；消息输出跨度不等于请求耗时",
                                            "仅统计选定日志/轮次，未自动并入子代理或分叉继承数据"])
                runs.append(current)
                mapping[tid] = current
        run = mapping.get(str(pl.get("turn_id")), current)
        if run is None:
            continue
        if kind == "turn_context":
            if pl.get("model"):
                run["models"].append(pl["model"])
        if kind == "token_usage_record":
            uid = pl.get("response_id") or ref
            if not pl.get("response_id"):
                run["warnings"].append("token_usage_record 缺少 response_id；无法跨重复事件去重")
            if uid not in usage_seen and isinstance(pl.get("usage"), dict):
                usage_seen.add(uid)
                run["usage_records"].append(usage_record(pl["usage"], ref, uid))
        if kind == "event_msg" and typ == "token_count":
            info = pl.get("info") or {}
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                if previous_total is not None:
                    vals = {k: total.get(k) - previous_total[k] for k in total if isinstance(total.get(k), (int, float)) and isinstance(previous_total.get(k), (int, float))}
                    if any(v < 0 for v in vals.values()):
                        run["warnings"].append("累计 Token 计数下降，无法可靠计算该条增量")
                    elif vals.get("total_tokens", 0) > 0:
                        fallback.setdefault(id(run), []).append(usage_record(vals, ref))
                else:
                    last = info.get("last_token_usage")
                    if isinstance(last, dict) and last.get("total_tokens") == total.get("total_tokens"):
                        fallback.setdefault(id(run), []).append(usage_record(last, ref))
                    else:
                        run["warnings"].append("首条累计用量含未知基线，未把继承历史计入本轮")
                previous_total = total
        if kind == "event_msg" and typ in ("task_complete", "turn_aborted"):
            run["execution_status"] = "completed" if typ == "task_complete" else "aborted"
            run["end_ms"] = first(timestamp(pl.get("completed_at")), ts)
            if typ == "task_complete":
                duration = number(pl.get("duration_ms"))
                if duration is not None:
                    run["duration"] = metric(duration, "reported_turn_duration", [ref])
                ttft = number(pl.get("time_to_first_token_ms"))
                run["first_token"] = metric(ttft, "reported_turn_ttft" if ttft is not None else "missing", [ref])
                final = text_content(pl.get("last_agent_message"))
                if final:
                    run["final_answer"], run["final_ref"], run["final_kind"] = redact(final), ref, "explicit_final"
        if kind == "event_msg" and typ == "user_message":
            txt = redact(text_content(pl.get("message")))
            if txt and not any(x["text"] == txt for x in run["user_messages"]):
                run["user_messages"].append({"text": txt, "ref": ref})
                add_event(run, ref, "user", txt)
        if kind == "event_msg" and typ == "item_completed":
            item = pl.get("item") or {}
            key = item.get("id") or ref
            if key in item_seen:
                continue
            item_seen.add(key)
            if item.get("type") == "UserMessage":
                txt = redact(text_content(item.get("content")))
                if txt and not any(x["text"] == txt for x in run["user_messages"]):
                    run["user_messages"].append({"text": txt, "ref": ref})
                    add_event(run, ref, "user", txt)
            elif item.get("type") == "AgentMessage":
                if item.get("phase") in ("final_answer", "final"):
                    run["final_answer"], run["final_ref"], run["final_kind"] = redact(text_content(item.get("content"))), ref, "explicit_final"
                start, end = number(pl.get("started_at_ms")), number(pl.get("completed_at_ms"))
                if start is not None and end is not None and end >= start:
                    run["assistant_output_spans_ms"].append(end - start)
            # Reasoning and hidden/internal messages are deliberately not exported.
        if kind == "response_item":
            key = pl.get("id") or ((typ, pl["call_id"]) if pl.get("call_id") else ref)
            if key in response_seen:
                continue
            response_seen.add(key)
            if typ in ("function_call", "custom_tool_call", "local_shell_call"):
                call_event(run, str(pl.get("call_id") or pl.get("id") or ref), pl.get("name") or typ, ref, ts)
            elif typ in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                result_event(run, str(pl.get("call_id")), ref, pl.get("output", ""), ts, codex_result_error(run, pl))
            elif typ == "message" and pl.get("role") == "assistant" and pl.get("channel") == "final":
                run["final_answer"], run["final_ref"], run["final_kind"] = redact(text_content(pl.get("content"))), ref, "explicit_final"
            elif typ == "message" and pl.get("role") == "user" and not run["user_messages"]:
                txt = redact(text_content(pl.get("content")))
                run["user_messages"].append({"text": txt, "ref": ref})
                add_event(run, ref, "user", txt)
    for run in runs:
        if not run["usage_records"]:
            run["usage_records"] = fallback.get(id(run), [])
            if run["usage_records"]:
                run["warnings"].append("旧版 token_count：按累计差分去重统计，缺失基线不补猜")
        pair_calls(run)
        if run["duration"]["value"] is None and run["end_ms"] is not None and run["start_ms"] is not None:
            delta = run["end_ms"] - run["start_ms"]
            if delta >= 0:
                run["duration"] = metric(delta, "turn_event_span")
        if run["final_ref"]:
            add_event(run, run["final_ref"], "final_answer", run["final_answer"])
        finish(run)
    if not runs:
        raise ValueError(f"{path}: 未找到 task_started；旧版格式需补充适配，不能默默输出空用量")
    return runs


def parse_codex_exec_rows(path, sha, rows):
    """Fallback for official `codex exec --json` captured by the publisher.

    This is turn-level usage, with observer timestamps; native rollout takes priority.
    """
    session = next((o.get("thread_id") for _, o in rows if o.get("type") == "thread.started"), path.stem)
    prompt_row = next(((n, o) for n, o in rows if o.get("type") == "agent_run_compare.prompt"), None)
    prompt = prompt_row[1].get("prompt", "") if prompt_row else ""
    runs, run, completed_items = [], None, set()
    for line, obj in rows:
        kind = obj.get("type")
        ts = timestamp(first(obj.get("timestamp"), obj.get("observed_at")))
        if kind == "turn.started":
            run = new_run("codex", path, sha, {"turn_id": str(obj.get("turn_id") or f"exec-turn-{len(runs)+1}")}, session)
            run["source_format"] = "codex_exec_json"
            run["start_ms"] = ts
            if prompt:
                ref = f"L{prompt_row[0]}"
                run["user_messages"].append({"text": redact(prompt), "ref": ref})
                add_event(run, ref, "user", redact(prompt))
            run["warnings"].extend(["来源为 codex exec JSON 事件；Token 在 turn.completed 后才有轮次汇总",
                                    "时间取发布器接收事件的时间，不冒充服务端请求时长；LLM 延迟留空"])
            runs.append(run)
        if run is None:
            continue
        item = obj.get("item") or {}
        if kind in ("item.started", "item.completed") and isinstance(item, dict):
            typ, iid = item.get("type"), str(item.get("id") or f"L{line}")
            if typ in ("command_execution", "mcp_tool_call", "web_search", "file_change", "collab_tool_call"):
                name = item.get("tool") or item.get("name") or typ
                if kind == "item.started":
                    call_event(run, iid, name, f"L{line}", ts)
                elif iid not in completed_items:
                    completed_items.add(iid)
                    call_event(run, iid, name, f"L{line}", None)
                    result_event(run, iid, f"L{line}", first(item.get("aggregated_output"), item.get("result"), ""), ts,
                                 item.get("status") == "failed" or (isinstance(item.get("exit_code"), int) and item["exit_code"] != 0))
            elif kind == "item.completed" and typ == "agent_message":
                run["final_answer"], run["final_ref"], run["final_kind"] = redact(item.get("text", "")), f"L{line}", "last_assistant_candidate"
        elif kind in ("turn.completed", "turn.failed"):
            run["end_ms"] = ts
            run["execution_status"] = "completed" if kind == "turn.completed" else "failed"
            if kind == "turn.completed" and isinstance(obj.get("usage"), dict):
                run["usage_records"] = [usage_record(obj["usage"], f"L{line}", run["selector"]["turn_id"])]
            if run["start_ms"] is not None and ts is not None and ts >= run["start_ms"]:
                run["duration"] = metric(ts-run["start_ms"], "client_observed_turn_span", [f"L{line}"])
    for run in runs:
        pair_calls(run)
        if run["final_ref"]:
            add_event(run, run["final_ref"], "final_answer", run["final_answer"])
        finish(run)
        for m in run["tokens"].values():
            if m["value"] is not None:
                m["method"] = "reported_turn_usage"
    return runs


from mesh_adapter import parse_mesh
from epsilon_adapter import parse_epsilon

PARSERS = {"codex": parse_codex, "workbuddy": parse_workbuddy, "jahead": parse_jahead, "mesh": parse_mesh, "epsilon": parse_epsilon}


def parse(path, platform):
    if platform not in PARSERS:
        raise ValueError(f"不支持的平台 {platform}")
    return PARSERS[platform](path)
