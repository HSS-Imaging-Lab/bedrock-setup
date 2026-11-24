#!/usr/bin/env python3
"""Fail when counts of mocks, casts, ignores, or `Any` rise, or a new file passes 800 lines.

Copy this file to `scripts/check_ratchet.py` in a project. The baseline lives
in `scripts/ratchet.toml`. The script scans the files `git` tracks plus new
files not yet ignored or added, so it also runs straight from the skill folder
against any repo for a read-only audit.

Standard library only, Python 3.10 or newer, so a host's `python3` runs it.

Usage:
    python3 scripts/check_ratchet.py                  compare with the baseline
    python3 scripts/check_ratchet.py --list casts     show every hit in a category
    python3 scripts/check_ratchet.py --list long_files  show files over 800 lines, longest first
    python3 scripts/check_ratchet.py --json           print the current counts
    python3 scripts/check_ratchet.py --rev HEAD~5     count a past revision
    python3 scripts/check_ratchet.py --init           write the first baseline
    python3 scripts/check_ratchet.py --update         lower the baseline

Effects are not counted (owner, 17 Nov 2026): they follow react.dev "You Might
Not Need an Effect", reviewed, not ratcheted. A baseline that still has an
`effects` key is read, the key is ignored, and a note asks you to delete it.

Counts only go down. Raising one is a hand edit of `scripts/ratchet.toml`,
reviewed in the pull request.

A `.py`, `.ts`, or `.tsx` file over 800 physical lines fails unless the baseline's
`long_files` list names it. The list only shrinks: `--update` drops a file once it is
back to 800 lines or fewer, and refuses while a file over 800 is not listed. Fixture
folders (`fixtures/`, `__fixtures__/`) are exempt from this check only. A baseline
with no `long_files` key is read with a note and skips the check until `--update`.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path, PurePosixPath

BASELINE_PATH = "scripts/ratchet.toml"

LONG_FILE_LINES = 800
# Fixture data is long on purpose; these globs skip the long-file check only.
LONG_FILE_EXEMPT = ("**/__fixtures__/**", "**/fixtures/**")
LONG_FILES = "long_files"

PY_SUFFIXES = frozenset({".py"})
TS_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".cts"})

BUILTIN_EXCLUDES = (
    "vendor/**",
    "third_party/**",
    ".repos/**",
    "tests/data/**",
    "testdata/**",
    "**/node_modules/**",
)

# category -> language -> regex sources. Edit this table to change what counts.
PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "mocks": {
        "py": (
            r"\bunittest\.mock\b",
            r"\bfrom unittest import mock\b",
            r"\bpytest_mock\b",
            r"\bmocker\b",
            r"\bmonkeypatch\.setattr\b",
            r"\bmock\.patch\b",
        ),
        "ts": (
            r"\bvi\.mock\(",
            r"\bvi\.doMock\(",
            r"\bjest\.mock\(",
        ),
    },
    "casts": {
        "py": (r"\bcast\(",),
        "ts": (
            r"\bas\s+any\b",
            r"\bas\s+unknown\s+as\b",
            r"<any>",
        ),
    },
    "ignores": {
        "py": (
            r"#\s*type:\s*ignore",
            r"#\s*noqa\b",
            r"#\s*pyright:\s*ignore",
        ),
        "ts": (
            r"@ts-ignore",
            r"@ts-expect-error",
            r"eslint-disable",
            r"oxlint-disable",
            r"biome-ignore",
        ),
    },
    "any": {
        "py": (r"\bAny\b",),
        "ts": (
            r":\s*any\b",
            r"<any\b",
            r"\bany\[\]",
            r",\s*any\b",
        ),
    },
}

CATEGORIES = tuple(PATTERNS)
COMPILED: dict[str, dict[str, list[re.Pattern[str]]]] = {
    category: {lang: [re.compile(p) for p in sources] for lang, sources in langs.items()}
    for category, langs in PATTERNS.items()
}


# The ratchet never counts its own script: its docstrings name the patterns it counts.
SELF_PATH = "scripts/check_ratchet.py"
# Tokens that open a statement, so a string right after one is a docstring or bare string.
STATEMENT_START = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}


def blank_python_prose(text: str, *, comments: bool) -> str:
    """Blank docstrings (and comments when asked) so prose is not counted as code.

    Line numbers survive: every blanked character becomes a space, newlines stay.
    Text that does not tokenize is returned unchanged.
    """
    lines = text.splitlines(keepends=True)
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    previous = tokenize.NEWLINE
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            is_docstring = token.type == tokenize.STRING and previous in STATEMENT_START
            if is_docstring or (comments and token.type == tokenize.COMMENT):
                spans.append((token.start, token.end))
            if token.type not in (tokenize.COMMENT, tokenize.NL):
                previous = token.type
    except (tokenize.TokenError, SyntaxError):
        return text
    for (start_row, start_col), (end_row, end_col) in spans:
        for row in range(start_row, end_row + 1):
            line = lines[row - 1]
            first = start_col if row == start_row else 0
            last = end_col if row == end_row else len(line.rstrip("\r\n"))
            lines[row - 1] = line[:first] + " " * (last - first) + line[last:]
    return "".join(lines)


class RatchetError(Exception):
    """A problem the user can fix, reported without a traceback."""


def parse_baseline(text: str) -> dict[str, object]:
    """Parse the baseline file this script writes.

    Uses `tomllib` where it exists (Python 3.11+). On 3.10 it reads the subset
    `format_baseline` writes: comments, an `exclude` list, a `long_files` list
    (one line, or one JSON string per line with an optional trailing comma),
    and a `[counts]` table of integers, with comments on their own lines.

    Raises:
        RatchetError: If a line is outside that subset.

    """
    try:
        import tomllib
    except ModuleNotFoundError:
        pass
    else:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise RatchetError(f"{BASELINE_PATH} is not valid TOML: {error}") from error

    parsed: dict[str, object] = {}
    counts: dict[str, object] | None = None
    open_list: list[str] | None = None
    list_start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if open_list is not None:
            open_list.append(line)
            if line.endswith("]"):
                parsed[LONG_FILES] = parse_string_list(" ".join(open_list), list_start)
                open_list = None
            continue
        if line == "[counts]":
            counts = {}
            parsed["counts"] = counts
            continue
        key, sep, value = (part.strip() for part in line.partition("="))
        if not sep:
            raise RatchetError(f"{BASELINE_PATH}:{number}: cannot read {raw!r}")
        if counts is None and key == "exclude":
            try:
                parsed["exclude"] = json.loads(value)
            except json.JSONDecodeError as error:
                raise RatchetError(f"{BASELINE_PATH}:{number}: bad exclude list") from error
        elif counts is None and key == LONG_FILES:
            if value == "[" or (value.startswith("[") and not value.endswith("]")):
                open_list, list_start = [value], number
            else:
                parsed[LONG_FILES] = parse_string_list(value, number)
        elif counts is not None and value.lstrip("-").isdigit():
            counts[key] = int(value)
        else:
            raise RatchetError(f"{BASELINE_PATH}:{number}: cannot read {raw!r}")
    if open_list is not None:
        raise RatchetError(f"{BASELINE_PATH}:{list_start}: unterminated {LONG_FILES} list")
    return parsed


def parse_string_list(value: str, number: int) -> list[object]:
    """Read a TOML list of strings written as JSON, allowing a trailing comma.

    Raises:
        RatchetError: If the text is not a list of JSON strings.

    """
    try:
        items = json.loads(re.sub(r",\s*\]$", "]", value))
    except json.JSONDecodeError as error:
        raise RatchetError(f"{BASELINE_PATH}:{number}: bad {LONG_FILES} list") from error
    if not isinstance(items, list):
        raise RatchetError(f"{BASELINE_PATH}:{number}: bad {LONG_FILES} list")
    return items


def git(root: Path | None, *args: str) -> str:
    """Run a git command and return its stdout.

    Args:
        root: Directory to run in, or None for the current directory.
        *args: Arguments after `git`.

    Returns:
        Standard output as text.

    Raises:
        RatchetError: If git fails.

    """
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RatchetError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def language_of(path: str) -> str | None:
    """Return "py" or "ts" for a scanned suffix, else None."""
    suffix = PurePosixPath(path).suffix
    if suffix in PY_SUFFIXES:
        return "py"
    if suffix in TS_SUFFIXES:
        return "ts"
    return None


def is_excluded(path: str, globs: list[str]) -> bool:
    """Return True when a posix path matches any exclude glob.

    A glob ending in `/**` also matches the folder's direct children, and a
    glob starting with `**/` also matches at the repo root.
    """
    for glob in globs:
        if fnmatch.fnmatchcase(path, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatchcase(path, glob[3:]):
            return True
    return False


def list_files(root: Path, rev: str | None) -> list[str]:
    """List files to scan: tracked and untracked-not-ignored, or a revision's tree."""
    if rev is None:
        out = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    else:
        out = git(root, "ls-tree", "-r", "--name-only", "-z", rev)
    return [name for name in out.split("\0") if name]


def read_file(root: Path, rev: str | None, path: str) -> str | None:
    """Read a file's text from the working tree or a revision.

    Returns:
        The text, or None when the file is missing (tracked but deleted) or
        is not a regular file such as a submodule.

    """
    if rev is not None:
        try:
            return git(root, "show", f"{rev}:{path}")
        except RatchetError:
            return None
    file = root / path
    if not file.is_file():
        return None
    return file.read_text(encoding="utf-8", errors="replace")


def load_baseline(root: Path) -> dict[str, object] | None:
    """Parse the baseline file, or return None when it does not exist."""
    file = root / BASELINE_PATH
    if not file.is_file():
        return None
    return parse_baseline(file.read_text(encoding="utf-8"))


def baseline_excludes(baseline: dict[str, object] | None) -> list[str]:
    """Return the `exclude` globs from a parsed baseline."""
    if baseline is None:
        return []
    value = baseline.get("exclude", [])
    if not isinstance(value, list) or not all(isinstance(g, str) for g in value):
        raise RatchetError(f"`exclude` in {BASELINE_PATH} must be a list of strings")
    return [str(g) for g in value]


def baseline_counts(baseline: dict[str, object]) -> dict[str, int]:
    """Return the `[counts]` table of a parsed baseline, one int per category.

    Raises:
        RatchetError: If a category is missing or not an integer.

    """
    table = baseline.get("counts")
    if not isinstance(table, dict):
        raise RatchetError(f"{BASELINE_PATH} has no [counts] table")
    if "effects" in table:
        print(
            f"Note: `effects` in {BASELINE_PATH} is no longer counted; delete the key.",
            file=sys.stderr,
        )
    counts: dict[str, int] = {}
    for category in CATEGORIES:
        value = table.get(category)
        if value is None:
            raise RatchetError(f"{BASELINE_PATH} has no `{category}` under [counts]")
        if not isinstance(value, int) or isinstance(value, bool):
            raise RatchetError(f"[counts] {category} in {BASELINE_PATH} must be an integer")
        counts[category] = value
    return counts


def baseline_long_files(baseline: dict[str, object]) -> list[str] | None:
    """Return the `long_files` list of a parsed baseline, or None when it has none.

    An older baseline without the key prints a note and skips the long-file check.

    Raises:
        RatchetError: If the value is not a list of strings.

    """
    if LONG_FILES not in baseline:
        print(
            f"Note: {BASELINE_PATH} has no long_files list; "
            f"run --update to record today's files over {LONG_FILE_LINES} lines.",
            file=sys.stderr,
        )
        return None
    value = baseline[LONG_FILES]
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise RatchetError(f"`long_files` in {BASELINE_PATH} must be a list of strings")
    return [str(p) for p in value]


def scan(
    root: Path,
    rev: str | None,
    excludes: list[str],
) -> tuple[dict[str, list[tuple[str, int, str]]], dict[str, int]]:
    """Find every pattern match and every file over the line limit.

    Args:
        root: Repo root.
        rev: Git revision to read, or None for the working tree.
        excludes: Globs from the baseline, added to the built-in ones.

    Returns:
        Category to a list of (path, line number, stripped line), where a line
        that matches twice appears twice; and each long file's path to its line
        count. Fixture folders are left out of the second only.

    """
    skip = [*BUILTIN_EXCLUDES, *excludes]
    hits: dict[str, list[tuple[str, int, str]]] = {c: [] for c in CATEGORIES}
    long_files: dict[str, int] = {}
    for path in list_files(root, rev):
        lang = language_of(path)
        if lang is None or path == SELF_PATH or is_excluded(path, skip):
            continue
        text = read_file(root, rev, path)
        if text is None:
            continue
        # Prose is not code: Python docstrings never count; comments count only for
        # the categories that are comments (ignores), not for `any`.
        docstring_free = blank_python_prose(text, comments=False) if lang == "py" else text
        code_only = blank_python_prose(text, comments=True) if lang == "py" else text
        shown = text.splitlines()
        if len(shown) > LONG_FILE_LINES and not is_excluded(path, list(LONG_FILE_EXEMPT)):
            long_files[path] = len(shown)
        for category in CATEGORIES:
            scanned = (code_only if category == "any" else docstring_free).splitlines()
            for number, line in enumerate(scanned, start=1):
                for pattern in COMPILED[category].get(lang, ()):
                    hits[category].extend(
                        (path, number, shown[number - 1].strip()) for _ in pattern.finditer(line)
                    )
    return hits, long_files


def format_baseline(counts: dict[str, int], excludes: list[str], long_files: list[str]) -> str:
    """Render the baseline file."""
    quoted = ", ".join(json.dumps(g) for g in excludes)
    if long_files:
        listed = [
            "long_files = [",
            *(f"  {json.dumps(p)}," for p in sorted(long_files)),
            "]",
        ]
    else:
        listed = ["long_files = []"]
    lines = [
        (
            "# Counts only go down. After a cleanup, "
            "`python3 scripts/check_ratchet.py --update` lowers them."
        ),
        f"exclude = [{quoted}]",
        (
            f"# Files over {LONG_FILE_LINES} lines when recorded. "
            "The list only shrinks: split a file to drop it."
        ),
        *listed,
        "",
        "[counts]",
        *(f"{category} = {counts[category]}" for category in CATEGORIES),
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Fail when mock, cast, ignore, or Any counts rise above the baseline, "
        f"or a new file passes {LONG_FILE_LINES} lines."
    )
    parser.add_argument(
        "--list",
        dest="list_category",
        choices=(*CATEGORIES, LONG_FILES),
        metavar="CATEGORY",
        help=f"print every hit in one category ({', '.join(CATEGORIES)}), "
        f"or every file over {LONG_FILE_LINES} lines, longest first ({LONG_FILES})",
    )
    parser.add_argument("--json", action="store_true", help="print the current counts as JSON")
    parser.add_argument("--rev", help="count the files as they were at this git revision")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--init",
        action="store_true",
        help=f"write {BASELINE_PATH} at the current counts",
    )
    mode.add_argument(
        "--update", action="store_true", help="lower the baseline to the current counts"
    )
    return parser.parse_args(argv)


def init_baseline(
    root: Path, current: dict[str, int], long_files: dict[str, int], *, exists: bool
) -> int:
    """Write the first baseline at the current counts and long files."""
    if exists:
        print(f"{BASELINE_PATH} already exists. Use --update.", file=sys.stderr)
        return 1
    target = root / BASELINE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_baseline(current, [], list(long_files)), encoding="utf-8")
    print(
        f"Wrote {BASELINE_PATH}: "
        + ", ".join(f"{c} {current[c]}" for c in CATEGORIES)
        + f", {LONG_FILES} {len(long_files)}"
    )
    return 0


def update_baseline(
    root: Path,
    current: dict[str, int],
    risen: list[str],
    excludes: list[str],
    long_files: dict[str, int],
    unlisted: list[str],
) -> int:
    """Lower the baseline to the current state, refusing when a count rose or a long file is new."""
    if risen or unlisted:
        if risen:
            print(
                f"Not updating: {', '.join(risen)} rose. Remove the new hits, or raise "
                f"{BASELINE_PATH} by hand and explain it in the PR.",
                file=sys.stderr,
            )
        if unlisted:
            print(
                f"Not updating: {', '.join(unlisted)} over {LONG_FILE_LINES} lines and not in "
                f"{LONG_FILES}. Split them, or add them to {BASELINE_PATH} by hand and "
                "explain it in the PR.",
                file=sys.stderr,
            )
        return 1
    target = root / BASELINE_PATH
    target.write_text(format_baseline(current, excludes, list(long_files)), encoding="utf-8")
    print(f"Updated {BASELINE_PATH}.")
    return 0


def compare(
    recorded: dict[str, int],
    current: dict[str, int],
    risen: list[str],
    listed: list[str] | None,
    long_files: dict[str, int],
) -> int:
    """Print the baseline table, name every category that rose, and flag long files.

    Args:
        recorded: Baseline count per category.
        current: Current count per category.
        risen: Categories whose count rose.
        listed: The baseline's `long_files`, or None when it has no such list.
        long_files: Each long file's path to its line count.

    Returns:
        1 when a count rose or a long file is not listed, else 0.

    """
    width = max(len(c) for c in (*CATEGORIES, LONG_FILES))
    print(f"{'category':<{width}}  baseline  current")
    for category in CATEGORIES:
        print(f"{category:<{width}}  {recorded[category]:>8}  {current[category]:>7}")
    recorded_long = "-" if listed is None else str(len(listed))
    print(f"{LONG_FILES:<{width}}  {recorded_long:>8}  {len(long_files):>7}")
    for category in risen:
        print(
            f"{category} rose from {recorded[category]} to {current[category]}. "
            f"Run `python3 scripts/check_ratchet.py --list {category}` to see the hits."
        )
    unlisted = unlisted_long_files(listed, long_files)
    for path in unlisted:
        print(
            f"{path} has {long_files[path]} lines; the limit is {LONG_FILE_LINES}. "
            f"Split it by domain concept, or see --list {LONG_FILES}."
        )
    for path in sorted(set(listed or ()) - set(long_files)):
        print(
            f"{path} is no longer over {LONG_FILE_LINES} lines; "
            "--update drops it from the baseline."
        )
    return 1 if risen or unlisted else 0


def unlisted_long_files(listed: list[str] | None, long_files: dict[str, int]) -> list[str]:
    """Return the long files the baseline does not list, sorted; none when it has no list."""
    if listed is None:
        return []
    return sorted(path for path in long_files if path not in listed)


def run(args: argparse.Namespace) -> int:
    """Run the command chosen by the parsed arguments and return the exit code."""
    root = Path(git(None, "rev-parse", "--show-toplevel").strip())
    baseline = load_baseline(root)
    excludes = baseline_excludes(baseline)
    hits, long_files = scan(root, args.rev, excludes)
    current = {category: len(found) for category, found in hits.items()}

    if args.list_category:
        try:
            if args.list_category == LONG_FILES:
                for path, length in sorted(
                    long_files.items(), key=lambda item: (-item[1], item[0])
                ):
                    print(f"{path}: {length} lines")
            else:
                for path, number, text in hits[args.list_category]:
                    print(f"{path}:{number}: {text}")
        except BrokenPipeError:
            sys.stderr.close()
        return 0
    if args.json:
        print(json.dumps({**current, LONG_FILES: len(long_files)}, indent=2))
        return 0
    if args.init:
        return init_baseline(root, current, long_files, exists=baseline is not None)
    if baseline is None:
        print(f"No {BASELINE_PATH}. Run with --init to write it.", file=sys.stderr)
        return 1

    recorded = baseline_counts(baseline)
    listed = baseline_long_files(baseline)
    risen = [c for c in CATEGORIES if current[c] > recorded[c]]
    if args.update:
        unlisted = unlisted_long_files(listed, long_files)
        return update_baseline(root, current, risen, excludes, long_files, unlisted)
    return compare(recorded, current, risen, listed, long_files)


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    try:
        return run(args)
    except RatchetError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
