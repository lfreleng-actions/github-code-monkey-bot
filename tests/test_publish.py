# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Publication: push, open, comment text, commit replay, report and the CLI."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
checks = import_module("proposal_check")
bounded = import_module("git_bounded")
policy = import_module("proposal_policy")
publish = import_module("publish")
refs = import_module("branch_refs")
review = import_module("copilot_review")
comments = import_module("issue_comment")
reporting = import_module("proposal_report")

BASE = "a" * 40
BLOB_ONE = "1" * 40
BLOB_TWO = "2" * 40
REPOSITORY = "owner/repo"
FORK_ORG = "lfreleng-bot-forks"
FORK = f"{FORK_ORG}/repo"
BRANCH = "code-monkey/issue-7"
FORK_BRANCH_URL = f"https://github.com/{FORK}/tree/{BRANCH}"


BOT = "code-monkey[bot]"


def check_json(**overrides: Any) -> dict[str, Any]:
    """A check.json document with a proposed verdict and two commits."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "repo-7",
        "repository": REPOSITORY,
        "fork_org": FORK_ORG,
        "fork_repository": FORK,
        "issue": 7,
        "branch": BRANCH,
        "base_sha": BASE,
        "default_branch": "main",
        "verdict": "proposed",
        "reasons": [],
        "needs_workflows": False,
        "commits": [
            {
                "sha": "c" * 40,
                "tree": "7" * 40,
                "headline": "Fix: One",
                "body": "Body one\n\nSigned-off-by: b <b@x>",
                "additions": [{"path": "a.txt", "blob": BLOB_ONE, "size": 3}],
                "deletions": ["gone.txt"],
            },
            {
                "sha": "d" * 40,
                "tree": "8" * 40,
                "headline": "Fix: Two",
                "body": "Signed-off-by: b <b@x>",
                "additions": [{"path": "dir/b.txt", "blob": BLOB_TWO, "size": 3}],
                "deletions": [],
            },
        ],
        "pr_title": "Fix: Both",
        "pr_body": "Closes #7\n",
        "stats": {"files_changed": 3, "added_bytes": 6},
        "commands": [],
        "premium_requests": 4,
        "agent_seconds": 90,
        "bot_login": BOT,
    }
    data.update(overrides)
    return data


HEAD = "e" * 40
NEW_HEAD = "d" * 40


def revision_check(**overrides: Any) -> dict[str, Any]:
    """A check.json for a revision round, proposed."""
    data = check_json(
        task="revise",
        rebase_sha="b" * 40,
        rebased=False,
        head_sha=HEAD,
        pull_number=41,
        pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
        round=2,
        round_summary="Fixed both findings.",
        collected_at="2026-10-03T00:00:00Z",
        responses=[
            {
                "thread_id": "PRRT_1",
                "comment_id": 100,
                "reply": "Fixed.",
                "resolve": True,
            },
            {
                "thread_id": "PRRT_2",
                "comment_id": 101,
                "reply": "Disputed.",
                "resolve": False,
            },
        ],
    )
    data.update(overrides)
    return data


def result_json(**overrides: Any) -> dict[str, Any]:
    """A result.json document."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "repo-7",
        "repository": REPOSITORY,
        "fork_repository": FORK,
        "issue": 7,
        "verdict": "proposed",
        "reasons": [],
        "warnings": [],
        "dry_run": False,
        "mode": "pull-requests",
        "branch_url": FORK_BRANCH_URL,
        "commits": ["e" * 40],
        "pull_request_url": f"https://github.com/{REPOSITORY}/pull/8",
        "comment_url": None,
        "pr_title": "Fix: Both",
        "premium_requests": 4,
        "agent_seconds": 90,
    }
    data.update(overrides)
    return data


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

    def write_check(self, data: dict[str, Any]) -> Path:
        """Write a check.json and return its path."""
        path = self.root / "check.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def push_args(
        self, check: Path, *, mode: str = "pull-requests", dry_run: bool = False
    ) -> argparse.Namespace:
        """The namespace ``run_push`` receives."""
        return argparse.Namespace(
            check=check,
            workdir=self.root / "work",
            mode=mode,
            dry_run=dry_run,
            run_attempt=1,
            author_session="",
            output=self.root / "result.json",
        )

    def open_args(self, result: dict[str, Any], check: Path) -> argparse.Namespace:
        """The namespace ``run_open`` receives, with ``result`` written out."""
        path = self.root / "pushed.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return argparse.Namespace(
            result=path, check=check, output=self.root / "result.json"
        )

    def written_result(self) -> dict[str, Any]:
        """The result.json a failing command left behind."""
        return json.loads((self.root / "result.json").read_text(encoding="utf-8"))


class CommentBodyTest(unittest.TestCase):
    """``comment_body`` renders one line per verdict."""

    def test_proposed_pull_request(self) -> None:
        """A pull request URL names the pull request."""
        body = comments.comment_body(result_json(), "https://run")
        self.assertEqual(
            body,
            "🐒 An AI agent has proposed a change for this issue: pull request "
            f"https://github.com/{REPOSITORY}/pull/8",
        )

    def test_proposed_branch_only(self) -> None:
        """Without a pull request the branch URL is named."""
        body = comments.comment_body(result_json(pull_request_url=None), "")
        self.assertIsNotNone(body)
        self.assertIn(f"branch {FORK_BRANCH_URL}", body or "")

    def test_proposed_without_url(self) -> None:
        """A proposed result with no URL (dry run) says nothing."""
        result = result_json(pull_request_url=None, branch_url=None)
        self.assertIsNone(comments.comment_body(result, ""))

    def test_abstain(self) -> None:
        """Abstain quotes the joined reasons."""
        result = result_json(verdict="abstain", reasons=["too big", "unclear"])
        self.assertEqual(
            comments.comment_body(result, ""),
            "🐒 An AI agent looked at this issue and did not attempt it: too big; unclear",
        )

    def test_rejected(self) -> None:
        """Rejected names the check and the run."""
        result = result_json(verdict="rejected", reasons=["symlink"])
        self.assertEqual(
            comments.comment_body(result, "https://run"),
            "🐒 An AI agent attempted this issue but its proposal failed a policy "
            "check (symlink). Run: https://run",
        )

    def test_author_failed(self) -> None:
        """author-failed says it did not finish; missing run URL is n/a."""
        result = result_json(verdict="author-failed", reasons=["timeout"])
        self.assertEqual(
            comments.comment_body(result, ""),
            "🐒 An AI agent attempted this issue but did not finish (timeout). Run: n/a",
        )

    def test_unknown_verdict(self) -> None:
        """An unknown verdict yields no comment."""
        self.assertIsNone(comments.comment_body(result_json(verdict="weird"), ""))

    def test_long_reasons_are_bounded_and_keep_the_run_url(self) -> None:
        """An oversized manifest reason cannot push the comment past GitHub's limit."""
        text = comments.comment_body(
            {"verdict": "abstain", "reasons": ["x" * 70_000]}, "https://run"
        )
        self.assertIsNotNone(text)
        self.assertLess(len(text or ""), policy.MAX_PR_BODY)
        self.assertTrue((text or "").endswith("…"))
        rejected = comments.comment_body(
            {"verdict": "rejected", "reasons": ["y" * 70_000]}, "https://run"
        )
        self.assertIn("https://run", rejected or "")

    def test_reasons_render_on_one_line(self) -> None:
        """A multiline reason cannot add blocks or mentions to the comment."""
        text = comments.comment_body(
            {"verdict": "abstain", "reasons": ["no\n\n# Owned\n@alice"]},
            "https://run",
        )
        self.assertNotIn("\n", text or "")
        self.assertNotIn("@alice", text or "")

    def test_publish_failed_states_the_reason_not_a_promise(self) -> None:
        """The reason says whether the branch came off; no eligibility promise."""
        for reason in ("boom; branch removed", "boom; branch b could not be removed"):
            with self.subTest(reason=reason):
                text = comments.comment_body(
                    {"verdict": "publish-failed", "reasons": [reason]}, "https://run"
                )
                self.assertIn(reason, text or "")
                self.assertIn("https://run", text or "")
                self.assertNotIn("eligible", text or "")

    def test_review_verdict_is_stated(self) -> None:
        """The comment says what Copilot's review of the head concluded."""
        clean = comments.comment_body(
            result_json(review=review_record("clean", assessment="approval")), ""
        )
        self.assertIn("pull request", clean or "")
        self.assertIn("Copilot review of it is clean", clean or "")
        findings = comments.comment_body(
            result_json(
                review=review_record(
                    "findings", assessment="changes", unresolved_threads=2, suppressed=1
                )
            ),
            "",
        )
        self.assertIn("2 unresolved review threads", findings or "")
        self.assertIn("1 suppressed finding", findings or "")
        missed = comments.comment_body(
            result_json(
                review=review_record(
                    "findings", assessment="closer-look", previously_missed=3
                )
            ),
            "",
        )
        self.assertIn("3 previously missed findings", missed or "")
        self.assertIn("needs a closer look", missed or "")
        self.assertIn("changes recommended", findings or "")
        self.assertIn("a maintainer must act", findings or "")
        self.assertNotIn("ready", (findings or "").lower())
        pending = comments.comment_body(
            result_json(review=review_record("pending")), ""
        )
        self.assertIn("no Copilot review", pending or "")
        self.assertNotIn("clean", pending or "")

    def test_revision_round_comment(self) -> None:
        """A round names itself, the pull request and the summary, then the verdict."""
        result = result_json(
            round=2,
            round_summary="Fixed both findings.",
            review=review_record("clean", assessment="approval"),
        )
        text = comments.comment_body(result, "") or ""
        self.assertTrue(
            text.startswith("\U0001f412 Round 2: an AI agent revised pull request")
        )
        self.assertIn(f"https://github.com/{REPOSITORY}/pull/8", text)
        self.assertIn("Fixed both findings.", text)
        self.assertIn("Copilot review of it is clean", text)
        # At the cap with findings, a human takes over.
        capped = result_json(
            round=10,
            round_summary="s",
            review=review_record("findings", assessment="changes"),
        )
        text = comments.comment_body(capped, "") or ""
        self.assertIn("round cap of 10", text)
        self.assertIn("a human must take over", text)

    def test_round_cap_notice_on_every_unfinished_outcome(self) -> None:
        """Whatever ends the tenth round short of clean says a human takes over."""
        unfinished = (
            result_json(round=10, round_summary="s", review=review_record("pending")),
            result_json(
                verdict="handoff", round=10, round_summary="s", reasons=["Wrong."]
            ),
            result_json(verdict="rejected", round=10, reasons=["head moved"]),
            result_json(verdict="author-failed", round=10, reasons=["timed out"]),
            result_json(verdict="publish-failed", round=10, reasons=["refused"]),
        )
        for result in unfinished:
            with self.subTest(verdict=result["verdict"]):
                text = comments.comment_body(result, "https://x/run") or ""
                self.assertIn("round cap of 10", text)
                self.assertIn("a human must take over", text)
                self.assertNotIn("once a maintainer comments", text)
        # A clean tenth round is finished; before the cap, nothing is said.
        for result in (
            result_json(
                round=10,
                round_summary="s",
                review=review_record("clean", assessment="approval"),
            ),
            result_json(verdict="handoff", round=9, round_summary="s", reasons=["W."]),
        ):
            with self.subTest(verdict=result["verdict"], round=result["round"]):
                text = comments.comment_body(result, "https://x/run") or ""
                self.assertNotIn("round cap", text)

    def test_agent_text_holds_no_html(self) -> None:
        """Raw HTML in a summary or reason is text; it wraps no notice after it."""
        summary = "<details><summary>Details</summary>Fixed <b>one</b>."
        for result in (
            result_json(
                round=10,
                round_summary=summary,
                review=review_record("findings", assessment="changes"),
            ),
            result_json(verdict="handoff", round=10, round_summary=summary),
            result_json(verdict="abstain", reasons=["<!-- hidden"]),
            result_json(verdict="author-failed", reasons=[summary]),
        ):
            with self.subTest(verdict=result["verdict"]):
                text = comments.comment_body(result, "https://x/run") or ""
                self.assertNotIn("<", text)
                self.assertIn("&lt;", text)
        text = (
            comments.comment_body(
                result_json(
                    round=10,
                    round_summary=summary,
                    review=review_record("findings", assessment="changes"),
                ),
                "",
            )
            or ""
        )
        self.assertIn("round cap of 10", text)
        self.assertIn("Fixed &lt;b&gt;one&lt;/b&gt;.", text)

    def test_unrecognised_review_record_reads_as_pending(self) -> None:
        """A record the publisher does not know is never read as clean."""
        for record in (
            {"verdict": "approved"},
            {"verdict": "clean", "extra": 1},
            {"verdict": "clean"},
            "clean",
        ):
            with self.subTest(record=record):
                text = comments.comment_body(result_json(review=record), "") or ""
                self.assertNotIn("is clean", text)
                self.assertIn("no Copilot review", text)


def review_record(verdict: str, **overrides: Any) -> dict[str, Any]:
    """The ``review`` record result.json carries (DESIGN.md 18.4)."""
    record: dict[str, Any] = {
        "verdict": verdict,
        "assessment": None,
        "unresolved_threads": 0,
        "suppressed": 0,
        "previously_missed": 0,
        "reasons": [],
        "review_id": 5 if verdict != "pending" else None,
        "commit_id": "e" * 40,
        "requested": True,
    }
    record.update(overrides)
    return record


class RunPushTest(NoNetworkCase):
    """``run_push`` gates fork writes on verdict, dry-run and mode."""

    def setUp(self) -> None:
        """Make the fork ready without a network; tests override to fail it."""
        super().setUp()
        self.ensure_fork = patch.object(
            publish, "ensure_fork", return_value=None
        ).start()
        self.sync_fork = patch.object(publish, "sync_fork", return_value=None).start()
        self.addCleanup(patch.stopall)

    def test_rejected_verdict_touches_nothing(self) -> None:
        """A rejected check returns a result without any GitHub call."""
        path = self.write_check(check_json(verdict="rejected", reasons=["nope"]))
        with (
            patch.object(publish, "create_branch") as branch,
            patch.object(publish, "replay_commits") as replay,
        ):
            result = publish.run_push(self.push_args(path))
        self.ensure_fork.assert_not_called()
        branch.assert_not_called()
        replay.assert_not_called()
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["reasons"], ["nope"])
        self.assertEqual(result["fork_repository"], FORK)
        self.assertIsNone(result["branch_url"])
        self.assertIsNone(result["pull_request_url"])
        self.assertEqual(result["commits"], [])
        self.assertEqual(result["premium_requests"], 4)
        self.assertEqual(result["agent_seconds"], 90)
        self.assertEqual(result["pr_title"], "Fix: Both")

    def test_author_session_is_recorded(self) -> None:
        """The trusted proposal artifact ID travels into the result."""
        path = self.write_check(check_json(verdict="abstain"))
        args = self.push_args(path)
        self.assertIsNone(publish.run_push(args)["author_session"])
        args.author_session = "101"
        self.assertEqual(publish.run_push(args)["author_session"], "101")

    def test_dry_run_and_select_mode_never_write(self) -> None:
        """Dry run records the flag and stops; select mode never writes."""
        path = self.write_check(check_json())
        with patch.object(publish, "create_branch") as branch:
            dry = publish.run_push(self.push_args(path, dry_run=True))
            select = publish.run_push(self.push_args(path, mode="select"))
        branch.assert_not_called()
        self.ensure_fork.assert_not_called()
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["verdict"], "proposed")
        self.assertIsNone(dry["branch_url"])
        self.assertFalse(select["dry_run"])
        self.assertIsNone(select["branch_url"])

    def test_branches_mode(self) -> None:
        """Branches mode readies the fork, creates the branch there and replays."""
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch") as branch,
            patch.object(publish, "replay_commits", return_value=["f" * 40]) as replay,
        ):
            result = publish.run_push(self.push_args(path, mode="branches"))
        self.ensure_fork.assert_called_once_with(REPOSITORY, FORK_ORG, "repo")
        self.sync_fork.assert_called_once_with(FORK, "main", BASE)
        branch.assert_called_once_with(FORK, BRANCH, BASE)
        replay.assert_called_once()
        self.assertEqual(replay.call_args.args[0], self.root / "work" / "clone")
        self.assertEqual(replay.call_args.args[1]["key"], "repo-7")
        self.assertEqual(result["commits"], ["f" * 40])
        self.assertEqual(result["branch_url"], FORK_BRANCH_URL)
        self.assertIsNone(result["pull_request_url"])
        self.assertEqual(result["warnings"], [])
        self.assertFalse(publish.needs_pull_request(result))

    def test_pull_requests_mode_leaves_the_pr_to_open(self) -> None:
        """Push never opens the pull request: that needs the target token."""
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch"),
            patch.object(publish, "replay_commits", return_value=["f" * 40]),
            patch.object(publish, "open_pull_request") as pull,
        ):
            result = publish.run_push(self.push_args(path))
        pull.assert_not_called()
        self.assertEqual(result["branch_url"], FORK_BRANCH_URL)
        self.assertIsNone(result["pull_request_url"])
        self.assertTrue(publish.needs_pull_request(result))

    def test_fork_not_ready_fails_without_rollback(self) -> None:
        """A fork that cannot be made or synced fails before any branch exists."""
        path = self.write_check(check_json())
        self.ensure_fork.side_effect = policy.PublishError("x exists but is not a fork")
        with (
            patch.object(publish, "create_branch") as branch,
            patch.object(publish, "delete_branch") as delete,
            self.assertRaisesRegex(policy.PublishError, "not a fork"),
        ):
            publish.run_push(self.push_args(path))
        branch.assert_not_called()
        delete.assert_not_called()
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn("not a fork", written["reasons"][0])

    def test_replay_failure_rolls_back_the_branch(self) -> None:
        """A failure after branch creation deletes the fork branch and fails.

        Without the rollback the half-built branch would count as a prior
        attempt and keep the issue out of every later run.
        """
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch"),
            patch.object(
                publish,
                "replay_commits",
                side_effect=policy.PublishError("createCommitOnBranch failed"),
            ),
            patch.object(publish, "delete_branch", return_value=None) as delete,
            self.assertRaisesRegex(policy.PublishError, "branch removed"),
        ):
            publish.run_push(self.push_args(path))
        delete.assert_called_once_with(FORK, BRANCH)
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn("createCommitOnBranch failed", written["reasons"][0])
        self.assertIsNone(written["pull_request_url"])
        self.assertIsNone(written["branch_url"])

    def test_rollback_failure_reports_leftover_branch(self) -> None:
        """When rollback itself fails, the leftover is named in the reason."""
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch"),
            patch.object(
                publish,
                "replay_commits",
                side_effect=github.GitHubError("boom (HTTP 502)"),
            ),
            patch.object(publish, "delete_branch", return_value="branch x stays"),
            self.assertRaisesRegex(policy.PublishError, "branch x stays"),
        ):
            publish.run_push(self.push_args(path))

    def reconcile_existing(
        self, mode: str, *, same_tree: bool, pr: str | None = None
    ) -> dict[str, Any]:
        """Run push against an existing fork branch and return the result."""
        path = self.write_check(check_json())
        with (
            patch.object(
                publish, "create_branch", side_effect=policy.Rejection("branch exists")
            ),
            patch.object(publish, "existing_pull_request", return_value=pr) as lookup,
            patch.object(
                publish,
                "branch_matches",
                return_value=["c" * 40] if same_tree else None,
            ) as match,
            patch.object(publish, "replay_commits") as replay,
            patch.object(publish, "delete_branch") as delete,
        ):
            result = publish.run_push(self.push_args(path, mode=mode))
        replay.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(match.call_args.args[:2], (FORK, BRANCH))
        if same_tree:
            lookup.assert_called_once_with(REPOSITORY, BRANCH, BOT, FORK)
        return result

    def test_adopted_branch_records_its_chain(self) -> None:
        """The verified chain's commits are the published head the review binds to."""
        result = self.reconcile_existing("pull-requests", same_tree=True)
        self.assertEqual(result["commits"], ["c" * 40])

    def test_rerun_after_success_reports_the_existing_pull_request(self) -> None:
        """A retry meeting its own open PR is published, not rejected, and silent."""
        result = self.reconcile_existing(
            "pull-requests", same_tree=True, pr="https://x/pull/5"
        )
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["pull_request_url"], "https://x/pull/5")
        self.assertIn(
            "an earlier attempt already published this work", result["warnings"]
        )
        self.assertFalse(publish.needs_pull_request(result))

    def test_branches_mode_retry_is_not_a_rejection(self) -> None:
        """A branch carrying the proposal's chain is the earlier attempt's work."""
        result = self.reconcile_existing("branches", same_tree=True)
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["branch_url"], FORK_BRANCH_URL)
        # The comment step still runs; its marker makes the retry safe.
        self.assertIsNotNone(comments.comment_body(result, "https://run"))

    def test_pushed_branch_without_pr_is_resumed(self) -> None:
        """Pull-requests mode adopts the branch and leaves the PR to ``open``."""
        result = self.reconcile_existing("pull-requests", same_tree=True)
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["branch_url"], FORK_BRANCH_URL)
        self.assertIsNone(result["pull_request_url"])
        self.assertIn("resumed an earlier attempt's branch", result["warnings"])
        self.assertTrue(publish.needs_pull_request(result))

    def test_foreign_branch_stays_a_rejection(self) -> None:
        """A branch with other content is a conflict and is left alone."""
        result = self.reconcile_existing("pull-requests", same_tree=False)
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["reasons"], ["branch exists"])
        self.assertIsNone(result["branch_url"])

    def test_reconciliation_failure_records_a_result(self) -> None:
        """An error while reconciling still writes result.json and keeps the branch."""
        path = self.write_check(check_json())
        with (
            patch.object(
                publish, "create_branch", side_effect=policy.Rejection("branch exists")
            ),
            patch.object(publish, "branch_matches", return_value=["c" * 40]),
            patch.object(
                publish,
                "existing_pull_request",
                side_effect=github.GitHubError("gh: Server Error (HTTP 502)"),
            ),
            patch.object(publish, "delete_branch") as delete,
            self.assertRaisesRegex(policy.PublishError, "existing branch kept"),
        ):
            publish.run_push(self.push_args(path))
        delete.assert_not_called()
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn("existing branch kept", written["reasons"][-1])

    def test_ambiguous_create_keeps_the_branch(self) -> None:
        """A create that failed unclearly never leads to a delete."""
        path = self.write_check(check_json())
        with (
            patch.object(
                publish,
                "create_branch",
                side_effect=policy.PublishError("gh: Bad Gateway (HTTP 502)"),
            ),
            patch.object(publish, "replay_commits") as replay,
            patch.object(publish, "delete_branch") as delete,
            self.assertRaisesRegex(policy.PublishError, "did not confirm"),
        ):
            publish.run_push(self.push_args(path))
        replay.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(self.written_result()["verdict"], "publish-failed")

    def test_unknown_verdict_is_error(self) -> None:
        """A check.json with an unknown verdict is an operational failure."""
        path = self.write_check(check_json(verdict="maybe"))
        with self.assertRaises(policy.PublishError):
            publish.run_push(self.push_args(path))


class RunPushRevisionTest(NoNetworkCase):
    """A revision replays onto a temporary branch, then moves the bot branch."""

    def test_revision_moves_the_branch_and_records_the_pull_request(self) -> None:
        """Sync, temporary branch, replay, compare-and-swap, cleanup; the PR is known."""
        path = self.write_check(revision_check())
        with (
            patch.object(refs, "ensure_fork") as fork,
            patch.object(refs, "sync_fork") as sync,
            patch.object(refs, "branch_matches", return_value=None),
            patch.object(
                publish.revision_publish, "move_branch", return_value=[NEW_HEAD]
            ) as move,
        ):
            result = publish.run_push(self.push_args(path))
        fork.assert_called_once()
        sync.assert_called_once()
        move.assert_called_once()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["commits"], [NEW_HEAD])
        self.assertEqual(
            result["pull_request_url"], f"https://github.com/{REPOSITORY}/pull/41"
        )
        self.assertEqual(result["branch_url"], FORK_BRANCH_URL)
        self.assertEqual(result["round"], 2)
        self.assertFalse(publish.needs_pull_request(result))

    def test_retry_adopts_the_round_already_on_the_branch(self) -> None:
        """A rerun after the swap succeeded finds its own chain and moves nothing.

        The swap's beforeOid names the pre-round head, so replaying
        again would refuse the bot's own earlier publication; the
        branch that already holds the verified chain is adopted, as
        the new-issue path adopts its branch, and the round goes on
        to its replies and the review gate.
        """
        path = self.write_check(revision_check())
        chain = ["c" * 40, NEW_HEAD]
        with (
            patch.object(refs, "ensure_fork"),
            patch.object(refs, "sync_fork"),
            patch.object(refs, "branch_matches", return_value=chain) as matches,
            patch.object(publish.revision_publish, "move_branch") as move,
        ):
            result = publish.run_push(self.push_args(path))
        move.assert_not_called()
        self.assertEqual(
            matches.call_args.args,
            (FORK, "code-monkey/issue-7", BASE, revision_check()["commits"], BOT),
        )
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["commits"], chain)
        self.assertEqual(
            result["pull_request_url"], f"https://github.com/{REPOSITORY}/pull/41"
        )
        self.assertTrue(any("earlier attempt" in w for w in result["warnings"]))
        # A branch holding anything else goes to the swap, which decides.
        with (
            patch.object(refs, "ensure_fork"),
            patch.object(refs, "sync_fork"),
            patch.object(refs, "branch_matches", return_value=None),
            patch.object(
                publish.revision_publish, "move_branch", return_value=[NEW_HEAD]
            ) as move,
        ):
            result = publish.run_push(self.push_args(path))
        move.assert_called_once()
        self.assertEqual(result["commits"], [NEW_HEAD])

    def test_moved_head_is_a_rejection_not_a_failure(self) -> None:
        """A head that changed since selection is refused, nothing forced."""
        path = self.write_check(revision_check())
        with (
            patch.object(refs, "ensure_fork"),
            patch.object(refs, "sync_fork"),
            patch.object(refs, "branch_matches", return_value=None),
            patch.object(
                publish.revision_publish,
                "move_branch",
                side_effect=policy.Rejection("head moved from e to f"),
            ),
        ):
            result = publish.run_push(self.push_args(path))
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("head moved from e to f", result["reasons"])
        self.assertIsNone(result["pull_request_url"])


class HandoffTest(NoNetworkCase):
    """A handoff writes replies and nothing else; the gate and the move are skipped."""

    def test_push_does_nothing_and_names_the_pull_request(self) -> None:
        """No sync, no move: the branch is untouched; respond gets the pull request."""
        path = self.write_check(revision_check(verdict="handoff", commits=[]))
        with (
            patch.object(refs, "ensure_fork") as fork,
            patch.object(publish.revision_publish, "move_branch") as move,
            patch.object(review, "pull_head") as head,
        ):
            result = publish.run_push(self.push_args(path))
        fork.assert_not_called()
        move.assert_not_called()
        # No token in the push step for a handoff: no read either.
        head.assert_not_called()
        self.assertEqual(result["verdict"], "handoff")
        self.assertEqual(
            result["pull_request_url"], f"https://github.com/{REPOSITORY}/pull/41"
        )
        self.assertEqual(result["commits"], [])
        self.assertFalse(publish.needs_pull_request(result))

    def respond_handoff(self, head: Any) -> dict[str, Any]:
        """Run ``respond`` on a handoff; the pull request head then reads as ``head``."""
        check = self.write_check(
            revision_check(
                verdict="handoff",
                commits=[],
                responses=[
                    {
                        "thread_id": "PRRT_1",
                        "comment_id": 100,
                        "reply": "Disputed.",
                        "resolve": False,
                    }
                ],
            )
        )
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    verdict="handoff",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        lookup = (
            patch.object(review, "pull_head", side_effect=head)
            if isinstance(head, Exception)
            else patch.object(review, "pull_head", return_value=head)
        )
        with (
            patch.object(
                publish.revision_publish.threads_of, "read_thread", return_value=[]
            ),
            patch.object(publish.revision_publish, "reply", return_value=None) as reply,
            patch.object(publish.revision_publish, "resolve"),
            lookup as read,
            redirect_stdout(io.StringIO()),
        ):
            try:
                publish.main(
                    [
                        "respond",
                        "--result",
                        str(result),
                        "--check",
                        str(check),
                        "--output",
                        str(result),
                    ]
                )
            except SystemExit as exc:
                # main() turns a PublishError into the step's failure.
                error: BaseException | None = exc
            else:
                error = None
        read.assert_called_once_with(REPOSITORY, 41)
        stored = json.loads(result.read_text(encoding="utf-8"))
        stored["_replied"] = reply.call_count
        stored["_error"] = error
        return stored

    def test_a_handoff_for_a_moved_head_is_refused(self) -> None:
        """A push during the session makes the handoff stale: rejected, not posted.

        The push step holds no token for a handoff (no fork mint), so
        the guard runs in ``respond``, with the pull request token,
        before any reply goes up.
        """
        stored = self.respond_handoff("f" * 40)
        self.assertEqual(stored["verdict"], "rejected")
        self.assertTrue(any("head moved" in r for r in stored["reasons"]))
        self.assertEqual(stored["_replied"], 0)
        self.assertIsNone(stored["_error"])
        # The head still the recorded one: the handoff stands and answers.
        stored = self.respond_handoff(HEAD)
        self.assertEqual(stored["verdict"], "handoff")
        self.assertEqual(stored["_replied"], 1)
        # The head unreadable: a failure, never a handoff on trust.
        stored = self.respond_handoff(github.GitHubError("unreachable"))
        self.assertEqual(stored["verdict"], "publish-failed")
        self.assertEqual(stored["_replied"], 0)
        self.assertIsInstance(stored["_error"], SystemExit)

    def test_respond_posts_replies_for_a_handoff(self) -> None:
        """Replies reach the threads, nothing is resolved, the body is left alone."""
        check = self.write_check(
            revision_check(
                verdict="handoff",
                commits=[],
                responses=[
                    {
                        "thread_id": "PRRT_1",
                        "comment_id": 100,
                        "reply": "Disputed.",
                        "resolve": False,
                    }
                ],
            )
        )
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    verdict="handoff",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        with (
            patch.object(
                publish.revision_publish.threads_of, "read_thread", return_value=[]
            ),
            patch.object(publish.revision_publish, "reply", return_value=None) as reply,
            patch.object(publish.revision_publish, "resolve") as resolve,
            patch.object(publish.revision_publish, "update_pull_request") as update,
            patch.object(review, "pull_head", return_value=HEAD),
            redirect_stdout(io.StringIO()),
        ):
            publish.main(
                [
                    "respond",
                    "--result",
                    str(result),
                    "--check",
                    str(check),
                    "--output",
                    str(result),
                ]
            )
        reply.assert_called_once()
        resolve.assert_not_called()
        update.assert_not_called()

    def test_handoff_comment(self) -> None:
        """The issue hears that the agent handed the pull request back."""
        text = (
            comments.comment_body(
                result_json(
                    verdict="handoff", round=2, reasons=["Both findings are wrong."]
                ),
                "",
            )
            or ""
        )
        self.assertIn("Round 2", text)
        self.assertIn("handed", text)
        self.assertIn("Both findings are wrong.", text)
        self.assertIn("a maintainer", text)
        self.assertIn(f"https://github.com/{REPOSITORY}/pull/8", text)

    def test_handoff_comment_carries_the_round_summary(self) -> None:
        """The agent's summary of what it disputed reaches the issue, defused."""
        text = (
            comments.comment_body(
                result_json(
                    verdict="handoff",
                    round=2,
                    reasons=["Both findings are wrong."],
                    round_summary="Thread one misreads the lock; @ops decides.",
                ),
                "",
            )
            or ""
        )
        self.assertIn("Thread one misreads the lock", text)
        self.assertNotIn("@ops", text)

    def test_check_warnings_survive_every_early_return(self) -> None:
        """An unanswered-thread warning reaches the result when nothing is pushed."""
        warning = "1 thread left unanswered; they stay open for a human"
        cases: tuple[tuple[dict[str, Any], str, bool], ...] = (
            (
                revision_check(verdict="handoff", commits=[], warnings=[warning]),
                "pull-requests",
                False,
            ),
            (revision_check(warnings=[warning]), "pull-requests", True),
            (revision_check(warnings=[warning]), "branches", False),
        )
        for check, mode, dry_run in cases:
            with (
                self.subTest(verdict=check["verdict"], mode=mode, dry_run=dry_run),
                patch.object(refs, "ensure_fork"),
                patch.object(publish.revision_publish, "move_branch"),
            ):
                path = self.write_check(check)
                result = publish.run_push(
                    self.push_args(path, mode=mode, dry_run=dry_run)
                )
            self.assertIn(warning, result["warnings"])

    def test_branches_mode_publishes_no_revision(self) -> None:
        """A branch-only run never moves a live pull request's head."""
        path = self.write_check(revision_check())
        with (
            patch.object(refs, "ensure_fork") as fork,
            patch.object(publish.revision_publish, "move_branch") as move,
        ):
            result = publish.run_push(self.push_args(path, mode="branches"))
        fork.assert_not_called()
        move.assert_not_called()
        self.assertEqual(result["verdict"], "proposed")
        self.assertTrue(any("pull-requests mode" in w for w in result["warnings"]))

    def test_cleanup_warning_survives_an_operational_failure(self) -> None:
        """A scratch branch left after a failed replay reaches the written result."""
        path = self.write_check(revision_check())

        def move(_clone: Path, check: dict[str, Any]) -> list[str]:
            check.setdefault("warnings", []).append("branch x stays")
            raise policy.PublishError("replay broke")

        with (
            patch.object(refs, "ensure_fork"),
            patch.object(refs, "sync_fork"),
            patch.object(refs, "branch_matches", return_value=None),
            patch.object(publish.revision_publish, "move_branch", side_effect=move),
            self.assertRaises(policy.PublishError),
        ):
            publish.run_push(self.push_args(path))
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn("branch x stays", written["warnings"])
        self.assertTrue(any("unchanged" in r for r in written["reasons"]))

    def test_an_unknown_swap_outcome_is_not_called_unchanged(self) -> None:
        """When the branch may have moved, the failure says so rather than deny it."""
        path = self.write_check(revision_check())
        with (
            patch.object(refs, "ensure_fork"),
            patch.object(refs, "sync_fork"),
            patch.object(refs, "branch_matches", return_value=None),
            patch.object(
                publish.revision_publish,
                "move_branch",
                side_effect=publish.revision_publish.SwapUnknown(
                    "updateRefs outcome unknown: the branch reads as b"
                ),
            ),
            self.assertRaises(policy.PublishError),
        ):
            publish.run_push(self.push_args(path))
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertTrue(any("unknown" in r for r in written["reasons"]))
        self.assertFalse(any("unchanged" in r for r in written["reasons"]))

    def test_cleanup_warning_survives_a_failed_swap(self) -> None:
        """A scratch branch left after a failed move is reported, not lost."""
        path = self.write_check(revision_check())

        def move(_clone: Path, check: dict[str, Any]) -> list[str]:
            check.setdefault("warnings", []).append("branch x stays")
            raise policy.Rejection("head moved")

        with (
            patch.object(refs, "ensure_fork"),
            patch.object(refs, "sync_fork"),
            patch.object(refs, "branch_matches", return_value=None),
            patch.object(publish.revision_publish, "move_branch", side_effect=move),
        ):
            result = publish.run_push(self.push_args(path))
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("branch x stays", result["warnings"])


class RunRespondTest(NoNetworkCase):
    """``publish.py respond`` answers the threads and updates the description."""

    def test_replies_resolves_and_updates(self) -> None:
        """Each response posts and resolves as marked; the body and title follow."""
        check = self.write_check(revision_check())
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[NEW_HEAD],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        with (
            patch.object(review, "pull_head", return_value=NEW_HEAD),
            patch.object(
                publish.revision_publish.threads_of, "read_thread", return_value=[]
            ),
            patch.object(publish.revision_publish, "reply", return_value=None) as reply,
            patch.object(
                publish.revision_publish, "resolve", return_value=None
            ) as resolve,
            patch.object(
                publish.revision_publish, "update_pull_request", return_value=None
            ) as update,
            redirect_stdout(io.StringIO()),
        ):
            publish.main(
                [
                    "respond",
                    "--result",
                    str(result),
                    "--check",
                    str(check),
                    "--output",
                    str(result),
                ]
            )
        self.assertEqual(reply.call_count, 2)
        self.assertEqual(
            [c.args[1:3] for c in reply.call_args_list], [(41, 100), (41, 101)]
        )
        resolve.assert_called_once_with(REPOSITORY, "PRRT_1")
        update.assert_called_once()
        stored = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["responded"],
            {"replied": 2, "already": 0, "resolved": 1, "failed": 0},
        )

    def test_rerun_posts_no_reply_twice(self) -> None:
        """A retry finds the round's reply already on a thread and posts it no more.

        The reply's last line is a round marker; a bot-authored comment
        ending with it means the earlier attempt posted. The resolve
        still runs, since a resolve lost after the reply is its own
        retry, and resolving a resolved thread changes nothing.
        """
        check = self.write_check(revision_check())
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[NEW_HEAD],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        marker = "<!-- code-monkey reply round=2 since=2026-10-03T00:00:00Z -->"
        threads = {
            "PRRT_1": [
                {"author": {"login": "copilot-pull-request-reviewer"}, "body": "Fix."},
                {"author": {"login": "code-monkey"}, "body": f"Fixed.\n\n{marker}"},
            ],
            # An older round's reply, another session's at the same
            # round, and a human quoting the marker mid-body, are not
            # this session's reply.
            "PRRT_2": [
                {"author": {"login": "copilot-pull-request-reviewer"}, "body": "Fix."},
                {
                    "author": {"login": "code-monkey"},
                    "body": "Earlier.\n\n<!-- code-monkey reply round=1 since=t -->",
                },
                {
                    "author": {"login": "code-monkey"},
                    "body": "Disputed.\n\n<!-- code-monkey reply round=2 "
                    "since=2026-10-02T00:00:00Z -->",
                },
                {"author": {"login": "maintainer"}, "body": f"{marker} is the marker"},
            ],
        }

        def read(thread_id: str) -> list[dict[str, Any]]:
            return threads[thread_id]

        with (
            patch.object(review, "pull_head", return_value=NEW_HEAD),
            patch.object(publish.revision_publish.threads_of, "read_thread", read),
            patch.object(publish.revision_publish, "reply", return_value=None) as reply,
            patch.object(
                publish.revision_publish, "resolve", return_value=None
            ) as resolve,
            patch.object(
                publish.revision_publish, "update_pull_request", return_value=None
            ),
            redirect_stdout(io.StringIO()),
        ):
            publish.main(
                [
                    "respond",
                    "--result",
                    str(result),
                    "--check",
                    str(check),
                    "--output",
                    str(result),
                ]
            )
        self.assertEqual([c.args[2] for c in reply.call_args_list], [101])
        resolve.assert_called_once_with(REPOSITORY, "PRRT_1")
        stored = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["responded"],
            {"replied": 1, "already": 1, "resolved": 1, "failed": 0},
        )

    def test_a_thread_too_long_to_read_is_a_warning(self) -> None:
        """A bounded read that fails leaves its thread alone; the rest go on."""
        check = self.write_check(revision_check())
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[NEW_HEAD],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        selection_error = publish.revision_publish.threads_of.reads.SelectionError

        def read(thread_id: str) -> list[dict[str, Any]]:
            if thread_id == "PRRT_1":
                raise selection_error("thread PRRT_1 holds more comments than read")
            return []

        with (
            patch.object(review, "pull_head", return_value=NEW_HEAD),
            patch.object(publish.revision_publish.threads_of, "read_thread", read),
            patch.object(publish.revision_publish, "reply", return_value=None) as reply,
            patch.object(publish.revision_publish, "resolve") as resolve,
            patch.object(
                publish.revision_publish, "update_pull_request", return_value=None
            ) as update,
            redirect_stdout(io.StringIO()),
        ):
            publish.main(
                [
                    "respond",
                    "--result",
                    str(result),
                    "--check",
                    str(check),
                    "--output",
                    str(result),
                ]
            )
        self.assertEqual([c.args[2] for c in reply.call_args_list], [101])
        resolve.assert_not_called()
        update.assert_called_once()
        stored = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(stored["responded"]["failed"], 1)
        self.assertEqual(stored["responded"]["replied"], 1)
        self.assertTrue(any("PRRT_1" in w for w in stored["warnings"]))

    def test_a_moved_head_stops_the_responses(self) -> None:
        """Another run moved the head after this swap: no reply, no description edit.

        The compare-and-swap guards the branch move alone; the writes
        after it would resolve threads and replace a newer description
        with this round's. The result keeps its publication.
        """
        check = self.write_check(revision_check())
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[NEW_HEAD],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        for head in ("f" * 40, github.GitHubError("unreachable")):
            lookup = (
                patch.object(review, "pull_head", side_effect=head)
                if isinstance(head, Exception)
                else patch.object(review, "pull_head", return_value=head)
            )
            with (
                self.subTest(head=str(head)[:7]),
                patch.object(
                    publish.revision_publish.threads_of, "read_thread", return_value=[]
                ),
                lookup,
                patch.object(publish.revision_publish, "reply") as reply,
                patch.object(publish.revision_publish, "resolve") as resolve,
                patch.object(publish.revision_publish, "update_pull_request") as update,
                redirect_stdout(io.StringIO()),
            ):
                publish.main(
                    [
                        "respond",
                        "--result",
                        str(result),
                        "--check",
                        str(check),
                        "--output",
                        str(result),
                    ]
                )
            reply.assert_not_called()
            resolve.assert_not_called()
            update.assert_not_called()
            stored = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(stored["verdict"], "proposed")
            self.assertEqual(stored["commits"], [NEW_HEAD])
            self.assertTrue(
                any(
                    "head" in w and "moved" in w or "could not read" in w
                    for w in stored["warnings"]
                )
            )

    def test_a_failed_reply_is_a_warning_and_the_rest_proceed(self) -> None:
        """One refused write does not stop the others or fail the step."""
        check = self.write_check(revision_check())
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                result_json(
                    key="repo-7",
                    pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
                    commits=[NEW_HEAD],
                    round=2,
                )
            ),
            encoding="utf-8",
        )
        with (
            patch.object(review, "pull_head", return_value=NEW_HEAD),
            patch.object(
                publish.revision_publish.threads_of, "read_thread", return_value=[]
            ),
            patch.object(
                publish.revision_publish,
                "reply",
                side_effect=[github.GitHubError("boom (HTTP 500)"), None],
            ),
            patch.object(publish.revision_publish, "resolve", return_value=None),
            patch.object(
                publish.revision_publish, "update_pull_request", return_value=None
            ),
            redirect_stdout(io.StringIO()),
        ):
            publish.main(
                [
                    "respond",
                    "--result",
                    str(result),
                    "--check",
                    str(check),
                    "--output",
                    str(result),
                ]
            )
        stored = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(stored["responded"]["failed"], 1)
        self.assertTrue(any("PRRT_1" in w for w in stored["warnings"]))


class RunRetitleTest(NoNetworkCase):
    """``publish.py retitle`` sets a revision's title before the branch moves."""

    def retitle(
        self,
        check_data: dict[str, Any],
        current: str,
        head: str = HEAD,
        chain: list[str] | None = None,
    ) -> Any:
        """Run retitle against a pull request titled ``current`` at ``head``."""
        check = self.write_check(check_data)
        with (
            patch.object(github, "api_object", return_value={"title": current}),
            patch.object(review, "pull_head", return_value=head),
            patch.object(refs, "branch_matches", return_value=chain),
            patch.object(github, "api_write", return_value={}) as write,
            redirect_stdout(io.StringIO()),
        ):
            publish.main(["retitle", "--check", str(check)])
        return write

    def test_a_stale_dispatch_retitles_nothing(self) -> None:
        """A head that moved since selection is another session's; the title stays.

        The swap will refuse the stale branch update; the title must
        not be left behind. A retry after this run's own push finds
        the head at its verified chain and may still retitle.
        """
        write = self.retitle(
            revision_check(pr_title="Fix: New subject"), "Fix: Old", head="f" * 40
        )
        write.assert_not_called()
        write = self.retitle(
            revision_check(pr_title="Fix: New subject"),
            "Fix: Old",
            head=NEW_HEAD,
            chain=["c" * 40, NEW_HEAD],
        )
        write.assert_called_once()

    def test_a_changed_subject_retitles_before_the_push(self) -> None:
        """The Semantic Pull Request check runs on the push; the title must lead it."""
        write = self.retitle(revision_check(pr_title="Fix: New subject"), "Fix: Old")
        write.assert_called_once_with(
            "PATCH", f"repos/{REPOSITORY}/pulls/41", {"title": "Fix: New subject"}
        )
        # Already right: nothing written.
        write = self.retitle(revision_check(pr_title="Fix: Both"), "Fix: Both")
        write.assert_not_called()
        # Not a revision, or not a proposal: nothing to do.
        write = self.retitle(check_json(), "x")
        write.assert_not_called()
        write = self.retitle(revision_check(verdict="handoff", commits=[]), "x")
        write.assert_not_called()


class RunOpenTest(NoNetworkCase):
    """``run_open`` raises the pull request for a branch ``push`` left in the fork."""

    def test_nothing_to_open_is_a_no_op(self) -> None:
        """Dry runs, non-proposed, already-opened and branches-mode results pass."""
        check = self.write_check(check_json())
        for result in (
            result_json(pull_request_url=None, dry_run=True),
            result_json(pull_request_url=None, verdict="rejected"),
            result_json(),
            result_json(pull_request_url=None, mode="branches"),
            result_json(pull_request_url=None, branch_url=None),
        ):
            with patch.object(publish, "open_pull_request") as pull:
                self.assertEqual(
                    publish.run_open(self.open_args(result, check)), result
                )
            pull.assert_not_called()

    def test_mismatched_key_is_error(self) -> None:
        """A result for another proposal is never paired with this check."""
        check = self.write_check(check_json(key="other-1"))
        with self.assertRaisesRegex(policy.PublishError, "different proposals"):
            publish.run_open(self.open_args(result_json(), check))

    def test_opens_and_records_url_and_warning(self) -> None:
        """A successful open records the URL and any labelling warning."""
        check = self.write_check(check_json())
        with patch.object(
            publish,
            "open_pull_request",
            return_value=("https://x/pull/1", "could not label the pull request: x"),
        ) as pull:
            result = publish.run_open(
                self.open_args(result_json(pull_request_url=None), check)
            )
        self.assertEqual(pull.call_args.args[0]["fork_org"], FORK_ORG)
        self.assertEqual(result["pull_request_url"], "https://x/pull/1")
        self.assertEqual(result["warnings"], ["could not label the pull request: x"])
        self.assertEqual(result["branch_url"], FORK_BRANCH_URL)

    def test_lost_reply_is_recovered_by_lookup(self) -> None:
        """A PR POST whose reply is lost is looked up, not failed."""
        check = self.write_check(check_json())
        with (
            patch.object(
                publish,
                "open_pull_request",
                side_effect=github.GitHubError("gh timed out after 60 seconds"),
            ),
            patch.object(
                publish, "existing_pull_request", return_value="https://x/pull/9"
            ) as lookup,
        ):
            result = publish.run_open(
                self.open_args(result_json(pull_request_url=None), check)
            )
        lookup.assert_called_once_with(REPOSITORY, BRANCH, BOT, FORK)
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["pull_request_url"], "https://x/pull/9")
        self.assertTrue(any("pull request exists" in w for w in result["warnings"]))

    def test_failure_keeps_the_fork_branch(self) -> None:
        """A failed open records publish-failed and leaves the fork branch."""
        check = self.write_check(check_json())
        with (
            patch.object(
                publish,
                "open_pull_request",
                side_effect=github.GitHubError("boom (HTTP 502)"),
            ),
            patch.object(publish, "existing_pull_request", return_value=None),
            patch.object(publish, "delete_branch") as delete,
            self.assertRaisesRegex(policy.PublishError, "fork branch kept"),
        ):
            publish.run_open(self.open_args(result_json(pull_request_url=None), check))
        delete.assert_not_called()
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn("fork branch kept for the next run", written["reasons"][-1])
        self.assertEqual(written["branch_url"], FORK_BRANCH_URL)
        self.assertIsNone(written["pull_request_url"])

    def test_unknown_pr_state_keeps_the_branch(self) -> None:
        """PR call and lookup both failing: keep the branch, record the failure."""
        check = self.write_check(check_json())
        with (
            patch.object(
                publish,
                "open_pull_request",
                side_effect=github.GitHubError("gh timed out after 60 seconds"),
            ),
            patch.object(
                publish,
                "existing_pull_request",
                side_effect=github.GitHubError("gh: Server Error (HTTP 502)"),
            ),
            self.assertRaisesRegex(policy.PublishError, "fork branch kept"),
        ):
            publish.run_open(self.open_args(result_json(pull_request_url=None), check))
        written = self.written_result()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn(
            "could not tell whether the pull request exists", written["reasons"][-1]
        )


class CreateBranchTest(NoNetworkCase):
    """``create_branch`` distinguishes an existing branch from other failures."""

    def test_success(self) -> None:
        """The ref is created at the base SHA."""
        with patch.object(github, "api_write", return_value={}) as write:
            publish.create_branch(REPOSITORY, BRANCH, BASE)
        write.assert_called_once_with(
            "POST",
            f"repos/{REPOSITORY}/git/refs",
            {"ref": f"refs/heads/{BRANCH}", "sha": BASE},
        )

    def test_already_exists_is_rejection(self) -> None:
        """A 422 naming an existing reference is a Rejection."""
        error = github.GitHubError("gh: Reference already exists (HTTP 422)")
        with (
            patch.object(github, "api_write", side_effect=error),
            self.assertRaisesRegex(policy.Rejection, "already exists"),
        ):
            publish.create_branch(REPOSITORY, BRANCH, BASE)

    def test_other_errors_are_operational(self) -> None:
        """Any other failure, including an unrelated 422, is a PublishError."""
        for message in ("gh: Forbidden (HTTP 403)", "gh: Validation Failed (HTTP 422)"):
            with (
                self.subTest(message=message),
                patch.object(
                    github, "api_write", side_effect=github.GitHubError(message)
                ),
                self.assertRaises(policy.PublishError),
            ):
                publish.create_branch(REPOSITORY, BRANCH, BASE)


class DeleteBranchTest(NoNetworkCase):
    """``delete_branch`` is best effort and reports rather than raises."""

    def test_deletes_via_git_refs(self) -> None:
        """The ref endpoint receives a DELETE for the branch."""
        with patch.object(github, "run_gh", return_value="") as run:
            self.assertIsNone(publish.delete_branch("o/r", "code-monkey/issue-1"))
        self.assertEqual(
            run.call_args.args[0],
            [
                "api",
                "--method",
                "DELETE",
                "repos/o/r/git/refs/heads/code-monkey/issue-1",
            ],
        )

    def test_absent_branch_is_a_clean_rollback(self) -> None:
        """Nothing to delete after an ambiguous create is not a leftover."""
        for message in (
            "gh: Not Found (HTTP 404)",
            "gh: Reference does not exist (HTTP 422)",
        ):
            with (
                self.subTest(message=message),
                patch.object(github, "run_gh", side_effect=github.GitHubError(message)),
            ):
                self.assertIsNone(publish.delete_branch("o/r", "b"))

    def test_failure_becomes_a_note(self) -> None:
        """A failed delete returns text for the result instead of raising."""
        with patch.object(
            github, "run_gh", side_effect=github.GitHubError("nope (HTTP 403)")
        ):
            note = publish.delete_branch("o/r", "b")
        self.assertIsNotNone(note)
        self.assertIn("could not be removed", note or "")


def compare_reply(**overrides: Any) -> dict[str, Any]:
    """A compare API reply matching check_json's two commits."""
    data: dict[str, Any] = {
        "merge_base_commit": {"sha": BASE},
        "behind_by": 0,
        "ahead_by": 2,
        "commits": [
            {
                "sha": f"{i}" * 40,
                "parents": [{"sha": BASE if i == 1 else f"{i - 1}" * 40}],
                "author": {"login": BOT},
                "commit": {
                    "message": f"{c['headline']}\n\n{c['body']}",
                    "verification": {"verified": True},
                    "tree": {"sha": c["tree"]},
                },
            }
            for i, c in enumerate(check_json()["commits"], start=1)
        ],
    }
    data.update(overrides)
    return data


class BranchMatchesTest(NoNetworkCase):
    """``branch_matches`` adopts only the chain the replay would make."""

    def matches(self, reply: dict[str, Any]) -> bool:
        """Run the check against a canned compare reply."""
        with patch.object(github, "api_object", return_value=reply) as read:
            found = publish.branch_matches(
                REPOSITORY, BRANCH, BASE, check_json()["commits"], BOT
            )
        # The branch name carries a slash, which the compare path needs
        # encoded; an unencoded one fails reconciliation on retry.
        self.assertEqual(
            read.call_args.args[0],
            f"repos/{REPOSITORY}/compare/{BASE}...code-monkey%2Fissue-7",
        )
        return found

    def test_exact_chain_matches(self) -> None:
        """Bot-authored, Verified commits with the checked messages match."""
        self.assertTrue(self.matches(compare_reply()))

    def test_any_deviation_is_a_conflict(self) -> None:
        """A foreign author, an unsigned commit, another message or base fails."""
        good = compare_reply()["commits"]

        def edit(index: int, **changes: Any) -> list[dict[str, Any]]:
            commits = json.loads(json.dumps(good))
            for key, value in changes.items():
                if key == "login":
                    commits[index]["author"]["login"] = value
                elif key == "verified":
                    commits[index]["commit"]["verification"]["verified"] = value
                elif key == "tree":
                    commits[index]["commit"]["tree"]["sha"] = value
                elif key == "parents":
                    commits[index]["parents"] = value
                else:
                    commits[index]["commit"]["message"] = value
            return commits

        for label, reply in (
            ("author", compare_reply(commits=edit(0, login="mallory"))),
            ("unsigned", compare_reply(commits=edit(1, verified=False))),
            ("message", compare_reply(commits=edit(0, message="Fix: Other"))),
            ("base", compare_reply(merge_base_commit={"sha": "9" * 40})),
            ("extra commit", compare_reply(ahead_by=3)),
            ("behind", compare_reply(behind_by=1)),
            ("other content", compare_reply(commits=edit(1, tree="9" * 40))),
            ("other parent", compare_reply(commits=edit(1, parents=[{"sha": BASE}]))),
            (
                "merge",
                compare_reply(
                    commits=edit(0, parents=[{"sha": BASE}, {"sha": "5" * 40}])
                ),
            ),
        ):
            with self.subTest(case=label):
                self.assertFalse(self.matches(reply))


class ExistingPullRequestTest(NoNetworkCase):
    """``existing_pull_request`` finds the bot's own open PR from the fork."""

    def pr(self, login: str, head: str) -> dict[str, Any]:
        """A pulls API entry."""
        return {
            "html_url": f"https://x/pull/{login}",
            "user": {"login": login},
            "head": {"repo": {"full_name": head}},
        }

    def test_bot_pr_from_fork_found(self) -> None:
        """Only the bot's PR whose head repository is the fork counts."""
        entries = [
            self.pr("mallory", FORK),
            self.pr(BOT, REPOSITORY),
            self.pr(BOT, "someone-else/repo"),
            self.pr(BOT, FORK.upper()),
        ]
        with patch.object(reads, "api_page", return_value=entries) as read:
            url = publish.existing_pull_request(REPOSITORY, BRANCH, BOT, FORK)
        self.assertEqual(url, f"https://x/pull/{BOT}")
        self.assertIn(f"head={FORK_ORG}:{BRANCH}", read.call_args.args[0])
        self.assertIn("state=open", read.call_args.args[0])

    def test_someone_elses_pr_is_not_ours(self) -> None:
        """A human's PR from the same fork branch is not an earlier attempt."""
        with patch.object(reads, "api_page", return_value=[self.pr("mallory", FORK)]):
            self.assertIsNone(
                publish.existing_pull_request(REPOSITORY, BRANCH, BOT, FORK)
            )


class ReplayCommitsTest(NoNetworkCase):
    """``replay_commits`` chains createCommitOnBranch calls."""

    def fake_git(self, _workdir: Path, *args: str, binary: bool = False) -> bytes | str:
        """Serve blob contents for ``cat-file blob`` and nothing else."""
        self.assertEqual(args[:2], ("cat-file", "blob"))
        self.assertTrue(binary)
        return {BLOB_ONE: b"one", BLOB_TWO: b"two"}[args[2]]

    def test_payload_chaining(self) -> None:
        """Each commit carries the previous oid as expectedHeadOid."""
        oids = ["b" * 40, "e" * 40]
        replies = [
            {"createCommitOnBranch": {"commit": {"oid": oid, "url": "u"}}}
            for oid in oids
        ]
        with (
            patch.object(checks, "git", side_effect=self.fake_git),
            patch.object(github, "graphql", side_effect=replies) as gql,
        ):
            created = publish.replay_commits(self.root / "clone", check_json())
        self.assertEqual(created, oids)
        self.assertEqual(gql.call_count, 2)
        first = gql.call_args_list[0].args[1]["input"]
        second = gql.call_args_list[1].args[1]["input"]
        self.assertEqual(gql.call_args_list[0].args[0], refs.COMMIT_MUTATION)
        self.assertEqual(
            first["branch"],
            {"repositoryNameWithOwner": FORK, "branchName": BRANCH},
        )
        self.assertEqual(first["expectedHeadOid"], BASE)
        self.assertEqual(second["expectedHeadOid"], oids[0])
        self.assertEqual(
            first["message"],
            {"headline": "Fix: One", "body": "Body one\n\nSigned-off-by: b <b@x>"},
        )
        self.assertEqual(
            first["fileChanges"],
            {
                "additions": [
                    {"path": "a.txt", "contents": base64.b64encode(b"one").decode()}
                ],
                "deletions": [{"path": "gone.txt"}],
            },
        )
        self.assertEqual(
            second["fileChanges"],
            {
                "additions": [
                    {"path": "dir/b.txt", "contents": base64.b64encode(b"two").decode()}
                ],
                "deletions": [],
            },
        )

    def test_reply_without_commit_raises(self) -> None:
        """A reply lacking the commit node is an operational failure."""
        with (
            patch.object(checks, "git", side_effect=self.fake_git),
            patch.object(
                github,
                "graphql",
                return_value={"createCommitOnBranch": {"commit": None}},
            ),
            self.assertRaisesRegex(policy.PublishError, "returned no commit"),
        ):
            publish.replay_commits(self.root / "clone", check_json())

    def test_graphql_failure_wrapped(self) -> None:
        """A GitHubError from the mutation becomes a PublishError."""
        with (
            patch.object(checks, "git", side_effect=self.fake_git),
            patch.object(
                github, "graphql", side_effect=github.GitHubError("GraphQL errors: x")
            ),
            self.assertRaisesRegex(policy.PublishError, "createCommitOnBranch failed"),
        ):
            publish.replay_commits(self.root / "clone", check_json())

    def test_malformed_blob_refused(self) -> None:
        """A blob id that is not a SHA never reaches git."""
        check = check_json()
        check["commits"][0]["additions"][0]["blob"] = "--upload-pack=evil"
        with (
            patch.object(checks, "git") as git,
            patch.object(github, "graphql") as gql,
            self.assertRaisesRegex(policy.PublishError, "malformed blob"),
        ):
            publish.replay_commits(self.root / "clone", check)
        git.assert_not_called()
        gql.assert_not_called()


class OpenPullRequestTest(NoNetworkCase):
    """``open_pull_request`` opens, then labels when the label exists."""

    def test_labelled(self) -> None:
        """An existing label is applied and no warning returned."""
        with (
            patch.object(
                github,
                "api_write",
                side_effect=[{"html_url": "https://x/pull/1", "number": 1}, {}],
            ) as write,
            patch.object(github, "api_object", return_value={"name": "code-monkey"}),
        ):
            url, warning = publish.open_pull_request(check_json())
        self.assertEqual((url, warning), ("https://x/pull/1", None))
        opened = write.call_args_list[0].args
        self.assertEqual(opened[1], f"repos/{REPOSITORY}/pulls")
        self.assertEqual(opened[2]["head"], f"{FORK_ORG}:{BRANCH}")
        self.assertEqual(opened[2]["base"], "main")
        self.assertFalse(opened[2]["maintainer_can_modify"])
        self.assertFalse(opened[2]["draft"])
        labelled = write.call_args_list[1].args
        self.assertEqual(labelled[1], f"repos/{REPOSITORY}/issues/1/labels")
        self.assertEqual(labelled[2], {"labels": ["code-monkey"]})

    def test_missing_label_is_silent(self) -> None:
        """A 404 on the label read is not a warning."""
        with (
            patch.object(
                github,
                "api_write",
                return_value={"html_url": "https://x/pull/1", "number": 1},
            ) as write,
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 404)")
            ),
        ):
            url, warning = publish.open_pull_request(check_json())
        self.assertEqual((url, warning), ("https://x/pull/1", None))
        self.assertEqual(write.call_count, 1)

    def test_label_failure_is_warning(self) -> None:
        """Any other labelling failure is returned as a warning with the URL."""
        with (
            patch.object(
                github,
                "api_write",
                return_value={"html_url": "https://x/pull/1", "number": 1},
            ),
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 403)")
            ),
        ):
            url, warning = publish.open_pull_request(check_json())
        self.assertEqual(url, "https://x/pull/1")
        self.assertIn("could not label the pull request", warning or "")


class RunCommentTest(NoNetworkCase):
    """``run_comment`` posts once and records the comment URL."""

    def write_result(self, data: dict[str, Any]) -> Path:
        """Write a result.json and return its path."""
        path = self.root / "result.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_posts_and_records(self) -> None:
        """The comment lands on the issue, marked, and its URL is written back."""
        path = self.write_result(result_json(bot_login=BOT))
        with (
            patch.object(reads, "api_page", return_value=[]) as search,
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/1"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(
                    result=path, run_url="https://run", since="2026-09-30T09:00:00Z"
                )
            )
        self.assertIn("since=2026-09-30T09:00:00Z", search.call_args.args[0])
        self.assertEqual(
            write.call_args.args[1], f"repos/{REPOSITORY}/issues/7/comments"
        )
        body = write.call_args.args[2]["body"]
        self.assertIn("pull request", body)
        self.assertTrue(body.endswith("<!-- code-monkey https://run proposed -->"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["comment_url"], "https://x/c/1")

    def test_retry_finds_its_earlier_comment(self) -> None:
        """A marked comment by the bot from this run means no second post."""
        path = self.write_result(result_json(bot_login=BOT))
        earlier = {
            "user": {"login": BOT},
            "body": "text\n\n<!-- code-monkey https://run proposed -->",
            "html_url": "https://x/c/9",
        }
        with (
            patch.object(reads, "api_page", return_value=[earlier]),
            patch.object(github, "api_write") as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_not_called()
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["comment_url"], "https://x/c/9")

    def test_copied_marker_from_someone_else_is_ignored(self) -> None:
        """A marker pasted by another account does not suppress the comment."""
        path = self.write_result(result_json(bot_login=BOT))
        spoof = {
            "user": {"login": "mallory"},
            "body": "<!-- code-monkey https://run proposed -->",
            "html_url": "https://x/c/evil",
        }
        with (
            patch.object(reads, "api_page", return_value=[spoof]),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/3"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_called_once()
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["comment_url"], "https://x/c/3")

    def test_changed_outcome_posts_again(self) -> None:
        """A different verdict in the same run is a new outcome to report."""
        path = self.write_result(result_json(bot_login=BOT))
        earlier = {
            "user": {"login": BOT},
            "body": "<!-- code-monkey https://run publish-failed -->",
        }
        with (
            patch.object(reads, "api_page", return_value=[earlier]),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/2"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_called_once()

    def test_marker_carries_the_round(self) -> None:
        """A revision's marker names its round, so the next selection counts it."""
        path = self.write_result(
            result_json(
                bot_login=BOT, round=3, round_summary="s", review=review_record("clean")
            )
        )
        with (
            patch.object(reads, "api_page", return_value=[]),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/1"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        body = write.call_args.args[2]["body"]
        self.assertTrue(
            body.endswith(
                "<!-- code-monkey https://run proposed review=clean round=3 -->"
            )
        )

    def test_marker_carries_the_read_time(self) -> None:
        """The footer names when the packet was read, the next round's watermark."""
        path = self.write_result(
            result_json(bot_login=BOT, collected_at="2026-10-01T12:00:00Z")
        )
        with (
            patch.object(reads, "api_page", return_value=[]),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/1"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        body = write.call_args.args[2]["body"]
        self.assertTrue(
            body.endswith(
                "<!-- code-monkey https://run proposed since=2026-10-01T12:00:00Z -->"
            )
        )

    def test_marker_carries_the_review_verdict(self) -> None:
        """A rerun whose review verdict changed is a new outcome, not a repeat."""
        path = self.write_result(
            result_json(bot_login=BOT, review=review_record("findings"))
        )
        earlier = {
            "user": {"login": BOT},
            "body": "<!-- code-monkey https://run proposed review=pending -->",
            "html_url": "https://x/c/1",
        }
        with (
            patch.object(reads, "api_page", return_value=[earlier]),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/2"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_called_once()
        body = write.call_args.args[2]["body"]
        self.assertTrue(
            body.endswith(
                "<!-- code-monkey https://run proposed review=findings/0/0/0/None -->"
            ),
            body,
        )

    def test_marker_carries_the_review_counts(self) -> None:
        """Counts that changed under the same verdict are a new outcome to report."""
        path = self.write_result(
            result_json(
                bot_login=BOT,
                review=review_record(
                    "findings", assessment="changes", unresolved_threads=0, suppressed=1
                ),
            )
        )
        earlier = {
            "id": 1,
            "user": {"login": BOT},
            "body": "<!-- code-monkey https://run proposed review=findings/2/1/0/changes -->",
            "html_url": "https://x/c/1",
        }
        with (
            patch.object(reads, "api_page", return_value=[earlier]),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/2"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_called_once()
        body = write.call_args.args[2]["body"]
        self.assertTrue(
            body.endswith(
                "<!-- code-monkey https://run proposed review=findings/0/1/0/changes -->"
            ),
            body,
        )
        # The same counts again: nothing posts.
        with (
            patch.object(reads, "api_page", return_value=[{**earlier, "body": body}]),
            patch.object(github, "api_write") as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_not_called()

    def test_an_exhausted_comment_search_posts_rather_than_trusts(self) -> None:
        """Past the pages the search reads, an older match proves nothing."""
        path = self.write_result(
            result_json(bot_login=BOT, review=review_record("pending"))
        )
        match = {
            "id": 1,
            "user": {"login": BOT},
            "body": "<!-- code-monkey https://run proposed review=pending -->",
            "html_url": "https://x/c/1",
        }
        filler = [
            {"id": 2 + i, "user": {"login": "x"}, "body": "..."} for i in range(99)
        ]
        full = [match, *filler]
        with (
            patch.object(reads, "api_page", return_value=full) as page,
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/9"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        # Every page the search allows was full, so the latest outcome may
        # sit beyond them: the comment posts.
        self.assertEqual(page.call_count, comments.MAX_COMMENT_PAGES)
        write.assert_called_once()

    def test_only_the_latest_outcome_of_the_run_suppresses_a_repeat(self) -> None:
        """pending, clean, pending: the third posts, since the latest says clean."""
        path = self.write_result(
            result_json(bot_login=BOT, review=review_record("pending"))
        )
        earlier = [
            {
                "id": 1,
                "user": {"login": BOT},
                "body": "a\n\n<!-- code-monkey https://run proposed review=pending -->",
                "html_url": "https://x/c/1",
            },
            {
                "id": 2,
                "user": {"login": BOT},
                "body": "b\n\n<!-- code-monkey https://run proposed review=clean -->",
                "html_url": "https://x/c/2",
            },
        ]
        with (
            patch.object(reads, "api_page", return_value=earlier),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/3"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_called_once()
        # And when the latest already says what this result says, nothing posts.
        later = [
            {**earlier[1], "id": 1},
            {**earlier[0], "id": 2, "html_url": "https://x/c/9"},
        ]
        with (
            patch.object(reads, "api_page", return_value=later),
            patch.object(github, "api_write") as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url="https://run", since="t")
            )
        write.assert_not_called()
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["comment_url"], "https://x/c/9")

    def test_dry_run_and_silent_verdicts_skip(self) -> None:
        """Dry run and results with nothing to say post nothing."""
        for data in (result_json(dry_run=True), result_json(verdict="weird")):
            path = self.write_result(data)
            with (
                self.subTest(verdict=data["verdict"], dry_run=data["dry_run"]),
                patch.object(github, "api_write") as write,
                redirect_stdout(io.StringIO()),
            ):
                comments.run_comment(
                    argparse.Namespace(result=path, run_url="", since="")
                )
            write.assert_not_called()


class ReportRowTest(unittest.TestCase):
    """``report_row`` renders one escaped table cell whatever the source."""

    def test_title_fallback_is_escaped_and_flattened(self) -> None:
        """A title with a pipe and a newline stays inside its cell."""
        row = reporting.report_row(
            {
                "repository": "o/r",
                "issue": 1,
                "verdict": "proposed",
                "reasons": [],
                "pr_title": "Feat: A | B\n::error::x",
                "pull_request_url": "https://x/pull/1",
            }
        )
        self.assertEqual(row.count("\n"), 0)
        self.assertIn("A \\| B", row)
        self.assertNotIn("::error::", row)
        self.assertEqual(row.count("|") - row.count("\\|"), 7)


class RunReportTest(NoNetworkCase):
    """``run_report`` merges nested result files into one table."""

    def test_states_the_revision_disclosure_once(self) -> None:
        """The summary says, once, how review findings are answered."""
        results = self.root / "results"
        for key in ("a-1", "b-2"):
            directory = results / key
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(
                json.dumps(result_json(key=key)), encoding="utf-8"
            )
        md = self.root / "report.md"
        out = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(results=results, output_md=md, output_json=out)
        )
        text = md.read_text(encoding="utf-8")
        self.assertEqual(text.count(policy.REVISION_DISCLOSURE), 1)

    def test_fractional_spend_is_kept(self) -> None:
        """Half-requests add up; nothing rounds away."""
        results = self.root / "results"
        for key in ("a-1", "b-2"):
            directory = results / key
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(
                json.dumps(result_json(key=key, premium_requests=0.5)),
                encoding="utf-8",
            )
        md = self.root / "report.md"
        out = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(results=results, output_md=md, output_json=out)
        )
        self.assertEqual(
            json.loads(out.read_text(encoding="utf-8"))["premium_requests"], 1.0
        )
        text = md.read_text(encoding="utf-8")
        self.assertIn("| 0.5 |", text)
        self.assertIn("premium requests 1.", text)

    def test_retried_entry_counts_once(self) -> None:
        """Two attempts of one issue yield one row: the latest attempt's."""
        results = self.root / "results"
        for attempt, verdict, spend in ((1, "publish-failed", 30), (2, "proposed", 5)):
            directory = results / f"monkey-result-ns-repo-7-{attempt}"
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(
                json.dumps(
                    result_json(
                        run_attempt=attempt, verdict=verdict, premium_requests=spend
                    )
                ),
                encoding="utf-8",
            )
        md = self.root / "report.md"
        out = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(results=results, output_md=md, output_json=out)
        )
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["run_attempt"], 2)
        self.assertEqual(report["totals"]["proposed"], 1)
        self.assertEqual(report["totals"]["publish-failed"], 0)
        self.assertEqual(report["premium_requests"], 5)

    def test_each_author_session_is_paid_for_once(self) -> None:
        """A rerun author session adds its spend; a publish retry does not."""
        results = self.root / "results"
        attempts = (
            (1, "publish-failed", 30, "101"),
            (2, "publish-failed", 5, "102"),
            (3, "proposed", 5, "102"),
        )
        for attempt, verdict, spend, session in attempts:
            directory = results / f"monkey-result-ns-repo-7-{attempt}"
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(
                json.dumps(
                    result_json(
                        run_attempt=attempt,
                        verdict=verdict,
                        premium_requests=spend,
                        author_session=session,
                    )
                ),
                encoding="utf-8",
            )
        out = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(
                results=results, output_md=self.root / "report.md", output_json=out
            )
        )
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["run_attempt"], 3)
        self.assertEqual(report["premium_requests"], 35)

    def test_forged_fields_stay_inside_their_cells(self) -> None:
        """Newlines and pipes in any field cannot add rows or headings."""
        results = self.root / "results" / "forged"
        results.mkdir(parents=True)
        forged = result_json(
            repository="o/r|x\n## Injected heading",
            issue="7\n| fake | row |",
            verdict="proposed\n# Also injected",
            pull_request_url="https://x/pull/1\n\n- list | item",
        )
        (results / "result.json").write_text(json.dumps(forged), encoding="utf-8")
        md = self.root / "report.md"
        reporting.run_report(
            argparse.Namespace(
                results=results.parent,
                output_md=md,
                output_json=self.root / "report.json",
            )
        )
        lines = md.read_text(encoding="utf-8").splitlines()
        rows = [line for line in lines if line.startswith("|")]
        self.assertEqual(len(rows), 3, rows)
        self.assertFalse(
            [line for line in lines if line.startswith(("#", "- "))][1:], lines
        )
        self.assertEqual(rows[2].count(" | "), 5, rows[2])

    def test_malformed_note_fields_do_not_abort_the_report(self) -> None:
        """Non-list reasons or warnings still yield one row per result."""
        results = self.root / "results"
        for name, overrides in (
            ("a", {"reasons": 5, "warnings": ["w"]}),
            ("b", {"key": "repo-8", "issue": 8, "warnings": True}),
        ):
            (results / name).mkdir(parents=True)
            (results / name / "result.json").write_text(
                json.dumps(result_json(**overrides)), encoding="utf-8"
            )
        md = self.root / "report.md"
        reporting.run_report(
            argparse.Namespace(
                results=results, output_md=md, output_json=self.root / "r.json"
            )
        )
        rows = [
            line
            for line in md.read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and "#" in line
        ]
        self.assertEqual(len(rows), 2, rows)

    def test_report(self) -> None:
        """One row per result, an unreadable row and correct totals."""
        results = self.root / "results"
        first = results / "a" / "deep"
        second = results / "b"
        broken = results / "c"
        for directory in (first, second, broken):
            directory.mkdir(parents=True)
        (first / "result.json").write_text(json.dumps(result_json()), encoding="utf-8")
        (second / "result.json").write_text(
            json.dumps(
                result_json(
                    key="other-2",
                    issue=2,
                    verdict="rejected",
                    reasons=["a | b"],
                    warnings=["w"],
                    pull_request_url=None,
                    branch_url=None,
                    premium_requests=3,
                )
            ),
            encoding="utf-8",
        )
        (broken / "result.json").write_text("{nope", encoding="utf-8")
        (results / "ignored.json").write_text("[]", encoding="utf-8")
        output_md = self.root / "out" / "report.md"
        output_json = self.root / "out" / "report.json"
        reporting.run_report(
            argparse.Namespace(
                results=results, output_md=output_md, output_json=output_json
            )
        )
        markdown = output_md.read_text(encoding="utf-8")
        self.assertIn(
            f"| {REPOSITORY}#7 | proposed | https://github.com/{REPOSITORY}/pull/8 "
            "| — | 4 | Fix: Both |",
            markdown,
        )
        self.assertIn(
            f"| {REPOSITORY}#2 | rejected | — | — | 3 | a \\| b; w |", markdown
        )
        self.assertEqual(markdown.count("| unreadable |"), 1)
        self.assertIn(
            "Proposed 1, abstained 0, handed off 0, rejected 1, failed 0, "
            "publish failures 0; premium requests 7.",
            markdown,
        )
        report = json.loads(output_json.read_text(encoding="utf-8"))
        self.assertEqual(
            report["totals"],
            {
                "proposed": 1,
                "abstain": 0,
                "handoff": 0,
                "rejected": 1,
                "author-failed": 0,
                "publish-failed": 0,
            },
        )
        self.assertEqual(report["premium_requests"], 7)
        self.assertEqual(len(report["unreadable"]), 1)
        self.assertEqual(len(report["results"]), 2)

    def test_empty_report(self) -> None:
        """No results renders the placeholder row."""
        results = self.root / "results"
        results.mkdir()
        output_md = self.root / "report.md"
        output_json = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(
                results=results, output_md=output_md, output_json=output_json
            )
        )
        self.assertIn("| no proposals |", output_md.read_text(encoding="utf-8"))

    def test_review_column_and_totals(self) -> None:
        """Each row states the Copilot verdict; the summary counts them."""
        results = self.root / "results"
        for key, record in (
            ("a-1", review_record("clean", assessment="approval")),
            ("b-2", review_record("findings", unresolved_threads=1)),
            ("c-3", None),
        ):
            directory = results / key
            directory.mkdir(parents=True)
            data = result_json(key=key)
            if record is not None:
                data["review"] = record
            (directory / "result.json").write_text(json.dumps(data), encoding="utf-8")
        md = self.root / "report.md"
        out = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(results=results, output_md=md, output_json=out)
        )
        text = md.read_text(encoding="utf-8")
        self.assertIn("| Copilot review |", text)
        self.assertIn("| clean |", text)
        self.assertIn("| findings (1 thread) |", text)
        self.assertIn("Copilot reviews: clean 1, findings 1, pending 0, none 1.", text)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            report["review_totals"],
            {"clean": 1, "findings": 1, "pending": 0, "none": 1},
        )

    def test_review_column_carries_the_counts(self) -> None:
        """A findings review names its thread and suppressed counts (issue #29)."""
        for record, cell in (
            (
                review_record(
                    "findings", assessment="changes", unresolved_threads=2, suppressed=1
                ),
                "findings (2 threads, 1 suppressed)",
            ),
            (review_record("findings", suppressed=3), "findings (3 suppressed)"),
            (
                review_record("findings", previously_missed=2),
                "findings (2 previously missed)",
            ),
            (review_record("findings", assessment="changes"), "findings"),
            (review_record("clean", assessment="approval"), "clean"),
            (review_record("pending"), "pending"),
            # Counts of the wrong shape stay out of the table.
            (
                review_record("findings", unresolved_threads="2", suppressed=None),
                "findings",
            ),
        ):
            with self.subTest(cell=cell):
                row = reporting.report_row(result_json(review=record))
                self.assertIn(f"| {cell} |", row)

    def test_dry_run_row(self) -> None:
        """A dry-run proposed result shows ``dry run`` instead of a URL."""
        row = reporting.report_row(result_json(dry_run=True))
        self.assertIn("| proposed | dry run |", row)


class CliTest(NoNetworkCase):
    """``build_parser`` and ``main`` wire the commands and the error path."""

    def test_check_requires_arguments(self) -> None:
        """``check`` without its required options is a usage error."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            publish.main(["check", "--key", "x"])
        self.assertEqual(caught.exception.code, 2)

    def test_no_command_is_usage_error(self) -> None:
        """A missing subcommand is a usage error."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            publish.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_publish_error_exits_one(self) -> None:
        """An operational failure exits 1 with the ``publish:`` prefix."""
        path = self.write_check(check_json(verdict="maybe"))
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            publish.main(
                [
                    "push",
                    "--check",
                    str(path),
                    "--workdir",
                    str(self.root),
                    "--mode",
                    "branches",
                    "--output",
                    str(self.root / "result.json"),
                ]
            )
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(stderr.getvalue().startswith("publish: "))
        self.assertIn("unknown verdict", stderr.getvalue())
        self.assertFalse((self.root / "result.json").exists())

    def test_push_and_open_write_results(self) -> None:
        """A dry-run push writes result.json; open passes it through."""
        path = self.write_check(check_json())
        output = self.root / "nested" / "result.json"
        with redirect_stdout(io.StringIO()) as stdout:
            publish.main(
                [
                    "push",
                    "--check",
                    str(path),
                    "--workdir",
                    str(self.root),
                    "--mode",
                    "pull-requests",
                    "--dry-run",
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(stdout.getvalue(), "push: proposed; branch=None\n")
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "pull-requests")
        self.assertEqual(result["fork_repository"], FORK)
        with redirect_stdout(io.StringIO()) as stdout:
            publish.main(
                [
                    "open",
                    "--result",
                    str(output),
                    "--check",
                    str(path),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(stdout.getvalue(), "open: proposed; pr=None\n")

    def test_check_command_writes_outputs(self) -> None:
        """``check`` writes check.json and the summary from ``run_check``."""
        check = checks.Check(
            key="repo-7",
            repository=REPOSITORY,
            issue=7,
            branch=BRANCH,
            base_sha=BASE,
            default_branch="main",
            verdict="abstain",
            reasons=["nothing to do"],
        )
        output = self.root / "check.json"
        summary = self.root / "summary.md"
        with (
            patch.object(checks, "run_check", return_value=check) as run,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            publish.main(
                [
                    "check",
                    "--selection",
                    "s.json",
                    "--key",
                    "repo-7",
                    "--proposal-dir",
                    "p",
                    "--workdir",
                    "w",
                    "--coauthors",
                    "c.json",
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                    "--run-url",
                    "https://run",
                ]
            )
        self.assertEqual(run.call_args.kwargs["key"], "repo-7")
        self.assertEqual(run.call_args.kwargs["run_url"], "https://run")
        self.assertEqual(stdout.getvalue(), "verdict: abstain; nothing to do\n")
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["verdict"], "abstain"
        )
        self.assertIn("`abstain`", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
