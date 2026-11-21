"""Tests for tools/lanes/mutants.py.

    python3 -m unittest discover -s tools/lanes -p 'test_mutants*.py' -v

Every git repository is a throwaway under /tmp.  The real lanes root is
never touched and no network is used.
"""
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mutants  # noqa: E402

GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1")


def snapshot(root):
    digest = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest[rel] = "link:" + os.readlink(path)
            else:
                digest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-mutants-", dir="/tmp")
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "lane-test@example.invalid")
        self.git("config", "user.name", "Lane Test")
        self.git("commit", "-q", "--allow-empty", "-m", "empty base")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=check,
                              capture_output=True, text=True, env=GIT_ENV)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, files=None, message="base"):
        if files:
            for rel, text in files.items():
                self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def propose(self, **kwargs):
        return mutants.propose(self.repo, "HEAD", **kwargs)

    @staticmethod
    def names(result):
        return [item["name"] for item in result]

    @staticmethod
    def line_of(item):
        return int(item["name"].split(":")[2].split()[0])


class ChangedLinesTests(RepoCase):
    def test_no_changes_returns_empty(self):
        self.commit({"src/app.py": "def f(a, b):\n    return a >= b\n"})
        self.assertEqual(self.propose(), [])

    def test_only_changed_lines_are_mutated(self):
        original = ("def f(a, b):\n"
                    "    return a >= b\n\n"
                    "def g(a, b):\n"
                    "    return a >= b\n")
        self.commit({"src/app.py": original})
        changed = original.replace("    return a >= b\n",
                                   "    return a >= b  # changed\n", 1)
        self.write("src/app.py", changed)
        result = self.propose()
        self.assertTrue(any(name.startswith("py:src/app.py:2 ") for name in self.names(result)))
        self.assertFalse(any(name.startswith("py:src/app.py:5 ") for name in self.names(result)))

    def test_untracked_file_is_included(self):
        self.commit({"src/tracked.py": "x = 1\n"})
        self.write("src/new.py", "def f(a, b):\n    return a > b\n")
        result = self.propose()
        self.assertTrue(any(item["file"] == "src/new.py" for item in result))
        self.assertTrue(any("flip > to >=" in item["name"] for item in result))

    def test_text_only_changed_lines_are_mutated(self):
        original = ("class C {\n"
                    "    bool A(int a, int b) { return a >= b; }\n"
                    "    bool B(int a, int b) { return a >= b; }\n"
                    "}\n")
        self.commit({"src/Foo.cs": original})
        changed = original.replace(
            "bool A(int a, int b) { return a >= b; }",
            "bool A(int a, int b) { return a >= b; } // changed")
        self.write("src/Foo.cs", changed)
        result = self.propose()
        lines = {self.line_of(item) for item in result if item["file"] == "src/Foo.cs"}
        self.assertIn(2, lines)
        self.assertNotIn(3, lines)

    def test_ignored_untracked_file_is_not_included(self):
        self.commit({".gitignore": "ignored.py\n",
                     "src/base.py": "x = 1\n"})
        self.write("ignored.py", "x = a >= b\n")
        self.write("src/visible.py", "y = a >= b\n")
        result = self.propose()
        files = {item["file"] for item in result}
        self.assertNotIn("ignored.py", files)
        self.assertIn("src/visible.py", files)

    def test_file_filter_limits_output(self):
        self.commit({"src/a.py": "x = a >= b\n", "src/b.py": "y = a >= b\n"})
        self.write("src/a.py", "x = a > b\n")
        self.write("src/b.py", "y = a > b\n")
        result = self.propose(files=["src/a.py"])
        self.assertTrue(result)
        self.assertEqual({item["file"] for item in result}, {"src/a.py"})


class SkipRuleTests(RepoCase):
    def test_test_files_and_test_directories_are_skipped(self):
        files = {
            "tests/test_a.py": "x = a >= b\n",
            "test_root.py": "x = a >= b\n",
            "src/b_test.py": "x = a >= b\n",
            "src/c.test.js": "const x = a >= b;\n",
            "src/DTests.cs": "var x = a >= b;\n",
        }
        self.commit(files)
        for rel in files:
            self.write(rel, files[rel].replace(">=", ">"))
        self.write("tests/test_new.py", "x = a >= b\n")
        self.assertEqual(self.propose(), [])

    def test_lane_directories_are_skipped(self):
        self.commit({"src/base.py": "x = 1\n"})
        self.write(".lane/forged.py", "x = a >= b\n")
        self.write(".lane.prev/forged.py", "x = a >= b\n")
        self.assertEqual(self.propose(), [])

    def test_non_source_files_are_skipped(self):
        self.commit({"src/base.py": "x = 1\n"})
        self.write("README.md", "a >= b && true\n")
        self.write("data.json", '{"x": "a >= b"}\n')
        self.write("src/style.css", "a >= b;\n")
        self.assertEqual(self.propose(), [])


class UniquenessTests(RepoCase):
    def test_unique_widening_includes_previous_line(self):
        self.commit({"src/seed.py": "x = 0\n"})
        text = "a = 1\nx = foo >= bar\nx = foo >= bar\n"
        self.write("src/dup.py", text)
        result = [item for item in self.propose(max_mutants=100)
                  if item["file"] == "src/dup.py" and "flip >= to >" in item["name"]]
        self.assertEqual({self.line_of(item) for item in result}, {2, 3})
        for item in result:
            self.assertIn("\n", item["find"])
            self.assertEqual(text.count(item["find"]), 1)

    def test_line_still_non_unique_after_widening_is_skipped(self):
        self.write("src/same.py", "x = foo >= bar\n" * 4)
        self.assertEqual(self.propose(), [])


class PythonRuleTests(RepoCase):
    def test_comparison_boolean_and_constant_rules(self):
        text = ("result = a < b\n"
                "result = a <= b\n"
                "result = a > b\n"
                "result = a >= b\n"
                "result = a == b\n"
                "result = a != b\n"
                "result = a in b\n"
                "result = a not in b\n"
                "result = a is b\n"
                "result = a is not b\n"
                "flag = a and b\n"
                "flag = a or b\n"
                "flag = not a\n"
                "value = True\n"
                "value = False\n")
        self.write("src/rules.py", text)
        joined = "\n".join(self.names(self.propose(max_mutants=100)))
        for label in ("flip < to <=", "flip <= to <", "flip > to >=",
                      "flip >= to >", "flip == to !=", "flip != to ==",
                      "flip in to not in", "flip not in to in",
                      "flip is to is not", "flip is not to is",
                      "swap and to or", "swap or to and", "remove not",
                      "swap True to False", "swap False to True"):
            self.assertIn(label, joined)

    def test_numeric_rules(self):
        self.write("src/numbers.py", "a = 1\nb = 2.5\nc = 0\n")
        names = "\n".join(self.names(self.propose(max_mutants=100)))
        self.assertIn("numeric +1 (1 -> 2)", names)
        self.assertIn("numeric 0 (2.5 -> 0)", names)

    def test_if_guard_and_return_rules(self):
        self.write("src/guard.py",
                   "def f(x):\n"
                   "    if x:\n"
                   "        return 1\n"
                   "    if not x:\n"
                   "        raise ValueError('bad')\n"
                   "    return x\n")
        result = self.propose(max_mutants=100)
        names = "\n".join(self.names(result))
        self.assertIn("disable guard (if False)", names)
        self.assertIn("return -> None", names)
        self.assertEqual(sum("disable guard (if False)" in item["name"]
                             for item in result), 2)

    def test_keyword_argument_can_be_dropped(self):
        self.write("src/call.py", "result = call(a, keep=1, drop=2)\n")
        result = self.propose(max_mutants=100)
        self.assertTrue(any("drop keyword 'drop'" in item["name"] for item in result))
        dropped = next(item for item in result if "drop keyword 'drop'" in item["name"])
        self.assertNotIn("drop=2", dropped["replace"])
        self.assertIn("keep=1", dropped["replace"])

    def test_every_python_mutant_parses(self):
        text = ("def f(x, a, b, flag):\n"
                "    if x > 0:\n"
                "        return True\n"
                "    if x < 0:\n"
                "        raise ValueError('x')\n"
                "    y = a >= b and not flag\n"
                "    z = a == b or a != b\n"
                "    w = call(a, keep=1, drop=2)\n"
                "    n = 5\n"
                "    m = 2.5\n"
                "    k = False\n"
                "    return y\n")
        self.write("src/every.py", text)
        result = self.propose(max_mutants=200)
        self.assertGreater(len(result), 10)
        for item in result:
            self.assertEqual(text.count(item["find"]), 1)
            mutated = text.replace(item["find"], item["replace"], 1)
            ast.parse(mutated)
            self.assertNotEqual(item["find"], item["replace"])


class TextRuleTests(RepoCase):
    def test_csharp_rules(self):
        text = ("class C {\n"
                "    bool A(int x, int y) {\n"
                "        var a = x < y;\n"
                "        var b = x >= y;\n"
                "        var c = x == y;\n"
                "        var d = x && y;\n"
                "        var e = x || y;\n"
                "        var f = true;\n"
                "        var g = false;\n"
                "        var h = !flag;\n"
                "        var i = !(x && y);\n"
                "        var n = 41;\n"
                "        return true;\n"
                "    }\n"
                "}\n")
        self.write("src/Foo.cs", text)
        joined = "\n".join(self.names(self.propose(max_mutants=100)))
        for label in ("flip < to <=", "flip >= to >", "flip == to !=",
                      "swap && to ||", "swap || to &&", "swap true to false",
                      "swap false to true", "remove !", "numeric +1 (41 -> 42)",
                      "return true -> false"):
            self.assertIn(label, joined)

    def test_csharp_non_bool_return_is_not_mutated(self):
        self.write("src/R.cs", "class R {\n    int F() {\n        return value;\n    }\n}\n")
        self.assertEqual(self.propose(), [])

    def test_comment_and_string_only_lines_are_skipped(self):
        self.write("src/Notes.cs",
                   "// var a = x < y;\n"
                   "/* var b = x > y; */\n"
                   "\"a < b && c > d\";\n"
                   "var s = \"a < b && c > d\";\n"
                   "var t = 'x';\n")
        self.assertEqual(self.propose(), [])

    def test_multiline_block_comment_is_skipped(self):
        self.write("src/Block.cs",
                   "/*\n"
                   "var a = x < y && true;\n"
                   "*/\n"
                   "var b = x < y;\n")
        result = self.propose()
        self.assertTrue(any(self.line_of(item) == 4 for item in result))
        self.assertFalse(any(self.line_of(item) == 2 for item in result))

    def test_js_rules(self):
        text = ("function f(x, y) {\n"
                "  if (x < y) { return x; }\n"
                "  var a = x >= y;\n"
                "  var b = x == y;\n"
                "  var c = x && y;\n"
                "  var d = x || y;\n"
                "  var e = true;\n"
                "  var f = false;\n"
                "  var g = !flag;\n"
                "  var n = 41;\n"
                "  return y;\n"
                "}\n")
        self.write("src/app.js", text)
        result = self.propose(max_mutants=100)
        joined = "\n".join(self.names(result))
        for label in ("flip < to <=", "flip >= to >", "flip == to !=",
                      "swap && to ||", "swap || to &&", "swap true to false",
                      "swap false to true", "remove !", "numeric +1 (41 -> 42)",
                      "return expr -> null"):
            self.assertIn(label, joined)
        returned = next(item for item in result if "return expr -> null" in item["name"])
        self.assertIn("return null;", returned["replace"])

    def test_js_ts_and_mjs_extensions_are_included(self):
        for rel in ("src/a.js", "src/b.mjs", "src/c.ts"):
            self.write(rel, "const x = a >= b;\n")
        result = self.propose(max_mutants=100)
        self.assertEqual({item["file"] for item in result},
                         {"src/a.js", "src/b.mjs", "src/c.ts"})

    def test_shell_rules(self):
        text = ("if [ \"$a\" -lt \"$b\" ]; then\n"
                "  exit 1\n"
                "fi\n"
                "[ \"$a\" -eq 1 ]\n"
                "[ \"$x\" ] && [ \"$y\" ]\n"
                "[ \"$x\" ] || [ \"$y\" ]\n")
        self.write("scripts/run.sh", text)
        joined = "\n".join(self.names(self.propose(max_mutants=100)))
        for label in ("flip -lt to -ge", "flip -eq to -ne", "swap && to ||",
                      "swap || to &&", "exit 1 -> exit 0"):
            self.assertIn(label, joined)


class SelectionAndCliTests(RepoCase):
    def test_cap_keeps_guards_and_returns_first(self):
        self.write("src/cap.py",
                   "def f(a, b):\n"
                   "    if a > b:\n"
                   "        return True\n"
                   "    return a\n")
        result = self.propose(max_mutants=2)
        self.assertEqual(len(result), 2)
        for item in result:
            self.assertTrue("guard" in item["name"] or "return" in item["name"])

    def test_ranking_is_comparison_then_boolean_then_constant(self):
        self.write("src/rank.py", "a = x >= y\nb = x and y\nc = 7\n")
        result = self.propose(max_mutants=100)
        self.assertEqual([self.line_of(item) for item in result][:3], [1, 2, 3])
        self.assertIn("flip", result[0]["name"])
        self.assertIn("swap and", result[1]["name"])
        self.assertIn("numeric", result[2]["name"])

    def test_max_zero_returns_empty(self):
        self.write("src/x.py", "a = x >= y\n")
        self.assertEqual(self.propose(max_mutants=0), [])

    def test_output_is_deterministic(self):
        text = ("def f(x, a, b):\n"
                "    if x > 0:\n"
                "        return a >= b\n"
                "    return a and b\n")
        self.write("src/det.py", text)
        self.assertEqual(self.propose(max_mutants=100), self.propose(max_mutants=100))

    def test_cli_writes_valid_json(self):
        self.commit({"src/app.py": "x = a >= b\n"})
        self.write("src/app.py", "x = a > b  # changed\n")
        out = Path(self.tmp.name) / "mutants.json"
        proc = subprocess.run([sys.executable, str(HERE / "mutants.py"),
                               str(self.repo), "HEAD", "-o", str(out)],
                              capture_output=True, text=True, env=GIT_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)
        self.assertEqual(set(payload[0]), {"name", "file", "find", "replace"})

    def test_cli_stdout_is_json(self):
        self.commit({"src/app.py": "x = a >= b\n"})
        self.write("src/app.py", "x = a > b  # changed\n")
        proc = subprocess.run([sys.executable, str(HERE / "mutants.py"),
                               str(self.repo), "HEAD"],
                              capture_output=True, text=True, env=GIT_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)

    def test_propose_never_modifies_the_worktree(self):
        self.commit({"src/app.py": "def f(a, b):\n    return a >= b\n",
                     "src/Notes.cs": "var x = a && b;\n"})
        self.write("src/app.py", "def f(a, b):\n    return a > b  # changed\n")
        self.write("src/new.js", "const x = a == b;\n")
        before = snapshot(self.repo)
        self.propose(max_mutants=100)
        self.assertEqual(before, snapshot(self.repo))


if __name__ == "__main__":
    unittest.main()
