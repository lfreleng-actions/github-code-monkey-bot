# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The revision writes: compare-and-swap branch move, replies and resolves."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
refs = import_module("branch_refs")
revision = import_module("revision_publish")
policy = import_module("proposal_policy")

BASE = "a" * 40
HEAD = "e" * 40
NEW = "f" * 40
FORK = "lfreleng-bot-forks/repo"


def check(**overrides: Any) -> dict[str, Any]:
    """A revision check.json as the publisher reads it."""
    data: dict[str, Any] = {
        "repository": "owner/repo",
        "fork_repository": FORK,
        "branch": "code-monkey/issue-7",
        "base_sha": BASE,
        "head_sha": HEAD,
        "round": 3,
        "commits": [{"sha": "c" * 40}],
    }
    data.update(overrides)
    return data


class MoveBranchTest(unittest.TestCase):
    """The chain lands on a scratch branch; the swap is conditional; cleanup always."""

    def test_swap_from_the_recorded_head(self) -> None:
        """updateRefs names the recorded head as beforeOid and the new tip after."""
        with (
            patch.object(refs, "create_branch") as create,
            patch.object(refs, "replay_commits", return_value=[NEW]) as replay,
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", return_value={}) as gql,
            patch.object(refs, "delete_branch", return_value=None) as delete,
        ):
            commits = revision.move_branch(Path("clone"), check())
        self.assertEqual(commits, [NEW])
        create.assert_called_once_with(FORK, "code-monkey/issue-7-r3", BASE)
        self.assertEqual(replay.call_args.args[1]["branch"], "code-monkey/issue-7-r3")
        update = gql.call_args.args[1]["input"]["refUpdates"][0]
        self.assertEqual(
            update,
            {
                "name": "refs/heads/code-monkey/issue-7",
                "afterOid": NEW,
                "beforeOid": HEAD,
                "force": True,
            },
        )
        self.assertEqual(gql.call_args.args[1]["input"]["repositoryId"], "R_1")
        delete.assert_called_once_with(FORK, "code-monkey/issue-7-r3")

    def test_moved_head_is_a_rejection_and_the_scratch_branch_goes(self) -> None:
        """A beforeOid mismatch means someone moved the branch: refuse, clean up."""
        failure = github.GitHubError(
            'GraphQL errors: [{"message": "Expected branch to point to beforeOid"}]'
        )
        with (
            patch.object(refs, "create_branch"),
            patch.object(refs, "replay_commits", return_value=[NEW]),
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", side_effect=failure),
            patch.object(refs, "delete_branch", return_value=None) as delete,
            self.assertRaisesRegex(policy.Rejection, "head moved"),
        ):
            revision.move_branch(Path("clone"), check())
        delete.assert_called_once()

    def test_other_failures_are_operational_and_clean_up(self) -> None:
        """A replay fault is a publish failure; the scratch branch still goes."""
        with (
            patch.object(refs, "create_branch"),
            patch.object(
                refs, "replay_commits", side_effect=policy.PublishError("boom")
            ),
            patch.object(refs, "delete_branch", return_value=None) as delete,
            self.assertRaises(policy.PublishError),
        ):
            revision.move_branch(Path("clone"), check())
        delete.assert_called_once()

    def test_leftover_scratch_branch_is_a_warning(self) -> None:
        """A cleanup that fails is recorded beside the outcome, not hidden."""
        data = check()
        with (
            patch.object(refs, "create_branch"),
            patch.object(refs, "replay_commits", return_value=[NEW]),
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", return_value={}),
            patch.object(refs, "delete_branch", return_value="branch x stays"),
        ):
            revision.move_branch(Path("clone"), data)
        self.assertEqual(data["warnings"], ["branch x stays"])

    def swap_lost(
        self, head: Any, error: str = "timed out"
    ) -> tuple[dict[str, Any], list[str] | None]:
        """A swap answered with ``error``; the fork branch then reads as ``head``."""
        data = check()
        lookup = (
            patch.object(reads, "branch_head", side_effect=head)
            if isinstance(head, Exception)
            else patch.object(reads, "branch_head", return_value=head)
        )
        with (
            patch.object(refs, "create_branch"),
            patch.object(refs, "replay_commits", return_value=[NEW]),
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", side_effect=github.GitHubError(error)),
            lookup as read,
            patch.object(refs, "delete_branch", return_value=None),
        ):
            try:
                commits = revision.move_branch(Path("clone"), data)
            except (policy.PublishError, policy.Rejection) as exc:
                data["error"] = exc
                commits = None
        read.assert_called_once_with(FORK, "code-monkey/issue-7")
        return data, commits

    def test_a_lost_swap_reply_is_reconciled_from_the_branch(self) -> None:
        """The fork branch, not the error, says whether the swap applied."""
        # Applied: the move stands, with a warning that the reply was lost.
        data, commits = self.swap_lost(NEW)
        self.assertEqual(commits, [NEW])
        self.assertTrue(any("reply" in w for w in data["warnings"]))
        # Not applied: the branch is unchanged and the failure says so.
        data, commits = self.swap_lost(HEAD)
        self.assertIsNone(commits)
        self.assertNotIsInstance(data["error"], revision.SwapUnknown)
        # Another head, or no answer: the outcome is unknown and said to be.
        for state in ("b" * 40, github.GitHubError("unreachable")):
            with self.subTest(state=str(state)[:12]):
                data, commits = self.swap_lost(state)
                self.assertIsNone(commits)
                self.assertIsInstance(data["error"], revision.SwapUnknown)
                self.assertIn("unknown", str(data["error"]))

    @unittest.expectedFailure
    def test_only_the_mismatch_message_is_a_moved_head(self) -> None:
        """ "unexpected EOF" is a lost reply, not a compare-and-swap refusal."""
        # The swap applied before the connection dropped: the move stands.
        data, commits = self.swap_lost(NEW, "unexpected EOF")
        self.assertEqual(commits, [NEW])
        self.assertTrue(any("reply" in w for w in data["warnings"]))
        # The real refusal, with the branch elsewhere, is the rejection.
        mismatch = (
            'GraphQL errors: [{"message": "Expected branch to point to beforeOid"}]'
        )
        data, commits = self.swap_lost("b" * 40, mismatch)
        self.assertIsInstance(data["error"], policy.Rejection)
        self.assertIn("head moved", str(data["error"]))
        # The refusal with the branch unreadable is still the refusal:
        # the message itself says the swap did not apply.
        data, commits = self.swap_lost(github.GitHubError("unreachable"), mismatch)
        self.assertIsInstance(data["error"], policy.Rejection)
        # A refusal while the branch holds the new tip is a lost reply.
        data, commits = self.swap_lost(NEW, mismatch)
        self.assertEqual(commits, [NEW])


class AnswerTest(unittest.TestCase):
    """Replies, resolves and the description edit address the right endpoints."""

    def test_reply_resolve_update(self) -> None:
        """REST reply, GraphQL resolve, REST PATCH."""
        with patch.object(github, "api_write", return_value={}) as write:
            revision.reply("owner/repo", 41, 100, "text")
            revision.update_pull_request("owner/repo", 41, "T", "B")
        self.assertEqual(
            write.call_args_list[0].args,
            (
                "POST",
                "repos/owner/repo/pulls/41/comments/100/replies",
                {"body": "text"},
            ),
        )
        self.assertEqual(
            write.call_args_list[1].args,
            ("PATCH", "repos/owner/repo/pulls/41", {"title": "T", "body": "B"}),
        )
        with patch.object(github, "graphql", return_value={}) as gql:
            revision.resolve("owner/repo", "PRRT_1")
        self.assertEqual(gql.call_args.args[1], {"id": "PRRT_1"})
        self.assertIn("resolveReviewThread", gql.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
