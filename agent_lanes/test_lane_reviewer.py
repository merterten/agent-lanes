"""Reviewer (Claude) tests added at integration: laptop fallback and gc in-use protection.

Run: python3 -m unittest discover -s tools/lanes -p 'test_lane*.py' -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane  # noqa: E402
from test_lane import LAPTOP, VALID_WORKER, LaneTest  # noqa: E402

STATUS_OK = 'if cmd == "status":\n    print("working")\n    print("END rc=0")\n    sys.exit(0)'
STATUS_LOST = 'if cmd == "status":\n    sys.exit(255)'
STATUS_RUNNING = 'if cmd == "status":\n    print("== 01: RUNNING")\n    sys.exit(0)'
PUSH_OK = 'if cmd == "push":\n    sys.exit(0)'
PUSH_FAIL = 'if cmd == "push":\n    sys.exit(12)'


def variant(old, new):
    assert LAPTOP.count(old) == 1, old
    return LAPTOP.replace(old, new)


class TestLaptopFallback(LaneTest):
    def laptop_env(self, body=LAPTOP):
        os.environ["LANE_LAPTOP_BIN"] = str(self.script("fake-laptop", body))
        os.environ["FAKE_LAPTOP_LOG"] = str(self.tmp / "laptop.log")
        os.environ["FAKE_LAPTOP_STDIN"] = str(self.tmp / "laptop.stdin")

    def calls(self):
        log = self.tmp / "laptop.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_unreachable_refuses_and_starts_nothing(self):
        self.laptop_env()
        os.environ["LANE_LAPTOP_PROBE"] = "false"
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--laptop", "--poll", "0")
        self.assertEqual(3, code, out)
        self.assertIn("laptop unreachable", out)
        self.assertEqual([], self.calls())
        self.assertEqual("created", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])

    def test_probe_hang_counts_as_unreachable(self):
        os.environ["LANE_LAPTOP_PROBE"] = f"{sys.executable} -c 'import time; time.sleep(30)'"
        orig = lane.laptop_reachable
        self.assertFalse(orig(timeout=0.5))

    def test_laptop_or_local_falls_back_to_local_worker(self):
        self.laptop_env()
        os.environ["LANE_LAPTOP_PROBE"] = "false"
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", VALID_WORKER))
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--laptop", "--laptop-or-local")
        self.assertEqual(0, code, out)
        self.assertEqual([], self.calls())
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual(("returned", "local"), (entry["status"], entry["where"]))

    def test_reachable_laptop_is_used_even_with_fallback_flag(self):
        self.laptop_env()
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--laptop", "--laptop-or-local",
                             "--poll", "0")
        self.assertEqual(0, code, out)
        self.assertEqual(["push 01", "run 01", "status 01", "pull 01"], self.calls())

    def test_lost_laptop_while_polling_stops_and_stays_running(self):
        self.laptop_env(variant(STATUS_OK, STATUS_LOST))
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--laptop", "--poll", "0",
                             "--max-poll-failures", "3")
        self.assertEqual(3, code, out)
        self.assertEqual(3, self.calls().count("status 01"))
        self.assertNotIn("pull 01", self.calls())
        entry = lane.load_ledger()["lanes"]["oc-worker-01"]
        self.assertEqual("running", entry["status"])
        self.assertIn("unreachable", entry["note"])

    def test_push_failure_does_not_start_run(self):
        self.laptop_env(variant(PUSH_OK, PUSH_FAIL))
        self.make_lane("oc-worker-01")
        code, out = self.cli("run", "oc-worker-01", str(self.brief()), "--laptop", "--poll", "0")
        self.assertEqual(1, code, out)
        self.assertEqual(["push 01"], self.calls())

    def test_collect_waits_for_end_line(self):
        self.laptop_env(variant(STATUS_OK, STATUS_RUNNING))
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="laptop"))
        code, out = self.cli("collect", "oc-worker-01")
        self.assertEqual(3, code, out)
        self.assertIn("still running", out)
        self.assertNotIn("pull 01", self.calls())

    def test_collect_unreachable_does_nothing(self):
        self.laptop_env()
        os.environ["LANE_LAPTOP_PROBE"] = "false"
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="laptop"))
        code, out = self.cli("collect", "oc-worker-01")
        self.assertEqual(3, code, out)
        self.assertEqual([], self.calls())
        self.assertEqual("running", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])

    def test_collect_after_end_pulls_and_validates(self):
        self.laptop_env()
        self.make_lane("oc-worker-01")
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="running", where="laptop"))
        code, out = self.cli("collect", "oc-worker-01")
        self.assertEqual(0, code, out)
        self.assertEqual(["status 01", "pull 01"], self.calls())
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
        path = self.make_lane("oc-worker-08")  # status created, no commits: would otherwise look merged
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
