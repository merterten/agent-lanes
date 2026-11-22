"""Tests for multi-machine queue scheduling (AL2) and host capacity output."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane  # noqa: E402
import lanequeue  # noqa: E402
from lanequeue import HostAllocator, load_queue, run_queue  # noqa: E402
from test_lane import LaneTest  # noqa: E402

LANES = [f"oc-worker-{number:02d}" for number in range(1, 10)]


def brief(root, name):
    path = Path(root) / f"{name}.md"
    path.write_text("brief\n", encoding="utf-8")
    return str(path)


def make_entries(root, lanes, **extra):
    return [dict({"lane": name, "brief": brief(root, name)}, **extra) for name in lanes]


class Runner:
    def __init__(self, log_dir, delay=0.0, rc_map=None, text_map=None):
        self.log_dir = Path(log_dir)
        self.delay = delay
        self.rc_map = rc_map or {}
        self.text_map = text_map or {}
        self.calls = []
        self.hosts = {}
        self.max_seen = {}
        self.lock = threading.Lock()

    def __call__(self, lane_name, brief_path, args, host="local"):
        with self.lock:
            self.calls.append((lane_name, host))
            self.hosts[lane_name] = host
            self.max_seen[host] = max(self.max_seen.get(host, 0), self.hosts_seen(host) + 1)
            self._active = getattr(self, "_active", {})
            self._active[host] = self._active.get(host, 0) + 1
        try:
            if self.delay:
                time.sleep(self.delay)
            path = self.log_dir / f"{lane_name}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.text_map.get(lane_name, "all good\n"), encoding="utf-8")
            return self.rc_map.get(lane_name, 0), str(path)
        finally:
            with self.lock:
                self._active[host] -= 1

    def hosts_seen(self, host):
        active = getattr(self, "_active", {})
        return active.get(host, 0)


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state" / "queue.json"
        self.state.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def run_q(self, entries, assign, capacities, runner=None, **kwargs):
        runner = runner or Runner(self.root / "logs")
        summary = run_queue(
            entries,
            runner,
            max_parallel=5,
            state_path=self.state,
            poll=0.01,
            assign=assign,
            capacities=capacities,
            **kwargs,
        )
        return summary, runner

    def state_entries(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["entries"]


class CapacityTests(QueueTestCase):
    def test_single_machine_default_is_unchanged(self):
        entries = make_entries(self.root, LANES[:4])
        runner = Runner(self.root / "logs", delay=0.02)
        summary = run_queue(entries, runner, max_parallel=2, state_path=self.state, poll=0.01)
        self.assertEqual(4, summary["done"])
        self.assertEqual(0, summary["skipped"])
        self.assertLessEqual(max(runner.max_seen.values()), 2)
        self.assertEqual({"local"}, set(runner.hosts.values()))
        for record in self.state_entries().values():
            self.assertEqual("local", record["host"])

    def test_capacity_per_host_is_respected(self):
        entries = make_entries(self.root, LANES[:8])
        allocator = HostAllocator({"local": 1, "gpu-box": 2, "laptop": 1})
        runner = Runner(self.root / "logs", delay=0.03)
        summary, runner = self.run_q(entries, allocator, allocator.capacities, runner=runner)
        self.assertEqual(8, summary["done"])
        self.assertLessEqual(runner.max_seen.get("local", 0), 1)
        self.assertLessEqual(runner.max_seen.get("gpu-box", 0), 2)
        self.assertLessEqual(runner.max_seen.get("laptop", 0), 1)
        self.assertEqual(2, runner.max_seen.get("gpu-box", 0))

    def test_pinned_lane_uses_its_host(self):
        entry = make_entries(self.root, [LANES[0]], host="gpu-box")[0]
        allocator = HostAllocator({"local": 1, "gpu-box": 1})
        summary, runner = self.run_q([entry], allocator, allocator.capacities)
        self.assertEqual(1, summary["done"])
        self.assertEqual("gpu-box", runner.hosts[LANES[0]])

    def test_pinned_local_uses_local(self):
        entry = make_entries(self.root, [LANES[0]], host="local")[0]
        allocator = HostAllocator({"local": 1, "gpu-box": 1})
        summary, runner = self.run_q([entry], allocator, allocator.capacities)
        self.assertEqual(1, summary["done"])
        self.assertEqual("local", runner.hosts[LANES[0]])

    def test_pinned_host_capacity_serializes_two_lanes(self):
        entries = make_entries(self.root, LANES[:2], host="gpu-box")
        allocator = HostAllocator({"local": 0, "gpu-box": 1})
        runner = Runner(self.root / "logs", delay=0.03)
        summary, runner = self.run_q(entries, allocator, allocator.capacities, runner=runner)
        self.assertEqual(2, summary["done"])
        self.assertEqual(1, runner.max_seen["gpu-box"])
        self.assertEqual(2, len(runner.calls))

    def test_zero_capacity_host_gets_nothing(self):
        entries = make_entries(self.root, LANES[:1])
        probes = []

        def probe(name):
            probes.append(name)
            return True

        allocator = HostAllocator({"local": 1, "gpu-box": 0}, probe=probe)
        summary, runner = self.run_q(entries, allocator, allocator.capacities)
        self.assertEqual(1, summary["done"])
        self.assertEqual("local", runner.hosts[LANES[0]])
        self.assertNotIn("gpu-box", probes)

    def test_run_queue_enforces_capacity_even_for_a_bad_assigner(self):
        entries = make_entries(self.root, LANES[:3])

        def greedy(lane_name, record, usage):
            return "gpu-box"

        runner = Runner(self.root / "logs", delay=0.03)
        summary, runner = self.run_q(entries, greedy, {"local": 1, "gpu-box": 1}, runner=runner)
        self.assertEqual(3, summary["done"])
        self.assertEqual(1, runner.max_seen["gpu-box"])

    def test_allocator_itself_returns_none_when_all_capacities_are_full(self):
        allocator = HostAllocator({"local": 1, "gpu-box": 1},
                                  probe=lambda name: True)
        self.assertEqual("gpu-box", allocator(LANES[0], {}, {}))
        self.assertEqual("local", allocator(LANES[1], {}, {"gpu-box": 1}))
        self.assertIsNone(allocator(LANES[2], {}, {"gpu-box": 1, "local": 1}))

    def test_negative_capacity_is_rejected(self):
        with self.assertRaises(ValueError):
            run_queue([], Runner(self.root / "logs"), state_path=self.state,
                      capacities={"local": -1})


class ReachabilityTests(QueueTestCase):
    def test_unreachable_host_gets_no_lanes(self):
        entries = make_entries(self.root, LANES[:2])
        allocator = HostAllocator({"local": 2, "gpu-box": 2}, probe=lambda name: False)
        summary, runner = self.run_q(entries, allocator, allocator.capacities)
        self.assertEqual(2, summary["done"])
        self.assertEqual({"local"}, set(runner.hosts.values()))

    def test_pinned_unreachable_without_fallback_is_not_run(self):
        entry = make_entries(self.root, [LANES[0]], host="gpu-box")[0]
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: False)
        summary, runner = self.run_q([entry], allocator, allocator.capacities)
        self.assertEqual([], runner.calls)
        self.assertEqual(1, summary["skipped"])
        self.assertEqual("skipped-host", self.state_entries()[LANES[0]]["status"])

    def test_pinned_unreachable_with_fallback_runs_locally(self):
        entry = make_entries(self.root, [LANES[0]], host="gpu-box",
                             fallback_local=True)[0]
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: False)
        summary, runner = self.run_q([entry], allocator, allocator.capacities)
        self.assertEqual(1, summary["done"])
        self.assertEqual("local", runner.hosts[LANES[0]])
        self.assertEqual("local", self.state_entries()[LANES[0]]["host"])

    def test_unpinned_prefers_a_reachable_remote_over_local(self):
        entries = make_entries(self.root, LANES[:2])
        allocator = HostAllocator({"local": 5, "gpu-box": 2}, probe=lambda name: True)
        summary, runner = self.run_q(entries, allocator, allocator.capacities)
        self.assertEqual(2, summary["done"])
        self.assertEqual({"gpu-box"}, set(runner.hosts.values()))

    def test_probe_failure_is_retried_before_the_next_assignment(self):
        entries = make_entries(self.root, LANES[:1]) + make_entries(
            self.root, [LANES[1]], host="gpu-box")
        calls = iter([False, False, True])
        probes = []

        def probe(name):
            probes.append(name)
            return next(calls, False)

        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=probe)
        summary, runner = self.run_q(entries, allocator, allocator.capacities)
        self.assertEqual(2, summary["done"])
        self.assertEqual("local", runner.hosts[LANES[0]])
        self.assertEqual("gpu-box", runner.hosts[LANES[1]])
        self.assertEqual(3, probes.count("gpu-box"))

    def test_run_queue_primes_each_remote_once_at_start(self):
        entries = make_entries(self.root, LANES[:2], host="gpu-box")
        probes = []

        def probe(name):
            probes.append(name)
            return True

        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=probe)
        summary, runner = self.run_q(entries, allocator, allocator.capacities)
        self.assertEqual(2, summary["done"])
        self.assertEqual(1, probes.count("gpu-box"))

    def test_successful_probe_is_not_repeated(self):
        probes = []

        def probe(name):
            probes.append(name)
            return True

        allocator = HostAllocator({"local": 1, "gpu-box": 2}, probe=probe)
        self.assertEqual("gpu-box", allocator(LANES[0], {}, {}))
        self.assertEqual("gpu-box", allocator(LANES[1], {}, {"gpu-box": 1}))
        self.assertEqual(1, probes.count("gpu-box"))

    def test_failed_probe_is_retried_on_each_assign_call(self):
        probes = []

        def probe(name):
            probes.append(name)
            return False

        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=probe)
        self.assertEqual("local", allocator(LANES[0], {}, {}))
        self.assertIsNone(allocator(LANES[1], {"host": "gpu-box"}, {}))
        self.assertEqual(2, probes.count("gpu-box"))


class BrakeAndStateTests(QueueTestCase):
    def test_quota_stop_works_across_hosts(self):
        entries = make_entries(self.root, LANES[:3])
        allocator = HostAllocator({"local": 0, "gpu-box": 1}, probe=lambda name: True)
        runner = Runner(self.root / "logs", text_map={LANES[0]: "quota exceeded\n"})
        summary, runner = self.run_q(entries, allocator, allocator.capacities, runner=runner)
        self.assertEqual(1, summary["done"])
        self.assertEqual(2, summary["skipped"])
        self.assertTrue(summary["quota_stop"])
        self.assertEqual(["skipped-quota", "skipped-quota"],
                         [self.state_entries()[lane]["status"] for lane in LANES[1:3]])

    def test_stop_file_works_across_hosts(self):
        (self.state.parent / "STOP").write_text("stop\n", encoding="utf-8")
        entries = make_entries(self.root, LANES[:2])
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: True)
        summary, runner = self.run_q(entries, allocator, allocator.capacities)
        self.assertEqual([], runner.calls)
        self.assertEqual(2, summary["skipped"])
        self.assertEqual("STOP", summary["stop_reason"])

    def test_state_records_the_host_for_each_lane(self):
        entries = make_entries(self.root, [LANES[0]], host="local") + make_entries(
            self.root, [LANES[1]], host="gpu-box")
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: True)
        self.run_q(entries, allocator, allocator.capacities)
        state = self.state_entries()
        self.assertEqual("local", state[LANES[0]]["host"])
        self.assertEqual("gpu-box", state[LANES[1]]["host"])

    def test_resume_keeps_host_and_does_not_rerun_done_lane(self):
        done = {"lane": LANES[0], "brief": brief(self.root, LANES[0]), "args": [],
                "status": "done", "rc": 0, "log": None, "started": "2026-01-01T00:00:00Z",
                "ended": "2026-01-01T00:01:00Z", "host": "gpu-box"}
        self.state.write_text(json.dumps({"entries": {LANES[0]: done}}), encoding="utf-8")
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: True)
        summary, runner = self.run_q(make_entries(self.root, [LANES[0]]), allocator,
                                   allocator.capacities)
        self.assertEqual([], runner.calls)
        self.assertEqual(1, summary["done"])
        self.assertEqual("gpu-box", self.state_entries()[LANES[0]]["host"])


class QueueFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, entries):
        path = self.root / "queue.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def test_load_queue_keeps_host_and_fallback(self):
        path = self.write([{"lane": LANES[0], "brief": brief(self.root, LANES[0]),
                            "host": "gpu-box", "fallback_local": True}])
        loaded = load_queue(path)
        self.assertEqual("gpu-box", loaded[0]["host"])
        self.assertTrue(loaded[0]["fallback_local"])

    def test_load_queue_rejects_empty_host(self):
        path = self.write([{"lane": LANES[0], "brief": brief(self.root, LANES[0]),
                            "host": ""}])
        with self.assertRaises(ValueError) as caught:
            load_queue(path)
        self.assertIn("host must be", str(caught.exception))

    def test_load_queue_rejects_non_bool_fallback(self):
        path = self.write([{"lane": LANES[0], "brief": brief(self.root, LANES[0]),
                            "fallback_local": "yes"}])
        with self.assertRaises(ValueError) as caught:
            load_queue(path)
        self.assertIn("fallback_local", str(caught.exception))


class LaneWiringTests(LaneTest):
    def write_many_hosts(self, specs):
        lines = []
        for name, max_lanes in specs:
            lines += [f"[hosts.{name}]", 'ssh = "user@203.0.113.10"',
                      'root = "~/agent-lanes/worktrees"', f"max_lanes = {max_lanes}"]
        Path(os.environ["LANE_HOSTS"]).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def queue_file(self, lanes):
        entries = [{"lane": name, "brief": str(self.brief())} for name in lanes]
        path = self.tmp / "queue.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def test_hosts_shows_used_and_free_capacity_from_ledger(self):
        self.write_many_hosts([("gpu-box", 2)])
        self.fake_remote()

        def seed(lanes):
            lanes["oc-worker-01"] = {"status": "running", "where": "gpu-box"}
            lanes["oc-worker-02"] = {"status": "running", "where": "gpu-box"}

        lane.mutate_ledger(seed)
        with mock.patch.object(lane.remote, "probe", return_value=True):
            code, out = self.cli("hosts")
        self.assertEqual(0, code, out)
        self.assertIn("USED", out)
        self.assertIn("FREE", out)
        row = [line for line in out.splitlines() if line.startswith("gpu-box")][0]
        cells = row.split()
        self.assertIn("2", cells)
        self.assertIn("0", cells)

    def test_cmd_queue_without_hosts_keeps_single_machine_arguments(self):
        captured = {}

        def fake_run(entries, run_fn, **kwargs):
            captured.update(kwargs)
            return {"done": 0, "failed": 0, "skipped": 0,
                    "quota_stop": False, "stop_reason": None}

        with mock.patch.object(lanequeue, "run_queue", side_effect=fake_run):
            code, out = self.cli("queue", str(self.queue_file(["oc-worker-01"])))
        self.assertEqual(0, code, out)
        self.assertIsNone(captured["assign"])
        self.assertIsNone(captured["capacities"])

    def test_cmd_queue_hosts_builds_capacities_and_allocator(self):
        self.write_many_hosts([("gpu-box", 2), ("laptop", 1)])
        captured = {}

        def fake_run(entries, run_fn, **kwargs):
            captured.update(kwargs)
            captured["entries"] = entries
            return {"done": 0, "failed": 0, "skipped": 0,
                    "quota_stop": False, "stop_reason": None}

        with mock.patch.object(lanequeue, "run_queue", side_effect=fake_run):
            code, out = self.cli("queue", str(self.queue_file(["oc-worker-01"])),
                                 "--hosts", "local,gpu-box,laptop", "--max", "3")
        self.assertEqual(0, code, out)
        self.assertEqual({"local": 3, "gpu-box": 2, "laptop": 1}, captured["capacities"])
        self.assertIsInstance(captured["assign"], HostAllocator)
        self.assertEqual(["gpu-box", "laptop"], captured["assign"].remote_order)
        with mock.patch.object(lane.remote, "probe", return_value=True):
            self.assertEqual("gpu-box", captured["assign"]("oc-worker-01", {}, {}))

    def test_cmd_queue_refuses_unknown_host(self):
        self.write_many_hosts([("gpu-box", 2)])
        code, out = self.cli("queue", str(self.queue_file(["oc-worker-01"])),
                             "--hosts", "local,missing")
        self.assertEqual(1, code, out)
        self.assertIn("unknown host", out)


if __name__ == "__main__":
    unittest.main()


class ReviewerAllocatorCapacityTest(unittest.TestCase):
    """Reviewer: the allocator itself refuses a full pinned host (run_queue's guard is a second line)."""

    def test_full_pinned_host_is_not_returned(self):
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: True)
        record = {"host": "gpu-box", "fallback_local": False}
        self.assertEqual("gpu-box", allocator("oc-worker-01", record, {"local": 0, "gpu-box": 0}))
        self.assertIsNone(allocator("oc-worker-02", record, {"local": 0, "gpu-box": 1}))

    def test_full_pinned_host_falls_back_to_local_only_when_allowed(self):
        allocator = HostAllocator({"local": 1, "gpu-box": 1}, probe=lambda name: True)
        self.assertEqual("local", allocator("oc-worker-02", {"host": "gpu-box", "fallback_local": True},
                                            {"local": 0, "gpu-box": 1}))
