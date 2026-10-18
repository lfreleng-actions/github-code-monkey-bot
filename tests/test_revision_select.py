# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Revision selection: which bot pull requests get a round, and the entry."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
reads = import_module("issue_reads")
repository_reads = import_module("repository_reads")
revisions = import_module("revision_reads")
round_reads = import_module("round_reads")
thread_reads = import_module("thread_reads")
select = import_module("revision_select")
github = import_module("bot_github")

HEAD = "a" * 40
BASE = "b" * 40
TIP = "c" * 40
BOT = {"login": "lf-code-monkey[bot]", "email": "e"}
FORK_ORGS: dict[str, Any] = {"default": "lfreleng-bot-forks", "organisations": {}}


def pull(**overrides: Any) -> dict[str, Any]:
    """A bot pull request as ``revision_reads.bot_pull_request`` returns it."""
    data: dict[str, Any] = {
        "number": 41,
        "url": "https://github.com/org/alpha/pull/41",
        "title": "Fix: Thing",
        "issue": 7,
        "branch": "code-monkey/issue-7",
        "head_sha": HEAD,
        "base_ref": "main",
        "mergeable": True,
        "created_at": "2026-10-01T00:00:00Z",
    }
    data.update(overrides)
    return data


def thread() -> dict[str, Any]:
    """One unresolved thread in packet form."""
    return {
        "thread_id": "PRRT_1",
        "comment_id": 100,
        "path": "x.py",
        "line": 1,
        "outdated": False,
        "author": "copilot-pull-request-reviewer",
        "maintainer": False,
        "created_at": "2026-10-02T00:00:00Z",
        "body": "Fix.",
        "replies": [],
        "replies_dropped": False,
    }


class RevisionCase(unittest.TestCase):
    """Every read patched with a healthy default."""

    def setUp(self) -> None:
        """A public repository, a bot pull request, a clean head, no rounds."""
        self.meta = {
            "name": "alpha",
            "archived": False,
            "template": False,
            "fork": False,
            "public": True,
            "default_branch": "main",
        }
        self.pull: dict[str, Any] | None = pull()
        self.threads: list[dict[str, Any]] = []
        self.dropped = 0
        self.thread_word = False
        self.threads_since: list[str | None] = []
        self.review: dict[str, Any] | None = {"verdict": "clean"}
        self.rounds: tuple[int, list[dict[str, Any]], str | None, str | None] = (
            0,
            [],
            None,
            None,
        )
        self.newer = False
        patch.object(
            repository_reads, "repository_meta", side_effect=self.meta_of
        ).start()
        patch.object(revisions, "bot_pull_request", side_effect=self.pull_of).start()
        patch.object(
            thread_reads, "unresolved_threads", side_effect=self.threads_of
        ).start()
        patch.object(revisions, "latest_review", side_effect=self.review_of).start()
        patch.object(round_reads, "round_history", side_effect=self.rounds_of).start()
        patch.object(
            round_reads, "newer_member_comment", side_effect=self.newer_of
        ).start()
        patch.object(revisions, "merge_base", return_value=BASE).start()
        patch.object(round_reads, "comments_since", return_value=([], 0)).start()
        patch.object(
            reads,
            "issue_details",
            return_value={
                "state": "open",
                "title": "T",
                "body": "B",
                "labels": ["bug"],
                "type": "Bug",
                "assignees": [],
                "priority": "High",
                "url": "https://github.com/org/alpha/issues/7",
                "created_at": "2026-09-01T00:00:00Z",
                "author_association": "MEMBER",
            },
        ).start()
        patch.object(reads, "filtered_comments", return_value=([], 0, False)).start()
        patch.object(reads, "branch_head", return_value=TIP).start()
        self.addCleanup(patch.stopall)

    def meta_of(self, _org: str, _name: str) -> dict[str, Any] | None:
        """The fixture repository."""
        return self.meta

    def pull_of(self, *_args: Any) -> dict[str, Any] | None:
        """The fixture pull request."""
        return self.pull

    def threads_of(self, *args: Any) -> Any:
        """The fixture threads as a packet, noting the watermark asked for."""
        self.threads_since.append(args[2] if len(args) > 2 else None)
        return thread_reads.ThreadPacket(self.threads, self.dropped, self.thread_word)

    def review_of(self, *_args: Any) -> dict[str, Any] | None:
        """The fixture verdict."""
        return self.review

    def rounds_of(self, *_args: Any) -> Any:
        """The fixture rounds."""
        highest, summaries, since, verdict = self.rounds
        return round_reads.RoundHistory(highest, summaries, since, verdict)

    def newer_of(self, *_args: Any) -> bool:
        """Whether a maintainer commented since."""
        return self.newer

    def build(self, *, explicit: bool) -> dict[str, Any]:
        """Build the entry for the fixture pull request."""
        return select.build_entry(
            "org", "alpha", 41, bot=BOT, fork_orgs=FORK_ORGS, explicit=explicit
        )


class BuildEntryTest(RevisionCase):
    """The entry carries both bases, the pull request and its review state."""

    def test_shape(self) -> None:
        """Key as the issue's, task revise, both bases, round one."""
        self.threads = [thread()]
        entry = self.build(explicit=False)
        self.assertEqual(entry["key"], "alpha-7")
        self.assertEqual(entry["task"], "revise")
        self.assertEqual(entry["number"], 7)
        self.assertEqual(entry["base_sha"], BASE)
        self.assertEqual(entry["rebase_sha"], TIP)
        self.assertEqual(entry["checkout_sha"], TIP)
        self.assertEqual(entry["branch"], "code-monkey/issue-7")
        self.assertEqual(entry["fork_repository"], "lfreleng-bot-forks/alpha")
        self.assertEqual(entry["pull_request"]["number"], 41)
        self.assertEqual(entry["pull_request"]["head_sha"], HEAD)
        self.assertEqual(entry["round"], 1)
        self.assertEqual(entry["threads"], [thread()])
        self.assertEqual(entry["round_reasons"], ["1 unresolved review thread(s)"])
        self.assertNotIn("state", entry)
        self.assertNotIn("assignees", entry)

    def test_handoff_pauses_the_scan_until_a_maintainer_speaks(self) -> None:
        """After a handoff, unresolved threads alone bring no round; a comment does."""
        self.threads = [thread()]
        self.rounds = (2, [], "2026-10-03T00:00:00Z", "handoff")
        with self.assertRaisesRegex(select.Skip, "handed"):
            self.build(explicit=False)
        self.newer = True
        self.assertEqual(self.build(explicit=False)["round"], 3)
        self.newer = False
        # The operator may still name it.
        self.assertEqual(self.build(explicit=True)["round"], 3)

    def test_maintainer_reply_on_a_thread_resumes_after_a_handoff(self) -> None:
        """A maintainer's word on any unresolved thread, kept or dropped, resumes."""
        self.rounds = (2, [], "2026-10-03T00:00:00Z", "handoff")
        self.threads = [thread()]
        with self.assertRaisesRegex(select.Skip, "handed"):
            self.build(explicit=False)
        # The thread reads heard a maintainer since the watermark, on a
        # thread the bounded packet may or may not carry.
        self.thread_word = True
        self.dropped = 1
        entry = self.build(explicit=False)
        self.assertEqual(entry["round"], 3)
        self.assertIn(
            "a maintainer commented on a review thread since the last round",
            entry["round_reasons"],
        )
        self.assertIn("2 unresolved review thread(s)", entry["round_reasons"])
        self.assertEqual(entry["threads_dropped"], 1)
        # The reads were asked with the watermark, so they could tell.
        self.assertEqual(self.threads_since[-1], "2026-10-03T00:00:00Z")

    def test_round_cap_and_safety(self) -> None:
        """The cap, a foreign pull request and an unusable repository all skip."""
        self.rounds = (revisions.MAX_ROUNDS, [], "t", "proposed")
        with self.assertRaisesRegex(select.Skip, "round cap"):
            self.build(explicit=True)
        self.rounds = (3, [], "t", "proposed")
        self.pull = None
        with self.assertRaisesRegex(select.Skip, "not an open pull request"):
            self.build(explicit=True)
        self.pull = pull()
        self.meta["public"] = False
        with self.assertRaisesRegex(select.Skip, "not public"):
            self.build(explicit=True)

    def test_repository_and_issue_safety_still_apply(self) -> None:
        """An archived repository, a closed issue or the opt-out stop a round."""
        self.meta["archived"] = True
        with self.assertRaisesRegex(select.Skip, "archived"):
            self.build(explicit=True)
        self.meta["archived"] = False
        details = {
            "state": "closed",
            "title": "T",
            "body": "B",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": None,
            "url": "u",
            "created_at": "t",
            "author_association": "MEMBER",
        }
        with patch.object(reads, "issue_details", return_value=details):
            with self.assertRaisesRegex(select.Skip, "closed"):
                self.build(explicit=True)
            details["state"] = "open"
            details["labels"] = ["no-agent"]
            with self.assertRaisesRegex(select.Skip, "no-agent"):
                self.build(explicit=True)

    def test_packet_carries_the_comments_since_the_last_round(self) -> None:
        """The instruction that caused the round reaches the agent, whatever the cap."""
        self.rounds = (
            1,
            [{"round": 1, "body": "r1"}],
            "2026-10-04T00:00:00Z",
            "proposed",
        )
        self.newer = True
        new_comment = {
            "author": "m",
            "association": "MEMBER",
            "created_at": "2026-10-05T00:00:00Z",
            "body": "please rebase",
        }
        with patch.object(
            round_reads, "comments_since", return_value=([new_comment], 0)
        ) as since:
            entry = self.build(explicit=False)
        self.assertEqual(entry["new_comments"], [new_comment])
        self.assertEqual(entry["new_pr_comments"], [new_comment])
        self.assertEqual(since.call_args.args[2], "2026-10-04T00:00:00Z")

    def test_next_round_number_and_summaries(self) -> None:
        """Round numbering continues from the bot's markers."""
        self.rounds = (
            2,
            [{"round": 1, "body": "r1"}, {"round": 2, "body": "r2"}],
            "t",
            "proposed",
        )
        self.newer = True
        entry = self.build(explicit=False)
        self.assertEqual(entry["round"], 3)
        self.assertEqual([r["round"] for r in entry["rounds"]], [1, 2])


class ScanTest(RevisionCase):
    """The owner-wide scan keeps the pull requests with work, oldest first."""

    def test_scan_orders_and_records_skips(self) -> None:
        """Hits with nothing outstanding vanish quietly; other skips are named."""
        hits = [
            {
                "repository": "org/alpha",
                "number": 41,
                "created_at": "2026-10-02T00:00:00Z",
            },
            {
                "repository": "org/alpha",
                "number": 40,
                "created_at": "2026-10-01T00:00:00Z",
            },
            {
                "repository": "org/beta",
                "number": 9,
                "created_at": "2026-10-03T00:00:00Z",
            },
        ]
        skipped = {"revision": 0}
        named: list[dict[str, str]] = []
        calls: list[int] = []

        def per_pull(_repo: str, number: int, *_a: Any) -> dict[str, Any] | None:
            calls.append(number)
            if number == 9:
                return None
            return pull(number=number, issue=number - 33)

        patch.object(revisions, "bot_pull_request", side_effect=per_pull).start()
        self.threads = [thread()]
        with patch.object(revisions, "search_bot_pull_requests", return_value=hits):
            builders = select.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
            )
        # Nothing is read until a builder is called: the budget decides first.
        self.assertEqual(calls, [])
        entries = [entry for _, build in builders if (entry := build()) is not None]
        self.assertEqual(calls, [40, 41, 9])
        self.assertEqual([e["pull_request"]["number"] for e in entries], [40, 41])
        self.assertEqual(skipped["revision"], 1)
        self.assertEqual(named[0]["issue"], "org/beta#9")

    def test_scan_honours_the_repository_policy(self) -> None:
        """Hits outside the run's repositories, or excluded, are passed over unread."""
        hits = [
            {"repository": "org/alpha", "number": 41, "created_at": "t1"},
            {"repository": "org/beta", "number": 9, "created_at": "t2"},
        ]
        self.threads = [thread()]
        reads_made: list[int] = []

        def per_pull(_repo: str, number: int, *_a: Any) -> dict[str, Any] | None:
            reads_made.append(number)
            return pull(number=number)

        def alpha_alone(name: str) -> bool:
            return name == "alpha"

        patch.object(revisions, "bot_pull_request", side_effect=per_pull).start()
        with patch.object(revisions, "search_bot_pull_requests", return_value=hits):
            builders = select.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped={"revision": 0},
                skipped_issues=[],
                allowed=alpha_alone,
            )
        entries = [entry for _, build in builders if (entry := build()) is not None]
        self.assertEqual(len(entries), 1)
        self.assertEqual(reads_made, [41])


class PullRequestForBranchTest(unittest.TestCase):
    """The bot's open pull request from a fork branch, by number."""

    def test_finds_the_bots_pull_request(self) -> None:
        """Someone else's pull request from the same branch does not count."""
        entries = [
            {"user": {"login": "mallory"}, "number": 5},
            {"user": {"login": BOT["login"]}, "number": 6},
        ]
        with patch.object(reads, "api_page", return_value=entries) as page:
            number = select.pull_request_for_branch(
                "org/alpha", "lfreleng-bot-forks", "code-monkey/issue-7", BOT["login"]
            )
        self.assertEqual(number, 6)
        self.assertIn(
            "head=lfreleng-bot-forks:code-monkey/issue-7", page.call_args.args[0]
        )
        with patch.object(reads, "api_page", return_value=[]):
            self.assertIsNone(
                select.pull_request_for_branch("org/alpha", "f", "b", BOT["login"])
            )


if __name__ == "__main__":
    unittest.main()
