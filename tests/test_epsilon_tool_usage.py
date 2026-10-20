import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from adapters import parse
from epsilon_api import submit
from epsilon_protocol import sanitize_event
from test_epsilon import FakeClient, stream_events, event


class EpsilonToolUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def run_events(self, events):
        receipt = submit(FakeClient(events=events), {}, "fixture", self.root/"receipt.json",
                         self.root/"workspace", sink=lambda _: None)
        path = Path(receipt["journal_path"])
        return path, parse(path, "epsilon")[0]
    def usage(self):
        return {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 20,
                "cache_write_tokens": 2, "last_model": "tool-model",
                "details": {"tool-model": {"input_tokens": 10, "output_tokens": 5,
                                         "cache_read_tokens": 20, "cache_write_tokens": 2,
                                         "private": "SECRET"}}}

    def test_tool_usage_is_separate_and_details_are_not_double_counted(self):
        events = stream_events();events[4]["payload"]["usage"] = self.usage()
        path, run = self.run_events(events)
        self.assertEqual(run["tokens"]["total_tokens"]["value"], 52)
        self.assertEqual(run["tool_extra_tokens"]["value"], 37)
        self.assertEqual(run["tool_extra_tokens"]["method"], "reported_tool_usage_sum")
        self.assertEqual(run["models"], ["epsilon-model"])
        self.assertEqual(run["tool_usage_models"], ["tool-model"])
        self.assertEqual(run["tool_usage_records"][0]["tool_call_id"], "c1")
        self.assertNotIn("SECRET", path.read_text())
        self.assertAlmostEqual(run["cache_rate"]["value"], 25/44*100)

    def test_replayed_sse_sequence_does_not_count_usage_again(self):
        events=stream_events();events[4]["payload"]["usage"]=self.usage()
        path, _ = self.run_events(events)
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        record=next(r for r in rows if r.get("event", {}).get("type")=="tool_exec_completed")
        with path.open("a") as f:f.write(json.dumps(record)+"\n")
        run=parse(path,"epsilon")[0]
        self.assertEqual(run["tool_extra_tokens"]["value"],37)

    def test_distinct_tool_phases_have_separate_usage_with_one_call_id(self):
        events=stream_events();events[4]["payload"]["usage"]=self.usage()
        second=copy.deepcopy(events[4]);second["payload"]["is_error"]=False
        events.insert(5,second)
        _,run=self.run_events(events)
        self.assertEqual(run["tool_call_count"],1)
        self.assertEqual(run["tool_extra_tokens"]["value"],74)
        self.assertEqual(run["tool_extra_tokens"]["observed_records"],2)

    def test_old_log_without_capture_marker_remains_unknown(self):
        path,_=self.run_events(stream_events())
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row.get("type")=="epsilon.session":row.pop("capture_capabilities",None)
            if row.get("event",{}).get("type")=="tool_exec_completed":
                row["event"]["payload"].pop("usage_reported",None)
        path.write_text("".join(json.dumps(r)+"\n" for r in rows))
        run=parse(path,"epsilon")[0]
        self.assertIsNone(run["tool_extra_tokens"]["value"])
        self.assertEqual(run["tool_extra_tokens"]["method"],"tool_usage_not_captured")

    def test_new_capture_without_tool_usage_reports_recorded_zero(self):
        _,run=self.run_events(stream_events())
        self.assertEqual(run["tool_extra_tokens"]["value"],0)
        self.assertEqual(run["tool_extra_tokens"]["method"],"reported_no_tool_usage")
        self.assertTrue(run["tool_extra_tokens"]["capture_supported"])

    def test_incomplete_usage_does_not_become_zero(self):
        events=stream_events();events[4]["payload"]["usage"]={"input_tokens":10}
        _,run=self.run_events(events)
        self.assertIsNone(run["tool_extra_tokens"]["value"])
        self.assertEqual(run["tool_extra_tokens"]["observed_records"],1)

    def test_partial_usage_is_not_labeled_complete(self):
        events=stream_events();events[4]["payload"]["usage"]=self.usage()
        extra=event("tool_exec_completed",{"tool_use_id":"c1","tool_name":"bash",
                                          "output":"done","is_error":False,"usage":{"output_tokens":5}})
        events.insert(5,extra)
        _,run=self.run_events(events)
        self.assertEqual(run["tool_extra_tokens"]["value"],37)
        self.assertEqual(run["tool_extra_tokens"]["method"],"partial_tool_usage_sum")
        self.assertEqual(run["tool_extra_tokens"]["known_records"],1)

    def test_unrelated_metadata_and_messages_cannot_supply_tool_usage(self):
        raw=event("tool_exec_completed",{"tool_use_id":"c","tool_name":"tool","output":"ok",
                                        "is_error":False,"metadata":{"usage":self.usage()},
                                        "message":{"usage":self.usage()}})
        clean=sanitize_event(raw)
        self.assertFalse(clean["payload"]["usage_reported"])
        self.assertNotIn("usage",clean["payload"])


if __name__ == "__main__":
    unittest.main()
