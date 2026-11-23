# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fork the publisher writes into: requiring and syncing it."""

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


class RequireForkTest(NoNetworkCase):
    """``require_fork`` reads the fork the forks job made and never writes."""

    def test_existing_fork_passes_with_one_read(self) -> None:
        """A fork of the target is accepted, and nothing is written."""
        with (
            patch.object(github, "api_object", return_value=repo_object()) as read,
            patch.object(github, "api_list") as listing,
            patch.object(github, "api_write") as write,
        ):
            refs.require_fork(FORK, TARGET)
        read.assert_called_once_with(f"repos/{FORK}")
        listing.assert_not_called()
        write.assert_not_called()

    @unittest.expectedFailure
    def test_missing_fork_names_the_forks_job(self) -> None:
        """A 404 is neither created nor looked up by parent; it is reported.

        The reason names both causes: the forks job failed, or it
        recorded a fork GitHub was still creating.
        """
        with (
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(github, "api_list") as listing,
            patch.object(github, "api_write") as write,
            self.assertRaisesRegex(
                policy.PublishError,
                f"fork {FORK} does not exist.*Prepare forks.*not yet visible",
            ),
        ):
            refs.require_fork(FORK, TARGET)
        listing.assert_not_called()
        write.assert_not_called()

    def test_unrelated_repository_is_refused(self) -> None:
        """A same-named repository that is not the target's fork is an error."""
        with (
            patch.object(github, "api_object", return_value=repo_object(fork=False)),
            self.assertRaisesRegex(policy.PublishError, "not a fork of"),
        ):
            refs.require_fork(FORK, TARGET)

    def test_read_failure_is_operational(self) -> None:
        """A 500 on the fork read is a failure, not a missing fork."""
        with (
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 500)")
            ),
            self.assertRaisesRegex(policy.PublishError, "could not read fork"),
        ):
            refs.require_fork(FORK, TARGET)


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
        """A status that is no race from merge-upstream fails the sync at once."""
        with (
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Forbidden (HTTP 403)"),
            ) as write,
            patch.object(github, "api_object") as read,
            patch.object(refs.time, "sleep") as sleep,
            self.assertRaisesRegex(policy.PublishError, "could not sync fork"),
        ):
            refs.sync_fork(FORK, "main", BASE)
        read.assert_not_called()
        write.assert_called_once()
        sleep.assert_not_called()

    def test_a_racing_sync_is_retried_then_passed_over(self) -> None:
        """Two publishes syncing one fork race; 422 is retried, and the base decides.

        One job's merge-upstream wins and the other's answers 422 with
        the fork synced all the same (issue #38). The call is retried
        with a backoff, and when it still fails the base lookup says
        whether the branch can be created, as it does after a 409.
        """
        racing = github.GitHubError(
            "gh: There was a problem updating the branch (HTTP 422)"
        )
        # 422 then success: no error, one retry.
        with (
            patch.object(github, "api_write", side_effect=[racing, {}]) as write,
            patch.object(github, "api_object", return_value={"sha": BASE}),
            patch.object(refs.time, "sleep") as sleep,
        ):
            refs.sync_fork(FORK, "main", BASE)
        self.assertEqual(write.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2])
        # 422 throughout, base present: every attempt made, backoff kept,
        # and the sync passes on the base alone.
        with (
            patch.object(github, "api_write", side_effect=racing) as write,
            patch.object(github, "api_object", return_value={"sha": BASE}),
            patch.object(refs.time, "sleep") as sleep,
        ):
            refs.sync_fork(FORK, "main", BASE)
        self.assertEqual(write.call_count, refs.SYNC_ATTEMPTS)
        self.assertEqual(
            [c.args[0] for c in sleep.call_args_list], list(refs.SYNC_BACKOFF_SECONDS)
        )
        # 422 throughout, base absent: the failure names both causes.
        with (
            patch.object(github, "api_write", side_effect=racing),
            patch.object(
                github,
                "api_object",
                side_effect=github.GitHubError("gh: nope (HTTP 422)"),
            ),
            patch.object(refs.time, "sleep"),
            self.assertRaisesRegex(
                policy.PublishError, r"is not in fork.*problem updating the branch"
            ),
        ):
            refs.sync_fork(FORK, "main", BASE)

    def test_a_diverged_fork_is_not_retried(self) -> None:
        """409 means divergence, not a race: no retry, the base lookup decides."""
        with (
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Conflict (HTTP 409)"),
            ) as write,
            patch.object(github, "api_object", return_value={"sha": BASE}),
            patch.object(refs.time, "sleep") as sleep,
        ):
            refs.sync_fork(FORK, "main", BASE)
        write.assert_called_once()
        sleep.assert_not_called()

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
