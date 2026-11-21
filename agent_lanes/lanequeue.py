#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

LANE_RE = re.compile(r"^(cc|oc)-worker-[0-9]{1,3}$")
DEFAULT_STOP_PATTERNS = [
    "insufficient balance",
    "rate.?limit",
    "quota",
    r"\b429\b",
    "usage limit",
    "too many requests",
]
TERMINAL_STATUSES = {"done", "failed"}
STATUSES = {
    "pending",
    "running",
    "done",
    "failed",
    "skipped-quota",
    "skipped-stop",
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_path(value):
    try:
        return Path(os.fspath(value))
    except TypeError:
        return None


def load_queue(path):
    queue_path = Path(path)
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read queue {queue_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid queue JSON in {queue_path}: {exc}") from exc

    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if not isinstance(data, list):
        raise ValueError("queue must be a JSON list of entries")

    problems = []
    entries = []
    seen = {}
    base = queue_path.parent

    for index, item in enumerate(data):
        label = f"entry {index}"
        if not isinstance(item, dict):
            problems.append(f"{label}: must be an object")
            continue

        lane = item.get("lane")
        if not isinstance(lane, str) or not lane:
            problems.append(f"{label}: missing or invalid lane")
            lane_for_message = "<missing>"
        else:
            lane_for_message = lane
            if not LANE_RE.fullmatch(lane):
                problems.append(f"{label}: invalid lane name {lane!r}")
            seen.setdefault(lane, []).append(index)

        brief = item.get("brief")
        brief_path = None
        if not isinstance(brief, str) or not brief:
            problems.append(f"{label} ({lane_for_message}): missing or invalid brief")
        else:
            candidate = Path(brief)
            if not candidate.is_absolute():
                nearby = base / candidate
                if nearby.exists() or not candidate.exists():
                    candidate = nearby
            if candidate.is_file():
                brief_path = str(candidate)
            else:
                problems.append(
                    f"{label} ({lane_for_message}): brief file not found: {brief}"
                )

        args = item.get("args", [])
        if args is None:
            args = []
        if not isinstance(args, list):
            problems.append(f"{label} ({lane_for_message}): args must be a list")
            args = []
        else:
            bad_args = [arg for arg in args if not isinstance(arg, str)]
            if bad_args:
                problems.append(
                    f"{label} ({lane_for_message}): args must contain only strings"
                )

        if brief_path is not None and isinstance(lane, str) and LANE_RE.fullmatch(lane):
            entries.append(
                {"lane": lane, "brief": brief_path, "args": list(args)}
            )

    for lane, indexes in seen.items():
        if len(indexes) > 1:
            places = ", ".join(str(i) for i in indexes)
            problems.append(f"duplicate lane {lane!r} at entries {places}")

    if problems:
        raise ValueError("; ".join(problems))
    return entries


def _load_state(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    records = {}

    def add(record, lane=None):
        if isinstance(record, str):
            record = {"status": record}
        if not isinstance(record, dict):
            return
        record = dict(record)
        if lane is not None:
            record.setdefault("lane", lane)
        name = record.get("lane")
        if isinstance(name, str) and name:
            records[name] = record

    if isinstance(data, list):
        for item in data:
            add(item)
    elif isinstance(data, dict):
        for key in ("entries", "lanes"):
            group = data.get(key)
            if isinstance(group, dict):
                for lane, record in group.items():
                    add(record, lane)
            elif isinstance(group, list):
                for record in group:
                    add(record)
        for lane, record in data.items():
            if lane in ("entries", "lanes"):
                continue
            add(record, lane)
    return records


def _atomic_write_json(path, data, lock):
    path = Path(path)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _tail(path, lines=200):
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "".join(deque(handle, maxlen=lines))
    except (OSError, TypeError, ValueError):
        return ""


def _match_stop(log_path, patterns):
    text = _tail(log_path)
    if not text:
        return None
    for raw, compiled in patterns:
        if compiled.search(text):
            return raw
    return None


def _build_state(order, records):
    entries = {lane: records[lane] for lane in order}
    data = {
        "entries": entries,
        "lanes": {lane: records[lane] for lane in order},
    }
    for lane in order:
        data[lane] = records[lane]
    return data


def run_queue(
    entries,
    run_fn,
    max_parallel=5,
    state_path=None,
    poll=1.0,
    stop_patterns=None,
    on_change=None,
):
    if state_path is None:
        raise ValueError("state_path is required")
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    try:
        max_parallel = int(max_parallel)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_parallel must be an integer") from exc
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")

    state_path = Path(state_path)
    stop_file = state_path.parent / "STOP"
    lock = threading.Lock()

    if stop_patterns is None:
        raw_patterns = list(DEFAULT_STOP_PATTERNS)
    else:
        raw_patterns = list(stop_patterns)
    patterns = [(raw, re.compile(raw, re.IGNORECASE)) for raw in raw_patterns]

    previous = _load_state(state_path)
    order = []
    records = {}
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each entry must be an object")
        lane = item.get("lane")
        if not isinstance(lane, str) or not lane:
            raise ValueError("each entry needs a non-empty lane")
        if lane in seen:
            raise ValueError(f"duplicate lane {lane!r}")
        seen.add(lane)
        brief = item.get("brief", "")
        args = item.get("args", [])
        if args is None:
            args = []
        if not isinstance(args, list):
            raise ValueError(f"args for lane {lane!r} must be a list")
        old = previous.get(lane, {})
        old_status = old.get("status")

        record = {
            "lane": lane,
            "brief": brief,
            "args": list(args),
            "status": "pending",
            "rc": None,
            "log": None,
            "started": None,
            "ended": None,
            "restarted": bool(old.get("restarted", False)),
        }

        if old_status in TERMINAL_STATUSES:
            record["status"] = old_status
            record["rc"] = old.get("rc")
            record["log"] = old.get("log")
            record["started"] = old.get("started")
            record["ended"] = old.get("ended")
        elif old_status == "running":
            record["restarted"] = True
        elif old_status in STATUSES:
            record["status"] = "pending"

        order.append(lane)
        records[lane] = record

    def write(changed):
        data = _build_state(order, records)
        _atomic_write_json(state_path, data, lock)
        if on_change is not None:
            for lane in changed:
                try:
                    on_change(dict(records[lane]))
                except Exception:
                    pass

    def pending_lanes():
        return [lane for lane in order if records[lane]["status"] == "pending"]

    def skip_pending(status, reason):
        changed = []
        for lane in pending_lanes():
            records[lane]["status"] = status
            changed.append(lane)
        if changed:
            write(changed)
        return changed

    write(list(order))

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel)
    active = {}
    stop_reason = None
    quota_stop = False

    try:
        while True:
            finished = [future for future in active if future.done()]
            changed = []
            for future in finished:
                lane = active.pop(future)
                try:
                    result = future.result()
                    if isinstance(result, (tuple, list)) and len(result) >= 2:
                        rc, log_path = result[0], result[1]
                    else:
                        rc, log_path = 1, None
                except Exception:
                    rc, log_path = -1, None

                try:
                    rc = int(rc)
                except (TypeError, ValueError):
                    rc = 1

                record = records[lane]
                record["rc"] = rc
                record["log"] = str(log_path) if log_path is not None else None
                record["ended"] = _now()
                record["status"] = "done" if rc == 0 else "failed"
                changed.append(lane)

                if stop_reason is None and log_path is not None:
                    matched = _match_stop(log_path, patterns)
                    if matched is not None:
                        stop_reason = matched
                        quota_stop = True

            if changed:
                write(changed)

            if quota_stop:
                skip_pending("skipped-quota", stop_reason)

            if not active and not pending_lanes():
                break

            if stop_reason is None and stop_file.exists():
                stop_reason = "STOP"
                quota_stop = False
                skip_pending("skipped-stop", stop_reason)

            if stop_reason is not None:
                skip_pending(
                    "skipped-quota" if quota_stop else "skipped-stop",
                    stop_reason,
                )

            while stop_reason is None and len(active) < max_parallel:
                waiting = pending_lanes()
                if not waiting:
                    break
                lane = waiting[0]
                record = records[lane]
                record["status"] = "running"
                record["rc"] = None
                record["log"] = None
                record["started"] = _now()
                record["ended"] = None
                write([lane])
                future = executor.submit(
                    run_fn,
                    lane,
                    record["brief"],
                    list(record["args"]),
                )
                active[future] = lane

            if not active:
                if not pending_lanes():
                    break
                if stop_reason is not None:
                    continue
                if len(active) >= max_parallel:
                    continue
                continue

            concurrent.futures.wait(
                list(active),
                timeout=max(0.0, float(poll)),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
    finally:
        executor.shutdown(wait=True)

    counts = {"done": 0, "failed": 0, "skipped": 0}
    for lane in order:
        status = records[lane]["status"]
        if status == "done":
            counts["done"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status in ("skipped-quota", "skipped-stop"):
            counts["skipped"] += 1

    return {
        "done": counts["done"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "quota_stop": quota_stop,
        "stop_reason": stop_reason,
    }


def _build_run_fn(program, log_dir):
    def run_fn(lane, brief, args):
        log_path = log_dir / f"{lane}.log"
        command = [*program, lane, brief, *args]
        try:
            with open(log_path, "wb") as handle:
                completed = subprocess.run(
                    command,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                )
            return completed.returncode, str(log_path)
        except OSError as exc:
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"failed to start {program[0]!r}: {exc}\n")
            except OSError:
                pass
            return 127, str(log_path)

    return run_fn


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run lane workers from a queue file.")
    parser.add_argument("queue")
    parser.add_argument("--state", required=True)
    parser.add_argument("--max", type=int, default=5, dest="max_parallel")
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--stop-pattern", action="append", dest="stop_patterns")
    options = parser.parse_args(argv)

    try:
        entries = load_queue(options.queue)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        program = shlex.split(options.cmd)
    except ValueError as exc:
        print(f"error: invalid --cmd: {exc}", file=sys.stderr)
        return 2
    if not program:
        print("error: --cmd is empty", file=sys.stderr)
        return 2

    state_path = Path(options.state)
    log_dir = state_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    def report(record):
        print(
            f"{record.get('lane')}: {record.get('status')} "
            f"rc={record.get('rc')} log={record.get('log')}"
        )

    summary = run_queue(
        entries,
        _build_run_fn(program, log_dir),
        max_parallel=options.max_parallel,
        state_path=state_path,
        poll=options.poll,
        stop_patterns=options.stop_patterns,
        on_change=report,
    )
    print(json.dumps(summary, sort_keys=True))
    if summary["failed"] or summary["skipped"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
