#!/usr/bin/env python3
"""Ask the read-only worker a question about files in one folder."""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OC_BIN = "oc-worker"
DEFAULT_ROOT = "~/agent-lanes"
DEFAULT_TIMEOUT = 900
ANSWER_SUFFIX = (
    "Answer from the files in this folder only. Cite file:line for every claim. "
    "If not found, say NOT FOUND. Keep it under 40 lines."
)


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


def _allowed_roots() -> list[Path]:
    trading = Path(os.environ.get("LANE_ROOT", DEFAULT_ROOT))
    return [Path("/tmp").resolve(), trading.expanduser().resolve()]


def _validate_cwd(cwd: Path) -> Path:
    path = Path(cwd).expanduser().resolve()
    for root in _allowed_roots():
        if path == root or path.is_relative_to(root):
            return path
    raise ValueError(f"cwd {path} is outside the allowed roots (/tmp or LANE_ROOT)")


def run_ask(
    question: str,
    cwd: Path,
    out_dir: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Ask ``question`` about the files in ``cwd`` and save the answer.

    Returns ``{"rc": int, "answer_path": <path>}``. The answer file is
    written even when the worker times out or cannot be started.
    """
    cwd = _validate_cwd(cwd)
    prompt = question + "\n\n" + ANSWER_SUFFIX
    oc_bin = os.environ.get("LANE_OC_BIN", DEFAULT_OC_BIN)
    rc, answer = _run_worker([oc_bin, prompt], cwd, timeout)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    answer_path = out_dir / f"ask-{_utc_stamp()}.md"
    content = f"# Question\n\n{question}\n\n# Answer\n\n{answer}"
    if not content.endswith("\n"):
        content += "\n"
    answer_path.write_text(content, encoding="utf-8")

    print(answer)
    return {"rc": rc, "answer_path": str(answer_path)}


def _default_out() -> Path:
    root = Path(os.environ.get("LANE_ROOT", DEFAULT_ROOT)).expanduser()
    return root / "lanes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask the read-only worker a question.")
    parser.add_argument("question")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else _default_out()
    result = run_ask(args.question, Path(args.cwd), out_dir)
    print(f"answer: {result['answer_path']}")
    return 0 if result["rc"] == 0 else result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
