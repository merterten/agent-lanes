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


PUBLIC_DOCS = [ROOT / "README.md", ROOT / "README.tr.md", *sorted((ROOT / "docs").glob("*.md"))]
DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")
HTML_REF = re.compile(r'(?:src|srcset|href)="([^"]+)"')


def github_slug(heading):
    """GitHub's anchor for a markdown heading: lowercase, drop punctuation (keep letters incl. non-ASCII, digits,
    spaces, hyphens, underscores), spaces become hyphens."""
    text = re.sub(r"`", "", heading.strip().lower())
    text = "".join(ch for ch in text if ch.isalnum() or ch in " -_")
    return text.replace(" ", "-")


def anchors(path):
    return {github_slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)}


class PublicDocsTest(unittest.TestCase):
    def test_every_public_page_is_free_of_private_data(self):
        for path in PUBLIC_DOCS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for word in BANNED:
                    self.assertNotIn(word, text.lower(), f"{path.name} mentions {word!r}")
                # loopback and the RFC 5737 documentation ranges are examples, not addresses of real machines
                real = [ip for ip in IPV4.findall(text) if ip != "127.0.0.1" and not ip.startswith(DOC_NETS)]
                self.assertEqual(real, [], f"{path.name} contains an IPv4 address")

    def test_relative_links_images_and_anchors_resolve(self):
        checked = 0
        for path in PUBLIC_DOCS:
            text = path.read_text(encoding="utf-8")
            for raw in MARKDOWN_LINK.findall(text) + HTML_REF.findall(text):
                target = raw.strip().strip("<>")
                if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue
                file_part, _, frag = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part else path
                with self.subTest(page=path.name, link=target):
                    self.assertTrue(dest.exists(), f"{path.name}: broken link {target}")
                    if frag and dest.suffix == ".md":
                        self.assertIn(frag, anchors(dest), f"{path.name}: no heading for #{frag} in {dest.name}")
                checked += 1
        self.assertGreater(checked, 20)

    def test_diagram_files_match_the_generator(self):
        sys.path.insert(0, str(ROOT / "docs" / "img"))
        import gen_where  # noqa: E402
        for lang in gen_where.LABELS:
            for theme in gen_where.THEMES:
                f = ROOT / "docs" / "img" / f"where-{lang}-{theme}.svg"
                with self.subTest(file=f.name):
                    self.assertEqual(f.read_text(encoding="utf-8"), gen_where.svg(lang, theme),
                                     "regenerate with: python3 docs/img/gen_where.py")
                    self.assertNotRegex(f.read_text(encoding="utf-8"), r"<script|href=|url\(|@import")

    def test_english_and_turkish_readmes_match(self):
        en = (ROOT / "README.md").read_text(encoding="utf-8")
        tr = (ROOT / "README.tr.md").read_text(encoding="utf-8")
        heads = lambda t: [len(m.group(1)) for m in re.finditer(r"^(#{1,6})\s", t, re.M)]
        self.assertEqual(heads(en), heads(tr), "same heading structure")
        self.assertIn('href="README.tr.md"', en)
        self.assertIn('href="README.md"', tr)
        self.assertIn("where-en-dark.svg", en)
        self.assertIn("where-tr-dark.svg", tr)
        for cmd in re.findall(r"^lane \S+", en, re.M):
            self.assertIn(cmd, tr, "same commands in both")
        self.assertEqual(en.count("311 tests"), tr.count("311 test"), "same test count in both")


if __name__ == "__main__":
    unittest.main()
