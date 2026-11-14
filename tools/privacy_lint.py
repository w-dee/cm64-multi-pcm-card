#!/usr/bin/env python3
"""Detect developer-specific home paths, including inside ZIP-based files."""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class PrivacyLintError(RuntimeError):
    """Raised when repository content cannot be inspected safely."""


@dataclass(frozen=True)
class Finding:
    path: str
    line_number: int
    matched_prefix: str


# Bound recursive decompression per repository file. Exceeding a limit fails
# the lint instead of silently skipping content. Nothing is extracted to disk.
_MAX_ZIP_DEPTH = 8
_MAX_ZIP_MEMBERS = 10_000
_MAX_ZIP_BYTES = 256 * 1024 * 1024
_ZIP_SUFFIXES = frozenset((".zip", ".fcstd", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"))
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    RuntimeError,
    OSError,
    EOFError,
    ValueError,
    zlib.error,
)


@dataclass
class _ZipBudget:
    members: int = 0
    expanded_bytes: int = 0


_LINUX_ROOT = b"/" + b"home" + b"/"
_MACOS_ROOT = b"/" + b"Users" + b"/"
_WINDOWS_SEPARATOR = b"\\"
_WINDOWS_USERS = b"Users"
_PLACEHOLDER_USERS = frozenset((b"USER", b"<user>"))

_PATTERNS = (
    re.compile(_LINUX_ROOT + rb"[^/\r\n]+" + re.escape(b"/")),
    re.compile(_MACOS_ROOT + rb"[^/\r\n]+" + re.escape(b"/")),
    re.compile(
        rb"[A-Za-z]:"
        + re.escape(_WINDOWS_SEPARATOR)
        + _WINDOWS_USERS
        + re.escape(_WINDOWS_SEPARATOR)
        + rb"[^\\\r\n]+"
        + re.escape(_WINDOWS_SEPARATOR)
    ),
)

_ALLOWED_UNIX_PREFIXES = frozenset(
    (
        _LINUX_ROOT + b"USER/",
        _LINUX_ROOT + b"<user>/",
        _MACOS_ROOT + b"USER/",
        _MACOS_ROOT + b"<user>/",
    )
)


def _git_bytes(arguments: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise PrivacyLintError(f"could not run git: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise PrivacyLintError(f"git command failed{suffix}") from exc
    return result.stdout


def _repo_root() -> Path:
    return Path(os.fsdecode(_git_bytes(["rev-parse", "--show-toplevel"]).rstrip(b"\0\n")))


def _allowed_prefix(match: bytes) -> bool:
    if match in _ALLOWED_UNIX_PREFIXES:
        return True

    parts = match.split(_WINDOWS_SEPARATOR)
    return (
        len(parts) == 4
        and parts[1] == _WINDOWS_USERS
        and parts[2] in _PLACEHOLDER_USERS
        and parts[3] == b""
    )


def _scan_plain_bytes(path: str, content: bytes) -> list[Finding]:
    """Return findings in bytes without requiring UTF-8 decoding."""

    findings: list[Finding] = []
    for line_number, line in enumerate(content.split(b"\n"), start=1):
        for pattern in _PATTERNS:
            for match in pattern.finditer(line):
                matched = match.group(0)
                if not _allowed_prefix(matched):
                    findings.append(
                        Finding(
                            path=path,
                            line_number=line_number,
                            matched_prefix=matched.decode("utf-8", errors="backslashreplace"),
                        )
                    )
    return findings


def scan_bytes(path: str, content: bytes) -> list[Finding]:
    """Scan plain bytes or recursively inspect ZIP contents, regardless of suffix.

    Findings use ``archive!member`` paths and member-local line numbers.
    Unreadable archives and decompression limits raise PrivacyLintError.
    """
    return _scan_content(path, content, _ZipBudget(), depth=0)


def _scan_content(path: str, content: bytes, budget: _ZipBudget, depth: int) -> list[Finding]:
    stream = io.BytesIO(content)
    if not zipfile.is_zipfile(stream):
        if Path(path).suffix.lower() in _ZIP_SUFFIXES or content.startswith(
            (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
        ):
            raise PrivacyLintError(f"could not read ZIP archive {path}: invalid or truncated ZIP")
        return _scan_plain_bytes(path, content)

    if depth >= _MAX_ZIP_DEPTH:
        raise PrivacyLintError(f"ZIP nesting limit ({_MAX_ZIP_DEPTH}) exceeded: {path}")

    try:
        with zipfile.ZipFile(stream) as archive:
            findings = _scan_plain_bytes(f"{path}!<archive comment>", archive.comment)
            for member in archive.infolist():
                member_path = f"{path}!{member.filename}"
                budget.members += 1
                if budget.members > _MAX_ZIP_MEMBERS:
                    raise PrivacyLintError(
                        f"ZIP member limit ({_MAX_ZIP_MEMBERS}) exceeded: {member_path}"
                    )
                findings.extend(
                    _scan_plain_bytes(f"{member_path}!<filename>", member.filename.encode("utf-8"))
                )
                findings.extend(_scan_plain_bytes(f"{member_path}!<comment>", member.comment))
                if member.is_dir():
                    continue
                remaining = _MAX_ZIP_BYTES - budget.expanded_bytes
                if member.file_size > remaining:
                    raise PrivacyLintError(
                        f"ZIP expanded size limit ({_MAX_ZIP_BYTES} bytes) exceeded: {member_path}"
                    )
                try:
                    with archive.open(member) as source:
                        expanded = source.read(remaining + 1)
                except _ZIP_READ_ERRORS as exc:
                    raise PrivacyLintError(f"could not read ZIP member {member_path}: {exc}") from exc
                budget.expanded_bytes += len(expanded)
                if budget.expanded_bytes > _MAX_ZIP_BYTES:
                    raise PrivacyLintError(
                        f"ZIP expanded size limit ({_MAX_ZIP_BYTES} bytes) exceeded: {member_path}"
                    )
                findings.extend(_scan_content(member_path, expanded, budget, depth + 1))
            return findings
    except PrivacyLintError:
        raise
    except _ZIP_READ_ERRORS as exc:
        raise PrivacyLintError(f"could not read ZIP archive {path}: {exc}") from exc


def _index_entries() -> dict[bytes, tuple[bytes, bytes]]:
    """Return stage-zero index entries as path -> (mode, blob id)."""

    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for raw_entry in _git_bytes(["ls-files", "--stage", "-z", "--"]).split(b"\0"):
        if not raw_entry:
            continue
        try:
            header, path = raw_entry.split(b"\t", 1)
            mode, blob, stage = header.split()
        except ValueError as exc:
            raise PrivacyLintError("could not parse the Git index") from exc
        if stage == b"0":
            entries[path] = (mode, blob)
    return entries


def _staged_files() -> list[tuple[str, bytes]]:
    staged_paths = _git_bytes(
        [
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "--no-renames",
            "-z",
            "--",
        ]
    ).split(b"\0")
    index = _index_entries()
    files: list[tuple[str, bytes]] = []
    for path_bytes in staged_paths:
        if not path_bytes:
            continue
        entry = index.get(path_bytes)
        if entry is None:
            raise PrivacyLintError(
                f"staged path is not available as a stage-zero index entry: {os.fsdecode(path_bytes)}"
            )
        mode, blob = entry
        if mode == b"160000":
            continue
        try:
            content = _git_bytes(["cat-file", "blob", blob.decode("ascii")])
        except PrivacyLintError as exc:
            raise PrivacyLintError(f"could not read staged content: {exc}") from exc
        files.append((os.fsdecode(path_bytes), content))
    return files


def _tracked_files() -> Iterable[tuple[str, bytes]]:
    root = _repo_root()
    for path_bytes in _git_bytes(["ls-files", "-z", "--"]).split(b"\0"):
        if not path_bytes:
            continue
        path = os.fsdecode(path_bytes)
        file_path = root / Path(path)
        try:
            content = file_path.read_bytes()
        except OSError as exc:
            raise PrivacyLintError(f"could not read tracked file {path}: {exc}") from exc
        yield path, content


def _report(mode: str, files: Iterable[tuple[str, bytes]]) -> int:
    findings: list[Finding] = []
    scanned = 0
    for path, content in files:
        scanned += 1
        findings.extend(scan_bytes(path, content))

    if findings:
        for finding in findings:
            print(
                f"{finding.path}:{finding.line_number}: "
                f"developer-specific home path {finding.matched_prefix}"
            )
        return 1

    print(f"privacy lint: clean ({scanned} {mode} file{'s' if scanned != 1 else ''} scanned)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--staged", action="store_true", help="scan staged/index content")
    modes.add_argument("--tracked", action="store_true", help="scan all tracked worktree files")
    args = parser.parse_args(argv)

    try:
        if args.staged:
            return _report("staged", _staged_files())
        return _report("tracked", _tracked_files())
    except PrivacyLintError as exc:
        print(f"privacy lint: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
