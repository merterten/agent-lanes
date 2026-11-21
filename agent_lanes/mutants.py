#!/usr/bin/env python3
"""Suggest mutation-testing candidates for the lines a lane changed.

The output is a JSON list in the shape accept.py consumes:

    [{"name": str, "file": "relative/path", "find": str, "replace": str}]

The worktree is only ever read.  Git is called with argument lists, never a
shell, and worker-produced files are data only: nothing here executes them.
"""
import argparse
import ast
import bisect
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

PY_EXT = {".py"}
CS_EXT = {".cs"}
JS_EXT = {".js", ".mjs", ".ts"}
SH_EXT = {".sh"}
SOURCE_EXT = PY_EXT | CS_EXT | JS_EXT | SH_EXT

RANK_RETURN = 0
RANK_COMPARE = 1
RANK_BOOL = 2
RANK_CONST = 3

CMP_FLIP = {
    ast.Lt: ("<", "<="),
    ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="),
    ast.GtE: (">=", ">"),
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
    ast.In: ("in", "not in"),
    ast.NotIn: ("not in", "in"),
    ast.Is: ("is", "is not"),
    ast.IsNot: ("is not", "is"),
}
BOOL_FLIP = {ast.And: ("and", "or"), ast.Or: ("or", "and")}
CMP_TEXT_FLIP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "=="}
SHELL_CMP_FLIP = {"-eq": "-ne", "-ne": "-eq", "-lt": "-ge", "-ge": "-lt"}

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
CMP_TEXT_RE = re.compile(r"<=|>=|==|!=|<|>")
LOGIC_RE = re.compile(r"&&|\|\|")
BOOL_RE = re.compile(r"\b(true|false)\b")
NOT_RE = re.compile(r"!(?=\s*[A-Za-z_(])")
JS_RETURN_RE = re.compile(r"\breturn\s+([^;\n]+);")
CS_BOOL_RETURN_RE = re.compile(r"\breturn\s+(true|false)\s*;")
NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
SHELL_CMP_RE = re.compile(r"(?<![-\w])-(eq|ne|lt|ge)(?!\w)")
SHELL_EXIT_RE = re.compile(r"\bexit\s+1\b")


def _norm_path(value):
    text = str(value).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _filter_path(worktree, value):
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(worktree.resolve()).as_posix()
        except ValueError:
            return _norm_path(value)
    return _norm_path(value)


def _git(worktree, args):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(["git", "-C", str(worktree), *args],
                              capture_output=True, env=env)
    except OSError as error:
        raise RuntimeError(f"cannot run git: {error}") from error
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or f"git exited {proc.returncode}")
    return proc.stdout


def _tracked_changes(worktree, base):
    try:
        raw = _git(worktree, ["-c", "core.quotePath=false", "diff", "-U0", "--no-color",
                              "--no-ext-diff", "--no-renames", base, "--"])
    except RuntimeError as error:
        if base.lower() == "head" and "bad revision" in str(error).lower():
            return {}
        raise
    changed = {}
    current = None
    new_line = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0]
            if path == "/dev/null":
                current = None
            else:
                if path.startswith("b/"):
                    path = path[2:]
                current = _norm_path(path)
        elif line.startswith("@@"):
            match = HUNK_RE.match(line)
            if match is not None and current is not None:
                new_line = int(match.group(1))
        elif current is not None:
            if line.startswith("+"):
                changed.setdefault(current, set()).add(new_line)
                new_line += 1
            elif line.startswith("-"):
                pass
            elif line.startswith(" "):
                new_line += 1
    return changed


def _untracked_changes(worktree):
    raw = _git(worktree, ["-c", "core.quotePath=false", "ls-files", "--others",
                          "--exclude-standard", "-z"])
    changed = {}
    for item in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not item:
            continue
        rel = _norm_path(item)
        try:
            data = (worktree / rel).read_bytes()
        except OSError:
            continue
        count = len(data.splitlines())
        changed[rel] = set(range(1, count + 1))
    return changed


def _is_skipped(rel):
    parts = PurePosixPath(rel).parts
    if any(part in (".lane", ".lane.prev") for part in parts):
        return True
    name = parts[-1] if parts else rel
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith(("_test.py", ".test.js", "Tests.cs")):
        return True
    if any(part in ("tests", "test") for part in parts[:-1]):
        return True
    return PurePosixPath(rel).suffix.lower() not in SOURCE_EXT


def _language(rel):
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix == ".py":
        return "py"
    if suffix == ".cs":
        return "cs"
    if suffix == ".sh":
        return "sh"
    if suffix == ".ts":
        return "ts"
    return "js"


def _count_occurrences(haystack, needle):
    if not needle:
        return 0
    count = 0
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found < 0:
            return count
        count += 1
        start = found + 1


def _line_mutant(source, lines, line_no, new_line, rel, label, rank):
    index = line_no - 1
    if index < 0 or index >= len(lines) or new_line == lines[index]:
        return None
    low = index
    while True:
        find = b"".join(lines[low:index + 1])
        replace = b"".join(lines[low:index] + [new_line])
        if find and replace != find and _count_occurrences(source, find) == 1:
            try:
                find_text = find.decode("utf-8")
                replace_text = replace.decode("utf-8")
            except UnicodeDecodeError:
                return None
            return {
                "name": f"{_language(rel)}:{rel}:{line_no} {label}",
                "file": rel,
                "find": find_text,
                "replace": replace_text,
                "_rank": rank,
                "_line": line_no,
                "_rule": label,
            }
        if low == 0 or index - low + 1 >= 3:
            return None
        low -= 1


def _find_token(source, start, end, token, word):
    region = source[start:end]
    pattern = rb"\s+".join(re.escape(part.encode("utf-8")) for part in token.split())
    if word:
        pattern = rb"(?<![A-Za-z0-9_])" + pattern + rb"(?![A-Za-z0-9_])"
    match = re.search(pattern, region)
    if match is None:
        return None
    return start + match.start(), start + match.end()


def _keyword_span(call, keyword, source, position):
    if keyword.arg is None:
        return None
    elements = list(call.args) + list(call.keywords)
    elements.sort(key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
    try:
        index = elements.index(keyword)
    except ValueError:
        return None
    start = position(keyword.lineno, keyword.col_offset)
    end = position(keyword.end_lineno, keyword.end_col_offset)
    if start is None or end is None:
        return None
    if index > 0:
        previous = elements[index - 1]
        previous_end = position(previous.end_lineno, previous.end_col_offset)
        if previous_end is None or b"," not in source[previous_end:start]:
            return None
        return previous_end, end
    if index + 1 < len(elements):
        following = elements[index + 1]
        following_start = position(following.lineno, following.col_offset)
        if following_start is None:
            return None
        return start, following_start
    scan = end
    while scan < len(source) and source[scan:scan + 1] in (b" ", b"\t"):
        scan += 1
    if source[scan:scan + 1] == b",":
        end = scan + 1
    return start, end


def _python_mutants(rel, changed_lines, text, raw):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = raw.splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)

    def position(lineno, column):
        if lineno is None or column is None or lineno < 1 or lineno > len(starts):
            return None
        return starts[lineno - 1] + column

    def line_index(offset):
        return bisect.bisect_right(starts, offset) - 1 if starts else 0

    mutants = []

    def emit(start, end, replacement, label, rank):
        if start is None or end is None or end <= start:
            return
        first = line_index(start)
        last = line_index(end)
        if first != last or first >= len(lines):
            return
        line_no = first + 1
        if line_no not in changed_lines:
            return
        line = lines[first]
        rel_start = start - starts[first]
        rel_end = end - starts[first]
        if rel_start < 0 or rel_end > len(line):
            return
        new_line = line[:rel_start] + replacement + line[rel_end:]
        mutated = raw[:start] + replacement + raw[end:]
        try:
            ast.parse(mutated)
        except SyntaxError:
            return
        candidate = _line_mutant(raw, lines, line_no, new_line, rel, label, rank)
        if candidate is not None:
            mutants.append(candidate)

    def span(node):
        return (position(node.lineno, node.col_offset),
                position(node.end_lineno, node.end_col_offset))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left = node.left
            for operator, right in zip(node.ops, node.comparators):
                flip = CMP_FLIP.get(type(operator))
                if flip is None:
                    continue
                old, new = flip
                left_end = position(left.end_lineno, left.end_col_offset)
                right_start = position(right.lineno, right.col_offset)
                if left_end is None or right_start is None or right_start < left_end:
                    left = right
                    continue
                token_span = _find_token(raw, left_end, right_start, old, True)
                if token_span is not None:
                    emit(token_span[0], token_span[1], new.encode("utf-8"),
                         f"flip {old} to {new}", RANK_COMPARE)
                left = right
        elif isinstance(node, ast.BoolOp):
            flip = BOOL_FLIP.get(type(node.op))
            if flip is not None:
                old, new = flip
                for left, right in zip(node.values, node.values[1:]):
                    left_end = position(left.end_lineno, left.end_col_offset)
                    right_start = position(right.lineno, right.col_offset)
                    if left_end is None or right_start is None or right_start < left_end:
                        continue
                    token_span = _find_token(raw, left_end, right_start, old, True)
                    if token_span is not None:
                        emit(token_span[0], token_span[1], new.encode("utf-8"),
                             f"swap {old} to {new}", RANK_BOOL)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand_start, operand_end = span(node.operand)
            node_start, node_end = span(node)
            if None not in (operand_start, operand_end, node_start, node_end):
                emit(node_start, node_end, raw[operand_start:operand_end],
                     "remove not", RANK_BOOL)
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            old = "True" if node.value else "False"
            new = "False" if node.value else "True"
            start, end = span(node)
            emit(start, end, new.encode("utf-8"), f"swap {old} to {new}", RANK_BOOL)
        elif (isinstance(node, ast.Constant)
              and isinstance(node.value, (int, float))
              and not isinstance(node.value, bool)):
            start, end = span(node)
            if isinstance(node.value, int):
                plus = str(node.value + 1)
            else:
                plus = repr(node.value + 1)
            emit(start, end, plus.encode("utf-8"),
                 f"numeric +1 ({node.value} -> {plus})", RANK_CONST)
            emit(start, end, b"0", f"numeric 0 ({node.value} -> 0)", RANK_CONST)
        elif isinstance(node, ast.If):
            if any(isinstance(statement, (ast.Return, ast.Raise)) for statement in node.body):
                start, end = span(node.test)
                emit(start, end, b"False", "disable guard (if False)", RANK_RETURN)
        elif isinstance(node, ast.Return):
            value = node.value
            if value is not None and not (isinstance(value, ast.Constant) and value.value is None):
                start, end = span(value)
                emit(start, end, b"None", "return -> None", RANK_RETURN)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                token_span = _keyword_span(node, keyword, raw, position)
                if token_span is not None:
                    emit(token_span[0], token_span[1], b"",
                         f"drop keyword {keyword.arg!r}", RANK_COMPARE)
    return mutants


def _code_mask(line, lang, in_block=False):
    mask = [True] * len(line)
    quote = None
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_block:
            mask[index] = False
            if line.startswith("*/", index):
                if index + 1 < len(line):
                    mask[index + 1] = False
                index += 2
                in_block = False
            else:
                index += 1
            continue
        if quote is not None:
            mask[index] = False
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if lang in ("cs", "js", "ts") and line.startswith("//", index):
            for rest in range(index, len(line)):
                mask[rest] = False
            break
        if lang in ("cs", "js", "ts") and line.startswith("/*", index):
            mask[index] = False
            if index + 1 < len(line):
                mask[index + 1] = False
            index += 2
            in_block = True
            continue
        if lang == "sh" and char == "#" and (index == 0 or line[index - 1].isspace()):
            for rest in range(index, len(line)):
                mask[rest] = False
            break
        if char in ('"', "'"):
            quote = char
            mask[index] = False
        elif lang in ("js", "ts") and char == "`":
            quote = "`"
            mask[index] = False
        index += 1
    return mask, in_block


def _mask_ok(mask, start, end):
    return 0 <= start <= end <= len(mask) and all(mask[start:end])


def _increment_literal(token):
    if "." in token:
        return repr(float(token) + 1)
    return str(int(token) + 1)


def _text_rules(content, lang, mask):
    if lang in ("cs", "js", "ts"):
        for match in CMP_TEXT_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            token = match.group()
            if token == "<":
                previous = content[match.start() - 1:match.start()]
                following = content[match.start() + 1:match.start() + 2]
                if following in ("<", ">", "=") or previous in ("<", ">", "="):
                    continue
            if token == ">":
                previous = content[match.start() - 1:match.start()]
                following = content[match.start() + 1:match.start() + 2]
                if following in (">", "=") or previous in ("=", "-", "<", ">"):
                    continue
            replacement = CMP_TEXT_FLIP[token]
            yield f"flip {token} to {replacement}", replacement, RANK_COMPARE, match
        for match in LOGIC_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            token = match.group()
            replacement = "||" if token == "&&" else "&&"
            yield f"swap {token} to {replacement}", replacement, RANK_BOOL, match
        for match in BOOL_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            token = match.group()
            replacement = "false" if token == "true" else "true"
            yield f"swap {token} to {replacement}", replacement, RANK_BOOL, match
        for match in NOT_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            yield "remove !", "", RANK_BOOL, match
        if lang in ("js", "ts"):
            for match in JS_RETURN_RE.finditer(content):
                if not (_mask_ok(mask, match.start(), match.start() + 6)
                        and _mask_ok(mask, match.end() - 1, match.end())):
                    continue
                expression = match.group(1).strip()
                if expression in ("null", "void 0"):
                    continue
                yield "return expr -> null", "return null;", RANK_RETURN, match
        else:
            for match in CS_BOOL_RETURN_RE.finditer(content):
                if not _mask_ok(mask, match.start(), match.end()):
                    continue
                token = match.group(1)
                replacement = "false" if token == "true" else "true"
                yield (f"return {token} -> {replacement}",
                       f"return {replacement};", RANK_RETURN, match)
        for match in NUM_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            token = match.group()
            replacement = _increment_literal(token)
            if replacement != token:
                yield (f"numeric +1 ({token} -> {replacement})", replacement,
                       RANK_CONST, match)
    else:
        for match in SHELL_CMP_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            token = match.group()
            replacement = SHELL_CMP_FLIP[token]
            yield f"flip {token} to {replacement}", replacement, RANK_COMPARE, match
        for match in LOGIC_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            token = match.group()
            replacement = "||" if token == "&&" else "&&"
            yield f"swap {token} to {replacement}", replacement, RANK_BOOL, match
        for match in SHELL_EXIT_RE.finditer(content):
            if not _mask_ok(mask, match.start(), match.end()):
                continue
            yield "exit 1 -> exit 0", "exit 0", RANK_RETURN, match


def _text_mutants(rel, changed_lines, raw):
    lang = _language(rel)
    lines = raw.splitlines(keepends=True)
    mutants = []
    in_block = False
    for index, line_bytes in enumerate(lines):
        line_no = index + 1
        try:
            line = line_bytes.decode("utf-8")
        except UnicodeDecodeError:
            in_block = False
            continue
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        mask, in_block = _code_mask(content, lang, in_block)
        if line_no not in changed_lines:
            continue
        if not any(mask):
            continue
        for label, replacement, rank, match in _text_rules(content, lang, mask):
            new_content = content[:match.start()] + replacement + content[match.end():]
            if new_content == content:
                continue
            new_line = (new_content + ending).encode("utf-8")
            candidate = _line_mutant(raw, lines, line_no, new_line, rel, label, rank)
            if candidate is not None:
                mutants.append(candidate)
    return mutants


def propose(worktree, base, max_mutants=12, files=None):
    """Return mutation candidates for lines changed relative to *base*."""
    worktree = Path(worktree)
    if max_mutants < 0:
        max_mutants = 0
    changed = _tracked_changes(worktree, base)
    changed.update(_untracked_changes(worktree))
    if files is not None:
        wanted = {_filter_path(worktree, item) for item in files}
        changed = {rel: lines for rel, lines in changed.items() if rel in wanted}

    candidates = []
    for rel in sorted(changed):
        if _is_skipped(rel):
            continue
        try:
            raw = (worktree / rel).read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in PY_EXT:
            candidates.extend(_python_mutants(rel, changed[rel], text, raw))
        elif suffix in CS_EXT or suffix in JS_EXT or suffix in SH_EXT:
            candidates.extend(_text_mutants(rel, changed[rel], raw))

    by_key = {}
    for candidate in candidates:
        key = (candidate["file"], candidate["find"], candidate["replace"])
        previous = by_key.get(key)
        if previous is None or ((candidate["_rank"], candidate["_rule"])
                                < (previous["_rank"], previous["_rule"])):
            by_key[key] = candidate
    unique = list(by_key.values())
    unique.sort(key=lambda item: (item["_rank"], item["file"], item["_line"],
                                  item["_rule"], item["replace"], item["find"]))

    result = []
    per_line = {}
    for candidate in unique:
        if len(result) >= max_mutants:
            break
        key = (candidate["file"], candidate["_line"])
        if per_line.get(key, 0) >= 2:
            continue
        per_line[key] = per_line.get(key, 0) + 1
        result.append({key: candidate[key] for key in ("name", "file", "find", "replace")})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree", type=Path)
    parser.add_argument("base")
    parser.add_argument("--max", type=int, default=12, dest="max_mutants")
    parser.add_argument("--file", action="append", default=None, dest="files")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = propose(args.worktree, args.base, max_mutants=args.max_mutants,
                         files=args.files)
    except (OSError, RuntimeError) as error:
        print(f"mutants: {error}", file=sys.stderr)
        return 2
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
