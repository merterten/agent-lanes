import json
import re
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lanequeue import (  # noqa: E402
    DEFAULT_STOP_PATTERNS,
    load_queue,
    main,
    run_queue,
)

LANE_A = "oc-worker-01"
LANE_B = "oc-worker-02"
LANE_C = "oc-worker-03"
LANE_D = "oc-worker-04"


def brief(root, name="brief.md"):
    path = Path(root) / name
    path.write_text("brief\n", encoding="utf-8")
    return str(path)


def write_queue(root, entries, name="queue.json"):
    path = Path(root) / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def entry(root, lane, args=None, name=None):
    data = {"lane": lane, "brief": brief(root, name or f"{lane}.md")}
    if args is not None:
        data["args"] = args
    return data


class FakeRunner:
    def __init__(self, log_dir, rc_map=None, text_map=None, delay=0.0):
        self.log_dir = Path(log_dir)
        self.rc_map = rc_map or {}
        self.text_map = text_map or {}
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self, lane, brief_path, args):
        with self.lock:
            self.calls.append(lane)
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)
            self.started.set()
        try:
            self.release.wait(timeout=5)
            if self.delay:
                time.sleep(self.delay)
            path = self.log_dir / f"{lane}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.text_map.get(lane, "all good\n"), encoding="utf-8")
            return self.rc_map.get(lane, 0), str(path)
        finally:
            with self.lock:
                self.current -= 1


class QueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state" / "lanes.json"
        self.state.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def entries(self, *lanes):
        return [entry(self.root, lane) for lane in lanes]

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_load_queue_valid_list(self):
        queue = write_queue(self.root, self.entries(LANE_A, LANE_B))
        loaded = load_queue(queue)
        self.assertEqual([item["lane"] for item in loaded], [LANE_A, LANE_B])
        self.assertTrue(Path(loaded[0]["brief"]).is_file())

    def test_load_queue_accepts_entries_wrapper(self):
        queue = write_queue(self.root, {"entries": self.entries(LANE_A)})
        loaded = load_queue(queue)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["lane"], LANE_A)

    def test_load_queue_resolves_relative_brief_against_queue_dir(self):
        brief(self.root, "shared.md")
        queue = write_queue(
            self.root, [{"lane": LANE_A, "brief": "shared.md"}]
        )
        loaded = load_queue(queue)
        self.assertEqual(Path(loaded[0]["brief"]), self.root / "shared.md")

    def test_load_queue_rejects_invalid_lane(self):
        queue = write_queue(
            self.root, [{"lane": "worker-1", "brief": brief(self.root)}]
        )
        with self.assertRaises(ValueError) as caught:
            load_queue(queue)
        self.assertIn("invalid lane name", str(caught.exception))

    def test_load_queue_rejects_missing_brief(self):
        queue = write_queue(
            self.root,
            [{"lane": LANE_A, "brief": str(self.root / "nope.md")}],
        )
        with self.assertRaises(ValueError) as caught:
            load_queue(queue)
        self.assertIn("brief file not found", str(caught.exception))

    def test_load_queue_rejects_duplicate_lanes(self):
        first = entry(self.root, LANE_A, name="one.md")
        second = entry(self.root, LANE_A, name="two.md")
        queue = write_queue(self.root, [first, second])
        with self.assertRaises(ValueError) as caught:
            load_queue(queue)
        self.assertIn("duplicate lane", str(caught.exception))

    def test_load_queue_reports_all_problems_together(self):
        queue = write_queue(
            self.root,
            [
                {"lane": "bad", "brief": str(self.root / "missing.md")},
                {"lane": LANE_A, "brief": brief(self.root)},
                {"lane": LANE_A, "brief": brief(self.root)},
            ],
        )
        with self.assertRaises(ValueError) as caught:
            load_queue(queue)
        message = str(caught.exception)
        self.assertIn("invalid lane name", message)
        self.assertIn("brief file not found", message)
        self.assertIn("duplicate lane", message)

    def test_load_queue_rejects_non_list(self):
        queue = write_queue(self.root, {"nope": True})
        with self.assertRaises(ValueError):
            load_queue(queue)

    def test_runs_all_lanes_when_clean(self):
        runner = FakeRunner(self.root / "logs")
        summary = run_queue(
            self.entries(LANE_A, LANE_B, LANE_C),
            runner,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(summary["done"], 3)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped"], 0)
        self.assertFalse(summary["quota_stop"])
        self.assertIsNone(summary["stop_reason"])
        self.assertEqual(sorted(runner.calls), sorted([LANE_A, LANE_B, LANE_C]))

    def test_concurrency_never_exceeds_max_parallel(self):
        lanes = [f"oc-worker-{number:02d}" for number in range(10, 20)]
        runner = FakeRunner(self.root / "logs", delay=0.04)
        summary = run_queue(
            self.entries(*lanes),
            runner,
            max_parallel=3,
            state_path=self.state,
            poll=0.01,
        )
        self.assertEqual(summary["done"], len(lanes))
        self.assertLessEqual(runner.max_seen, 3)
        self.assertEqual(runner.max_seen, 3)

    def test_failed_lanes_are_counted(self):
        runner = FakeRunner(
            self.root / "logs", rc_map={LANE_B: 7}, text_map={LANE_B: "boom\n"}
        )
        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            runner,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 0)
        state = self.read_state()
        self.assertEqual(state["entries"][LANE_B]["status"], "failed")
        self.assertEqual(state["entries"][LANE_B]["rc"], 7)

    def test_quota_pattern_stops_new_starts(self):
        runner = FakeRunner(
            self.root / "logs",
            text_map={LANE_A: "ERROR: quota exceeded\n"},
        )
        summary = run_queue(
            self.entries(LANE_A, LANE_B, LANE_C),
            runner,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["skipped"], 2)
        self.assertTrue(summary["quota_stop"])
        self.assertEqual(summary["stop_reason"], "quota")
        state = self.read_state()
        self.assertEqual(state["entries"][LANE_A]["status"], "done")
        self.assertEqual(state["entries"][LANE_B]["status"], "skipped-quota")
        self.assertEqual(state["entries"][LANE_C]["status"], "skipped-quota")

    def test_running_lanes_finish_after_quota_detected(self):
        runner = FakeRunner(self.root / "logs")
        original = runner.__call__

        def run(lane, brief_path, args):
            if lane == LANE_A:
                path = self.root / "logs" / f"{lane}.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("quota exceeded\n", encoding="utf-8")
                with runner.lock:
                    runner.calls.append(lane)
                return 0, str(path)
            if lane == LANE_B:
                with runner.lock:
                    runner.calls.append(lane)
                time.sleep(0.15)
                path = self.root / "logs" / f"{lane}.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("all good\n", encoding="utf-8")
                return 0, str(path)
            return original(lane, brief_path, args)

        summary = run_queue(
            self.entries(LANE_A, LANE_B, LANE_C, LANE_D),
            run,
            max_parallel=2,
            state_path=self.state,
            poll=0.01,
        )
        self.assertEqual(summary["done"], 2)
        self.assertEqual(summary["skipped"], 2)
        self.assertTrue(summary["quota_stop"])
        state = self.read_state()
        self.assertEqual(state["entries"][LANE_B]["status"], "done")
        self.assertEqual(state["entries"][LANE_C]["status"], "skipped-quota")
        self.assertEqual(state["entries"][LANE_D]["status"], "skipped-quota")

    def test_each_default_stop_pattern_is_detected(self):
        samples = {
            "insufficient balance": "Insufficient Balance",
            "rate.?limit": "Rate Limit reached",
            "quota": "quota exceeded",
            r"\b429\b": "HTTP 429",
            "usage limit": "usage limit reached",
            "too many requests": "Too Many Requests",
        }
        self.assertEqual(set(samples), set(DEFAULT_STOP_PATTERNS))
        for pattern, text in samples.items():
            with self.subTest(pattern=pattern):
                root = tempfile.TemporaryDirectory()
                try:
                    root_path = Path(root.name)
                    state = root_path / "state.json"
                    lanes = [
                        {"lane": LANE_A, "brief": brief(root_path, "a.md")},
                        {"lane": LANE_B, "brief": brief(root_path, "b.md")},
                    ]
                    runner = FakeRunner(
                        root_path / "logs", text_map={LANE_A: text + "\n"}
                    )
                    summary = run_queue(
                        lanes,
                        runner,
                        max_parallel=1,
                        state_path=state,
                        poll=0.02,
                    )
                    self.assertTrue(summary["quota_stop"])
                    self.assertEqual(summary["stop_reason"], pattern)
                    self.assertEqual(summary["skipped"], 1)
                finally:
                    root.cleanup()

    def test_no_false_stop_on_clean_logs(self):
        runner = FakeRunner(
            self.root / "logs",
            text_map={
                LANE_A: "job finished successfully\n",
                LANE_B: "all tests passed\n",
            },
        )
        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            runner,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
        )
        self.assertFalse(summary["quota_stop"])
        self.assertEqual(summary["done"], 2)
        self.assertEqual(summary["skipped"], 0)

    def test_custom_stop_patterns_replace_defaults(self):
        runner = FakeRunner(
            self.root / "logs",
            text_map={LANE_A: "quota exceeded\n", LANE_B: "boom happened\n"},
        )
        summary = run_queue(
            self.entries(LANE_A, LANE_B, LANE_C),
            runner,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
            stop_patterns=["boom"],
        )
        self.assertEqual(summary["done"], 2)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["stop_reason"], "boom")
        self.assertEqual(self.read_state()["entries"][LANE_C]["status"], "skipped-quota")

    def test_stop_file_brake_prevents_all_starts(self):
        (self.state.parent / "STOP").write_text("stop\n", encoding="utf-8")
        runner = FakeRunner(self.root / "logs")
        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            runner,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(summary["done"], 0)
        self.assertEqual(summary["skipped"], 2)
        self.assertFalse(summary["quota_stop"])
        self.assertEqual(summary["stop_reason"], "STOP")
        state = self.read_state()
        self.assertEqual(state["entries"][LANE_A]["status"], "skipped-stop")
        self.assertEqual(state["entries"][LANE_B]["status"], "skipped-stop")

    def test_stop_file_created_during_run_skips_remaining(self):
        stop_path = self.state.parent / "STOP"
        runner = FakeRunner(self.root / "logs")

        def run(lane, brief_path, args):
            if lane == LANE_A:
                stop_path.write_text("manual\n", encoding="utf-8")
            return runner(lane, brief_path, args)

        summary = run_queue(
            self.entries(LANE_A, LANE_B, LANE_C),
            run,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["stop_reason"], "STOP")
        state = self.read_state()
        self.assertEqual(state["entries"][LANE_B]["status"], "skipped-stop")
        self.assertEqual(state["entries"][LANE_C]["status"], "skipped-stop")

    def test_resume_does_not_rerun_done_or_failed(self):
        first = FakeRunner(
            self.root / "logs", rc_map={LANE_B: 2}, text_map={LANE_B: "bad\n"}
        )
        entries = self.entries(LANE_A, LANE_B)
        run_queue(
            entries,
            first,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        calls = []

        def should_not_run(lane, brief_path, args):
            calls.append(lane)
            raise AssertionError("resumed lane should not run")

        summary = run_queue(
            entries,
            should_not_run,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(calls, [])
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 0)

    def test_resume_restarts_stale_running_lane(self):
        stale = {
            "lane": LANE_A,
            "brief": brief(self.root, "a.md"),
            "args": [],
            "status": "running",
            "rc": None,
            "log": None,
            "started": "2026-01-01T00:00:00Z",
            "ended": None,
        }
        done = {
            "lane": LANE_B,
            "brief": brief(self.root, "b.md"),
            "args": [],
            "status": "done",
            "rc": 0,
            "log": "/tmp/old.log",
            "started": "2026-01-01T00:00:00Z",
            "ended": "2026-01-01T00:01:00Z",
        }
        self.state.write_text(
            json.dumps({"entries": {LANE_A: stale, LANE_B: done}}),
            encoding="utf-8",
        )
        runner = FakeRunner(self.root / "logs")
        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            runner,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(runner.calls, [LANE_A])
        self.assertEqual(summary["done"], 2)
        state = self.read_state()
        self.assertTrue(state["entries"][LANE_A]["restarted"])
        self.assertEqual(state["entries"][LANE_A]["status"], "done")
        self.assertEqual(state["entries"][LANE_B]["status"], "done")

    def test_state_file_is_valid_json_throughout_run(self):
        lanes = [f"oc-worker-{number:02d}" for number in range(20, 28)]
        runner = FakeRunner(self.root / "logs", delay=0.02)
        errors = []
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                if self.state.exists():
                    try:
                        json.loads(self.state.read_text(encoding="utf-8"))
                    except Exception as exc:
                        errors.append(exc)
                time.sleep(0.001)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            summary = run_queue(
                self.entries(*lanes),
                runner,
                max_parallel=3,
                state_path=self.state,
                poll=0.01,
            )
        finally:
            stop.set()
            watcher.join(timeout=2)
        self.assertEqual(summary["done"], len(lanes))
        self.assertEqual(errors, [])
        json.loads(self.state.read_text(encoding="utf-8"))

    def test_state_timestamps_and_fields(self):
        runner = FakeRunner(self.root / "logs")
        run_queue(
            self.entries(LANE_A),
            runner,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
        )
        record = self.read_state()["entries"][LANE_A]
        stamp = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["rc"], 0)
        self.assertRegex(record["started"], stamp)
        self.assertRegex(record["ended"], stamp)
        self.assertTrue(Path(record["log"]).is_file())

    def test_run_fn_exception_marks_lane_failed_and_continues(self):
        runner = FakeRunner(self.root / "logs")

        def run(lane, brief_path, args):
            if lane == LANE_A:
                raise RuntimeError("worker exploded")
            return runner(lane, brief_path, args)

        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            run,
            max_parallel=2,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["done"], 1)
        state = self.read_state()
        self.assertEqual(state["entries"][LANE_A]["status"], "failed")
        self.assertEqual(state["entries"][LANE_A]["rc"], -1)

    def test_quota_in_failed_lane_still_stops_queue(self):
        runner = FakeRunner(
            self.root / "logs",
            rc_map={LANE_A: 5},
            text_map={LANE_A: "insufficient balance\n"},
        )
        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            runner,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
        )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertTrue(summary["quota_stop"])
        self.assertEqual(summary["stop_reason"], "insufficient balance")

    def test_stop_pattern_outside_last_200_lines_is_ignored(self):
        runner = FakeRunner(self.root / "logs")
        lines = ["quota exceeded\n"] + ["ok\n"] * 249

        def run(lane, brief_path, args):
            if lane == LANE_A:
                path = self.root / "logs" / f"{lane}.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(lines), encoding="utf-8")
                with runner.lock:
                    runner.calls.append(lane)
                return 0, str(path)
            return runner(lane, brief_path, args)

        summary = run_queue(
            self.entries(LANE_A, LANE_B),
            run,
            max_parallel=1,
            state_path=self.state,
            poll=0.02,
        )
        self.assertFalse(summary["quota_stop"])
        self.assertEqual(summary["done"], 2)
        self.assertEqual(summary["skipped"], 0)

    def test_empty_queue_returns_zero_summary(self):
        summary = run_queue(
            [],
            FakeRunner(self.root / "logs"),
            max_parallel=2,
            state_path=self.state,
            poll=0.01,
        )
        self.assertEqual(
            summary,
            {
                "done": 0,
                "failed": 0,
                "skipped": 0,
                "quota_stop": False,
                "stop_reason": None,
            },
        )
        self.assertTrue(self.state.exists())


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state" / "lanes.json"
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.script = Path(__file__).resolve().parent / "lanequeue.py"

    def tearDown(self):
        self._tmp.cleanup()

    def make_program(self, body):
        path = self.root / "program.py"
        path.write_text(body, encoding="utf-8")
        return path

    def run_cli(self, queue, program, max_parallel=2, extra=None):
        command = [
            sys.executable,
            str(self.script),
            str(queue),
            "--state",
            str(self.state),
            "--max",
            str(max_parallel),
            "--poll",
            "0.02",
            "--cmd",
            shlex.join([sys.executable, str(program)]),
        ]
        if extra:
            command.extend(extra)
        import subprocess

        return subprocess.run(
            command, capture_output=True, text=True, timeout=30, shell=False
        )

    def test_cli_success_exit_zero_and_writes_logs(self):
        entries = [
            {"lane": LANE_A, "brief": brief(self.root, "a.md"), "args": ["--x", "1"]},
            {"lane": LANE_B, "brief": brief(self.root, "b.md")},
        ]
        queue = write_queue(self.root, entries)
        program = self.make_program(
            "import sys\n"
            "print('ARGS', *sys.argv[1:])\n"
            "raise SystemExit(0)\n"
        )
        result = self.run_cli(queue, program)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"done": 2', result.stdout)
        log = (self.state.parent / f"{LANE_A}.log").read_text(encoding="utf-8")
        self.assertIn(LANE_A, log)
        self.assertIn("--x", log)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["entries"][LANE_A]["status"], "done")

    def test_cli_failure_exit_one(self):
        entries = [
            {"lane": LANE_A, "brief": brief(self.root, "a.md")},
            {"lane": LANE_B, "brief": brief(self.root, "b.md")},
        ]
        queue = write_queue(self.root, entries)
        program = self.make_program(
            "import sys\n"
            "lane = sys.argv[1]\n"
            "print('running', lane)\n"
            "raise SystemExit(9 if lane.endswith('02') else 0)\n"
        )
        result = self.run_cli(queue, program)
        self.assertEqual(result.returncode, 1, result.stderr)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["entries"][LANE_B]["status"], "failed")
        self.assertEqual(state["entries"][LANE_B]["rc"], 9)

    def test_cli_quota_skip_exit_one(self):
        entries = [
            {"lane": LANE_A, "brief": brief(self.root, "a.md")},
            {"lane": LANE_B, "brief": brief(self.root, "b.md")},
        ]
        queue = write_queue(self.root, entries)
        program = self.make_program(
            "import sys\n"
            "lane = sys.argv[1]\n"
            "print('running', lane)\n"
            "if lane.endswith('01'):\n"
            "    print('quota exceeded')\n"
            "raise SystemExit(0)\n"
        )
        result = self.run_cli(queue, program, max_parallel=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["entries"][LANE_A]["status"], "done")
        self.assertEqual(state["entries"][LANE_B]["status"], "skipped-quota")

    def test_cli_invalid_queue_exit_nonzero(self):
        queue = write_queue(
            self.root, [{"lane": "nope", "brief": str(self.root / "missing.md")}]
        )
        program = self.make_program("raise SystemExit(0)\n")
        result = self.run_cli(queue, program)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid lane name", result.stderr)

    def test_main_returns_zero_for_clean_queue(self):
        entries = [{"lane": LANE_A, "brief": brief(self.root, "a.md")}]
        queue = write_queue(self.root, entries)
        program = self.make_program("raise SystemExit(0)\n")
        rc = main(
            [
                str(queue),
                "--state",
                str(self.state),
                "--max",
                "1",
                "--cmd",
                shlex.join([sys.executable, str(program)]),
            ]
        )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
