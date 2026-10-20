import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adapters import parse
from mesh_api import collect
from mesh_protocol import sanitize_event
from mesh_timing import first_token_time, safe_metrics


def fixture():
    events = [
        ("mesh/run-started", 1000, {"run_id": "r"}),
        ("step/start", 1200, {"turn": 1, "step": 1}),
        ("assistant/attempt", 1500, {"turn": 1, "step": 1, "stream": [
            {"type": "reasoning-chunks", "time0": 1250, "dt": [50], "texts": ["", "PRIVATE_REASONING"]}]}),
        ("assistant/message", 1800, {"turn": 1, "step": 1, "usage": {"totalTokens": 20, "outputTokens": 5},
                                    "message": {"content": [{"type": "text", "text": "first"}]}}),
        ("step/end", 1850, {"turn": 1, "step": 1}),
        ("step/start", 2000, {"turn": 1, "step": 2}),
        ("assistant/message", 3000, {"turn": 1, "step": 2, "usage": {"totalTokens": 30, "outputTokens": 8},
                                    "stream": [{"type": "chunk", "time": 2500,
                                                "chunk": {"type": "text-delta", "text": "answer"}}],
                                    "message": {"content": [{"type": "text", "text": "done"}]}}),
        ("step/end", 3100, {"turn": 1, "step": 2}),
        ("mesh/run-completed", 3200, {"run_id": "r"}),
    ]
    raw = [{"seq": i, "session_generation": 0, "time_ms": stamp, "type": kind, "data": data}
           for i, (kind, stamp, data) in enumerate(events)]
    metrics = {"type": "mesh.metrics", "session_id": "s", "run_id": "r", "generation": 0, "after_seq": 8,
               "metrics": {"as_of_seq": 8, "semantics_version": 1, "scope": "session_projection",
                           "includes_inherited_seed": False,
                           "session_stats": {"turns": 1, "steps": 2, "llm_ms": 1600, "ttft_ms": 600,
                                             "ttft_steps": 2, "tool_ms": 0, "decode_ms": 1000, "decode_tokens": 13}}}
    return raw, metrics


class MeshTimingTests(unittest.TestCase):
    def parse_fixture(self, raw, metrics=None):
        rows = [{"type": "mesh.session", "session_id": "s", "run_id": "r", "generation": 0}]
        rows += [{"type": "mesh.event", "session_id": "s", "event": sanitize_event(e)} for e in raw]
        if metrics:
            rows.append(metrics)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            return parse(path, "mesh")

    def test_api_means_and_event_p95_match_without_changing_tokens(self):
        raw, metrics = fixture()
        run = self.parse_fixture(raw, metrics)[0]
        self.assertEqual(run["tokens"]["total_tokens"]["value"], 50)
        self.assertEqual(run["llm_latency"]["mean_ms"], 800)
        self.assertEqual(run["llm_latency"]["sum_ms"], 1600)
        self.assertEqual(run["llm_latency"]["p95_ms"], 980)
        self.assertEqual(run["llm_latency"]["method"], "mesh_session_metrics")
        self.assertEqual(run["llm_latency"]["quantile_method"], "mesh_step_event_span")
        self.assertEqual(run["llm_first_token"]["value"], 300)
        self.assertEqual(run["first_token"]["value"], 300)
        self.assertEqual(run["llm_timing_samples"][0]["ttft_ms"], 100)
        self.assertNotIn("PRIVATE_REASONING", json.dumps(run))
        self.assertNotIn("stream", json.dumps([sanitize_event(e) for e in raw]))

    def test_packed_and_unpacked_first_token_timestamps(self):
        self.assertEqual(first_token_time([{"type": "tool-call-chunks", "time0": 100,
                                           "args": ["", "secret"], "dt": [25]}]), 125)
        self.assertEqual(first_token_time([{"type": "tool-call-chunks", "time0": 100,
                                           "name": "exec", "args": []}]), 100)
        self.assertEqual(first_token_time([{"type": "chunk", "time": 90, "chunk": {
            "type": "tool-call-delta", "name": "exec"}}]), 90)
        self.assertIsNone(first_token_time([{"type": "text-chunks", "time0": 100,
                                            "texts": ["", "x"], "dt": [-1]}]))
        self.assertIsNone(first_token_time([{"type": "chunk", "time": True,
                                            "chunk": {"type": "text-delta", "text": "x"}}]))

    def test_missing_first_step_stream_is_not_replaced_by_later_ttft(self):
        raw, metrics = fixture()
        raw[2]["data"].pop("stream")
        run = self.parse_fixture(raw, metrics)[0]
        self.assertIsNone(run["first_token"]["value"])
        # The independent API average remains available.
        self.assertEqual(run["llm_first_token"]["value"], 300)

    def test_aggregate_does_not_invent_quantiles(self):
        raw, metrics = fixture()
        raw[1]["type"] = "turn/start"  # No first step timing sample.
        run = self.parse_fixture(raw, metrics)[0]
        self.assertEqual(run["llm_latency"]["mean_ms"], 800)
        self.assertIsNone(run["llm_latency"]["p95_ms"])
        self.assertEqual(run["llm_latency"]["sample_count"], 1)

    def test_mismatched_scope_watermark_and_invalid_counts_are_rejected(self):
        raw, original = fixture()
        mutations = [
            lambda m: m.update(run_id="other"),
            lambda m: m.update(generation=1),
            lambda m: m["metrics"].update(as_of_seq=7),
            lambda m: m["metrics"].update(includes_inherited_seed=True),
            lambda m: m["metrics"]["session_stats"].update(steps=3),
            lambda m: m["metrics"]["session_stats"].update(ttft_steps=3),
            lambda m: m["metrics"]["session_stats"].update(ttft_ms=2000),
            lambda m: m["metrics"]["session_stats"].update(llm_ms=True),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                metrics = copy.deepcopy(original)
                mutation(metrics)
                run = self.parse_fixture(raw, metrics)[0]
                self.assertEqual(run["llm_latency"]["method"], "mesh_step_event_span")
                self.assertIsNone(run["llm_first_token"]["value"])

    def test_whole_session_metrics_are_not_assigned_to_one_of_multiple_runs(self):
        raw, metrics = fixture()
        raw += [{"seq": 9, "session_generation": 0, "time_ms": 4000,
                 "type": "mesh/run-started", "data": {"run_id": "another"}}]
        metrics["after_seq"] = metrics["metrics"]["as_of_seq"] = 9
        runs = self.parse_fixture(raw, metrics)
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["llm_latency"]["method"] != "mesh_session_metrics" for r in runs))
        self.assertTrue(all(r["llm_first_token"]["value"] is None for r in runs))

    def test_wrong_step_and_out_of_range_first_tokens_do_not_fill_task_ttft(self):
        raw, metrics = fixture()
        raw[2]["data"]["step"] = 9
        raw[3]["data"]["stream"] = [{"type": "chunk", "time": 1900,
                                     "chunk": {"type": "text-delta", "text": "late"}}]
        self.assertIsNone(self.parse_fixture(raw, metrics)[0]["first_token"]["value"])

    def test_collector_keeps_api_stats_and_scope_without_context_estimates(self):
        raw, envelope = fixture()
        class Client:
            scope = {"company_id": "c", "project_id": "p"}
            def events(self, session, after, generation):
                return {"events": raw if after < 0 else [], "next_after": 8}
            def request(self, method, path, query):
                return {**envelope["metrics"], "context_breakdown": {"PRIVATE": "DROP"},
                        "session_stats": {**envelope["metrics"]["session_stats"], "secret": "DROP"}}
        rows = []
        collect(Client(), {"session_id": "s", "run_id": "r", "generation": 0}, sink=rows.append)
        actual = next(r for r in rows if r["type"] == "mesh.metrics")
        self.assertEqual(actual, envelope)
        self.assertNotIn("DROP", json.dumps(rows))
        self.assertEqual(safe_metrics({"session_stats": {"steps": True, "llm_ms": -1}})["session_stats"], {})


if __name__ == "__main__":
    unittest.main()
