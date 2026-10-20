import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adapters import parse, call_event, result_event, codex_result_error
from common import finish, new_run, usage_record
from epsilon_api import submit, sse_frames, cancel, validate_epsilon_url
from epsilon_protocol import sanitize_event
from orchestrate import prepare, preflight, roots_for
from dispatch import ConfigurationError
from task_console import validate_request
from compare import load_report, apply_reviews, render_report


def event(kind, payload):
    return {"type": kind, "payload": payload, "create_time": "2026-10-20 00:00:00"}


def stream_events():
    return [
        event("run_started", {"run_id": "native-r"}),
        event("text_delta", {"text": "calculating"}),
        event("turn_completed", {"text": "calculating", "message": {"content": [
            {"type": "thinking", "thinking": "SECRET_THOUGHT"}, {"type": "tool_use", "id": "c1"}]},
            "usage": {"input_tokens": 10, "cache_read_tokens": 20, "cache_write_tokens": 2,
                      "output_tokens": 5, "last_model": "epsilon-model", "details": {"epsilon-model": {"input_tokens": 999}}}}),
        event("tool_exec_started", {"tool_use_id": "c1", "tool_name": "bash", "tool_input": {"secret": "SECRET_INPUT"}}),
        event("tool_exec_completed", {"tool_use_id": "c1", "tool_name": "bash", "output": "error", "is_error": True}),
        event("turn_completed", {"text": "323", "message": {"content": [{"type": "text", "text": "323"}]},
            "usage": {"input_tokens": 7, "cache_read_tokens": 5, "cache_write_tokens": 0,
                      "output_tokens": 3, "last_model": "epsilon-model"}})]


class Response(io.BytesIO):
    headers = {"Content-Type": "text/event-stream"}


class FakeClient:
    base_url = "http://127.0.0.1:30100"
    scope = {"user_id": "u", "company_id": "c", "project_id": "p", "agent_id": None}
    def __init__(self, events=None, done=True):
        events = stream_events() if events is None else events
        self.raw = (": heartbeat\n\n"+ "".join("data: "+json.dumps(e)+"\n\n" for e in events)
                    + ("data: [DONE]\n\n" if done else "")).encode()
        self.posts = 0
    def open(self, method, path, body, stream=False):
        self.posts += 1
        self.body = body
        return Response(self.raw)
    def request(self, method, path, body):
        self.cancel_body = body
        return {"cancelled": True}


class EpsilonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def submit(self, client):
        return submit(client, {"base_url": "http://127.0.0.1:30100", "model": "epsilon-model"},
                      "17*19", self.root/"receipt.json", self.root/"workspace", sink=lambda _: None)

    def test_capture_usage_error_and_completion_without_private_blocks(self):
        receipt = self.submit(FakeClient())
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["run_id"], "native-r")
        journal = Path(receipt["journal_path"])
        content = journal.read_text()
        self.assertNotIn("SECRET_THOUGHT", content)
        self.assertNotIn("SECRET_INPUT", content)
        run = parse(journal, "epsilon")[0]
        self.assertEqual(run["tokens"]["total_tokens"]["value"], 52)
        self.assertEqual(run["tokens"]["input_tokens"]["value"], 44)
        self.assertAlmostEqual(run["cache_rate"]["value"], 25/44*100)
        self.assertEqual(run["tool_error_count"]["value"], 1)
        self.assertEqual(run["tool_call_count"], 1)
        self.assertEqual(run["execution_status"], "completed")
        self.assertEqual(run["models"], ["epsilon-model"])
        self.assertIsNotNone(run["first_token"]["value"])
        self.assertIsNone(run["llm_latency"]["mean_ms"])

    def test_uncertain_stream_and_repeated_receipt_do_not_post_again(self):
        client = FakeClient(done=False)
        with self.assertRaisesRegex(ValueError, "断开"):
            self.submit(client)
        receipt = self.submit(client)
        self.assertEqual(client.posts, 1)
        self.assertEqual(receipt["status"], "dispatch_unknown")

    def test_turn_completed_is_not_whole_run_completion(self):
        receipt = self.submit(FakeClient(events=stream_events()[:3]))
        self.assertEqual(receipt["status"], "dispatch_unknown")

    def test_suspension_and_error_done_are_not_success(self):
        for kind, payload, expected in [
            ("agent_suspended", {"resume_token": "t", "reason": "user_input"}, "needs_input"),
            ("error", {"message": "failed", "recoverable": True}, "failed"),
            ("response_cancelled", {"run_id": "native-r"}, "cancelled")]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                r=submit(FakeClient(events=stream_events()+[event(kind,payload)]), {}, "x",
                         Path(temp)/"receipt.json", Path(temp), sink=lambda _:None)
                self.assertEqual(r["status"], expected)
                if kind == "error":
                    self.assertEqual(parse(r["journal_path"], "epsilon")[0]["execution_error"], "failed")

    def test_sse_multiline_comments_and_missing_done(self):
        raw=b': heartbeat\n\ndata: {"type": "status",\ndata: "payload": {}}\n\ndata: [DONE]\n\n'
        self.assertEqual(len(list(sse_frames(io.BytesIO(raw)))), 2)
        with self.assertRaises(ValueError):
            list(sse_frames(io.BytesIO(b'data: {}\n\n')))

    def test_recoverable_error_followed_by_success_is_not_failed(self):
        events = stream_events()
        events.insert(1, event("error", {"message": "temporary", "recoverable": True}))
        receipt = self.submit(FakeClient(events=events))
        self.assertEqual(receipt["status"], "completed")
        self.assertNotIn("execution_error", parse(receipt["journal_path"], "epsilon")[0])

    def test_cancel_requires_exact_owned_run(self):
        client=FakeClient();r=self.submit(client)
        cancel(client,r)
        self.assertEqual(client.cancel_body["run_id"],"native-r")
        self.assertEqual(client.cancel_body["session_id"],r["session_id"])
        with self.assertRaises(ValueError):cancel(client,{"submitted":True})

    def test_remote_private_url_and_no_client_workspace_sent(self):
        for value in ["http://10.0.0.2:39001", "http://localhost:30100", "http://[::1]:30100"]:
            self.assertEqual(validate_epsilon_url(value), value)
        for value in ["http://example.com", "http://8.8.8.8", "http://169.254.169.254",
                      "http://user:password@10.0.0.2", "http://10.0.0.2/?token=x"]:
            with self.assertRaises(ValueError):validate_epsilon_url(value)
        client=FakeClient();client.base_url="http://10.0.0.2:39001"
        self.submit(client)
        self.assertNotIn("cwd",client.body["options"])

    def test_parser_rejects_cross_session_and_conflicting_replay(self):
        receipt=self.submit(FakeClient());p=Path(receipt["journal_path"])
        rows=[json.loads(line) for line in p.read_text().splitlines()]
        target=next(x for x in rows if x["type"]=="epsilon.event")
        p.write_text(p.read_text()+json.dumps(target)+"\n")
        self.assertEqual(parse(p,"epsilon")[0]["tool_call_count"],1)
        conflict=copy.deepcopy(target);conflict["time_ms"]+=1
        p.write_text(p.read_text()+json.dumps(conflict)+"\n")
        with self.assertRaises(ValueError):parse(p,"epsilon")

    def test_console_driver_scope_and_attachment_rejection(self):
        spec={"request_id":str(uuid.uuid4()),"tasks":[{"prompt":"x"}],"providers":{"epsilon":{"model":"m","user_id":"u"}}}
        normalized=validate_request(spec)
        self.assertEqual(normalized["providers"]["epsilon"]["driver"],"epsilon_api")
        spec["providers"]["epsilon"]["base_url"]="http://example.com"
        with self.assertRaises(ValueError):validate_request(spec)
        spec["providers"]["epsilon"]={"token":"secret"}
        with self.assertRaises(ValueError):validate_request(spec)
        f=self.root/"input.txt";f.write_text("x")
        task=self.root/"task.json";task.write_text(json.dumps({"prompt":"x","criteria":[{"id":"c","description":"x"}],
            "input_files":[{"source":str(f)}],"providers":{"epsilon":{"driver":"epsilon_api"}}}))
        root,exp=prepare(task,self.root/"exp")
        with self.assertRaises(ConfigurationError):preflight(root,exp,"epsilon")
        self.assertEqual(roots_for(exp,"epsilon"),[])

    def test_report_accepts_epsilon_and_new_metric_rows(self):
        r=self.submit(FakeClient())
        task={"id":"t","prompt":"17*19","criteria":[{"id":"c","description":"323"}],
              "runs":[{"id":"e","platform":"epsilon","path":r["journal_path"],"selector":r["selector"]}]}
        path=self.root/"manifest.json";path.write_text(json.dumps({"schema_version":1,"tasks":[task]}))
        report=load_report(path);apply_reviews(report,[]);render_report(report,self.root/"report")
        html=(self.root/"report/index.html").read_text()
        self.assertIn("工具调用错误数（error）",html);self.assertIn("缓存率",html)
        self.assertNotIn("即先计费 Token（独立口径）",html)


class ExtraMetricTests(unittest.TestCase):
    def make_run(self):
        return new_run("codex","fixture","sha",{},"s")
    def test_cache_is_weighted_ratio_and_partial_cache_only_lower_bound(self):
        r=self.make_run()
        r["usage_records"]=[usage_record({"input_tokens":100,"cached_input_tokens":80},"L1"),
                            usage_record({"input_tokens":900,"cached_input_tokens":90},"L2")]
        finish(r);self.assertEqual(r["cache_rate"]["value"],17)
        r["usage_records"][1]["usage"]["cached_input_tokens"]=None
        finish(r);self.assertEqual(r["cache_rate"]["value"],8)
        self.assertEqual(r["cache_rate"]["method"],"cached_input_ratio_lower_bound")
        r["usage_records"][1]["usage"]["input_tokens"]=None
        finish(r);self.assertIsNone(r["cache_rate"]["value"])
    def test_zero_input_and_unknown_errors_are_missing(self):
        r=self.make_run();r["usage_records"]=[usage_record({"input_tokens":0,"cached_input_tokens":0},"L1")]
        call_event(r,"c","tool","L2");result_event(r,"c","L3","the word error is not a flag")
        finish(r);self.assertIsNone(r["cache_rate"]["value"]);self.assertIsNone(r["tool_error_count"]["value"])
    def test_error_call_count_deduplicates_recovery_same_id(self):
        r=self.make_run();call_event(r,"c","tool","L1")
        result_event(r,"c","L2","bad",error=True);result_event(r,"c","L3","recovered",error=False)
        finish(r);self.assertEqual(r["tool_error_count"]["value"],1)
        self.assertEqual(r["tool_error_count"]["evidence"],["L2"])
    def test_codex_command_exit_code_is_explicit_but_free_text_is_not(self):
        r=self.make_run();call_event(r,"c","functions.exec_command","L1")
        self.assertTrue(codex_result_error(r,{"call_id":"c","output":"Chunk ID: a\nProcess exited with code 1\nOutput:\nx"}))
        self.assertFalse(codex_result_error(r,{"call_id":"c","is_error":False}))
        self.assertIsNone(codex_result_error(r,{"call_id":"c","output":"error somewhere"}))


if __name__ == "__main__":
    unittest.main()
