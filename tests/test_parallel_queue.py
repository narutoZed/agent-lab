import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from task_console import ConsoleStore
from dispatch import ConfigurationError
from epsilon_api import submit as epsilon_submit
from mesh_api import submit as mesh_submit
from test_epsilon import FakeClient as EpsilonFake
from test_mesh import FakeClient as MeshFake


def payload(count=12, platforms=("mesh", "epsilon")):
    return {"request_id": str(uuid.uuid4()), "title": "parallel fixture",
            "tasks": [{"id": str(i), "prompt": f"task-{i}"} for i in range(count)],
            "providers": {p: {} for p in platforms}}


class ParallelQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.launched, self.states, self.observed = [], {}, []
        def launcher(root, exp, platform):
            key = (str(root), platform)
            self.assertNotIn(key, self.launched)
            self.launched.append(key)
            self.states[key] = {"status": "running"}
            return self.states[key]
        def observer(root, exp, platform):
            key = (str(root), platform)
            self.observed.append(key)
            return self.states[key]
        self.store = ConsoleStore(self.root, launcher, observer)
    def tearDown(self):
        self.temp.cleanup()
    def count_active(self, platform):
        return sum(j["platform"] == platform and self.store._active(j)
                   for b in self.store.batches.values() for j in b["jobs"])

    def test_ten_slots_each_and_refill_only_vacated_slots(self):
        self.store.create(payload())
        self.store.tick("mesh");self.store.tick("epsilon")
        self.assertEqual(self.count_active("mesh"), 10)
        self.assertEqual(self.count_active("epsilon"), 10)
        self.assertEqual(len(self.launched), 20)
        self.states[next(k for k in self.launched if k[1] == "mesh")] = {"status": "completed"}
        self.store.tick("mesh")
        self.assertEqual(self.count_active("mesh"), 10)
        self.assertEqual(len(self.launched), 21)
        self.store.tick("mesh")
        self.assertEqual(len(self.launched), 21)

    def test_limit_is_global_across_batches_and_tick_calls(self):
        self.store.create(payload(6, ("epsilon",)))
        self.store.create(payload(8, ("epsilon",)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.store.tick("epsilon"), range(4)))
        self.assertEqual(len(self.launched), 10)
        self.assertEqual(self.count_active("epsilon"), 10)

    def test_other_platforms_remain_serial(self):
        self.store.create(payload(3, ("codex", "workbuddy", "jahead")))
        for p in ("codex", "workbuddy", "jahead"):
            self.store.tick(p)
            self.assertEqual(self.count_active(p), 1)
        self.assertEqual(len(self.launched), 3)

    def test_pause_does_not_cancel_running_slots(self):
        bid = self.store.create(payload(12, ("epsilon",)))["id"]
        self.store.tick("epsilon")
        self.store.set_paused(bid, True)
        for key in self.launched:
            self.states[key] = {"status": "completed"}
        self.store.tick("epsilon")
        self.assertEqual(len(self.launched), 10)
        self.assertEqual(self.count_active("epsilon"), 0)
        self.store.set_paused(bid, False)
        self.store.tick("epsilon")
        self.assertEqual(len(self.launched), 12)

    def test_attention_blocks_new_work_but_other_slots_keep_being_observed(self):
        self.store.create(payload(12, ("mesh",)))
        self.store.tick("mesh")
        first, second = self.launched[:2]
        self.states[first] = {"status": "timed_out"}
        self.states[second] = {"status": "completed"}
        self.store.tick("mesh")
        self.assertEqual(len(self.launched), 10)
        self.assertIn(second, self.observed)
        self.assertEqual(self.count_active("mesh"), 9)

    def test_restart_preserves_parallelism_and_never_replays_active_jobs(self):
        bid = self.store.create(payload(12, ("epsilon",)))["id"]
        self.store.tick("epsilon")
        recovered = ConsoleStore(self.root, self.store.launcher, self.store.observer)
        self.assertEqual(recovered.batches[bid]["parallelism"]["epsilon"], 10)
        self.assertTrue(recovered.batches[bid]["paused"])
        recovered.tick("epsilon")
        self.assertEqual(len(self.launched), 10)
        self.assertEqual(len(self.observed), 10)

    def test_first_configuration_failure_does_not_dispatch_remaining_slots(self):
        self.store.create(payload(12, ("epsilon",)))
        attempts = []
        def fail(*args):
            attempts.append(1)
            raise ConfigurationError("unavailable")
        self.store.launcher = fail
        self.store.tick("epsilon")
        self.store.tick("epsilon")
        self.assertEqual(len(attempts), 1)

    def test_one_observation_error_does_not_hide_other_running_slots(self):
        self.store.create(payload(12, ("mesh",)))
        self.store.tick("mesh")
        original = self.store.observer
        bad = self.launched[0]
        def observe(root, exp, platform):
            if (str(root), platform) == bad:
                raise OSError("temporarily unreadable")
            return original(root, exp, platform)
        self.store.observer = observe
        self.states[self.launched[1]] = {"status": "completed"}
        self.store.tick("mesh")
        self.assertEqual(len(self.launched), 10)
        self.assertIn(self.launched[1], self.observed)
        self.store.observer = original
        self.store.tick("mesh")
        self.assertEqual(len(self.launched), 11)

    def test_legacy_batch_retains_serial_policy_after_upgrade(self):
        bid = self.store.create(payload(3, ("mesh",)))["id"]
        file = self.root/bid/"batch.json"
        batch = json.loads(file.read_text())
        batch.pop("parallelism")
        file.write_text(json.dumps(batch))
        recovered = ConsoleStore(self.root, self.store.launcher, self.store.observer)
        self.assertEqual(recovered.batches[bid]["parallelism"]["mesh"], 1)
        recovered.set_paused(bid, False)
        recovered.tick("mesh")
        recovered.tick("mesh")
        self.assertEqual(len(self.launched), 1)

    def test_publishers_generate_ten_distinct_sessions_without_real_model_calls(self):
        for name, publish, client in (("mesh", mesh_submit, MeshFake), ("epsilon", epsilon_submit, EpsilonFake)):
            def one(i):
                dest = self.root/name/str(i)/"receipt.json"
                if name == "mesh":
                    return publish(client(), {}, "fixture", dest)["session_id"]
                return publish(client(), {}, "fixture", dest, dest.parent/"workspace",
                               sink=lambda _: None)["session_id"]
            with ThreadPoolExecutor(max_workers=10) as pool:
                sessions = list(pool.map(one, range(10)))
            self.assertEqual(len(set(sessions)), 10)


if __name__ == "__main__":
    unittest.main()
