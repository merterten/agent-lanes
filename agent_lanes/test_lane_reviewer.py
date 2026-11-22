"""Reviewer tests: remote host fallback, collect/stop, and gc in-use protection.

Run: python3 -m unittest discover -s agent_lanes -p 'test_lane*.py' -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane  # noqa: E402
from test_lane import VALID_WORKER, LaneTest  # noqa: E402


class TestHostFallback(LaneTest):
    def test_unreachable_refuses_and_starts_nothing(self):
        self.fake_remote(fail="always")
        self.write_hosts()
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--poll", "0")
        self.assertEqual(3, code, out)
        self.assertIn("unreachable", out)
        self.assertEqual([], self.rsync_calls())
        self.assertEqual("created", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])

    def test_probe_hang_counts_as_unreachable(self):
        slow = self.script("slow-ssh", "import time\ntime.sleep(30)\n")
        os.environ["LANE_SSH"] = str(slow)
        host = lane.remote.Host(name="slow", ssh="user@203.0.113.10", root="~/agent-lanes")
        self.assertFalse(lane.remote.probe(host, timeout=0.5))

    def test_host_or_local_falls_back_to_local_worker(self):
        self.fake_remote(fail="always")
        self.write_hosts()
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", VALID_WORKER))
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--host-or-local")
        self.assertEqual(0, code, out)
        self.assertEqual([], self.rsync_calls())
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual(("returned", "local"), (entry["status"], entry["where"]))

    def test_reachable_host_is_used_even_with_fallback_flag(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote()
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--host-or-local", "--poll", "0")
        self.assertEqual(0, code, out)
        self.assertTrue(self.rsync_calls())
        self.assertEqual("gpu-box", lane.load_ledger()["lanes"]["oc-worker-01"]["where"])

    def test_lost_host_while_polling_stops_and_stays_running(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote(fail="status")
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--poll", "0", "--max-poll-failures", "3")
        self.assertEqual(3, code, out)
        status_calls = [c for c in self.ssh_calls() if "status" in c[-1]]
        self.assertEqual(3, len(status_calls))
        self.assertFalse([c for c in self.rsync_calls() if ":" in c[-2]])
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual("running", entry["status"])
        self.assertIn("unreachable", entry["note"])

    def test_push_failure_does_not_start_run(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote()
        self.setup_remote(root)
        os.environ["FAKE_RSYNC_FAIL"] = "1"
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--poll", "0")
        self.assertEqual(1, code, out)
        self.assertEqual(1, len(self.rsync_calls()))
        self.assertFalse([c for c in self.ssh_calls() if " run " in c[-1]])

    def test_collect_waits_for_end_line(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote()
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="gpu-box"))
        code, out = self.cli("collect", "oc-worker-01")
        self.assertEqual(3, code, out)
        self.assertIn("still running", out)
        self.assertEqual([], self.rsync_calls())

    def test_collect_unreachable_does_nothing(self):
        self.fake_remote(fail="always")
        self.write_hosts()
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="gpu-box"))
        code, out = self.cli("collect", "oc-worker-01")
        self.assertEqual(3, code, out)
        self.assertEqual([], self.rsync_calls())
        self.assertEqual("running", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])

    def test_collect_after_end_pulls_and_validates(self):
        worker = self.script("fake-worker", VALID_WORKER)
        root = self.write_hosts(worker=str(worker))
        self.fake_remote()
        self.setup_remote(root)
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--host", "gpu-box",
                             "--poll", "0")
        self.assertEqual(0, code, out)
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="gpu-box"))
        before = len(self.rsync_calls())
        code, out = self.cli("collect", "oc-worker-01")
        self.assertEqual(0, code, out)
        self.assertGreater(len(self.rsync_calls()), before)
        self.assertEqual("returned", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])


class TestGcInUse(LaneTest):
    def line_for(self, out, lane_name):
        return [l for l in out.splitlines() if lane_name in l and not l.startswith("counts")]

    def test_gc_skips_lane_with_a_process_inside(self):
        path = self.make_lane("oc-worker-07")
        self.cli("set", "oc-worker-07", "rejected")
        proc_root = self.tmp / "proc"
        (proc_root / "4242").mkdir(parents=True)
        (path / "sub").mkdir()
        os.symlink(str(path / "sub"), proc_root / "4242" / "cwd")
        os.environ["LANE_PROC"] = str(proc_root)
        code, out = self.cli("gc", "--apply")
        self.assertEqual(0, code, out)
        self.assertTrue(path.exists())
        self.assertTrue(self.line_for(out, "oc-worker-07")[0].startswith("busy"), out)

    def test_process_in_similarly_named_folder_is_not_busy(self):
        path = self.make_lane("oc-worker-07")
        self.cli("set", "oc-worker-07", "rejected")
        sibling = Path(str(path) + "0")
        sibling.mkdir()
        proc_root = self.tmp / "proc"
        (proc_root / "4242").mkdir(parents=True)
        os.symlink(str(sibling), proc_root / "4242" / "cwd")
        os.environ["LANE_PROC"] = str(proc_root)
        code, out = self.cli("gc", "--apply")
        self.assertEqual(0, code, out)
        self.assertFalse(path.exists(), out)

    def test_gc_skips_open_ledger_lane(self):
        path = self.make_lane("oc-worker-08")
        code, out = self.cli("gc", "--apply")
        self.assertEqual(0, code, out)
        self.assertTrue(path.exists())
        self.assertTrue(self.line_for(out, "oc-worker-08")[0].startswith("open"), out)

    def test_gc_removes_merged_lane_unknown_to_ledger(self):
        path = self.make_lane("oc-worker-09")
        lane.mutate_ledger(lambda l: l.pop("oc-worker-09"))
        code, out = self.cli("gc", "--apply")
        self.assertEqual(0, code, out)
        self.assertFalse(path.exists(), out)


if __name__ == "__main__":
    unittest.main()
