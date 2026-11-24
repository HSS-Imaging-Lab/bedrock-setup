#!/usr/bin/env python3
"""Fail when imports that skip a subfolder's public surface rise above the baseline.

Copy this file to `scripts/check_imports.py` in a project. The baseline lives in
`scripts/imports.toml`. The script scans the files `git` tracks plus new files
not yet ignored or added, so it also runs straight from the skill folder
against any repo for a read-only audit.

Standard library only, Python 3.10 or newer, so a host's `python3` runs it.

The rule, from the repo standard's "Subfolders" section: code outside a
subfolder's parent imports it through its `index.ts` (TypeScript and
JavaScript) or `__init__.py` (Python), never a file inside it. A subfolder only
its parent uses needs neither. Folder splits themselves are a readability
judgment for reviewers and are not checked here.

Source files are `.py .ts .tsx .mts .cts .js .jsx .mjs .cjs`. Tests (`*.test.*`,
`*.spec.*`, `test_*.py`, `*_test.py`, `conftest.py`, anything under `test/` or
`tests/`) are not checked as importers: a test imports the module it covers.

Usage:
    python3 scripts/check_imports.py             compare with the baseline
    python3 scripts/check_imports.py --list      show every finding
    python3 scripts/check_imports.py --json      print the current counts
    python3 scripts/check_imports.py --init      write the first baseline
    python3 scripts/check_imports.py --update    lower the baseline

Exit codes: 0 ok, 1 a count rose or a finding is new, 2 a usage or config error.

Counts only go down. Raising one is a hand edit of `scripts/imports.toml`,
reviewed in the pull request. With no `scripts/imports.toml` the script audits
against an empty baseline, so every finding is new. Settings in the file:

    exclude = []            globs added to the built-in excludes
    [aliases]               import prefix to repo path: "@/" = "frontend/src/"
    [deep_imports]          "subfolder" = imports that bypass its surface
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import posixpath
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

BASELINE_PATH = "scripts/imports.toml"

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
JS_SUFFIXES = SOURCE_SUFFIXES[1:]
# `import './x.js'` in a TypeScript file names the `.ts` file next to it.
JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}

BUILTIN_EXCLUDES = (
    "vendor/**",
    "third_party/**",
    ".repos/**",
    "tests/data/**",
    "testdata/**",
    "**/node_modules/**",
    "**/generated/**",
    "**/__fixtures__/**",
    "**/dist/**",
    "**/build/**",
)

CATEGORIES = ("deep_imports",)
SETTINGS_KEYS = frozenset({"exclude", "aliases", *CATEGORIES})

Counts = dict[str, dict[str, int]]


class ImportsError(Exception):
    """A usage or config problem the user can fix, reported without a traceback."""


@dataclass(frozen=True)
class Config:
    """Settings from the baseline file, or the defaults when there is none."""

    exclude: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeepImport:
    """One import that reaches past a subfolder's public surface."""

    importer: str
    line: int
    target: str
    subfolder: str


@dataclass(frozen=True)
class Findings:
    """Every import that bypasses a subfolder's surface, by subfolder."""

    deep_imports: dict[str, list[DeepImport]]

    def counts(self) -> Counts:
        """Return the number of bypassing imports per subfolder."""
        return {
            "deep_imports": {key: len(found) for key, found in sorted(self.deep_imports.items())},
        }


# ---------------------------------------------------------------------------
# Baseline file
# ---------------------------------------------------------------------------


def strip_comment(line: str) -> str:
    """Drop a trailing `#` comment that is outside quotes."""
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def parse_scalar(raw: str, where: str) -> object:
    """Read an integer, a quoted string, or a one-line list of strings."""
    value = raw.strip()
    try:
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            return value[1:-1]
        if re.fullmatch(r"[+-]?\d+", value):
            return int(value)
        if value.startswith(('"', "[")):
            return json.loads(value)
    except json.JSONDecodeError as error:
        raise ImportsError(f"{where}: cannot read {raw!r}") from error
    raise ImportsError(f"{where}: cannot read {raw!r}")


def parse_key(raw: str, where: str) -> str:
    """Read a bare or quoted key."""
    key = raw.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        parsed = parse_scalar(key, where)
        if isinstance(parsed, str):
            return parsed
    elif key and re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    raise ImportsError(f"{where}: cannot read the key {raw!r}")


def parse_toml_subset(text: str) -> dict[str, object]:
    """Read the subset this script writes, for Python 3.10 where `tomllib` is missing.

    Supports comments, `[table]` headers, and `key = value` lines whose key is bare
    or quoted and whose value is an integer, a string, or a one-line list of strings.
    """
    parsed: dict[str, object] = {}
    table: dict[str, object] = parsed
    for number, raw in enumerate(text.splitlines(), start=1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        where = f"{BASELINE_PATH}:{number}"
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise ImportsError(f"{where}: cannot read {raw!r}")
            name = parse_key(line[1:-1], where)
            if name in parsed:
                raise ImportsError(f"{where}: [{name}] appears twice")
            table = {}
            parsed[name] = table
            continue
        key_text, separator, value_text = _split_assignment(line)
        if not separator:
            raise ImportsError(f"{where}: cannot read {raw!r}")
        table[parse_key(key_text, where)] = parse_scalar(value_text, where)
    return parsed


def _split_assignment(line: str) -> tuple[str, str, str]:
    """Split `key = value` at the first `=` outside a quoted key."""
    if line[:1] in "\"'":
        end = line.find(line[0], 1)
        while end != -1 and line[end - 1] == "\\" and line[0] == '"':
            end = line.find(line[0], end + 1)
        if end == -1:
            return line, "", ""
        head, separator, tail = line[end + 1 :].partition("=")
        return line[: end + 1] + head, separator, tail
    return line.partition("=")


def parse_baseline(text: str) -> dict[str, object]:
    """Parse the baseline file with `tomllib` where it exists (3.11+), else the subset."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return parse_toml_subset(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ImportsError(f"{BASELINE_PATH} is not valid TOML: {error}") from error


def int_table(raw: dict[str, object], name: str) -> dict[str, int]:
    """Return a `[name]` table of non-negative integers, empty when absent."""
    table = raw.get(name, {})
    if not isinstance(table, dict):
        raise ImportsError(f"[{name}] in {BASELINE_PATH} must be a table")
    out: dict[str, int] = {}
    for key, value in table.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ImportsError(f"[{name}] {key} in {BASELINE_PATH} must be a whole number")
        out[str(key)] = value
    return out


def read_baseline(root: Path) -> tuple[Config, Counts | None]:
    """Read the config and recorded counts, or the defaults and None when no file exists."""
    file = root / BASELINE_PATH
    if not file.is_file():
        return Config(), None
    raw = parse_baseline(file.read_text(encoding="utf-8"))
    unknown = sorted(set(raw) - SETTINGS_KEYS)
    if unknown:
        raise ImportsError(f"{BASELINE_PATH} has unknown keys: {', '.join(unknown)}")

    exclude = raw.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(g, str) for g in exclude):
        raise ImportsError(f"`exclude` in {BASELINE_PATH} must be a list of strings")

    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()
    ):
        raise ImportsError(f"[aliases] in {BASELINE_PATH} must map strings to strings")

    config = Config(
        exclude=tuple(str(g) for g in exclude),
        aliases={str(k): str(v) for k, v in aliases.items()},
    )
    return config, {name: int_table(raw, name) for name in CATEGORIES}


def format_baseline(config: Config, counts: Counts) -> str:
    """Render the baseline file."""
    quoted = ", ".join(json.dumps(g) for g in config.exclude)
    lines = [
        "# Counts only go down. After a cleanup, "
        "`python3 scripts/check_imports.py --update` lowers them.",
        "# Raising one is a hand edit, reviewed in the pull request.",
        f"exclude = [{quoted}]",
        "",
        "[aliases]",
        *(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in sorted(config.aliases.items())),
        "",
        "[deep_imports]",
        *(f"{json.dumps(k)} = {v}" for k, v in counts["deep_imports"].items()),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading the repo (the shell)
# ---------------------------------------------------------------------------


def git(root: Path | None, *args: str) -> str:
    """Run a git command and return its stdout.

    Raises:
        ImportsError: If git is missing or fails.

    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ImportsError(f"cannot run git: {error}") from error
    if result.returncode != 0:
        raise ImportsError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


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


def read_sources(root: Path, excludes: tuple[str, ...]) -> dict[str, str]:
    """Return path to text for every scanned source file.

    A tracked file missing from disk, and a submodule, is skipped.
    """
    skip = [*BUILTIN_EXCLUDES, *excludes]
    listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    sources: dict[str, str] = {}
    for path in sorted(name for name in listing.split("\0") if name):
        if PurePosixPath(path).suffix not in SOURCE_SUFFIXES or is_excluded(path, skip):
            continue
        file = root / path
        if file.is_file():
            sources[path] = file.read_text(encoding="utf-8", errors="replace")
    return sources


# ---------------------------------------------------------------------------
# Public surface (pure)
# ---------------------------------------------------------------------------


def is_test_file(path: str) -> bool:
    """Return True for a test file or test support.

    That is `*.test.*`, `*.spec.*`, `test_*.py`, `*_test.py`, `conftest.py`, or any
    file under a `test/` or `tests/` folder (fixtures, fakes, setup).
    """
    folders = path.split("/")[:-1]
    if "tests" in folders or "test" in folders:
        return True
    name = posixpath.basename(path)
    if any(part in {"test", "spec"} for part in name.split(".")[1:-1]):
        return True
    if name.endswith(".py"):
        return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")
    return False


def is_surface_file(name: str) -> bool:
    """Return True for `index.*` and `__init__.py`."""
    if name == "__init__.py":
        return True
    stem, _, _ = name.partition(".")
    return stem == "index" and PurePosixPath(name).suffix in JS_SUFFIXES


_BLOCK_COMMENT = re.compile(r"(?<![\w.'\"`/:*])/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?m)(?:^|(?<=\s))//.*$")
_JS_IMPORTS = (
    re.compile(r"(?m)^[ \t]*(?:import|export)\b[\w\s{},*$]*?\bfrom\s*(['\"])([^'\"\n]+)\1"),
    re.compile(r"(?m)^[ \t]*import\s*(['\"])([^'\"\n]+)\1"),
    re.compile(r"\bimport\s*\(\s*(['\"])([^'\"\n]+)\1\s*\)"),
    re.compile(r"\brequire\s*\(\s*(['\"])([^'\"\n]+)\1\s*\)"),
)


def js_specs(text: str) -> list[tuple[int, str]]:
    """Return (line, specifier) for every import in JavaScript or TypeScript text."""
    text = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _LINE_COMMENT.sub("", text)
    found: set[tuple[int, str]] = set()
    for pattern in _JS_IMPORTS:
        for match in pattern.finditer(text):
            found.add((text.count("\n", 0, match.start(2)) + 1, match.group(2)))
    return sorted(found)


@dataclass
class Index:
    """What import resolution needs to know about the tree."""

    sources: dict[str, str]
    aliases: list[tuple[str, str]]
    py_roots: dict[str, list[str]]
    bound_names: dict[str, frozenset[str]] = field(default_factory=dict)


def python_package_roots(paths: set[str]) -> dict[str, list[str]]:
    """Map a package name to its root folders.

    A root is a folder with `__init__.py` whose parent has none.
    """
    roots: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        if posixpath.basename(path) != "__init__.py":
            continue
        folder = posixpath.dirname(path)
        if folder and posixpath.join(posixpath.dirname(folder), "__init__.py") not in paths:
            roots[posixpath.basename(folder)].append(folder)
    return dict(roots)


def build_index(sources: dict[str, str], aliases: dict[str, str]) -> Index:
    """Index the tree for import resolution; longest alias prefix first."""
    ordered = sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)
    return Index(sources, ordered, python_package_roots(set(sources)))


def resolve_js(index: Index, importer: str, spec: str) -> str | None:
    """Resolve a JavaScript or TypeScript specifier to a scanned file, or None."""
    base: str | None
    if spec.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    else:
        base = _alias_base(index, spec)
    if base is None or base.startswith(".."):
        return None
    if base == ".":
        base = ""
    candidates = [base]
    suffix = PurePosixPath(base).suffix
    candidates += [base[: -len(suffix)] + ts for ts in JS_TO_TS.get(suffix, ())]
    candidates += [base + s for s in JS_SUFFIXES]
    candidates += [posixpath.join(base, "index" + s) for s in JS_SUFFIXES]
    return next((c for c in candidates if c in index.sources), None)


def _alias_base(index: Index, spec: str) -> str | None:
    for prefix, target in index.aliases:
        if spec == prefix or spec.startswith(prefix if prefix.endswith("/") else prefix + "/"):
            return posixpath.normpath(target + spec[len(prefix) :])
    return None


def python_module_file(index: Index, path: str) -> str | None:
    """Return the package `__init__.py` or the module file at a path, or None."""
    for candidate in (posixpath.join(path, "__init__.py"), path + ".py"):
        if candidate in index.sources:
            return candidate
    return None


def python_bound_names(index: Index, init_path: str) -> frozenset[str]:
    """Return the names an `__init__.py` binds at module level."""
    cached = index.bound_names.get(init_path)
    if cached is not None:
        return cached
    names: set[str] = set()
    try:
        tree = ast.parse(index.sources[init_path])
    except (SyntaxError, ValueError):
        tree = ast.Module(body=[], type_ignores=[])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name.partition(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update((a.asname or a.name) for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    result = frozenset(names)
    index.bound_names[init_path] = result
    return result


def _shared_depth(a: str, b: str) -> int:
    depth = 0
    for left, right in zip(a.split("/"), b.split("/"), strict=False):
        if left != right:
            break
        depth += 1
    return depth


def python_module_path(index: Index, importer: str, level: int, module: str) -> str | None:
    """Return the repo path (no suffix) a Python module name points at, or None."""
    parts = [p for p in module.split(".") if p]
    if level == 0:
        roots = index.py_roots.get(parts[0]) if parts else None
        if not roots:
            return None
        folder = importer.rpartition("/")[0]
        root = max(roots, key=lambda r: _shared_depth(r.rpartition("/")[0], folder))
        return posixpath.join(root, *parts[1:])
    folder = posixpath.dirname(importer)
    for _ in range(level - 1):
        if not folder:
            return None
        folder = posixpath.dirname(folder)
    return posixpath.join(folder, *parts)


def python_imports(index: Index, importer: str) -> list[tuple[int, str]]:
    """Return (line, target file) for every first-party import in a Python file."""
    try:
        tree = ast.parse(index.sources[importer])
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = python_module_path(index, importer, 0, alias.name)
                target = python_module_file(index, path) if path is not None else None
                if target:
                    found.append((node.lineno, target))
        elif isinstance(node, ast.ImportFrom):
            path = python_module_path(index, importer, node.level, node.module or "")
            if path is None:
                continue
            base = python_module_file(index, path)
            for alias in node.names:
                sub = (
                    None
                    if alias.name == "*"
                    else python_module_file(index, posixpath.join(path, alias.name))
                )
                bound = base is not None and alias.name in python_bound_names(index, base)
                target = sub if sub and not bound else base
                if target:
                    found.append((node.lineno, target))
    return found


def bypassed_subfolder(importer: str, target: str, stop: set[str]) -> str | None:
    """Return the innermost subfolder whose public surface this import bypasses.

    Walk up from the target's folder. A folder S counts when its parent does not
    contain the importer; the import must then resolve to S's own `index.*` or
    `__init__.py`. The walk stops at a Python package root (in `stop`): the
    package is reached by its name, so the folders above it are not subfolders.
    """
    folder = posixpath.dirname(target)
    while folder and folder not in stop:
        parent = posixpath.dirname(folder)
        outside = bool(parent) and not importer.startswith(parent + "/")
        if outside and not _is_surface_of(target, folder):
            return folder
        folder = parent
    return None


def _is_surface_of(target: str, folder: str) -> bool:
    return posixpath.dirname(target) == folder and is_surface_file(posixpath.basename(target))


def find_deep_imports(
    sources: dict[str, str], aliases: dict[str, str]
) -> dict[str, list[DeepImport]]:
    """Find every import that reaches past a subfolder's public surface.

    Tests are not importers here: a test imports the module it covers directly
    (a mirrored `tests/unit/review/test_assignment.py` imports
    `<package>.review.assignment`), so a surface never re-exports internals just
    to be tested.
    """
    index = build_index(sources, aliases)
    stop = {root for roots in index.py_roots.values() for root in roots}
    found: dict[str, set[DeepImport]] = defaultdict(set)
    for importer in sources:
        if is_test_file(importer):
            continue
        if importer.endswith(".py"):
            hits = python_imports(index, importer)
        else:
            hits = [
                (line, target)
                for line, spec in js_specs(sources[importer])
                if (target := resolve_js(index, importer, spec))
            ]
        for line, target in hits:
            subfolder = bypassed_subfolder(importer, target, stop)
            if subfolder:
                found[subfolder].add(DeepImport(importer, line, target, subfolder))
    return {
        key: sorted(items, key=lambda d: (d.importer, d.line, d.target))
        for key, items in sorted(found.items())
    }


def analyze(sources: dict[str, str], config: Config) -> Findings:
    """Find every import over the scanned files that bypasses a subfolder's surface."""
    return Findings(
        deep_imports=find_deep_imports(sources, config.aliases),
    )


# ---------------------------------------------------------------------------
# Comparison and output
# ---------------------------------------------------------------------------


def compare_counts(
    recorded: dict[str, int], current: dict[str, int]
) -> tuple[list[tuple[str, int | None, int]], list[tuple[str, int, int]]]:
    """Return (risen, fallen). A new entry has a recorded value of None."""
    risen = [
        (key, recorded.get(key), count)
        for key, count in current.items()
        if key not in recorded or count > recorded[key]
    ]
    fallen = [
        (key, count, current.get(key, 0))
        for key, count in recorded.items()
        if current.get(key, 0) < count
    ]
    return risen, fallen


def describe_hit(findings: Findings, category: str, key: str) -> list[str]:
    """Return the indented lines that name what is behind one count."""
    return [f"  {_import_line(d)}" for d in findings.deep_imports[key]]


def _import_line(d: DeepImport) -> str:
    return f"{d.importer}:{d.line} -> {d.target} (bypasses {d.subfolder})"


def print_list(findings: Findings, config: Config) -> None:
    """Print every finding."""
    print("== deep_imports: imports past a subfolder's index or __init__ ==")
    for found in findings.deep_imports.values():
        print("\n".join(_import_line(d) for d in found))


def compare(baseline: Counts | None, findings: Findings) -> int:
    """Print the baseline table and every rise; return 1 when anything rose."""
    current = findings.counts()
    recorded = baseline or {name: {} for name in CATEGORIES}
    if baseline is None:
        print(f"No {BASELINE_PATH}: auditing against an empty baseline.")
    width = max(len(c) for c in CATEGORIES)
    print(f"{'category':<{width}}  baseline  current   (entries)")
    for category in CATEGORIES:
        print(f"{category:<{width}}  {len(recorded[category]):>8}  {len(current[category]):>7}")

    failed = False
    lowered = False
    for category in CATEGORIES:
        risen, fallen = compare_counts(recorded[category], current[category])
        for key, before, now in risen:
            failed = True
            if before is None:
                print(f"new {category}: {key} ({now})")
            else:
                print(f"{category}: {key} rose from {before} to {now}")
            print("\n".join(describe_hit(findings, category, key)))
        for key, before, now in fallen:
            lowered = True
            print(f"{category}: {key} fell from {before} to {now}")
    if lowered and not failed:
        print(f"`python3 scripts/check_imports.py --update` would lower {BASELINE_PATH}.")
    return 1 if failed else 0


def init_baseline(root: Path, config: Config, counts: Counts, *, exists: bool) -> int:
    """Write the first baseline at the current counts."""
    if exists:
        print(f"{BASELINE_PATH} already exists. Use --update.", file=sys.stderr)
        return 2
    target = root / BASELINE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_baseline(config, counts), encoding="utf-8")
    print(f"Wrote {BASELINE_PATH}: " + ", ".join(f"{c} {len(counts[c])}" for c in CATEGORIES))
    return 0


def update_baseline(root: Path, config: Config, baseline: Counts | None, findings: Findings) -> int:
    """Lower the baseline to the current counts, refusing when any count rose."""
    if baseline is None:
        print(f"No {BASELINE_PATH}. Run with --init to write it.", file=sys.stderr)
        return 2
    current = findings.counts()
    risen = [
        f"{category} {key}"
        for category in CATEGORIES
        for key, _, _ in compare_counts(baseline[category], current[category])[0]
    ]
    if risen:
        print(
            f"Not updating: {', '.join(risen)} rose. Remove the new findings, or raise "
            f"{BASELINE_PATH} by hand and explain it in the PR.",
            file=sys.stderr,
        )
        return 1
    (root / BASELINE_PATH).write_text(format_baseline(config, current), encoding="utf-8")
    print(f"Updated {BASELINE_PATH}.")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Fail when imports that skip a subfolder's index or __init__ rise above the baseline."
    )
    parser.add_argument("--list", action="store_true", help="print every finding")
    parser.add_argument("--json", action="store_true", help="print the current counts as JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--init", action="store_true", help=f"write {BASELINE_PATH} at the current counts"
    )
    mode.add_argument(
        "--update", action="store_true", help="lower the baseline to the current counts"
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Run the command chosen by the parsed arguments and return the exit code."""
    root = Path(git(None, "rev-parse", "--show-toplevel").strip())
    config, baseline = read_baseline(root)
    findings = analyze(read_sources(root, config.exclude), config)

    if args.list:
        try:
            print_list(findings, config)
        except BrokenPipeError:
            sys.stderr.close()
        return 0
    if args.json:
        print(json.dumps(findings.counts(), indent=2))
        return 0
    if args.init:
        return init_baseline(root, config, findings.counts(), exists=baseline is not None)
    if args.update:
        return update_baseline(root, config, baseline, findings)
    return compare(baseline, findings)


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    try:
        return run(args)
    except ImportsError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
