# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The proposal profile over the shared bounded artifact fetch.

The download and extraction mechanics are covered by
``test_artifact_fetch``; these tests pin the proposal's cap table and
the command-line contract the publish job relies on.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

fetcher = import_module("proposal_fetch")
artifacts = import_module("artifact_fetch")
github = import_module("bot_github")

MANIFEST = b'{"schema": 1, "outcome": "abstain", "reason": "no"}\n'
ARGS = ["--repository", "o/r", "--run-id", "1", "--name", "p", "--output", "x"]


def make_zip(entries: dict[str, bytes], path: Path) -> Path:
    """Write a deflated zip of the given entries."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    return path


class ProfileTest(unittest.TestCase):
    """``PROPOSAL_FILES`` names what the publisher takes from a proposal."""

    def setUp(self) -> None:
        """A scratch directory for archives and output."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.output = self.root / "accepted"

    def test_cap_table(self) -> None:
        """The manifest is required; the bundle and usage are optional."""
        self.assertEqual(
            fetcher.PROPOSAL_FILES,
            (
                ("manifest.json", 1024 * 1024, True),
                ("changes.bundle", 32 * 1024 * 1024, False),
                ("usage.json", 1024 * 1024, False),
            ),
        )

    def test_permitted_files_only(self) -> None:
        """Other entries are ignored and never written."""
        archive = make_zip(
            {"manifest.json": MANIFEST, "usage.json": b"{}", "evil.py": b"x"},
            self.root / "a.zip",
        )
        copied = artifacts.extract(archive, self.output, fetcher.PROPOSAL_FILES)
        self.assertEqual(copied, ["manifest.json", "usage.json"])
        self.assertEqual((self.output / "manifest.json").read_bytes(), MANIFEST)
        self.assertFalse((self.output / "evil.py").exists())

    def test_manifest_required(self) -> None:
        """A proposal without a manifest is refused."""
        archive = make_zip({"usage.json": b"{}"}, self.root / "a.zip")
        with self.assertRaisesRegex(fetcher.Refused, "lacks manifest.json"):
            artifacts.extract(archive, self.output, fetcher.PROPOSAL_FILES)

    def test_oversized_manifest_refused(self) -> None:
        """A manifest past its cap is refused."""
        big = b"0" * (fetcher.MAX_MANIFEST_BYTES + 1)
        archive = make_zip({"manifest.json": big}, self.root / "a.zip")
        with self.assertRaisesRegex(fetcher.Refused, "exceeds its cap"):
            artifacts.extract(archive, self.output, fetcher.PROPOSAL_FILES)

    def test_fetch_uses_the_proposal_profile(self) -> None:
        """``fetch`` hands the shared fetch the proposal cap table."""
        with patch.object(
            artifacts, "fetch_from_run", return_value=(5, ["manifest.json"])
        ) as shared:
            result = fetcher.fetch("o/r", "1", "p", Path("x"))
        self.assertEqual(result, (5, ["manifest.json"]))
        shared.assert_called_once_with(
            "o/r", "1", "p", Path("x"), fetcher.PROPOSAL_FILES
        )


class MainTest(unittest.TestCase):
    """Exit codes tell the workflow what happened."""

    def run_main(self, error: Exception) -> int:
        """Run main with fetch raising ``error``; return the exit code."""
        with (
            patch.object(fetcher, "fetch", side_effect=error),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            fetcher.main(ARGS)
        return int(raised.exception.code or 0)

    def test_exit_codes(self) -> None:
        """Missing is 3, refused is 4, an operational fault is 1."""
        self.assertEqual(self.run_main(fetcher.Missing("gone")), 3)
        self.assertEqual(self.run_main(fetcher.Refused("big")), 4)
        self.assertEqual(self.run_main(github.GitHubError("boom")), 1)
        self.assertEqual(self.run_main(subprocess.TimeoutExpired("gh", 1)), 1)

    def test_exceptions_are_the_shared_ones(self) -> None:
        """What the shared fetch raises is what the wrapper catches."""
        self.assertIs(fetcher.Missing, artifacts.Missing)
        self.assertIs(fetcher.Refused, artifacts.Refused)

    def test_success_writes_the_artifact_id_as_a_step_output(self) -> None:
        """Stdout carries only the step output; the file list goes to stderr."""
        out = io.StringIO()
        with (
            patch.object(fetcher, "fetch", return_value=(5, ["manifest.json"])),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            fetcher.main(ARGS)
        self.assertEqual(out.getvalue(), "artifact_id=5\n")


if __name__ == "__main__":
    unittest.main()
