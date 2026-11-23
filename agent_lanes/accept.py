#!/usr/bin/env python3
"""Accept (or reject) one DeepSeek worker lane.

The worker writes .lane/LANE_RESULT.json inside its worktree.  That file is
untrusted data: it is read for claims only and never used as a command source.
Reviewer commands are the only commands this tool runs.

Defaults are supplied by the caller (lane.py or the CLI).  All paths are
arguments so tests can use temporary git repositories.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

LANE_EXCLUDES = (".lane", ".lane.prev")


def parse_counts(output):
    """Return (passed, failed) parsed from common test-runner output, or None."""
    if not output:
        return None
    for parser in (_parse_pytest, _parse_unittest, _parse_dotnet, _parse_node):
        counts = parser(output)
        if counts is not None:
            return counts
    return None


_PYTEST_TOKEN = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b"
)
_PYTEST_SUMMARY = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")


def _parse_pytest(output):
    for line in reversed(output.splitlines()):
        if not _PYTEST_SUMMARY.search(line):
            continue
        tokens = _PYTEST_TOKEN.findall(line)
        if not tokens:
            continue
        passed = failed = 0
        is_test_summary = False
        for number, kind in tokens:
            value = int(number)
            if kind == "passed":
                passed += value
                is_test_summary = True
            elif kind in ("failed", "error", "errors"):
                failed += value
                is_test_summary = True
        if is_test_summary:
            return passed, failed
    return None


_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_UNITTEST_OK = re.compile(r"^OK\b", re.MULTILINE)
_UNITTEST_FAILED = re.compile(r"^FAILED\b", re.MULTILINE)


def _parse_unittest(output):
    ran = _UNITTEST_RAN.search(output)
    if not ran:
        return None
    total = int(ran.group(1))
    ok = _UNITTEST_OK.search(output)
    failed = _UNITTEST_FAILED.search(output)
    if not ok and not failed:
        return None
    failures = _int_group(r"failures=(\d+)", output)
    errors = _int_group(r"errors=(\d+)", output)
    skipped = _int_group(r"skipped=(\d+)", output)
    failed_count = failures + errors
    passed = max(0, total - failed_count - skipped)
    return passed, failed_count


def _int_group(pattern, text):
    match = re.search(pattern, text)
    return int(match.group(1)) if match else 0


_DOTNET = re.compile(
    r"(?:Passed|Failed)!\s*-\s*Failed:\s*(\d+),\s*Passed:\s*(\d+)"
)


def _parse_dotnet(output):
    rows = _DOTNET.findall(output)
    if not rows:
        return None
    failed = sum(int(f) for f, _p in rows)
    passed = sum(int(p) for _f, p in rows)
    return passed, failed


_NODE_PASS = re.compile(r"^\s*(?:#|\u2139)\s*pass\s+(\d+)", re.MULTILINE)
_NODE_FAIL = re.compile(r"^\s*(?:#|\u2139)\s*fail\s+(\d+)", re.MULTILINE)


def _parse_node(output):
    passes = _NODE_PASS.findall(output)
    fails = _NODE_FAIL.findall(output)
    if not passes and not fails:
        return None
    return sum(int(n) for n in passes), sum(int(n) for n in fails)


def _norm_cmd(cmd):
    return " ".join(str(cmd).split())


def _one_line(text, limit=240):
    line = " ".join(str(text).split())
    if len(line) > limit:
        line = line[: limit - 3] + "..."
    return line


def _table_cell(text):
    return _one_line(text).replace("|", "\\|")


def _is_lane_path(path):
    cleaned = str(path).strip().strip('"').replace("\\", "/")
    for name in LANE_EXCLUDES:
        if cleaned == name or cleaned.startswith(name + "/"):
            return True
    return False


def _diff_line_is_lane(line):
    stripped = line.strip()
    if not stripped:
        return False
    path_part = stripped.split("|", 1)[0].strip()
    if "=>" in path_part:
        path_part = path_part.split("=>")[-1].strip()
    return _is_lane_path(path_part)


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _run_shell(cmd, cwd, timeout, log_path):
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            errors="replace", stdin=subprocess.DEVNULL)
        output = _as_text(proc.stdout) + _as_text(proc.stderr)
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = None
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
    except Exception as exc:  # a broken shell should be a failed command, not a crash
        timed_out = False
        rc = 127
        output = f"could not run command: {exc}"
    seconds = time.monotonic() - start
    if log_path is not None:
        header = (
            f"$ {cmd}\n# rc={rc} timed_out={timed_out} seconds={seconds:.3f}\n"
        )
        Path(log_path).write_text(header + output, encoding="utf-8", errors="replace")
    return {
        "cmd": cmd,
        "rc": rc,
        "output": output,
        "seconds": seconds,
        "timed_out": timed_out,
        "log": str(log_path) if log_path is not None else "",
    }


def _git(worktree, args):
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree)] + args,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except Exception as exc:
        return False, str(exc)
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, text


def _read_backup(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_backup(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def _restore_backup(out_dir, worktree=None):
    backup = Path(out_dir) / "mutant-backup.json"
    data = _read_backup(backup)
    restored = []
    root = Path(worktree).resolve() if worktree is not None else None
    for path_text, original_text in data.items():
        target = Path(path_text)
        if root is not None:
            if not target.is_absolute():
                target = root / target
            target = target.resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"backup path escapes worktree: {path_text}")
        target.write_text(original_text, encoding="utf-8", errors="surrogateescape")
        restored.append(str(target))
    backup.unlink()
    return restored


def restore_backup(out_dir):
    """Restore files recorded in out_dir/mutant-backup.json; delete the backup."""
    return _restore_backup(out_dir)


def _validate_mutant(worktree, mutant):
    if not isinstance(mutant, dict):
        return None, "mutant is not an object"
    name = mutant.get("name")
    file_text = mutant.get("file")
    find = mutant.get("find")
    replace = mutant.get("replace")
    if not isinstance(file_text, str) or not file_text:
        return None, "missing file"
    if Path(file_text).is_absolute():
        return None, f"path escapes worktree: {file_text}"
    root = Path(worktree).resolve()
    target = (root / file_text).resolve()
    if target != root and root not in target.parents:
        return None, f"path escapes worktree: {file_text}"
    if not target.is_file():
        return None, f"file not found: {file_text}"
    if not isinstance(find, str) or find == "":
        return None, "missing find"
    if not isinstance(replace, str):
        return None, "missing replace"
    try:
        original_bytes = target.read_bytes()
    except OSError as exc:
        return None, f"cannot read file: {exc}"
    original_text = original_bytes.decode("utf-8", "surrogateescape")
    occurrences = original_text.count(find)
    if occurrences != 1:
        return None, f"find not unique: {file_text} ({occurrences} matches)"
    return {
        "name": name if isinstance(name, str) and name else file_text,
        "file": file_text,
        "target": target,
        "find": find,
        "replace": replace,
        "test": mutant.get("test") if isinstance(mutant.get("test"), str) else None,
        "original_bytes": original_bytes,
        "original_text": original_text,
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
    }, None


def _run_mutants(mutants, worktree, tests, timeout, out_dir, reasons):
    results = []
    backup = Path(out_dir) / "mutant-backup.json"
    restores_ok = True

    try:
        rows = json.loads(Path(mutants).read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("mutants file must contain a list")
    except Exception as exc:
        reasons.append(f"invalid mutants file: {exc}")
        return results

    if not isinstance(tests, list):
        tests = []

    for index, mutant in enumerate(rows):
        valid, error = _validate_mutant(worktree, mutant)
        if valid is None:
            name = mutant.get("name") if isinstance(mutant, dict) else None
            label = name if isinstance(name, str) and name else f"#{index}"
            reasons.append(f"refused mutant {label}: {error}")
            results.append({"name": label, "file": _mutant_file(mutant), "result": "refused"})
            continue

        name = valid["name"]
        target = valid["target"]
        original_bytes = valid["original_bytes"]
        original_text = valid["original_text"]
        key = str(target)

        try:
            backup_data = _read_backup(backup) if backup.exists() else {}
        except Exception as exc:
            reasons.append(f"invalid mutant-backup.json: {exc}")
            results.append({"name": name, "file": valid["file"], "result": "refused"})
            restores_ok = False
            continue
        if key not in backup_data:
            backup_data[key] = original_text
            _write_backup(backup, backup_data)

        commands = [valid["test"]] if valid["test"] else list(tests)
        killed = False
        try:
            target.write_text(
                original_text.replace(valid["find"], valid["replace"], 1),
                encoding="utf-8",
                errors="surrogateescape",
            )
            for test_index, command in enumerate(commands):
                log = Path(out_dir) / f"mutant-{index}-test-{test_index}.log"
                run = _run_shell(command, worktree, timeout, log)
                counts = parse_counts(run["output"])
                failed_count = counts[1] if counts else 0
                if run["timed_out"] or run["rc"] != 0 or failed_count > 0:
                    killed = True
                    break
        finally:
            try:
                target.write_bytes(original_bytes)
                restored_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                if restored_sha != valid["sha256"]:
                    restores_ok = False
                    reasons.append(f"mutant restore mismatch: {name} ({valid['file']})")
            except OSError as exc:
                restores_ok = False
                reasons.append(f"mutant restore failed: {name}: {exc}")

        result = "KILLED" if killed else "SURVIVED"
        results.append({"name": name, "file": valid["file"], "result": result})
        if not killed:
            reasons.append(f"mutant survived: {name} ({valid['file']})")

    if restores_ok and backup.exists():
        try:
            backup.unlink()
        except OSError as exc:
            reasons.append(f"could not delete mutant-backup.json: {exc}")
    return results


def _mutant_file(mutant):
    if isinstance(mutant, dict) and isinstance(mutant.get("file"), str):
        return mutant["file"]
    return ""


def _fmt(value):
    return "-" if value is None else str(value)


def _numbers_equal(claim, measured):
    if claim is None or measured is None:
        return False
    try:
        return int(claim) == int(measured)
    except (TypeError, ValueError):
        return False


def _append_items(lines, items, limit=10, bullet="- "):
    if not items:
        lines.append("- none")
        return
    for item in items[:limit]:
        lines.append(bullet + _one_line(item))
    if len(items) > limit:
        lines.append(f"- ... {len(items) - limit} more")


def _build_report(context):
    lines = []
    lines.append(f"VERDICT: {context['verdict']}")
    lines.append("")
    lines.append(f"Lane: {context['lane']}")
    lines.append(f"Worktree: {context['worktree']}")
    lines.append(f"Base: {context['base']}")
    for note in context["recovered"]:
        lines.append(f"Recovery: {_one_line(note)}")

    lines.append("")
    lines.append("Reasons:")
    _append_items(lines, context["reasons"], limit=20)

    lines.append("")
    lines.append("Change set (git diff --stat base, excluding .lane/.lane.prev):")
    if context["diff_lines"]:
        _append_items(lines, context["diff_lines"], limit=15)
    else:
        lines.append("- no tracked changes versus base")
    lines.append("Untracked (non-ignored, excluding .lane/.lane.prev):")
    _append_items(lines, context["untracked"], limit=15)

    lines.append("")
    lines.append("Tests:")
    lines.append("| cmd | rc | passed | failed | seconds | log |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    if context["test_results"]:
        for run in context["test_results"][:12]:
            rc = "timeout" if run["timed_out"] else run["rc"]
            lines.append(
                f"| {_table_cell(run['cmd'])} | {rc} | {_fmt(run['passed'])} | "
                f"{_fmt(run['failed'])} | {run['seconds']:.2f} | {_table_cell(run['log'])} |"
            )
        if len(context["test_results"]) > 12:
            lines.append(f"| ... {len(context['test_results']) - 12} more | | | | | |")
    else:
        lines.append("| none | | | | | |")

    lines.append("")
    lines.append("Claims vs measured:")
    lines.append("| cmd | claimed (pass/fail) | measured (pass/fail) | match |")
    lines.append("| --- | --- | --- | --- |")
    if context["claim_rows"]:
        for row in context["claim_rows"][:12]:
            lines.append(
                f"| {_table_cell(row['cmd'])} | {_fmt(row['claimed_p'])}/{_fmt(row['claimed_f'])} | "
                f"{_fmt(row['measured_p'])}/{_fmt(row['measured_f'])} | {row['match']} |"
            )
        if len(context["claim_rows"]) > 12:
            lines.append(f"| ... {len(context['claim_rows']) - 12} more | | | |")
    else:
        lines.append("| none | | | |")

    lines.append("")
    lines.append("Worker test claims not rerun (not a failure):")
    _append_items(lines, context["not_rerun"], limit=10)

    lines.append("")
    lines.append("Unverified claims - check by hand:")
    unverified = [
        f"{item.get('name', '?')} = {item.get('value', '?')} "
        f"[{item.get('evidence', 'no evidence')}]"
        for item in context["unverified"]
        if isinstance(item, dict)
    ]
    _append_items(lines, unverified, limit=10)

    lines.append("")
    lines.append("Mutants:")
    lines.append("| name | file | result |")
    lines.append("| --- | --- | --- |")
    if context["mutant_results"]:
        for row in context["mutant_results"][:15]:
            lines.append(
                f"| {_table_cell(row['name'])} | {_table_cell(row['file'])} | {row['result']} |"
            )
        if len(context["mutant_results"]) > 15:
            lines.append(f"| ... {len(context['mutant_results']) - 15} more | | |")
    else:
        lines.append("| none | | |")

    lines.append("")
    lines.append("Worker summary:")
    worker = context["worker"]
    if worker is None:
        lines.append("- no valid LANE_RESULT.json")
        if context["result_error"]:
            lines.append(f"- parse error: {_one_line(context['result_error'])}")
    else:
        lines.append("- " + _one_line(worker.get("summary", "no summary")))
    lines.append("Blockers:")
    blockers = []
    if isinstance(worker, dict) and isinstance(worker.get("blockers"), list):
        blockers = [str(item) for item in worker["blockers"]]
    _append_items(lines, blockers, limit=10)

    if len(lines) > 118:
        lines = lines[:117] + ["... report truncated"]
    return "\n".join(lines) + "\n"


def run_accept(worktree, lane, base, tests, mutants, timeout, out_dir):
    worktree = Path(worktree)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    reasons = []
    recovered = []

    backup = out_dir / "mutant-backup.json"
    if backup.exists():
        try:
            restored = _restore_backup(out_dir, worktree)
            recovered.append(
                f"leftover {backup.name} found: restored {len(restored)} file(s) first"
            )
        except Exception as exc:
            reasons.append(f"crash recovery failed: {exc}")

    ok, diff_text = _git(worktree, ["diff", "--stat", base])
    diff_lines = []
    if ok:
        diff_lines = [line for line in diff_text.splitlines() if not _diff_line_is_lane(line)]
    else:
        diff_lines = [f"git diff failed: {_one_line(diff_text)}"]

    ok, untracked_text = _git(worktree, ["ls-files", "--others", "--exclude-standard"])
    if ok:
        untracked = [
            line
            for line in untracked_text.splitlines()
            if line.strip() and not _is_lane_path(line)
        ]
    else:
        untracked = [f"git ls-files failed: {_one_line(untracked_text)}"]

    worker = None
    result_error = ""
    result_path = worktree / ".lane" / "LANE_RESULT.json"
    if result_path.is_file():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(data, dict):
                raise ValueError("top-level JSON is not an object")
            worker = data
        except Exception as exc:
            result_error = str(exc)
    else:
        result_error = "file is missing"
    if worker is None:
        reasons.append("no valid LANE_RESULT.json")

    if not isinstance(tests, list):
        tests = list(tests)
    test_results = []
    for index, command in enumerate(tests):
        log = out_dir / f"test-{index}.log"
        run = _run_shell(command, worktree, timeout, log)
        counts = parse_counts(run["output"])
        run["passed"], run["failed"] = counts if counts else (None, None)
        if run["timed_out"]:
            reasons.append(f"test timed out after {timeout}s: {command}")
            run["ok"] = False
        elif run["rc"] != 0:
            reasons.append(f"test failed (rc={run['rc']}): {command}")
            run["ok"] = False
        elif run["failed"] is not None and run["failed"] > 0:
            reasons.append(f"test reported failures: {command}")
            run["ok"] = False
        else:
            run["ok"] = True
        test_results.append(run)

    measured_by_cmd = {}
    for run in test_results:
        measured_by_cmd.setdefault(_norm_cmd(run["cmd"]), run)

    claim_rows = []
    not_rerun = []
    worker_tests = worker.get("tests") if isinstance(worker, dict) else []
    if not isinstance(worker_tests, list):
        worker_tests = []
    for claim in worker_tests:
        if not isinstance(claim, dict):
            continue
        command = claim.get("cmd")
        if not isinstance(command, str):
            continue
        measured = measured_by_cmd.get(_norm_cmd(command))
        if measured is None:
            not_rerun.append(command)
            continue
        claimed_p = claim.get("passed")
        claimed_f = claim.get("failed")
        measured_p = measured["passed"]
        measured_f = measured["failed"]
        if measured_p is None or measured_f is None:
            match = "unknown"
        else:
            match = "yes" if (
                _numbers_equal(claimed_p, measured_p)
                and _numbers_equal(claimed_f, measured_f)
            ) else "no"
        claim_rows.append(
            {
                "cmd": command,
                "claimed_p": claimed_p,
                "claimed_f": claimed_f,
                "measured_p": measured_p,
                "measured_f": measured_f,
                "match": match,
            }
        )
        if match == "no":
            reasons.append(
                f"claim mismatch: {_norm_cmd(command)} claimed "
                f"{_fmt(claimed_p)}/{_fmt(claimed_f)}, measured {measured_p}/{measured_f}"
            )

    unverified = worker.get("claims") if isinstance(worker, dict) else []
    if not isinstance(unverified, list):
        unverified = []

    if not tests:
        reasons.append("no reviewer test commands given (--test): nothing was checked")

    mutant_results = []
    if mutants is not None:
        if all(run["ok"] for run in test_results):
            mutant_results = _run_mutants(mutants, worktree, tests, timeout, out_dir, reasons)
        else:
            reasons.append("mutants not run: reviewer tests already fail, so every mutant would look killed")

    verdict = "FAIL" if reasons else "PASS"
    context = {
        "verdict": verdict,
        "lane": lane,
        "worktree": worktree,
        "base": base,
        "recovered": recovered,
        "reasons": reasons,
        "diff_lines": diff_lines,
        "untracked": untracked,
        "test_results": test_results,
        "claim_rows": claim_rows,
        "not_rerun": not_rerun,
        "unverified": unverified,
        "mutant_results": mutant_results,
        "worker": worker,
        "result_error": result_error,
    }
    report_path = out_dir / "ACCEPT_REPORT.md"
    report_path.write_text(_build_report(context), encoding="utf-8")
    return {"verdict": verdict, "reasons": reasons, "report": str(report_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Accept or reject one worker lane.")
    parser.add_argument("worktree", type=Path)
    parser.add_argument("lane")
    parser.add_argument("base")
    parser.add_argument("--test", dest="tests", action="append", default=[], metavar="CMD")
    parser.add_argument("--mutants", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out_dir = args.out if args.out is not None else args.worktree / ".lane" / "accept"
    result = run_accept(
        args.worktree,
        args.lane,
        args.base,
        args.tests,
        args.mutants,
        args.timeout,
        out_dir,
    )
    print(f"VERDICT: {result['verdict']}")
    for reason in result["reasons"]:
        print(f"- {reason}")
    print(f"report: {result['report']}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
