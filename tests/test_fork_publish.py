# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fork the publisher writes into: finding, creating and syncing it."""

from __future__ import annotations

import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
bounded = import_module("git_bounded")
policy = import_module("proposal_policy")
refs = import_module("branch_refs")

BASE = "a" * 40
TARGET = "owner/repo"
FORK_ORG = "lfreleng-bot-forks"
FORK = f"{FORK_ORG}/repo"


def repo_object(**overrides: Any) -> dict[str, Any]:
    """A repository API object describing a fork of the target."""
    data: dict[str, Any] = {
        "full_name": FORK,
        "fork": True,
        "parent": {"full_name": TARGET},
    }
    data.update(overrides)
    return data


def absent() -> Exception:
    """The error gh raises for a missing resource."""
    return github.GitHubError("gh: Not Found (HTTP 404)")


class NoNetworkCase(unittest.TestCase):
    """Base class failing any test that reaches a real subprocess."""

    def setUp(self) -> None:
        """Forbid ``gh`` and ``git`` subprocesses alike."""
        for target, name in ((github.subprocess, "run"), (bounded.subprocess, "Popen")):
            guard = patch.object(
                target, name, side_effect=AssertionError("unexpected subprocess")
            )
            guard.start()
            self.addCleanup(guard.stop)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)


class EnsureForkTest(NoNetworkCase):
    """``ensure_fork`` finds the fork, or creates it and waits for it."""

    def test_existing_fork_is_used(self) -> None:
        """A repository that is a fork of the target is the destination."""
        with (
            patch.object(github, "api_object", return_value=repo_object()) as read,
            patch.object(github, "api_write") as write,
        ):
            self.assertEqual(refs.ensure_fork(TARGET, FORK_ORG, "repo"), FORK)
        read.assert_called_once_with(f"repos/{FORK}")
        write.assert_not_called()

    def test_case_differences_in_the_parent_are_tolerated(self) -> None:
        """GitHub reports owner names in their own case; the match folds it."""
        parent = {"full_name": "Owner/Repo"}
        with patch.object(
            github, "api_object", return_value=repo_object(parent=parent)
        ):
            self.assertEqual(refs.ensure_fork(TARGET, FORK_ORG, "repo"), FORK)

    def test_unrelated_repository_is_refused(self) -> None:
        """A same-named repository that is not the target's fork is an error."""
        for data in (
            repo_object(fork=False, parent=None),
            repo_object(parent={"full_name": "someone/else"}),
            repo_object(parent=None),
        ):
            with (
                self.subTest(data=data),
                patch.object(github, "api_object", return_value=data),
                patch.object(github, "api_write") as write,
                self.assertRaisesRegex(policy.PublishError, "not a fork of"),
            ):
                refs.ensure_fork(TARGET, FORK_ORG, "repo")
            write.assert_not_called()

    def test_read_failure_is_operational(self) -> None:
        """A 500 on the fork read never leads to a fork request."""
        with (
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 500)")
            ),
            patch.object(github, "api_write") as write,
            self.assertRaisesRegex(policy.PublishError, "could not read fork"),
        ):
            refs.ensure_fork(TARGET, FORK_ORG, "repo")
        write.assert_not_called()

    def test_missing_fork_is_created_then_awaited(self) -> None:
        """A 404 forks the target into the pool and polls until it appears."""
        with (
            patch.object(
                github,
                "api_object",
                side_effect=[absent(), absent(), repo_object()],
            ) as read,
            patch.object(github, "api_write", return_value={}) as write,
            patch.object(refs.time, "sleep") as sleep,
            patch.object(refs.time, "monotonic", side_effect=[0.0, 1.0, 2.0]),
        ):
            self.assertEqual(refs.ensure_fork(TARGET, FORK_ORG, "repo"), FORK)
        write.assert_called_once_with(
            "POST",
            f"repos/{TARGET}/forks",
            {"organization": FORK_ORG, "default_branch_only": True},
        )
        self.assertEqual(read.call_count, 3)
        sleep.assert_called_once_with(refs.FORK_POLL_SECONDS)

    def test_fork_request_failure_is_operational(self) -> None:
        """A refused fork request is reported without polling."""
        with (
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Forbidden (HTTP 403)"),
            ),
            patch.object(refs.time, "sleep") as sleep,
            self.assertRaisesRegex(policy.PublishError, "could not fork"),
        ):
            refs.ensure_fork(TARGET, FORK_ORG, "repo")
        sleep.assert_not_called()

    def test_poll_timeout_is_operational(self) -> None:
        """A fork that never appears before the deadline is an error."""
        with (
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(github, "api_write", return_value={}),
            patch.object(refs.time, "sleep"),
            patch.object(
                refs.time,
                "monotonic",
                side_effect=[0.0, 1.0, refs.FORK_WAIT_SECONDS + 1.0],
            ),
            self.assertRaisesRegex(policy.PublishError, "did not appear"),
        ):
            refs.ensure_fork(TARGET, FORK_ORG, "repo")

    def test_created_repository_must_still_be_a_fork(self) -> None:
        """What appears after the request is checked like a found one."""
        with (
            patch.object(
                github, "api_object", side_effect=[absent(), repo_object(fork=False)]
            ),
            patch.object(github, "api_write", return_value={}),
            patch.object(refs.time, "sleep"),
            patch.object(refs.time, "monotonic", return_value=0.0),
            self.assertRaisesRegex(policy.PublishError, "not a fork of"),
        ):
            refs.ensure_fork(TARGET, FORK_ORG, "repo")


class SyncForkTest(NoNetworkCase):
    """``sync_fork`` merges upstream, then requires the base commit."""

    def test_synced_and_base_present(self) -> None:
        """A clean sync followed by a found base commit passes."""
        with (
            patch.object(github, "api_write", return_value={}) as write,
            patch.object(github, "api_object", return_value={"sha": BASE}) as read,
        ):
            refs.sync_fork(FORK, "main", BASE)
        write.assert_called_once_with(
            "POST", f"repos/{FORK}/merge-upstream", {"branch": "main"}
        )
        read.assert_called_once_with(f"repos/{FORK}/commits/{BASE}")

    def test_diverged_fork_is_tolerated_when_the_base_exists(self) -> None:
        """A 409 from merge-upstream is not fatal; the base lookup decides."""
        with (
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Conflict (HTTP 409)"),
            ),
            patch.object(github, "api_object", return_value={"sha": BASE}),
        ):
            refs.sync_fork(FORK, "main", BASE)

    def test_other_sync_failures_are_operational(self) -> None:
        """Any status other than 409 from merge-upstream fails the sync."""
        with (
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Forbidden (HTTP 403)"),
            ),
            patch.object(github, "api_object") as read,
            self.assertRaisesRegex(policy.PublishError, "could not sync fork"),
        ):
            refs.sync_fork(FORK, "main", BASE)
        read.assert_not_called()

    def test_missing_base_is_operational(self) -> None:
        """A base the fork does not hold (422 or 404) cannot anchor the branch."""
        for status in (422, 404):
            with (
                self.subTest(status=status),
                patch.object(github, "api_write", return_value={}),
                patch.object(
                    github,
                    "api_object",
                    side_effect=github.GitHubError(f"gh: nope (HTTP {status})"),
                ),
                self.assertRaisesRegex(policy.PublishError, "is not in fork"),
            ):
                refs.sync_fork(FORK, "main", BASE)

    def test_base_read_failure_is_operational(self) -> None:
        """A 500 on the commit read is reported as such, not as a missing base."""
        with (
            patch.object(github, "api_write", return_value={}),
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 500)")
            ),
            self.assertRaisesRegex(policy.PublishError, "could not read"),
        ):
            refs.sync_fork(FORK, "main", BASE)


if __name__ == "__main__":
    unittest.main()
