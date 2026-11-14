#!/usr/bin/env python3
"""Focused tests for the repository privacy lint."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from privacy_lint import PrivacyLintError, main, scan_bytes


def _linux_path(user: str) -> str:
    return "/" + "home" + "/" + user + "/project/file"


def _macos_path(user: str) -> str:
    return "/" + "Users" + "/" + user + "/project/file"


def _windows_path(user: str) -> str:
    return "C:" + "\\" + "Users" + "\\" + user + "\\project\\file"


def _run_git(root: pathlib.Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, stdout=subprocess.PIPE)


def _zip_bytes(entries: dict[str, bytes], comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = comment
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@contextlib.contextmanager
def _in_directory(path: pathlib.Path):
    previous = pathlib.Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class PrivacyLintTests(unittest.TestCase):
    def test_rejects_developer_specific_linux_macos_and_windows_paths(self) -> None:
        for path in (
            _linux_path("alice"),
            _macos_path("bob"),
            _windows_path("charlie"),
        ):
            with self.subTest(path=path):
                self.assertEqual(len(scan_bytes("fixture.txt", path.encode())), 1)

    def test_rejects_profile_components_containing_spaces(self) -> None:
        for path in (
            _linux_path("Alice Smith"),
            _macos_path("Jane Doe"),
            _windows_path("John Smith"),
        ):
            with self.subTest(path=path):
                self.assertEqual(len(scan_bytes("fixture.txt", path.encode())), 1)

    def test_allows_neutral_home_forms(self) -> None:
        allowed = (
            "$HOME/project/file",
            "${HOME}/project/file",
            "~/project/file",
            _linux_path("USER"),
            _linux_path("<user>"),
            _macos_path("USER"),
            _macos_path("<user>"),
            _windows_path("USER"),
            _windows_path("<user>"),
        )
        for path in allowed:
            with self.subTest(path=path):
                self.assertEqual(scan_bytes("fixture.txt", path.encode()), [])

    def test_non_utf8_binary_content_does_not_crash(self) -> None:
        self.assertEqual(scan_bytes("binary.dat", b"\x00\x80\xff\x00binary"), [])

    def test_clean_input_has_no_findings(self) -> None:
        self.assertEqual(scan_bytes("clean.txt", b"relative/project/file\n"), [])

    def test_scans_compressed_fcstd_members_with_member_line_numbers(self) -> None:
        content = _zip_bytes({
            "Document.xml": b"<Document>\n" + _linux_path("alice").encode() + b"\n</Document>",
            "GuiDocument.xml": _windows_path("bob").encode(),
        })
        self.assertNotIn(_linux_path("alice").encode(), content)
        findings = scan_bytes("model.FCStd", content)
        self.assertEqual([(f.path, f.line_number) for f in findings], [
            ("model.FCStd!Document.xml", 2),
            ("model.FCStd!GuiDocument.xml", 1),
        ])

    def test_zip_detection_uses_content_including_prepended_data(self) -> None:
        content = _zip_bytes({"Document.xml": _macos_path("alice").encode()})
        for path, data in [("model", content), ("model.bin", b"preamble\n" + content)]:
            with self.subTest(path=path):
                self.assertEqual(scan_bytes(path, data)[0].path, f"{path}!Document.xml")

    def test_scans_nested_archives(self) -> None:
        inner = _zip_bytes({"Document.xml": _linux_path("alice").encode()})
        outer = _zip_bytes({"models/part.FCStd": inner})
        self.assertEqual(scan_bytes("package.zip", outer)[0].path,
                         "package.zip!models/part.FCStd!Document.xml")

    def test_clean_zip_allows_binary_members_empty_zip_and_placeholders(self) -> None:
        for content in [
            _zip_bytes({}),
            _zip_bytes({"Document.xml": _linux_path("USER").encode(), "data.bin": b"\x00\x80\xff"}),
        ]:
            with self.subTest(content=content):
                self.assertEqual(scan_bytes("model.FCStd", content), [])

    def test_scans_archive_filenames_and_comments(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.comment = _macos_path("alice").encode()
            member = zipfile.ZipInfo(_linux_path("bob"))
            member.comment = _windows_path("charlie").encode()
            archive.writestr(member, b"relative/file")
        findings = scan_bytes("model.FCStd", buffer.getvalue())
        self.assertEqual(len(findings), 3)
        self.assertEqual({f.matched_prefix for f in findings}, {
            _macos_path("alice").removesuffix("project/file"),
            _linux_path("bob").removesuffix("project/file"),
            _windows_path("charlie").removesuffix("project\\file"),
        })

    def test_invalid_or_truncated_archives_fail_instead_of_passing(self) -> None:
        for path, content in [
            ("model.FCStd", b"not a zip"),
            ("model.bin", b"PK\x03\x04truncated"),
            ("model.zip", _zip_bytes({"data": b"text"})[:-22]),
        ]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(PrivacyLintError, "invalid or truncated ZIP"):
                    scan_bytes(path, content)

    def test_corrupt_member_fails_with_member_path(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("Document.xml", b"unique payload")
        corrupted = buffer.getvalue().replace(b"unique payload", b"broken payload")
        with self.assertRaisesRegex(PrivacyLintError, r"model.FCStd!Document.xml"):
            scan_bytes("model.FCStd", corrupted)

    def test_encrypted_member_fails_instead_of_being_skipped(self) -> None:
        content = bytearray(_zip_bytes({"Document.xml": b"data"}))
        # Set the encrypted bit in both the local and central-directory headers.
        content[6] |= 1
        central = content.index(b"PK\x01\x02")
        content[central + 8] |= 1
        with self.assertRaisesRegex(PrivacyLintError, "encrypted"):
            scan_bytes("model.FCStd", bytes(content))

    def test_nested_zip_limits_fail_instead_of_skipping_content(self) -> None:
        inner = _zip_bytes({"Document.xml": b"x" * 100})
        outer = _zip_bytes({"part.FCStd": inner})
        limits = [
            ("_MAX_ZIP_DEPTH", 1, "nesting limit"),
            ("_MAX_ZIP_MEMBERS", 1, "member limit"),
            ("_MAX_ZIP_BYTES", len(inner) + 99, "expanded size limit"),
        ]
        for constant, limit, message in limits:
            with self.subTest(constant=constant), patch(f"privacy_lint.{constant}", limit):
                with self.assertRaisesRegex(PrivacyLintError, message):
                    scan_bytes("package.zip", outer)

    def test_zip_size_budget_is_shared_between_siblings(self) -> None:
        content = _zip_bytes({"one": b"a" * 60, "two": b"b" * 60})
        with patch("privacy_lint._MAX_ZIP_BYTES", 100):
            with self.assertRaisesRegex(PrivacyLintError, "expanded size limit"):
                scan_bytes("model.FCStd", content)

    def test_staged_zip_uses_index_while_tracked_mode_reads_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _run_git(root, "init", "--quiet")
            target = root / "part.FCStd"
            clean = _zip_bytes({"Document.xml": b"relative/file"})
            private = _zip_bytes({"Document.xml": _linux_path("alice").encode()})
            target.write_bytes(clean)
            _run_git(root, "add", "part.FCStd")
            target.write_bytes(private)
            with _in_directory(root), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--staged"]), 0)
                self.assertEqual(main(["--tracked"]), 1)
                self.assertIn("part.FCStd!Document.xml:1:", output.getvalue())
            _run_git(root, "add", "part.FCStd")
            target.write_bytes(clean)
            with _in_directory(root), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--staged"]), 1)
                self.assertEqual(main(["--tracked"]), 0)

    def test_unreadable_staged_zip_returns_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _run_git(root, "init", "--quiet")
            (root / "part.FCStd").write_bytes(b"invalid zip")
            _run_git(root, "add", "part.FCStd")
            with _in_directory(root), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(main(["--staged"]), 2)
                self.assertIn("part.FCStd", output.getvalue())

    def test_staged_mode_reads_index_not_unstaged_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _run_git(root, "init", "--quiet")
            _run_git(root, "config", "user.email", "privacy-lint@example.invalid")
            _run_git(root, "config", "user.name", "Privacy Lint Test")
            target = root / "fixture.txt"
            target.write_text("relative/project/file\n", encoding="utf-8")
            _run_git(root, "add", "fixture.txt")
            target.write_text(_linux_path("alice") + "\n", encoding="utf-8")
            with _in_directory(root):
                self.assertEqual(main(["--staged"]), 0)

            _run_git(root, "add", "fixture.txt")
            with _in_directory(root):
                self.assertEqual(main(["--staged"]), 1)

    def test_tracked_mode_scans_tracked_worktree_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _run_git(root, "init", "--quiet")
            _run_git(root, "config", "user.email", "privacy-lint@example.invalid")
            _run_git(root, "config", "user.name", "Privacy Lint Test")
            (root / "fixture.txt").write_text(_macos_path("bob") + "\n", encoding="utf-8")
            _run_git(root, "add", "fixture.txt")
            _run_git(root, "commit", "--quiet", "-m", "fixture")
            with _in_directory(root):
                self.assertEqual(main(["--tracked"]), 1)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
