#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydicom>=3"]
# ///
r"""Fail when patient imaging data is about to be committed to a repo.

Copy this file to `scripts/check_patient_data.py` in a project. The settings live in
`scripts/patient_data.toml`. `uv run scripts/check_patient_data.py` installs `pydicom`
on its own (PEP 723 metadata above); nothing else is imported.

Two rules, from the team's rule that real patient data never goes into a repo:

1. Location. An imaging file outside the test-data folders fails. Imaging files are
   `.dcm .dicom .ima .nii .nii.gz .mha .mhd .nrrd .mgz`, plus any file, with any or no
   extension, that has `DICM` at byte offset 128 (the DICOM preamble).
2. DICOM header, checked everywhere, test-data folders included. A DICOM fails when any
   of these elements is not empty: PatientName, PatientID, PatientBirthDate,
   PatientAddress, OtherPatientNames, OtherPatientIDs, PatientTelephoneNumbers,
   PatientMotherBirthName, MilitaryRank, ReferringPhysicianName, InstitutionName,
   InstitutionAddress, AccessionNumber. Two ways out: PatientIdentityRemoved (0012,0062)
   is "YES", or every non-empty identifying value matches `allowed_ids`.
   A DICOM whose header cannot be read fails too: treat it as patient data until checked.

The script only reads headers (`stop_before_pixels`). It never prints, logs, or raises
with a tag value; messages name tag keywords and paths only. A file it opens only for
the 132-byte `DICM` check is skipped when it has a known text, code, document, picture,
archive, or model extension, or is 1 GB or larger. Compressed or zipped DICOM is not
opened. A path that is missing or a folder (deleted in the commit, a submodule) is
skipped.

Usage:
    uv run scripts/check_patient_data.py PATH [PATH ...]   check these files
    uv run scripts/check_patient_data.py --all             check every file `git ls-files` lists
    uv run scripts/check_patient_data.py --json PATH ...   findings as JSON, no summary line
    uv run scripts/check_patient_data.py --root DIR ...    the repo root (default: the cwd)

Exit codes: 0 ok, 1 a finding, 2 a usage or config error.

As a pre-commit hook (`pre-commit` passes the staged files):

    - repo: local
      hooks:
        - id: patient-data
          name: no patient imaging data
          entry: uv run scripts/check_patient_data.py
          language: system

Settings, all optional, in `scripts/patient_data.toml`:

    allow = ["testdata/**", "tests/data/**"]   folders imaging files may live in
    allowed_ids = '^(DT_\d{2}_\d{7}|DTE_\d{4}|SYNTH_.*)$'   synthetic values, whole-value match

With no file the allowed folders are `testdata/**`, `tests/data/**`, `tests/fixtures/**`,
and `**/__fixtures__/**`. A glob is matched against the repo-relative path: `*` stays inside
one folder, `**` crosses folders, and a glob starting with a folder name starts at the repo
root (write `**/testdata/**` for a `testdata` folder anywhere). Use a single-quoted TOML
string for `allowed_ids` so backslashes stay as written. With no `allowed_ids`, only
PatientIdentityRemoved=YES clears a header.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tomllib
import warnings
from collections.abc import MutableSequence, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydicom.dataset import Dataset

CONFIG_PATH = "scripts/patient_data.toml"
SETTINGS_KEYS = frozenset({"allow", "allowed_ids"})

DEFAULT_ALLOW = (
    "testdata/**",
    "tests/data/**",
    "tests/fixtures/**",
    "**/__fixtures__/**",
)

# Lower-case name endings. DICOM ones also get their header read.
DICOM_SUFFIXES = (".dcm", ".dicom", ".ima")
IMAGING_SUFFIXES = (*DICOM_SUFFIXES, ".nii", ".nii.gz", ".mha", ".mhd", ".nrrd", ".mgz")

# Extensions whose files are never opened for the DICM check.
NON_IMAGING_SUFFIXES = frozenset(
    {
        # code and config
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".jsonl",
        ".toml", ".yaml", ".yml", ".cfg", ".ini", ".lock", ".sh", ".bash", ".sql", ".c",
        ".h", ".cpp", ".java", ".go", ".rs", ".css", ".scss", ".html", ".xml", ".ipynb",
        # text and documents
        ".md", ".rst", ".txt", ".log", ".csv", ".tsv", ".tex", ".bib", ".pdf", ".docx",
        ".xlsx", ".pptx",
        # pictures
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".ico", ".webp",
        # archives and model files
        ".zip", ".tar", ".tgz", ".gz", ".7z", ".npy", ".npz", ".pt", ".pth", ".onnx",
        ".pkl", ".h5", ".whl",
    }
)  # fmt: skip

PREAMBLE_BYTES = 128
SNIFF_BYTES = PREAMBLE_BYTES + 4
SNIFF_MAX_SIZE = 1 << 30  # 1 GB

# DICOM elements that identify a patient or the place. Keyword order is the report order.
IDENTIFYING_KEYWORDS = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientAddress",
    "OtherPatientNames",
    "OtherPatientIDs",
    "PatientTelephoneNumbers",
    "PatientMotherBirthName",
    "MilitaryRank",
    "ReferringPhysicianName",
    "InstitutionName",
    "InstitutionAddress",
    "AccessionNumber",
)
IDENTITY_REMOVED = "PatientIdentityRemoved"
# A readable DICOM has file meta, or a top-level element from one of these groups.
DICOM_GROUPS = frozenset({0x0008, 0x0010, 0x0018, 0x0020, 0x0028})

RULE_LOCATION = "location"
RULE_HEADER = "dicom-header"
RULE_UNREADABLE = "unreadable-header"


class PatientDataError(Exception):
    """A usage or config problem the user can fix, reported without a traceback."""


@dataclass(frozen=True)
class Config:
    """Settings from `scripts/patient_data.toml`, or the defaults when there is none."""

    allow: tuple[str, ...] = DEFAULT_ALLOW
    allowed_ids: re.Pattern[str] | None = None


@dataclass(frozen=True)
class Finding:
    """One problem in one file. `tags` holds DICOM keywords, never values."""

    path: str
    rule: str
    tags: tuple[str, ...]
    message: str

    def as_json(self) -> dict[str, object]:
        """Return the JSON shape: path, rule, and tags."""
        return {"path": self.path, "rule": self.rule, "tags": list(self.tags)}


@dataclass(frozen=True)
class Tally:
    """What a run looked at: every path given, and how many were imaging files."""

    files: int
    imaging: int


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def read_config(root: Path) -> Config:
    """Read the settings file, or return the defaults when there is none."""
    file = root / CONFIG_PATH
    if not file.is_file():
        return Config()
    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise PatientDataError(f"{CONFIG_PATH} is not valid TOML: {error}") from error

    unknown = sorted(set(raw) - SETTINGS_KEYS)
    if unknown:
        raise PatientDataError(f"{CONFIG_PATH} has unknown keys: {', '.join(unknown)}")

    allow = raw.get("allow", list(DEFAULT_ALLOW))
    if not isinstance(allow, list) or not all(isinstance(glob, str) for glob in allow):
        raise PatientDataError(f"`allow` in {CONFIG_PATH} must be a list of strings")

    pattern = raw.get("allowed_ids")
    if pattern is None:
        return Config(allow=tuple(allow))
    if not isinstance(pattern, str):
        raise PatientDataError(f"`allowed_ids` in {CONFIG_PATH} must be a string")
    try:
        return Config(allow=tuple(allow), allowed_ids=re.compile(pattern))
    except re.error as error:
        raise PatientDataError(f"`allowed_ids` in {CONFIG_PATH} is not a regex: {error}") from error


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Turn a path glob into a regex: `*` and `?` stay inside a folder, `**` crosses folders."""
    out: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(out))


def in_allowed_folder(path: str, allow: Sequence[str]) -> bool:
    """Return True when a repo-relative path matches one of the allowed globs."""
    return any(glob_to_regex(glob).fullmatch(path) for glob in allow)


# ---------------------------------------------------------------------------
# Finding imaging files
# ---------------------------------------------------------------------------


def has_dicm_preamble(file: Path) -> bool:
    """Return True when `DICM` sits at byte offset 128. Reads 132 bytes and no more."""
    try:
        if not file.is_file() or not PREAMBLE_BYTES + 4 <= file.stat().st_size < SNIFF_MAX_SIZE:
            return False
        with file.open("rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError:
        return False
    return head[PREAMBLE_BYTES:] == b"DICM"


def classify(root: Path, rel: str) -> tuple[bool, bool]:
    """Return (is_imaging, is_dicom) for a repo-relative path, opening it only when needed."""
    name = rel.lower()
    if name.endswith(DICOM_SUFFIXES):
        return True, True
    suffix = Path(name).suffix
    if suffix not in NON_IMAGING_SUFFIXES and has_dicm_preamble(root / rel):
        return True, True
    return name.endswith(IMAGING_SUFFIXES), False


# ---------------------------------------------------------------------------
# DICOM header
# ---------------------------------------------------------------------------


def blank(text: str) -> bool:
    """Return True for an empty DICOM value: spaces, nulls, and bare name separators."""
    return not text.strip(" \x00^=")


def element_texts(value: object) -> list[str]:
    """Return the non-empty values of one element as text, for comparison only."""
    items = list(value) if isinstance(value, MutableSequence) else [value]
    texts: list[str] = []
    for item in items:
        if item is None:
            continue
        text = item.decode("latin-1") if isinstance(item, bytes) else str(item)
        if not blank(text):
            texts.append(text.strip())
    return texts


def identifying_values(dataset: Dataset) -> dict[str, list[str]]:
    """Return keyword -> non-empty values for the identifying elements the dataset carries."""
    found: dict[str, list[str]] = {}
    for keyword in IDENTIFYING_KEYWORDS:
        texts = element_texts(dataset.get(keyword))
        if texts:
            found[keyword] = texts
    return found


def offending_tags(
    values: dict[str, list[str]], removed: str, allowed_ids: re.Pattern[str] | None
) -> tuple[str, ...]:
    """Return the keywords that make a header fail, empty when it passes.

    Pure: `removed` is the PatientIdentityRemoved text. A header passes when that is
    "YES", or when every value of an identifying element matches `allowed_ids` whole.
    """
    if removed.strip().upper() == "YES":
        return ()
    if allowed_ids is None:
        return tuple(values)
    return tuple(
        keyword
        for keyword, texts in values.items()
        if not all(allowed_ids.fullmatch(text) for text in texts)
    )


def readable(dataset: Dataset) -> bool:
    """Return True when a parsed file looks like DICOM: it has file meta or a known group."""
    return len(dataset.file_meta) > 0 or any(elem.tag.group in DICOM_GROUPS for elem in dataset)


# What pydicom raises on a damaged or odd header. Its messages may quote values, so none is
# shown; anything else reaches `quiet_excepthook`.
READ_ERRORS = (
    OSError,
    EOFError,
    ValueError,
    LookupError,
    TypeError,
    AttributeError,
    struct.error,
    RuntimeError,
    ArithmeticError,
)


def quiet_excepthook(kind: type[BaseException], error: BaseException, trace: object) -> None:
    """Report an unexpected crash by its type only; its message may hold a tag value."""
    del error, trace
    print(
        f"check_patient_data.py stopped on an unexpected {kind.__name__}; the commit is refused.",
        file=sys.stderr,
    )


def header_findings(root: Path, rel: str, config: Config) -> list[Finding]:
    """Read one DICOM header and return its findings. No tag value leaves this function."""
    try:
        import pydicom
        import pydicom.errors
    except ImportError as error:
        raise PatientDataError(
            "pydicom is not installed; run this with `uv run scripts/check_patient_data.py`"
        ) from error

    # pydicom warns about bad values and quotes them; the log and warning text must not leak.
    logging.getLogger("pydicom").disabled = True
    unreadable = Finding(
        rel,
        RULE_UNREADABLE,
        (),
        f"{rel}: cannot read the DICOM header; treat it as patient data until checked.",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            dataset = pydicom.dcmread(root / rel, stop_before_pixels=True, force=True)
            if not readable(dataset):
                return [unreadable]
            values = identifying_values(dataset)
            removed = element_texts(dataset.get(IDENTITY_REMOVED))
        except (*READ_ERRORS, pydicom.errors.InvalidDicomError):
            return [unreadable]

    tags = offending_tags(values, removed[0] if removed else "", config.allowed_ids)
    if not tags:
        return []
    message = (
        f"{rel}: DICOM header carries {', '.join(tags)} "
        f"and no de-identification mark ({IDENTITY_REMOVED}=YES)"
    )
    return [Finding(rel, RULE_HEADER, tags, message)]


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def check_file(root: Path, rel: str, config: Config) -> tuple[bool, list[Finding]]:
    """Return (is_imaging, findings) for one repo-relative path."""
    imaging, dicom = classify(root, rel)
    if not imaging:
        return False, []
    findings: list[Finding] = []
    if not in_allowed_folder(rel, config.allow):
        message = (
            f"{rel}: imaging file outside the test-data folders ({', '.join(config.allow)}); "
            "move it there or out of the repo."
        )
        findings.append(Finding(rel, RULE_LOCATION, (), message))
    if dicom:
        findings.extend(header_findings(root, rel, config))
    return True, findings


def check_paths(root: Path, paths: Sequence[str], config: Config) -> tuple[Tally, list[Finding]]:
    """Check repo-relative paths. Missing paths and folders are skipped."""
    findings: list[Finding] = []
    files = imaging = 0
    for rel in paths:
        if not (root / rel).is_file():
            continue
        files += 1
        is_imaging, found = check_file(root, rel, config)
        imaging += is_imaging
        findings.extend(found)
    findings.sort(key=lambda finding: (finding.path, finding.rule))
    return Tally(files, imaging), findings


def tracked_files(root: Path) -> list[str]:
    """Return the repo-relative paths `git ls-files` lists."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PatientDataError(
            f"{root} is not a git repository, so --all cannot list files"
        ) from error
    return [item for item in result.stdout.decode("utf-8", "surrogateescape").split("\0") if item]


def relative_to_root(root: Path, raw: str) -> str:
    """Return a path argument as a repo-relative POSIX path."""
    absolute = Path(os.path.normpath(Path.cwd() / raw))  # `..` folded, symlinks kept
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError as error:
        raise PatientDataError(f"{raw} is outside the repo root {root}") from error


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Fail when patient imaging data is about to be committed."
    )
    parser.add_argument("paths", nargs="*", metavar="PATH", help="files to check")
    parser.add_argument("--all", action="store_true", help="check every file `git ls-files` lists")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument("--root", default=".", help="the repo root (default: the current folder)")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Run one check and return the exit code."""
    if args.all == bool(args.paths):
        raise PatientDataError("give either PATH arguments or --all")
    root = Path(os.path.normpath(Path.cwd() / args.root))
    config = read_config(root)
    paths = tracked_files(root) if args.all else [relative_to_root(root, p) for p in args.paths]
    tally, findings = check_paths(root, paths, config)

    if args.json:
        print(json.dumps([finding.as_json() for finding in findings], indent=2))
    else:
        for finding in findings:
            print(finding.message)
        noun = "finding" if len(findings) == 1 else "findings"
        print(f"Checked {tally.files} files ({tally.imaging} imaging): {len(findings)} {noun}.")
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    sys.excepthook = quiet_excepthook
    args = parse_args(argv)
    try:
        return run(args)
    except PatientDataError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
