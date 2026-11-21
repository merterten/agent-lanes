"""Reviewer (Claude) tests for the lane.py wiring of mutants / queue / review / ask / merge.

Run: python3 -m unittest discover -s tools/lanes -p 'test_lane*.py' -v
"""
import json
import os
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane  # noqa: E402
from test_lane import VALID_WORKER, LaneTest  # noqa: E402

QUOTA_WORKER = VALID_WORKER + '\nprint("Error: usage limit reached for this plan")\n'

ECHO_WORKER = textwrap.dedent('''
    import json, os, sys
    with open(os.environ["FAKE_OC_ARGS"], "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
    print("FINDING 1 | LOW | a.py:1")
    print("something")
    print("check it")
    print("FINDINGS: 1")
''')


class WiringTest(LaneTest):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_OC_ARGS"] = str(self.tmp / "oc-args.jsonl")
        self.addCleanup(os.environ.pop, "FAKE_OC_ARGS", None)

    def oc_calls(self):
        path = self.tmp / "oc-args.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def py_change(self, worktree):
        (worktree / "calc.py").write_text("def ok(x):\n    if x > 3:\n        return True\n    return False\n")


class TestMutantsWiring(WiringTest):
    def test_writes_mutants_json_for_changed_python(self):
        wt = self.make_lane("oc-worker-01")
        self.py_change(wt)
        code, out = self.cli("mutants", "oc-worker-01")
        self.assertEqual(0, code, out)
        rows = json.loads((lane.lane_dir("oc-worker-01") / "mutants.json").read_text())
        self.assertTrue(rows, out)
        self.assertTrue(all(r["file"] == "calc.py" for r in rows))
        self.assertEqual("def ok(x):\n    if x > 3:\n        return True\n    return False\n",
                         (wt / "calc.py").read_text())


class TestQueueWiring(WiringTest):
    def queue(self, lanes):
        entries = [{"lane": name, "brief": str(self.brief())} for name in lanes]
        path = self.tmp / "q.json"
        path.write_text(json.dumps(entries))
        return path

    def test_runs_all_lanes(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", VALID_WORKER))
        for name in ("oc-worker-01", "oc-worker-02"):
            self.make_lane(name)
        code, out = self.cli("queue", str(self.queue(["oc-worker-01", "oc-worker-02"])), "--max", "2")
        self.assertEqual(0, code, out)
        lanes = lane.load_ledger()["lanes"]
        self.assertEqual({"returned"}, {lanes[n]["status"] for n in ("oc-worker-01", "oc-worker-02")})

    def test_quota_message_in_worker_log_stops_new_lanes(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", QUOTA_WORKER))
        for name in ("oc-worker-01", "oc-worker-02"):
            self.make_lane(name)
        code, out = self.cli("queue", str(self.queue(["oc-worker-01", "oc-worker-02"])), "--max", "1")
        self.assertEqual(1, code, out)
        self.assertIn('"quota_stop": true', out)
        self.assertEqual("created", lane.load_ledger()["lanes"]["oc-worker-02"]["status"])


class TestReviewAskWiring(WiringTest):
    def test_review_is_read_only_and_saves_report(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", ECHO_WORKER))
        wt = self.make_lane("oc-worker-01")
        self.py_change(wt)
        code, out = self.cli("review", "oc-worker-01", "--focus", "timeouts")
        self.assertEqual(0, code, out)
        self.assertIn("findings: 1", out)
        call = self.oc_calls()[0]
        self.assertNotIn("--write", call["argv"])
        self.assertEqual(os.path.realpath(wt), os.path.realpath(call["cwd"]))
        self.assertEqual(1, len(list(lane.lane_dir("oc-worker-01").glob("review-*.md"))))
        self.assertFalse((wt / ".lane" / "review").exists())

    def test_ask_defaults_to_main_repo(self):
        os.environ["LANE_OC_BIN"] = str(self.script("fake-oc", ECHO_WORKER))
        code, out = self.cli("ask", "where is X?")
        self.assertEqual(0, code, out)
        call = self.oc_calls()[0]
        self.assertNotIn("--write", call["argv"])
        self.assertEqual(os.path.realpath(self.repo), os.path.realpath(call["cwd"]))
        self.assertEqual(1, len(list((lane.state_dir() / "ask").glob("ask-*.md"))))


class TestMergeWiring(WiringTest):
    def ready(self, name="oc-worker-01", fname="calc.py"):
        wt = self.make_lane(name)
        (wt / fname).write_text("x = 1\n")
        lane.mutate_ledger(lambda l: l[name].update(status="accept-pass"))
        return wt

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_refuses_without_tests(self):
        self.ready()
        before = self.head()
        code, out = self.cli("merge", "oc-worker-01", "-m", "msg")
        self.assertEqual(1, code, out)
        self.assertEqual(before, self.head())

    def test_refuses_unaccepted_lane(self):
        self.ready()
        lane.mutate_ledger(lambda l: l["oc-worker-01"].update(status="returned"))
        before = self.head()
        code, out = self.cli("merge", "oc-worker-01", "-m", "msg", "--test", "true")
        self.assertEqual(1, code, out)
        self.assertIn("accept-pass", out)
        self.assertEqual(before, self.head())

    def test_merges_accepted_lane_and_records_it(self):
        self.ready()
        before = self.head()
        code, out = self.cli("merge", "oc-worker-01", "-m", "add calc", "--test", "true")
        self.assertEqual(0, code, out)
        self.assertNotEqual(before, self.head())
        self.assertTrue((self.repo / "calc.py").exists())
        self.assertEqual("merged", lane.load_ledger()["lanes"]["oc-worker-01"]["status"])

    def test_refuses_overlap_with_other_open_lane(self):
        self.ready("oc-worker-01", "shared.py")
        self.ready("oc-worker-02", "shared.py")
        before = self.head()
        code, out = self.cli("merge", "oc-worker-01", "-m", "msg", "--test", "true")
        self.assertEqual(1, code, out)
        self.assertIn("oc-worker-02", out)
        self.assertEqual(before, self.head())


if __name__ == "__main__":
    unittest.main()
