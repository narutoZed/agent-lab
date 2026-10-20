"""Mesh sessionStats timing semantics, with per-step samples for quantiles."""
from __future__ import annotations

from common import metric


def count(value):
    return value if type(value) is int and value >= 0 else None


def first_token_time(stream):
    """Read only timing/presence metadata; never retain stream text or arguments."""
    if not isinstance(stream, (list, tuple)):
        return None
    for record in stream:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind == "chunk":
            chunk = record.get("chunk", {})
            if not isinstance(chunk, dict):
                continue
            token = (chunk.get("type") in ("text-delta", "reasoning-delta") and bool(chunk.get("text"))
                     or chunk.get("type") == "tool-call-delta"
                     and (bool(chunk.get("argumentsDelta")) or chunk.get("name") is not None))
            if token and count(record.get("time")) is not None:
                return record["time"]
        elif kind in ("text-chunks", "reasoning-chunks", "tool-call-chunks"):
            stamp = count(record.get("time0"))
            if stamp is None:
                continue
            if kind == "tool-call-chunks" and record.get("name") is not None:
                return stamp
            fragments = record.get("args" if kind == "tool-call-chunks" else "texts", [])
            deltas = record.get("dt", [])
            if not isinstance(fragments, (list, tuple)) or not isinstance(deltas, (list, tuple)):
                continue
            for index, fragment in enumerate(fragments):
                if index:
                    if index > len(deltas) or count(deltas[index - 1]) is None:
                        break
                    stamp += deltas[index - 1]
                if fragment:
                    return stamp
    return None


def safe_metrics(raw):
    if not isinstance(raw, dict):
        return {}
    result = {k: raw[k] for k in ("token_usage", "semantics_version", "usage_source", "usage_quality",
                                 "scope", "as_of_seq", "includes_inherited_seed") if k in raw}
    stats = raw.get("session_stats")
    if isinstance(stats, dict):
        result["session_stats"] = {
            k: stats[k] for k in ("turns", "steps", "llm_ms", "tool_ms", "ttft_ms", "ttft_steps",
                                  "decode_ms", "decode_tokens") if count(stats.get(k)) is not None}
    return result


class MeshTiming:
    def __init__(self):
        self.open_step = None
        self.first_at = None
        self.first_ref = None
        self.started_steps = 0
        self.ended_steps = 0
        self.samples = []
        self.task_first = None

    def apply(self, kind, data, stamp, ref):
        if count(stamp) is None:
            return
        if kind == "step/start":
            self.started_steps += 1
            self.open_step = (data.get("turn"), data.get("step"), stamp, ref, self.started_steps)
            self.first_at = None
            self.first_ref = None
        elif kind in ("assistant/attempt", "assistant/message"):
            step = self.open_step
            if step is None or step[:2] != (data.get("turn"), data.get("step")):
                return
            if self.first_at is None:
                self.first_at = count(data.get("first_token_at_ms"))
                self.first_ref = ref if self.first_at is not None else None
            if kind != "assistant/message":
                return
            self.open_step = None
            if stamp < step[2]:
                return
            first = self.first_at
            valid_first = first is not None and step[2] <= first <= stamp
            self.samples.append({"duration_ms": stamp - step[2],
                                 "ttft_ms": first - step[2] if valid_first else None,
                                 "start_ref": step[3], "end_ref": ref,
                                 "first_token_ref": self.first_ref if valid_first else None})
            # A later step's first token must not stand in for an unobserved first step.
            if step[4] == 1 and valid_first:
                self.task_first = (first, self.first_ref)
        elif kind in ("step/end", "turn/end", "mesh/run-failed", "mesh/run-cancelled"):
            self.open_step = None
            self.first_at = None
            self.first_ref = None
            if kind == "step/end":
                self.ended_steps += 1

    def prepare_run(self, run):
        run["llm_latencies"] = [s["duration_ms"] for s in self.samples]
        run["llm_latency_method"] = "mesh_step_event_span"
        run["llm_timing_samples"] = self.samples
        run["llm_first_token"] = metric()
        if self.task_first and count(run["start_ms"]) is not None and self.task_first[0] >= run["start_ms"]:
            run["first_token"] = metric(self.task_first[0] - run["start_ms"],
                                        "mesh_run_first_token_span", [self.task_first[1]])

    def apply_metrics(self, run, envelope, metadata, run_count, last_seq, ref):
        """Session aggregates apply only to a complete, single-run session snapshot."""
        raw = envelope.get("metrics", {})
        stats = raw.get("session_stats")
        if not isinstance(stats, dict):
            return
        identity_ok = (run_count == 1 and envelope.get("session_id") == metadata.get("session_id")
                       and envelope.get("run_id") == run["selector"]["run_id"]
                       and envelope.get("generation") == metadata.get("generation", 0)
                       and type(raw.get("as_of_seq")) is int and raw["as_of_seq"] >= last_seq
                       and raw["as_of_seq"] == envelope.get("after_seq")
                       and raw.get("semantics_version") == 1 and raw.get("scope") == "session_projection"
                       and raw.get("includes_inherited_seed") is False)
        fields = ("steps", "llm_ms", "ttft_ms", "ttft_steps")
        values_ok = (all(count(stats.get(k)) is not None for k in fields)
                     and stats.get("steps") == self.ended_steps
                     and stats.get("ttft_steps", 0) <= stats.get("steps", 0)
                     and stats.get("ttft_ms", 0) <= stats.get("llm_ms", 0))
        if not identity_ok or not values_ok:
            run["warnings"].append("Mesh session_stats 的会话范围、水位或步骤数量无法核对；未套用到本次运行")
            return
        run["reported_session_metrics"] = raw
        run["session_metrics_ref"] = ref
        steps = stats["steps"]
        if steps:
            distribution = run["llm_latency"]
            matched = (len(self.samples) == steps
                       and sum(s["duration_ms"] for s in self.samples) == stats["llm_ms"])
            distribution.update(count=steps, expected=steps, sum_ms=stats["llm_ms"],
                                mean_ms=stats["llm_ms"] / steps, method="mesh_session_metrics",
                                evidence=[ref], sample_count=len(self.samples),
                                quantile_method="mesh_step_event_span" if matched else "missing")
            if not matched:
                distribution["p50_ms"] = distribution["p95_ms"] = None
                run["warnings"].append("Mesh metrics 提供累计 LLM 耗时；逐步骤样本未完整匹配，P95 留空")
        if stats["ttft_steps"]:
            run["llm_first_token"] = metric(stats["ttft_ms"] / stats["ttft_steps"],
                                             "mesh_session_ttft_mean", [ref])
            run["llm_first_token"].update(count=stats["ttft_steps"], expected=steps,
                                           sum_ms=stats["ttft_ms"])
