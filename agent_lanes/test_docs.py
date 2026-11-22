"""Checks that the worker docs stay true to lane.py and clean of private data."""
import argparse
import re
import shlex
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import lane  # noqa: E402

WORKERS = ROOT / "docs" / "workers.md"
README = ROOT / "README.md"

# Generic leaks only. Project-specific words live in an untracked file, one per line (.docs-banned in the repo
# root, git-ignored), so the public repo does not name what it is keeping out.
BANNED = ("@gmail", "/home/")
_EXTRA = ROOT / ".docs-banned"
if _EXTRA.exists():
    BANNED += tuple(w.strip().lower() for w in _EXTRA.read_text(encoding="utf-8").splitlines() if w.strip())
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def lane_command_lines(text):
    """Yield stripped lines that start with 'lane ' (all are code-block commands)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("lane "):
            yield stripped


def subcommands():
    parser = lane.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("lane.build_parser() has no subparsers")


def flags(tokens):
    for token in tokens:
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            yield token.split("=", 1)[0]


class WorkerDocsTest(unittest.TestCase):
    def test_lane_commands_and_flags_exist(self):
        text = WORKERS.read_text(encoding="utf-8")
        lines = list(lane_command_lines(text))
        self.assertTrue(lines, "no 'lane ...' lines found in docs/workers.md code blocks")
        real = subcommands()
        for line in lines:
            tokens = shlex.split(line)
            self.assertEqual(tokens[0], "lane")
            command = tokens[1]
            with self.subTest(command=line):
                self.assertIn(command, real)
                accepted = real[command]._option_string_actions
                for flag in flags(tokens[2:]):
                    self.assertIn(flag, accepted, f"{flag!r} not accepted by 'lane {command}'")

    def test_docs_and_readme_contain_no_private_data(self):
        for path in (WORKERS, README):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            with self.subTest(path=str(path)):
                for word in BANNED:
                    self.assertNotIn(word, lowered, f"{path} mentions {word!r}")
                match = IPV4.search(text)
                self.assertIsNone(match, f"{path} contains an IPv4 address: {match and match.group(0)}")

    def test_relative_markdown_links_exist(self):
        text = WORKERS.read_text(encoding="utf-8")
        relative = []
        for raw in MARKDOWN_LINK.findall(text):
            target = raw.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if target.startswith("#"):
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                continue
            relative.append(target)
        self.assertTrue(relative, "no relative markdown links found in docs/workers.md")
        for target in relative:
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            with self.subTest(link=target):
                self.assertTrue((WORKERS.parent / path_part).exists(),
                                f"broken relative link in docs/workers.md: {target}")


if __name__ == "__main__":
    unittest.main()
