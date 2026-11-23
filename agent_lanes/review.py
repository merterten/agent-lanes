#!/usr/bin/env python3
"""Run a read-only code review lane against a worktree.

The worker is always invoked as ``<oc-bin> "<task>"`` with no ``--write``
and with its working directory set to the worktree, so it can read the
change set but cannot edit anything.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROMPT_FILE = Path(__file__).with_name("review_prompt.txt")
DEFAULT_OC_BIN = "oc-worker"
DEFAULT_ROOT = "~/agent-lanes"
DEFAULT_TIMEOUT = 1800
MAX_INLINE_BYTES = 200 * 1024
READ_INSTRUCTION = "Read .lane/review/diff.patch and the changed files in full"
_FINDING_RE = re.compile(r"^FINDING\s+\d+\s*\|", re.MULTILINE)
_DECLARED_RE = re.compile(r"^[ \t]*FINDINGS:\s*(\d+)[ \t]*$", re.MULTILINE)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _combine(stdout, stderr) -> str:
    out = _as_text(stdout)
    err = _as_text(stderr)
    if out and err and not out.endswith("\n"):
        out += "\n"
    return out + err


def _run_worker(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """Run the worker without a shell and return (rc, stdout+stderr)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout, stdin=subprocess.DEVNULL)
        return proc.returncode, _combine(proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        out = _as_text(getattr(exc, "stdout", ""))
        err = _as_text(getattr(exc, "stderr", ""))
        return 124, _combine(out, err) + f"\nTIMEOUT: worker exceeded {timeout} seconds\n"
    except FileNotFoundError as exc:
        return 127, f"ERROR: could not run worker {argv[0]!r}: {exc}\n"
    except OSError as exc:
        return 126, f"ERROR: could not run worker {argv[0]!r}: {exc}\n"


def _git(worktree: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _is_excluded(relative: str) -> bool:
    rel = relative.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return (
        rel == ".lane"
        or rel.startswith(".lane/")
        or rel == ".lane.prev"
        or rel.startswith(".lane.prev/")
    )


def _tracked_changed(worktree: Path, base: str) -> list[str]:
    out = _git(worktree, "diff", "--name-only", base)
    return [line.strip() for line in out.splitlines() if line.strip() and not _is_excluded(line.strip())]


def _untracked(worktree: Path) -> list[str]:
    out = _git(worktree, "ls-files", "--others", "--exclude-standard")
    paths = []
    for line in out.splitlines():
        rel = line.strip()
        if rel and not _is_excluded(rel):
            paths.append(rel)
    return paths


def _write_change_set(worktree: Path, base: str, review_dir: Path) -> tuple[list[str], list[tuple[str, int]]]:
    review_dir.mkdir(parents=True, exist_ok=True)
    diff = _git(worktree, "diff", base)
    tracked = _tracked_changed(worktree, base)
    untracked = _untracked(worktree)

    changed = list(tracked)
    for rel in untracked:
        changed.append(rel)

    parts = ["# Review change set", f"# Base: {base}", "", "## git diff", diff.rstrip("\n"), ""]
    parts.append("## untracked files (new file contents)")
    parts.append("")

    skipped: list[tuple[str, int]] = []
    for rel in untracked:
        path = worktree / rel
        try:
            size = path.stat().st_size
        except OSError as exc:
            parts.append(f"=== UNREADABLE FILE: {rel} ({exc}) ===")
            parts.append("")
            continue
        if size > MAX_INLINE_BYTES:
            skipped.append((rel, size))
            continue
        parts.append(f"=== NEW FILE: {rel} ({size} bytes) ===")
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            parts.append(f"<could not read: {exc}>")
        else:
            if data:
                parts.append(data.rstrip("\n"))
        parts.append("")

    if skipped:
        parts.append("## Skipped large files (skipped, not inlined)")
        parts.append("")
        for rel, size in skipped:
            parts.append(
                f"=== SKIPPED LARGE FILE: {rel} ({size} bytes, limit {MAX_INLINE_BYTES}) ==="
            )
        parts.append("")

    (review_dir / "diff.patch").write_text("\n".join(parts) + "\n", encoding="utf-8")
    (review_dir / "files.txt").write_text(
        "\n".join(changed) + ("\n" if changed else ""), encoding="utf-8"
    )
    return changed, skipped


def _build_task(focus: str) -> str:
    checklist = PROMPT_FILE.read_text(encoding="utf-8").strip()
    pieces = [checklist]
    if focus.strip():
        pieces.append(f"Focus for this review: {focus.strip()}")
    pieces.append(READ_INSTRUCTION + ".")
    return "\n\n".join(pieces)


def run_review(
    worktree: Path,
    lane: str,
    base: str,
    out_dir: Path,
    focus: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Review the worktree changes from ``base`` and save a report.

    Returns ``{"rc": int, "report": <path>, "findings": int}``. The
    ``.lane/review`` scratch directory is always removed afterwards.
    """
    worktree = Path(worktree)
    out_dir = Path(out_dir)
    review_dir = worktree / ".lane" / "review"

    if review_dir.exists():
        shutil.rmtree(review_dir, ignore_errors=True)
    review_dir.mkdir(parents=True, exist_ok=True)

    try:
        _write_change_set(worktree, base, review_dir)
        task = _build_task(focus)
        oc_bin = os.environ.get("LANE_OC_BIN", DEFAULT_OC_BIN)
        rc, raw = _run_worker([oc_bin, task], worktree, timeout)
    finally:
        shutil.rmtree(review_dir, ignore_errors=True)

    findings = len(_FINDING_RE.findall(raw))
    declared_matches = _DECLARED_RE.findall(raw)
    declared = int(declared_matches[-1]) if declared_matches else None

    note = ""
    if declared is not None and declared != findings:
        note = (
            f"NOTE: FINDINGS line says {declared} but {findings} FINDING block(s) were found.\n\n"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"review-{_utc_stamp()}.md"
    report_path.write_text(note + raw, encoding="utf-8")
    return {"rc": rc, "report": str(report_path), "findings": findings}


def _default_out(lane: str) -> Path:
    if not re.fullmatch(r"(cc|oc)-worker-[0-9]{1,3}", lane):
        raise ValueError(f"invalid lane name: {lane!r}")
    root = Path(os.environ.get("LANE_ROOT", DEFAULT_ROOT)).expanduser()
    return root / "lanes" / lane


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only review lane.")
    parser.add_argument("worktree")
    parser.add_argument("lane")
    parser.add_argument("base")
    parser.add_argument("--focus", default="")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else _default_out(args.lane)
    result = run_review(
        Path(args.worktree), args.lane, args.base, out_dir, focus=args.focus
    )
    print(f"report: {result['report']}")
    print(f"findings: {result['findings']}")
    return 0 if result["rc"] == 0 else result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
