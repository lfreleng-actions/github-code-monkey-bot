# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The select job reads a target's sudo and container marker (#189)."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
MARKER = ".enable-sudo-and-containers"


def marker_module() -> Any:
    """Import the module under test where a missing one fails the test alone."""
    return import_module("sudo_marker")


def tree(*entries: dict[str, Any], truncated: bool = False) -> dict[str, Any]:
    """A root tree reply as ``repos/{repo}/git/trees/{sha}`` returns it."""
    return {"sha": SHA_C, "tree": list(entries), "truncated": truncated}


def entry(path: str, mode: str, kind: str) -> dict[str, Any]:
    """One root tree entry."""
    return {"path": path, "mode": mode, "type": kind, "sha": SHA_C}


README = entry("README.md", "100644", "blob")


class OptedInTest(unittest.TestCase):
    """``opted_in`` reads one root tree and accepts a regular file alone."""

    def opted_in(self, reply: dict[str, Any]) -> tuple[bool, list[str]]:
        """Run ``opted_in`` against one canned reply; return it and the reads."""
        endpoints: list[str] = []

        def read(endpoint: str) -> dict[str, Any]:
            endpoints.append(endpoint)
            return reply

        with patch.object(github, "api_object", side_effect=read):
            result = marker_module().opted_in("org/alpha", SHA_A)
        return result, endpoints

    @unittest.expectedFailure
    def test_regular_file_opts_in(self) -> None:
        """A regular file at the root is the switch, executable or not."""
        for mode in ("100644", "100755"):
            with self.subTest(mode=mode):
                result, endpoints = self.opted_in(
                    tree(README, entry(MARKER, mode, "blob"))
                )
                self.assertIs(result, True)
                self.assertEqual(endpoints, [f"repos/org/alpha/git/trees/{SHA_A}"])

    @unittest.expectedFailure
    def test_absent_marker_stays_off(self) -> None:
        """No entry of that name is not opted in."""
        result, _ = self.opted_in(tree(README))
        self.assertIs(result, False)

    @unittest.expectedFailure
    def test_marker_that_is_not_a_regular_file_stays_off(self) -> None:
        """A symlink, a directory or a submodule of that name does not count."""
        for mode, kind in (
            ("120000", "blob"),
            ("040000", "tree"),
            ("160000", "commit"),
        ):
            with self.subTest(kind=kind, mode=mode):
                result, _ = self.opted_in(tree(entry(MARKER, mode, kind)))
                self.assertIs(result, False)

    @unittest.expectedFailure
    def test_name_matches_exactly(self) -> None:
        """Another case, or the name nested in a directory, does not count."""
        for path in (MARKER.upper(), f"docs/{MARKER}", MARKER + ".txt"):
            with self.subTest(path=path):
                result, _ = self.opted_in(tree(entry(path, "100644", "blob")))
                self.assertIs(result, False)

    @unittest.expectedFailure
    def test_truncated_tree_without_the_marker_fails(self) -> None:
        """A listing cut short cannot prove the marker absent."""
        with self.assertRaises(github.GitHubError):
            self.opted_in(tree(README, truncated=True))

    @unittest.expectedFailure
    def test_reply_without_entries_fails(self) -> None:
        """A tree reply with no entry list is a failed read, not an answer."""
        with self.assertRaises(github.GitHubError):
            self.opted_in({"sha": SHA_C})

    @unittest.expectedFailure
    def test_failed_read_propagates(self) -> None:
        """Any failed read fails the selection, as its other reads of a target do."""
        failure = github.GitHubError("boom", status=502)
        with (
            patch.object(github, "api_object", side_effect=failure),
            self.assertRaises(github.GitHubError),
        ):
            marker_module().opted_in("org/alpha", SHA_A)

    @unittest.expectedFailure
    def test_commit_must_be_a_sha(self) -> None:
        """A branch name is refused: the marker is read at a recorded commit."""
        with (
            patch.object(github, "api_object") as read,
            self.assertRaises(github.GitHubError),
        ):
            marker_module().opted_in("org/alpha", "main")
        read.assert_not_called()


class AttachTest(unittest.TestCase):
    """``attach`` records the answer per entry, read at its ``checkout_sha``."""

    @unittest.expectedFailure
    def test_reads_each_entry_at_its_default_branch_commit(self) -> None:
        """An issue reads its base; a round its rebase, never the bot head."""
        issue = {
            "repository": "org/alpha",
            "task": "issue",
            "base_sha": SHA_A,
            "checkout_sha": SHA_A,
            "branch": "code-monkey/issue-3",
        }
        revision = {
            "repository": "org/beta",
            "task": "revise",
            "base_sha": SHA_A,
            "rebase_sha": SHA_B,
            "checkout_sha": SHA_B,
            "branch": "code-monkey/issue-5",
            "pull_request": {"number": 9, "head_sha": SHA_C},
        }
        answers = {("org/alpha", SHA_A): False, ("org/beta", SHA_B): True}

        def answer(repository: str, commit: str) -> bool:
            """The canned answer for one repository at one commit."""
            return answers[(repository, commit)]

        module = marker_module()
        with patch.object(module, "opted_in", side_effect=answer) as read:
            module.attach([issue, revision])
        self.assertEqual(
            [call.args for call in read.call_args_list],
            [("org/alpha", SHA_A), ("org/beta", SHA_B)],
        )
        self.assertIs(issue["sudo_and_containers"], False)
        self.assertIs(revision["sudo_and_containers"], True)


if __name__ == "__main__":
    unittest.main()
