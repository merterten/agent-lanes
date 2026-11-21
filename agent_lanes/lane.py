#!/usr/bin/env python3
"""Run and check DeepSeek worker lanes.

The reviewer uses this tool to create isolated git worktrees ("lanes"), run a
worker in one, validate the result the worker wrote, and garbage-collect old
worktrees.  State lives outside every worktree in $LANE_ROOT/lanes so a
worker cannot forge the ledger.

Every external command is a subprocess argument list (never shell=True) and
every path can be redirected with an environment variable, so the tests only
touch temporary directories and temporary git repositories.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_ROOT = "~/agent-lanes"
LANE_RE = re.compile(r"^(cc|oc)-worker-[0-9]{1,3}$")
LAPTOP_LANE_RE = re.compile(r"^oc-worker-[0-9]{2}$")
VALID_STATUSES = ("created", "running", "returned", "no-result", "accept-pass",
                  "accept-fail", "merged", "rejected", "removed")
RESULT_STATUSES = ("done", "partial", "blocked")
REQUIRED_KEYS = ("lane", "status", "summary", "files_changed", "tests", "claims", "blockers")
LANE_DIRS = (".lane", ".lane.prev")
ALLOWED_IGNORED = frozenset({"bin", "obj", "__pycache__", ".pytest_cache", ".lane", ".lane.prev"})
SUMMARY_TAIL_LINES = 15
# ledger states that mean "someone is still working on / judging this lane": gc never removes these
OPEN_STATUSES = frozenset({"created", "running", "returned", "no-result", "accept-pass", "accept-fail"})
DEFAULT_LAPTOP_PROBE = "ssh -o ConnectTimeout=5 -o BatchMode=yes laptop exit 0"
UNREACHABLE = "unreachable"


# ---------------------------------------------------------------------------
# paths (all overridable so tests never touch the real tree)
# ---------------------------------------------------------------------------

def lanes_root():
    return Path(os.environ.get("LANE_ROOT") or DEFAULT_ROOT).expanduser()


def main_repo():
    value = os.environ.get("LANE_MAIN_REPO")
    return Path(value).expanduser() if value else lanes_root() / "repo"


def worktrees_root():
    value = os.environ.get("LANE_WORKTREES")
    return Path(value).expanduser() if value else lanes_root() / "worktrees"


def state_dir():
    value = os.environ.get("LANE_STATE_DIR")
    return Path(value).expanduser() if value else lanes_root() / "lanes"


def worktree_path(lane):
    return worktrees_root() / lane


def lane_dir(lane):
    return state_dir() / lane


def ledger_path():
    return state_dir() / "ledger.json"


def ledger_lock_path():
    return state_dir() / "ledger.lock"


def lanes_md_path():
    return state_dir() / "LANES.md"


def contract_path():
    return Path(__file__).resolve().parent / "contract.txt"


def oc_bin():
    return os.environ.get("LANE_OC_BIN") or "oc-worker"


def cc_bin():
    return os.environ.get("LANE_CC_BIN") or "cc-worker"


def worker_bin(worker):
    return oc_bin() if worker == "oc-worker" else cc_bin()


def laptop_bin():
    return os.environ.get("LANE_LAPTOP_BIN") or "oc-laptop"


def laptop_probe_argv():
    return shlex.split(os.environ.get("LANE_LAPTOP_PROBE") or DEFAULT_LAPTOP_PROBE)


def laptop_reachable(timeout=15):
    """True when the laptop answers within a few seconds (some networks block it)."""
    try:
        proc = subprocess.run(laptop_probe_argv(), capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def valid_lane(lane):
    return bool(LANE_RE.match(lane or ""))


def lane_worker(lane):
    return "oc-worker" if lane.startswith("oc-worker-") else "cc-worker"


def lane_number(lane):
    return lane.rsplit("-", 1)[-1]


def short_sha(sha):
    return (sha or "")[:10]


def unique_path(path):
    if not path.exists():
        return path
    stamp = path.stem
    suffix = path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stamp}-{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def is_lane_path(path):
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path in LANE_DIRS or any(path.startswith(d + "/") for d in LANE_DIRS)


def norm_path(path):
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def run_git(args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)


def head_sha(worktree):
    proc = run_git(["rev-parse", "HEAD"], cwd=worktree)
    return proc.stdout.strip() if proc.returncode == 0 else None


def branch_exists(branch):
    proc = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                   cwd=main_repo())
    return proc.returncode == 0


def tail_lines(path, count=SUMMARY_TAIL_LINES):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-count:]


# ---------------------------------------------------------------------------
# ledger: locked read-modify-write, atomic replace, LANES.md regeneration
# ---------------------------------------------------------------------------

def load_ledger():
    path = ledger_path()
    if not path.exists():
        return {"lanes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read ledger {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        raise RuntimeError(f"ledger {path} is not a {{'lanes': {{...}}}} object")
    return data


@contextlib.contextmanager
def ledger_lock():
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(ledger_lock_path(), "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def md_cell(value):
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_lanes_md(data):
    lanes = data.get("lanes", {})
    ordered = sorted(lanes.items(), key=lambda item: item[1].get("updated", ""), reverse=True)
    lines = [
        "# Lanes",
        "",
        "| Lane | Status | Branch | Base | Worker | Where | Updated | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, entry in ordered:
        lines.append("| " + " | ".join(md_cell(v) for v in (
            name,
            entry.get("status", ""),
            entry.get("branch", ""),
            short_sha(entry.get("base")),
            entry.get("worker", ""),
            entry.get("where", ""),
            entry.get("updated", ""),
            entry.get("note", ""),
        )) + " |")
    return "\n".join(lines) + "\n"


def write_lanes_md(data):
    path = lanes_md_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(render_lanes_md(data), encoding="utf-8")
    os.replace(tmp, path)


def mutate_ledger(fn):
    state_dir().mkdir(parents=True, exist_ok=True)
    with ledger_lock():
        data = load_ledger()
        lanes = data.setdefault("lanes", {})
        result = fn(lanes)
        write_json_atomic(ledger_path(), data)
        write_lanes_md(data)
        return result


def update_entry(lanes, lane, **fields):
    entry = lanes.setdefault(lane, {})
    entry.update(fields)
    entry["updated"] = utc_now()
    return entry


# ---------------------------------------------------------------------------
# result validation
# ---------------------------------------------------------------------------

def real_changes(worktree, base):
    """Tracked changes against base plus untracked non-ignored files, minus .lane/."""
    proc = run_git(["diff", "--name-only", str(base)], cwd=worktree)
    if proc.returncode != 0:
        return None
    paths = {norm_path(line) for line in proc.stdout.splitlines() if line.strip()}
    proc = run_git(["ls-files", "--others", "--exclude-standard"], cwd=worktree)
    if proc.returncode == 0:
        paths.update(norm_path(line) for line in proc.stdout.splitlines() if line.strip())
    return {p for p in paths if p and not is_lane_path(p)}


def _check_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_result(worktree, lane, base):
    """Return (result, problems).  Fatal problems mean result is None."""
    worktree = Path(worktree)
    path = worktree / ".lane" / "LANE_RESULT.json"
    if not path.is_file():
        return None, ["missing .lane/LANE_RESULT.json"]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"cannot read LANE_RESULT.json: {exc}"]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, [f"invalid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["LANE_RESULT.json is not a JSON object"]

    fatal = []
    expected = {
        "lane": (str, "a string"),
        "status": (str, "a string"),
        "summary": (str, "a string"),
        "files_changed": (list, "a list"),
        "tests": (list, "a list"),
        "claims": (list, "a list"),
        "blockers": (list, "a list"),
    }
    for key in REQUIRED_KEYS:
        typ, label = expected[key]
        if key not in data:
            fatal.append(f"missing key: {key}")
        elif not isinstance(data[key], typ):
            fatal.append(f"{key} must be {label}")

    if isinstance(data.get("lane"), str) and data["lane"] != lane:
        fatal.append(f"lane mismatch: result says {data['lane']!r}, expected {lane!r}")

    if isinstance(data.get("files_changed"), list):
        for i, item in enumerate(data["files_changed"]):
            if not isinstance(item, str):
                fatal.append(f"files_changed[{i}] must be a string")
    if isinstance(data.get("blockers"), list):
        for i, item in enumerate(data["blockers"]):
            if not isinstance(item, str):
                fatal.append(f"blockers[{i}] must be a string")
    if isinstance(data.get("tests"), list):
        for i, test in enumerate(data["tests"]):
            if not isinstance(test, dict):
                fatal.append(f"tests[{i}] must be an object")
                continue
            if not isinstance(test.get("cmd"), str):
                fatal.append(f"tests[{i}].cmd must be a string")
            for key in ("passed", "failed"):
                if not _check_int(test.get(key)):
                    fatal.append(f"tests[{i}].{key} must be an int")

    if fatal:
        return None, fatal

    problems = []
    if data["status"] not in RESULT_STATUSES:
        problems.append(f"status not in done/partial/blocked: {data['status']}")

    report = worktree / ".lane" / "REPORT.md"
    if not report.is_file():
        problems.append("missing .lane/REPORT.md")
    else:
        try:
            lines = len(report.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError as exc:
            problems.append(f"cannot read .lane/REPORT.md: {exc}")
        else:
            if lines > 200:
                problems.append(f"REPORT.md has {lines} lines (max 200)")

    if base:
        real = real_changes(worktree, base)
        if real is None:
            problems.append(f"could not compute change set against {base}")
        else:
            listed = {norm_path(p) for p in data["files_changed"] if not is_lane_path(p)}
            only_listed = sorted(listed - real)
            only_real = sorted(real - listed)
            if only_listed:
                problems.append("listed but not changed: " + ", ".join(only_listed))
            if only_real:
                problems.append("changed but not listed: " + ", ".join(only_real))

    return data, problems


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def print_summary(lane, rc, result, problems, log_path):
    print(f"lane: {lane}")
    print(f"rc: {rc}")
    if result is None:
        print("result: no valid result")
    else:
        print("result: valid")
        print(f"status: {result.get('status')}")
        print(f"summary: {result.get('summary')}")
    print(f"files_changed: {len(result.get('files_changed', [])) if result else 0}")
    if result:
        tests = result.get("tests", [])
        passed = sum(t.get("passed", 0) for t in tests if isinstance(t, dict))
        failed = sum(t.get("failed", 0) for t in tests if isinstance(t, dict))
        print(f"tests claimed: {len(tests)} (passed={passed}, failed={failed})")
        blockers = result.get("blockers", [])
        print("blockers: " + ("; ".join(blockers) if blockers else "none"))
    if problems:
        print("validation problems:")
        for problem in problems[:6]:
            print(f"  - {problem}")
        if len(problems) > 6:
            print(f"  - ... {len(problems) - 6} more")
    if log_path:
        print(f"log: {log_path}")
        if result is None:
            tail = tail_lines(log_path)
            if tail:
                print("log tail:")
                for line in tail:
                    print(line)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_new(args):
    lane = args.lane
    if not valid_lane(lane):
        print(f"invalid lane name: {lane!r} (expected cc-worker-N or oc-worker-N)")
        return 1
    worktree = worktree_path(lane)
    branch = args.branch or f"agent/{lane}"
    if worktree.exists():
        print(f"refused: worktree folder exists: {worktree}")
        return 1
    if branch_exists(branch):
        print(f"refused: branch already exists: {branch}")
        return 1
    repo = main_repo()
    if not repo.is_dir():
        print(f"refused: main repo not found: {repo}")
        return 1
    resolved = run_git(["rev-parse", "--verify", f"{args.from_ref}^{{commit}}"], cwd=repo)
    if resolved.returncode != 0:
        print(f"refused: cannot resolve ref {args.from_ref!r}: {resolved.stderr.strip()}")
        return 1
    base = resolved.stdout.strip()
    worktrees_root().mkdir(parents=True, exist_ok=True)
    proc = run_git(["worktree", "add", "-b", branch, str(worktree), args.from_ref], cwd=repo)
    if proc.returncode != 0:
        print(f"refused: git worktree add failed: {proc.stderr.strip()}")
        return 1
    now = utc_now()

    def fn(lanes):
        lanes[lane] = {
            "status": "created",
            "base": base,
            "branch": branch,
            "worktree": str(worktree),
            "created": now,
            "updated": now,
        }

    mutate_ledger(fn)
    print(f"created {lane} on {branch} at {short_sha(base)} -> {worktree}")
    return 0


def move_lane_aside(worktree):
    current = worktree / ".lane"
    previous = worktree / ".lane.prev"
    if not current.exists() and not current.is_symlink():
        return
    if previous.exists() or previous.is_symlink():
        if previous.is_dir() and not previous.is_symlink():
            shutil.rmtree(previous)
        else:
            previous.unlink()
    current.rename(previous)


def build_task(lane, brief_text):
    contract = contract_path().read_text(encoding="utf-8").replace("{LANE}", lane)
    return brief_text.rstrip("\n") + "\n\n" + contract.rstrip("\n") + "\n"


def _append(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def cmd_run(args):
    lane = args.lane
    if not valid_lane(lane):
        print(f"invalid lane name: {lane!r}")
        return 1
    worker = f"{args.worker}-worker" if args.worker else lane_worker(lane)
    if args.luxalgo and worker != "oc-worker":
        print("refused: --luxalgo is only valid for oc-worker")
        return 1
    if args.laptop and not LAPTOP_LANE_RE.match(lane):
        print("refused: --laptop needs an oc-worker-NN lane with exactly two digits")
        return 1

    worktree = worktree_path(lane)
    if not worktree.is_dir():
        print(f"refused: worktree not found: {worktree}")
        return 1
    brief = Path(args.brief_file)
    if not brief.is_file():
        print(f"refused: brief file not found: {brief}")
        return 1
    brief_text = brief.read_text(encoding="utf-8")

    if args.laptop and not laptop_reachable():
        if args.laptop_or_local:
            print("laptop unreachable: running this lane locally instead (--laptop-or-local)")
            args.laptop = False
        else:
            print("refused: laptop unreachable (ssh probe failed). Nothing was started. "
                  "Use --laptop-or-local to run here, or retry from a network that reaches the laptop.")
            return 3

    entry = load_ledger().get("lanes", {}).get(lane, {})
    base = entry.get("base") or head_sha(worktree)
    move_lane_aside(worktree)

    task = build_task(lane, brief_text)
    out_dir = lane_dir(lane)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = file_stamp()
    task_file = unique_path(out_dir / f"task-{stamp}.txt")
    task_file.write_text(task, encoding="utf-8")
    log_path = unique_path(out_dir / f"run-{stamp}.log")
    where = "laptop" if args.laptop else "local"

    def started(lanes):
        fields = {"status": "running", "worker": worker, "where": where,
                  "log": str(log_path), "worktree": str(worktree), "started": utc_now()}
        if base:
            fields["base"] = base
        update_entry(lanes, lane, **fields)

    mutate_ledger(started)

    rc = _execute_run(args, lane, worker, worktree, task, log_path)

    if rc == UNREACHABLE:
        def lost(lanes):
            update_entry(lanes, lane, status="running",
                         note="laptop unreachable while waiting; lane may still run there - `lane collect` later")
        mutate_ledger(lost)
        print(f"lane: {lane}")
        print("laptop became unreachable while waiting; the lane may still be running there.")
        print(f"run `lane collect {lane}` from a network that reaches the laptop")
        print(f"log: {log_path}")
        return 3

    if args.laptop and args.no_wait:
        print(f"lane: {lane}")
        print("started on laptop (no-wait); result not collected")
        print(f"log: {log_path}")
        return 0

    result, problems = validate_result(worktree, lane, base)
    status = "returned" if result is not None else "no-result"

    def finished(lanes):
        fields = {"status": status, "rc": rc, "worker": worker, "where": where,
                  "log": str(log_path), "worktree": str(worktree), "ended": utc_now()}
        if base:
            fields["base"] = base
        if result is not None:
            fields["result_status"] = result.get("status")
        update_entry(lanes, lane, **fields)
        if result is None:
            lanes[lane].pop("result_status", None)

    mutate_ledger(finished)
    print_summary(lane, rc, result, problems, log_path)
    return 0 if result is not None else 1


def _execute_run(args, lane, worker, worktree, task, log_path):
    if not args.laptop:
        argv = [worker_bin(worker)]
        if args.luxalgo:
            argv.append("--luxalgo")
        argv.append("--write")
        if args.model:
            argv += ["-m", args.model]
        argv.append(task)
        try:
            with open(log_path, "wb") as fh:
                proc = subprocess.run(argv, cwd=str(worktree), stdout=fh, stderr=subprocess.STDOUT)
            return proc.returncode
        except OSError as exc:
            _append(log_path, f"failed to start {argv[0]}: {exc}\n")
            return 127

    number = lane_number(lane)
    binary = laptop_bin()
    run_argv = [binary, "run", number]
    if args.model:
        run_argv += ["-m", args.model]
    try:
        with open(log_path, "ab") as fh:
            pushed = subprocess.run([binary, "push", number], cwd=str(worktree),
                                    stdout=fh, stderr=subprocess.STDOUT)
            if pushed.returncode != 0:
                _append(log_path, f"\nlane: {binary} push failed rc={pushed.returncode}; not started\n")
                return 126
            started = subprocess.run(run_argv, cwd=str(worktree), input=task.encode("utf-8"),
                                     stdout=fh, stderr=subprocess.STDOUT)
            if started.returncode != 0:
                _append(log_path, f"\nlane: {binary} run failed rc={started.returncode}\n")
                return 126
    except OSError as exc:
        _append(log_path, f"failed to start {binary}: {exc}\n")
        return 127

    if args.no_wait:
        return None

    rc = None
    failures = 0
    try:
        while True:
            try:
                proc = subprocess.run([binary, "status", number], cwd=str(worktree),
                                      capture_output=True, text=True, timeout=120)
                output = (proc.stdout or "") + (proc.stderr or "")
                ok = proc.returncode == 0
            except subprocess.TimeoutExpired:
                output, ok = "status timed out\n", False
            _append(log_path, output)
            match = re.search(r"END rc=(-?\d+)", output)
            if match:
                rc = int(match.group(1))
                break
            failures = 0 if ok else failures + 1
            if failures >= args.max_poll_failures:
                _append(log_path, f"\nlane: {failures} failed status polls in a row: laptop unreachable\n")
                return UNREACHABLE
            time.sleep(args.poll)
        with open(log_path, "ab") as fh:
            pulled = subprocess.run([binary, "pull", number], cwd=str(worktree),
                                    stdout=fh, stderr=subprocess.STDOUT)
        if pulled.returncode != 0:
            _append(log_path, f"\nlane: {binary} pull failed rc={pulled.returncode}\n")
            return UNREACHABLE
    except OSError as exc:
        _append(log_path, f"laptop polling failed: {exc}\n")
        return 127
    return rc


def cmd_collect(args):
    lane = args.lane
    if not valid_lane(lane):
        print(f"invalid lane name: {lane!r}")
        return 1
    entry = load_ledger().get("lanes", {}).get(lane, {})
    worktree = Path(entry.get("worktree") or worktree_path(lane))
    if not worktree.is_dir():
        print(f"refused: worktree not found: {worktree}")
        return 1
    where = entry.get("where", "local")
    log_value = entry.get("log")
    log_path = Path(log_value) if log_value else None
    rc = entry.get("rc")
    if where == "laptop":
        number = lane_number(lane)
        binary = laptop_bin()
        if not laptop_reachable():
            print("laptop unreachable (ssh probe failed): nothing collected; retry from a network that reaches it")
            return 3
        state = subprocess.run([binary, "status", number], cwd=str(worktree), capture_output=True, text=True)
        match = re.search(r"END rc=(-?\d+)", (state.stdout or "") + (state.stderr or ""))
        if not match:
            print(f"{lane} is still running on the laptop (no END line yet); collect again later")
            return 3
        rc = int(match.group(1))
        argv = [binary, "pull", number]
        try:
            if log_path:
                with open(log_path, "ab") as fh:
                    subprocess.run(argv, cwd=str(worktree), stdout=fh, stderr=subprocess.STDOUT)
            else:
                subprocess.run(argv, cwd=str(worktree), capture_output=True)
        except OSError as exc:
            print(f"refused: cannot run {binary} pull: {exc}")
            return 1
    base = entry.get("base") or head_sha(worktree)
    result, problems = validate_result(worktree, lane, base)
    status = "returned" if result is not None else "no-result"

    def fn(lanes):
        fields = {"status": status, "worktree": str(worktree),
                  "worker": entry.get("worker"), "where": where, "ended": utc_now()}
        if log_path:
            fields["log"] = str(log_path)
        if base:
            fields["base"] = base
        if result is not None:
            fields["result_status"] = result.get("status")
        update_entry(lanes, lane, **fields)
        if result is None:
            lanes[lane].pop("result_status", None)

    mutate_ledger(fn)
    print_summary(lane, rc, result, problems, log_path)
    return 0 if result is not None else 1


def cmd_set(args):
    lane = args.lane
    if not valid_lane(lane):
        print(f"invalid lane name: {lane!r}")
        return 1
    if args.status not in VALID_STATUSES:
        print(f"invalid status: {args.status!r}")
        return 1

    def fn(lanes):
        fields = {"status": args.status}
        if args.note is not None:
            fields["note"] = args.note
        update_entry(lanes, lane, **fields)

    mutate_ledger(fn)
    print(f"{lane}: {args.status}")
    return 0


def cmd_ls(args):
    lanes = load_ledger().get("lanes", {})
    rows = []
    for name, entry in lanes.items():
        status = entry.get("status", "")
        if not args.show_all and status in ("merged", "rejected", "removed"):
            continue
        rows.append((name, entry))
    rows.sort(key=lambda item: item[1].get("updated", ""), reverse=True)
    header = ("LANE", "STATUS", "BRANCH", "BASE", "WORKER", "WHERE", "UPDATED", "NOTE")
    table = [header]
    for name, entry in rows:
        table.append((name, entry.get("status", ""), entry.get("branch", ""),
                      short_sha(entry.get("base")), entry.get("worker", ""),
                      entry.get("where", ""), entry.get("updated", ""), entry.get("note", "")))
    widths = [max(len(str(row[i])) for row in table) for i in range(len(header))]
    for row in table:
        print("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)).rstrip())
    return 0


def cmd_accept(args):
    lane = args.lane
    if not valid_lane(lane):
        print(f"invalid lane name: {lane!r}")
        return 1
    entry = load_ledger().get("lanes", {}).get(lane, {})
    worktree = Path(entry.get("worktree") or worktree_path(lane))
    if not worktree.is_dir():
        print(f"refused: worktree not found: {worktree}")
        return 1
    base = args.base or entry.get("base")
    if not base:
        proc = run_git(["merge-base", "main", "HEAD"], cwd=worktree)
        if proc.returncode != 0:
            print(f"refused: cannot determine base: {proc.stderr.strip()}")
            return 1
        base = proc.stdout.strip()
    out_dir = lane_dir(lane)
    out_dir.mkdir(parents=True, exist_ok=True)
    mutants = Path(args.mutants) if args.mutants else None

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from accept import run_accept
    except Exception as exc:  # noqa: BLE001 - accept.py may be missing or broken
        print(f"refused: cannot import accept.run_accept: {exc}")
        return 2
    try:
        result = run_accept(worktree=worktree, lane=lane, base=base,
                            tests=list(args.tests or []), mutants=mutants,
                            timeout=args.timeout, out_dir=out_dir)
    except Exception as exc:  # noqa: BLE001 - a broken accept module must not crash the CLI
        print(f"accept failed: {exc}")
        return 2

    verdict = result.get("verdict") if isinstance(result, dict) else None
    status = "accept-pass" if verdict == "PASS" else "accept-fail"

    def fn(lanes):
        update_entry(lanes, lane, status=status, accept=str(verdict))

    mutate_ledger(fn)
    report = result.get("report") if isinstance(result, dict) else None
    if report:
        try:
            print(Path(report).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            print(report)
    reasons = result.get("reasons", []) if isinstance(result, dict) else []
    for reason in reasons:
        print(f"reason: {reason}")
    return 0 if verdict == "PASS" else 1


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------

def list_worktrees(repo):
    proc = run_git(["worktree", "list", "--porcelain"], cwd=repo)
    if proc.returncode != 0:
        return []
    items = []
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                items.append(current)
            current = {"path": line[len("worktree "):]}
        elif current is None:
            continue
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            current["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    if current is not None:
        items.append(current)
    return items


def parse_porcelain_path(line):
    path = line[3:] if len(line) > 3 else ""
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return norm_path(path.strip().strip('"'))


def status_paths(worktree, ignored=False):
    argv = ["status", "--porcelain"]
    if ignored:
        argv.append("--ignored")
    proc = run_git(argv, cwd=worktree)
    if proc.returncode != 0:
        return []
    return [parse_porcelain_path(line) for line in proc.stdout.splitlines() if line.strip()]


def dirty_paths(worktree):
    return [p for p in status_paths(worktree) if p and not is_lane_path(p)]


def ignored_data(worktree):
    out = []
    for path in status_paths(worktree, ignored=True):
        if not path or is_lane_path(path):
            continue
        if any(part in ALLOWED_IGNORED for part in Path(path).parts):
            continue
        out.append(path)
    return out


def path_size(worktree, path):
    target = worktree / path
    try:
        if target.is_file():
            return target.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(target):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def last_commit_date(worktree):
    proc = run_git(["log", "-1", "--format=%cs"], cwd=worktree)
    return proc.stdout.strip() if proc.returncode == 0 else "?"


def tag_name(branch):
    return f"archive/lanes/{branch}"


def existing_tag(repo, tag):
    proc = run_git(["rev-parse", "-q", "--verify", f"refs/tags/{tag}"], cwd=repo)
    return proc.stdout.strip() if proc.returncode == 0 else None


def busy_dirs():
    """Working directories of every process we can see (a worker started by hand has its cwd in the lane)."""
    root = Path(os.environ.get("LANE_PROC") or "/proc")
    cwds = set()
    try:
        entries = list(root.iterdir())
    except OSError:
        return cwds
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cwds.add(os.path.realpath(os.readlink(entry / "cwd")))
        except OSError:
            continue
    return cwds


def in_use(path, cwds):
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return any(c == real or c.startswith(real + os.sep) for c in cwds)


def cmd_gc(args):
    repo = main_repo()
    if not repo.is_dir():
        print(f"refused: main repo not found: {repo}")
        return 1
    ledger = load_ledger().get("lanes", {})
    removed = []
    counts = {name: 0 for name in ("running", "busy", "open", "dirty", "ignored-data", "merged", "unmerged")}
    busy = busy_dirs()

    for item in list_worktrees(repo):
        path = Path(item["path"])
        lane = path.name
        if not valid_lane(lane):
            continue
        try:
            if path.resolve() == repo.resolve():
                continue
        except OSError:
            pass

        branch = item.get("branch") or ""
        tip = item.get("head") or head_sha(path) or ""
        if branch:
            ref = run_git(["rev-parse", f"refs/heads/{branch}"], cwd=repo)
            if ref.returncode == 0:
                tip = ref.stdout.strip()
        entry = ledger.get(lane, {})
        if entry.get("status") == "running":
            cls = "running"
        elif in_use(path, busy):
            cls = "busy"
        elif entry.get("status") in OPEN_STATUSES:
            cls = "open"
        elif dirty_paths(path):
            cls = "dirty"
        else:
            ignored = ignored_data(path)
            if ignored:
                cls = "ignored-data"
            else:
                merged = bool(tip) and run_git(["merge-base", "--is-ancestor", tip, "main"], cwd=repo).returncode == 0
                cls = "merged" if merged else "unmerged"
        counts[cls] += 1

        tag = tag_name(branch) if branch else ""
        tag_sha = existing_tag(repo, tag) if tag else None
        tag_conflict = bool(tag and tag_sha and tag_sha != tip)

        if cls == "running":
            action = "skip (running)"
        elif cls == "busy":
            action = "skip (a process is working in it)"
        elif cls == "open":
            action = f"skip (ledger: {entry.get('status')}; `lane set` merged/rejected first)"
        elif cls == "dirty":
            action = "skip (dirty)"
        elif cls == "ignored-data":
            size = sum(path_size(path, p) for p in ignored_data(path))
            action = f"skip (ignored data {human_size(size)})"
        elif cls == "merged":
            action = "remove"
        elif not args.include_unmerged:
            action = "keep (unmerged)"
        elif not branch:
            action = "keep (no branch)"
        elif tag_conflict:
            action = f"keep (tag {tag} exists at a different SHA)"
        else:
            action = f"remove (tag {tag})"

        print(f"{cls:<13} {lane:<16} {branch:<26} {short_sha(tip):<10} "
              f"{last_commit_date(path):<10} {action}")

        if not args.apply or not action.startswith("remove"):
            continue
        if cls == "unmerged":
            if not tag_sha:
                made = run_git(["tag", tag, tip], cwd=repo)
                if made.returncode != 0:
                    print(f"error: cannot create tag {tag}: {made.stderr.strip()}")
                    continue
            elif tag_sha != tip:
                continue
        gone = run_git(["worktree", "remove", str(path)], cwd=repo)
        if gone.returncode != 0:
            print(f"error: cannot remove worktree {path}: {gone.stderr.strip()}")
            continue
        flag = "-D" if cls == "unmerged" else "-d"
        deleted = run_git(["branch", flag, branch], cwd=repo)
        if deleted.returncode != 0:
            print(f"error: cannot delete branch {branch}: {deleted.stderr.strip()}")
        removed.append(lane)

    if removed:
        def fn(lanes):
            for lane in removed:
                update_entry(lanes, lane, status="removed")

        mutate_ledger(fn)

    print("counts: " + " ".join(f"{name}={counts[name]}" for name in counts))
    if args.apply:
        print(f"removed: {len(removed)}")
    return 0


# ---------------------------------------------------------------------------
# wiring for the sibling modules (mutants, lanequeue, review, ask, merge)
# ---------------------------------------------------------------------------

def _sibling(name):
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    return __import__(name)


def lane_base(lane, worktree, override=None):
    if override:
        return override
    base = load_ledger().get("lanes", {}).get(lane, {}).get("base")
    if base:
        return base
    proc = run_git(["merge-base", "main", "HEAD"], cwd=worktree)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _lane_worktree(lane):
    if not valid_lane(lane):
        raise RuntimeError(f"invalid lane name: {lane!r}")
    entry = load_ledger().get("lanes", {}).get(lane, {})
    worktree = Path(entry.get("worktree") or worktree_path(lane))
    if not worktree.is_dir():
        raise RuntimeError(f"worktree not found: {worktree}")
    return worktree


def cmd_mutants(args):
    worktree = _lane_worktree(args.lane)
    base = lane_base(args.lane, worktree, args.base)
    if not base:
        print("refused: cannot determine base")
        return 1
    rows = _sibling("mutants").propose(worktree, base, max_mutants=args.max, files=args.file or None)
    out = Path(args.output) if args.output else lane_dir(args.lane) / "mutants.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        print(f"  {row['name']}")
    print(f"{len(rows)} mutants -> {out}")
    print("review them, drop equivalent ones, then: "
          f"lane accept {args.lane} --test '<cmd>' --mutants {out}")
    return 0


def cmd_queue(args):
    lq = _sibling("lanequeue")
    entries = lq.load_queue(args.queue_file)
    qdir = state_dir() / "queue"
    qdir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state) if args.state else qdir / (Path(args.queue_file).stem + ".state.json")
    me = [sys.executable, str(Path(__file__).resolve())]

    def run_fn(lane, brief, extra):
        outer = lane_dir(lane) / "queue-run.log"
        outer.parent.mkdir(parents=True, exist_ok=True)
        with open(outer, "wb") as fh:
            rc = subprocess.run(me + ["run", lane, brief] + list(extra), stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode
        # scan the WORKER log for quota / rate-limit messages, not just our summary
        worker_log = load_ledger().get("lanes", {}).get(lane, {}).get("log")
        if worker_log and Path(worker_log).exists():
            return rc, worker_log
        return rc, str(outer)

    def report(record):
        print(f"{record.get('lane')}: {record.get('status')} rc={record.get('rc')}", flush=True)

    print(f"queue state: {state_path}  (touch {state_path.parent / 'STOP'} to stop new starts)")
    summary = lq.run_queue(entries, run_fn, max_parallel=args.max, state_path=state_path,
                           poll=1.0, on_change=report)
    print(json.dumps(summary, sort_keys=True))
    return 0 if not (summary["failed"] or summary["skipped"]) else 1


def cmd_review(args):
    worktree = _lane_worktree(args.lane)
    base = lane_base(args.lane, worktree, args.base)
    if not base:
        print("refused: cannot determine base")
        return 1
    result = _sibling("review").run_review(worktree, args.lane, base, lane_dir(args.lane),
                                           focus=args.focus or "", timeout=args.timeout)
    print(f"findings: {result['findings']}  rc={result['rc']}")
    print(f"report: {result['report']}")
    print("check every finding in the source before acting on it")
    return 0 if result["rc"] == 0 else 1


def cmd_ask(args):
    cwd = Path(args.cwd) if args.cwd else main_repo()
    result = _sibling("ask").run_ask(args.question, cwd, state_dir() / "ask", timeout=args.timeout)
    print(f"answer saved: {result['answer_path']}")
    return 0 if result["rc"] == 0 else 1


def cmd_merge(args):
    worktree = _lane_worktree(args.lane)
    if not args.tests:
        print("refused: give at least one --test; the merge reruns them on main")
        return 1
    merge = _sibling("merge")
    ledger = load_ledger().get("lanes", {})
    status = ledger.get(args.lane, {}).get("status")
    if status != "accept-pass" and not args.force_unaccepted:
        print(f"refused: ledger status is {status!r}, not accept-pass. Run `lane accept` first "
              "(or --force-unaccepted for lanes made before the ledger).")
        return 1
    others = [name for name, e in ledger.items()
              if name != args.lane and e.get("status") in OPEN_STATUSES and worktree_path(name).is_dir()]
    base_of = {name: ledger[name].get("base") for name in others + [args.lane]}
    base_of[args.lane] = lane_base(args.lane, worktree)
    shared = merge.overlaps(main_repo(), args.lane, others, worktrees_root(), base_of)
    result = merge.run_merge(main_repo(), worktree, args.lane, args.message, args.tests,
                             timeout=args.timeout, dry_run=args.dry_run,
                             allow_overlap=args.allow_overlap, others=shared,
                             out_dir=lane_dir(args.lane))
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("ok") and not args.dry_run:
        mutate_ledger(lambda lanes: update_entry(lanes, args.lane, status="merged",
                                                 merge=result.get("merge")))
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="lane", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="create a lane worktree")
    p.add_argument("lane")
    p.add_argument("--from", dest="from_ref", required=True)
    p.add_argument("--branch")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("run", help="run a worker in a lane")
    p.add_argument("lane")
    p.add_argument("brief_file")
    p.add_argument("--worker", choices=("oc", "cc"))
    p.add_argument("--luxalgo", action="store_true")
    p.add_argument("-m", "--model")
    p.add_argument("--laptop", action="store_true")
    p.add_argument("--no-wait", dest="no_wait", action="store_true")
    p.add_argument("--poll", type=float, default=60)
    p.add_argument("--laptop-or-local", dest="laptop_or_local", action="store_true",
                   help="with --laptop: run here when the laptop cannot be reached")
    p.add_argument("--max-poll-failures", dest="max_poll_failures", type=int, default=10,
                   help="stop waiting after this many failed status polls in a row (laptop lost)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("collect", help="collect and validate a lane result")
    p.add_argument("lane")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("set", help="set a lane status")
    p.add_argument("lane")
    p.add_argument("status", choices=VALID_STATUSES)
    p.add_argument("--note")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("ls", help="list lanes")
    p.add_argument("--all", dest="show_all", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("accept", help="run the acceptance harness")
    p.add_argument("lane")
    p.add_argument("--test", dest="tests", action="append", default=[])
    p.add_argument("--mutants")
    p.add_argument("--base")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("mutants", help="suggest mutants for the lines a lane changed")
    p.add_argument("lane")
    p.add_argument("--base")
    p.add_argument("--max", type=int, default=12)
    p.add_argument("--file", action="append")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_mutants)

    p = sub.add_parser("queue", help="run lanes from a JSON list, max N at once, stop on quota")
    p.add_argument("queue_file")
    p.add_argument("--max", type=int, default=5)
    p.add_argument("--state")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("review", help="read-only DeepSeek review of a lane diff")
    p.add_argument("lane")
    p.add_argument("--base")
    p.add_argument("--focus")
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("ask", help="read-only DeepSeek question about a folder")
    p.add_argument("question")
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("merge", help="commit an accepted lane, merge into main, rerun tests")
    p.add_argument("lane")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--test", dest="tests", action="append", default=[])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-overlap", action="store_true")
    p.add_argument("--force-unaccepted", action="store_true")
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("gc", help="classify and optionally remove lane worktrees")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--include-unmerged", dest="include_unmerged", action="store_true")
    p.set_defaults(func=cmd_gc)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
