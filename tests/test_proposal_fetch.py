# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bounded fetch of an untrusted proposal artifact."""

from __future__ import annotations

import io
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

fetcher = import_module("proposal_fetch")
github = import_module("monkey_github")
evidence = import_module("monkey_evidence")

MANIFEST = b'{"schema": 1, "outcome": "abstain", "reason": "no"}\n'


def make_zip(entries: dict[str, bytes], path: Path) -> Path:
    """Write a deflated zip of the given entries."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    return path


def understate_sizes(path: Path, name: str, claimed: int) -> None:
    """Rewrite a zip's headers so ``name`` claims ``claimed`` bytes.

    Models a hostile archive whose directory lies about its size; the
    reader must still stop at the cap when it decompresses.
    """
    rewrite_headers(path, name, {b"PK\x03\x04": 22, b"PK\x01\x02": 24}, "<I", claimed)


def rewrite_headers(
    path: Path, name: str, offsets: dict[bytes, int], fmt: str, value: int
) -> None:
    """Overwrite one field of ``name``'s local and central headers."""
    data = bytearray(path.read_bytes())
    encoded = name.encode()
    for signature, field_offset in offsets.items():
        start = 0
        while (index := data.find(signature, start)) != -1:
            name_offset = index + (30 if signature == b"PK\x03\x04" else 46)
            if bytes(data[name_offset : name_offset + len(encoded)]) == encoded:
                struct.pack_into(fmt, data, index + field_offset, value)
            start = index + 4
    path.write_bytes(bytes(data))


class ExtractTest(unittest.TestCase):
    """``extract`` takes the permitted files alone, each under its cap."""

    def setUp(self) -> None:
        """A scratch directory for archives and output."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.output = self.root / "accepted"

    def test_permitted_files_only(self) -> None:
        """Other entries are ignored and never written."""
        archive = make_zip(
            {"manifest.json": MANIFEST, "usage.json": b"{}", "evil.py": b"x"},
            self.root / "a.zip",
        )
        copied = fetcher.extract(archive, self.output)
        self.assertEqual(copied, ["manifest.json", "usage.json"])
        self.assertEqual((self.output / "manifest.json").read_bytes(), MANIFEST)
        self.assertFalse((self.output / "evil.py").exists())

    def test_manifest_required(self) -> None:
        """A proposal without a manifest is refused."""
        archive = make_zip({"usage.json": b"{}"}, self.root / "a.zip")
        with self.assertRaisesRegex(fetcher.Refused, "lacks manifest.json"):
            fetcher.extract(archive, self.output)

    def test_oversized_entry_refused_from_its_header(self) -> None:
        """A declared size past the cap is refused before decompression."""
        big = b"0" * (evidence.MAX_MANIFEST_BYTES + 1)
        archive = make_zip({"manifest.json": big}, self.root / "a.zip")
        with self.assertRaisesRegex(fetcher.Refused, "exceeds its cap"):
            fetcher.extract(archive, self.output)

    def test_understated_header_cannot_expand_past_the_cap(self) -> None:
        """A lying header still stops at the cap on read."""
        big = b"0" * (evidence.MAX_MANIFEST_BYTES + 4096)
        archive = make_zip({"manifest.json": big}, self.root / "a.zip")
        understate_sizes(archive, "manifest.json", 10)
        with self.assertRaises(fetcher.Refused):
            fetcher.extract(archive, self.output)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_symlink_entry_refused(self) -> None:
        """An entry recorded as a symlink is refused."""
        archive = self.root / "a.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            info = zipfile.ZipInfo("manifest.json")
            info.external_attr = (0o120777) << 16
            bundle.writestr(info, "/etc/passwd")
        with self.assertRaisesRegex(fetcher.Refused, "not a regular file"):
            fetcher.extract(archive, self.output)

    def test_too_many_entries_refused(self) -> None:
        """An archive of many entries is refused."""
        entries = {f"f{i}": b"x" for i in range(fetcher.MAX_ENTRIES + 1)}
        entries["manifest.json"] = MANIFEST
        archive = make_zip(entries, self.root / "a.zip")
        with self.assertRaisesRegex(fetcher.Refused, "entries"):
            fetcher.extract(archive, self.output)

    def test_not_a_zip_refused(self) -> None:
        """Garbage is a refusal, not a crash."""
        archive = self.root / "a.zip"
        archive.write_bytes(b"not a zip")
        with self.assertRaisesRegex(fetcher.Refused, "not a valid zip"):
            fetcher.extract(archive, self.output)

    def test_unsupported_compression_refused(self) -> None:
        """A compression method zipfile cannot read is a refusal."""
        archive = self.root / "a.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            bundle.writestr("manifest.json", MANIFEST)
        # Method field: offset 8 locally, 10 in the central directory.
        offsets = {b"PK\x03\x04": 8, b"PK\x01\x02": 10}
        rewrite_headers(archive, "manifest.json", offsets, "<H", 99)
        with self.assertRaisesRegex(fetcher.Refused, "compression"):
            fetcher.extract(archive, self.output)

    def test_encrypted_entry_refused(self) -> None:
        """An entry flagged as encrypted is a refusal."""
        archive = self.root / "a.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            bundle.writestr("manifest.json", MANIFEST)
        # Flag field: offset 6 locally, 8 in the central directory.
        offsets = {b"PK\x03\x04": 6, b"PK\x01\x02": 8}
        rewrite_headers(archive, "manifest.json", offsets, "<H", 1)
        with self.assertRaisesRegex(fetcher.Refused, "encrypted"):
            fetcher.extract(archive, self.output)

    def test_corrupt_deflate_stream_refused(self) -> None:
        """Deflate data that fails to decode is a refusal."""
        archive = make_zip({"manifest.json": MANIFEST * 64}, self.root / "a.zip")
        data = bytearray(archive.read_bytes())
        # Corrupt the compressed bytes just past the local header and name.
        start = 30 + len("manifest.json")
        data[start : start + 8] = b"\xff" * 8
        archive.write_bytes(bytes(data))
        with self.assertRaises(fetcher.Refused):
            fetcher.extract(archive, self.output)


class FindArtifactTest(unittest.TestCase):
    """``find_artifact`` wants exactly one live artifact of the name."""

    def listing(self, *entries: dict[str, Any]) -> Any:
        """Patch the listing call to return these artifacts."""
        return patch.object(
            github, "api_object", return_value={"artifacts": list(entries)}
        )

    def test_one_live_artifact(self) -> None:
        """The id and zip size come back; the name is URL-encoded."""
        with self.listing({"name": "p", "id": 5, "size_in_bytes": 99}) as read:
            self.assertEqual(fetcher.find_artifact("o/r", "1", "p"), (5, 99))
        self.assertIn("name=p", read.call_args.args[0])

    def test_missing_and_expired(self) -> None:
        """No live artifact of the name is Missing."""
        with (
            self.listing({"name": "p", "id": 5, "size_in_bytes": 1, "expired": True}),
            self.assertRaises(fetcher.Missing),
        ):
            fetcher.find_artifact("o/r", "1", "p")

    def test_duplicates_refused(self) -> None:
        """Two artifacts of one name are ambiguous and refused."""
        entry = {"name": "p", "id": 5, "size_in_bytes": 1}
        with (
            self.listing(entry, {**entry, "id": 6}),
            self.assertRaises(fetcher.Refused),
        ):
            fetcher.find_artifact("o/r", "1", "p")


class FetchTest(unittest.TestCase):
    """``fetch`` refuses oversized artifacts before downloading them."""

    def test_reported_size_over_limit_is_never_downloaded(self) -> None:
        """GitHub's own size figure stops the fetch before any transfer."""
        with (
            patch.object(
                fetcher, "find_artifact", return_value=(5, fetcher.ZIP_LIMIT + 1)
            ),
            patch.object(fetcher, "download") as download,
            tempfile.TemporaryDirectory() as holder,
            self.assertRaisesRegex(fetcher.Refused, "over"),
        ):
            fetcher.fetch("o/r", "1", "p", Path(holder))
        download.assert_not_called()

    def test_stream_past_limit_is_cut_off(self) -> None:
        """A download that runs past the limit is stopped and refused."""
        stream = io.BytesIO(b"x" * (fetcher.ZIP_LIMIT + fetcher.CHUNK))

        class FakeProc:
            """A gh process whose stdout never ends soon enough."""

            stdout = stream
            returncode = -9

            def kill(self) -> None:
                """Record nothing; the fetch must call this."""

            def wait(self, timeout: float | None = None) -> int:
                """Report the kill."""
                return self.returncode

        with (
            patch.object(fetcher.subprocess, "Popen", return_value=FakeProc()),
            tempfile.TemporaryDirectory() as holder,
            self.assertRaisesRegex(fetcher.Refused, "exceeds"),
        ):
            fetcher.download("o/r", 5, Path(holder) / "z.zip")

    def test_stalled_download_is_killed_at_the_deadline(self) -> None:
        """A gh process that never writes cannot outlive the timeout."""
        real_popen = subprocess.Popen
        stalled = [sys.executable, "-c", "import time; time.sleep(5)"]

        def spawn(_args: list[str], **kwargs: Any) -> Any:
            """Start a silent process in place of gh."""
            return real_popen(stalled, **kwargs)

        started = time.monotonic()
        with (
            patch.object(fetcher, "TIMEOUT_SECONDS", 1),
            patch.object(fetcher.subprocess, "Popen", side_effect=spawn),
            tempfile.TemporaryDirectory() as holder,
            self.assertRaisesRegex(github.GitHubError, "timed out"),
        ):
            fetcher.download("o/r", 5, Path(holder) / "z.zip")
        self.assertLess(time.monotonic() - started, 4)


class MainTest(unittest.TestCase):
    """Exit codes tell the workflow what happened."""

    def run_main(self, error: Exception) -> int:
        """Run main with fetch raising ``error``; return the exit code."""
        with (
            patch.object(fetcher, "fetch", side_effect=error),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            fetcher.main(
                ["--repository", "o/r", "--run-id", "1", "--name", "p", "--output", "x"]
            )
        return int(raised.exception.code or 0)

    def test_exit_codes(self) -> None:
        """Missing is 3, refused is 4, an operational fault is 1."""
        self.assertEqual(self.run_main(fetcher.Missing("gone")), fetcher.NOT_FOUND)
        self.assertEqual(self.run_main(fetcher.Refused("big")), fetcher.REFUSED)
        self.assertEqual(self.run_main(github.GitHubError("boom")), 1)
        self.assertEqual(self.run_main(subprocess.TimeoutExpired("gh", 1)), 1)

    def test_success_writes_the_artifact_id_as_a_step_output(self) -> None:
        """Stdout carries only the step output; the file list goes to stderr."""
        out = io.StringIO()
        with (
            patch.object(fetcher, "fetch", return_value=(5, ["manifest.json"])),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            fetcher.main(
                ["--repository", "o/r", "--run-id", "1", "--name", "p", "--output", "x"]
            )
        self.assertEqual(out.getvalue(), "artifact_id=5\n")


if __name__ == "__main__":
    unittest.main()
