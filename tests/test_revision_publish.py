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
swap = import_module("branch_swap")
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
        "collected_at": "2026-10-03T00:00:00Z",
        "commits": [{"sha": "c" * 40}],
    }
    data.update(overrides)
    return data


SCRATCH = "code-monkey/issue-7-r3-20261003T000000Z"


class MoveBranchTest(unittest.TestCase):
    """The chain lands on a scratch branch; the swap is conditional; cleanup always."""

    @unittest.expectedFailure
    def test_swap_from_the_recorded_head(self) -> None:
        """updateRefs names the recorded head as beforeOid and the new tip after."""
        with (
            patch.object(refs, "create_branch") as create,
            patch.object(refs, "replay_commits", return_value=[NEW]) as replay,
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", return_value={}) as gql,
            patch.object(refs, "delete_branch", return_value=None) as delete,
        ):
            commits = swap.move_branch(Path("clone"), check())
        self.assertEqual(commits, [NEW])
        create.assert_called_once_with(FORK, SCRATCH, BASE)
        self.assertEqual(replay.call_args.args[1]["branch"], SCRATCH)
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
        delete.assert_called_once_with(FORK, SCRATCH)

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
            patch.object(reads, "branch_head", return_value="b" * 40),
            patch.object(refs, "delete_branch", return_value=None) as delete,
            self.assertRaisesRegex(policy.Rejection, "head moved"),
        ):
            swap.move_branch(Path("clone"), check())
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
            swap.move_branch(Path("clone"), check())
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
            swap.move_branch(Path("clone"), data)
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
                commits = swap.move_branch(Path("clone"), data)
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
        self.assertNotIsInstance(data["error"], swap.SwapUnknown)
        # Another head, or no answer: the outcome is unknown and said to be.
        for state in ("b" * 40, github.GitHubError("unreachable")):
            with self.subTest(state=str(state)[:12]):
                data, commits = self.swap_lost(state)
                self.assertIsNone(commits)
                self.assertIsInstance(data["error"], swap.SwapUnknown)
                self.assertIn("unknown", str(data["error"]))

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


class ScratchNameTest(unittest.TestCase):
    """The scratch branch is one session's alone."""

    @unittest.expectedFailure
    def test_the_name_carries_the_session(self) -> None:
        """Two sessions at one round, from two selections, build on two branches.

        Named dispatches may overlap and revise one pull request at the
        same round; a shared scratch name would let one session resume
        or delete the other's branch. The selection's read time names
        the session and is stable across retries of the same run.
        """
        first = swap.temporary_branch(check())
        second = swap.temporary_branch(check(collected_at="2026-10-03T00:00:05Z"))
        self.assertEqual(first, SCRATCH)
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("code-monkey/issue-7-r3-"))
        self.assertRegex(second, r"^[A-Za-z0-9/_.-]+$")
        # A retry of the same run names the same branch.
        self.assertEqual(swap.temporary_branch(check()), first)


class ScratchBranchTest(unittest.TestCase):
    """The scratch branch's creation is reconciled when its reply is lost."""

    def move(
        self, create: Any, head: Any, chain: list[str] | None = None
    ) -> tuple[dict[str, Any], list[str] | None, Any]:
        """Run the move with ``create`` as the create's outcome, the branch at ``head``."""
        data = check(bot_login="code-monkey[bot]")
        made = patch.object(refs, "create_branch", side_effect=create)
        lookup = (
            patch.object(reads, "branch_head", side_effect=head)
            if isinstance(head, Exception)
            else patch.object(reads, "branch_head", return_value=head)
        )
        with (
            made,
            lookup,
            patch.object(refs, "branch_matches", return_value=chain) as matches,
            patch.object(refs, "replay_commits", return_value=[NEW]) as replay,
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", return_value={}),
            patch.object(refs, "delete_branch", return_value=None) as delete,
        ):
            try:
                commits = swap.move_branch(Path("clone"), data)
            except policy.PublishError as exc:
                data["error"] = exc
                commits = None
        data["_replayed"] = replay.called
        data["_deleted"] = delete.called
        data["_matched"] = matches.called
        return data, commits, data.get("error")

    def test_a_created_scratch_branch_with_a_lost_reply_is_resumed(self) -> None:
        """The branch at the base is this attempt's: the replay goes on, with a note."""
        for create in (
            github.GitHubError("timed out"),
            policy.Rejection("branch code-monkey/issue-7-r3 already exists"),
        ):
            with self.subTest(create=str(create)):
                data, commits, error = self.move(create, BASE)
                self.assertIsNone(error)
                self.assertEqual(commits, [NEW])
                self.assertTrue(data["_replayed"])
                self.assertTrue(data["_deleted"])
                self.assertTrue(any("resumed" in w for w in data["warnings"]))

    def test_a_lost_create_reply_through_the_real_wrapper_is_resumed(self) -> None:
        """create_branch wraps the API error; the wrapped form is read back too."""
        data = check(bot_login="code-monkey[bot]")
        with (
            patch.object(
                github, "api_write", side_effect=github.GitHubError("timed out")
            ),
            patch.object(reads, "branch_head", return_value=BASE),
            patch.object(refs, "replay_commits", return_value=[NEW]) as replay,
            patch.object(github, "api_object", return_value={"node_id": "R_1"}),
            patch.object(github, "graphql", return_value={}),
            patch.object(refs, "delete_branch", return_value=None) as delete,
        ):
            commits = swap.move_branch(Path("clone"), data)
        self.assertEqual(commits, [NEW])
        replay.assert_called_once()
        delete.assert_called_once()
        self.assertTrue(any("resumed" in w for w in data["warnings"]))

    def test_a_scratch_branch_already_replayed_is_adopted(self) -> None:
        """A branch carrying the round's verified chain needs no second replay."""
        rejection = policy.Rejection("branch code-monkey/issue-7-r3 already exists")
        data, commits, error = self.move(rejection, NEW, chain=[NEW])
        self.assertIsNone(error)
        self.assertEqual(commits, [NEW])
        self.assertFalse(data["_replayed"])
        self.assertTrue(data["_deleted"])

    @unittest.expectedFailure
    def test_a_scratch_branch_of_unknown_content_is_kept(self) -> None:
        """Commits this run did not make are nobody's to delete; the ref is reported."""
        rejection = policy.Rejection("branch code-monkey/issue-7-r3 already exists")
        data, commits, error = self.move(rejection, "b" * 40, chain=None)
        self.assertIsInstance(error, policy.PublishError)
        self.assertIsNone(commits)
        self.assertFalse(data["_replayed"])
        self.assertFalse(data["_deleted"])
        self.assertTrue(any(SCRATCH in w for w in data["warnings"]))

    def test_a_create_that_left_no_branch_is_a_plain_failure(self) -> None:
        """No branch after the error means nothing was made; nothing is kept."""
        absent = github.GitHubError("Not Found (HTTP 404)", status=404)
        data, commits, error = self.move(github.GitHubError("timed out"), absent)
        self.assertIsInstance(error, policy.PublishError)
        self.assertNotIsInstance(error, policy.Rejection)
        self.assertIsNone(commits)
        self.assertFalse(data["_deleted"])
        self.assertEqual(data.get("warnings", []), [])


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
