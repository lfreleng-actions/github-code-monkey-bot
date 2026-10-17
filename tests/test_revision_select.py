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
        "body": "Fix.",
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
        self.review: dict[str, Any] | None = {"verdict": "clean"}
        self.rounds: tuple[int, list[dict[str, Any]], str | None] = (0, [], None)
        self.newer = False
        patch.object(
            repository_reads, "repository_meta", side_effect=self.meta_of
        ).start()
        patch.object(revisions, "bot_pull_request", side_effect=self.pull_of).start()
        patch.object(
            revisions, "unresolved_threads", side_effect=self.threads_of
        ).start()
        patch.object(revisions, "latest_review", side_effect=self.review_of).start()
        patch.object(revisions, "rounds_so_far", side_effect=self.rounds_of).start()
        patch.object(
            revisions, "newer_member_comment", side_effect=self.newer_of
        ).start()
        patch.object(revisions, "merge_base", return_value=BASE).start()
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

    def threads_of(self, *_args: Any) -> tuple[list[dict[str, Any]], int]:
        """The fixture threads."""
        return self.threads, 0

    def review_of(self, *_args: Any) -> dict[str, Any] | None:
        """The fixture verdict."""
        return self.review

    def rounds_of(self, *_args: Any) -> tuple[int, list[dict[str, Any]], str | None]:
        """The fixture rounds."""
        return self.rounds

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

    def test_nothing_outstanding_is_skipped_on_a_scan(self) -> None:
        """A clean head with no threads and no new comment needs no round."""
        with self.assertRaisesRegex(select.Skip, "nothing outstanding"):
            self.build(explicit=False)

    def test_explicit_ignores_the_outstanding_test(self) -> None:
        """An operator's choice is reason enough."""
        entry = self.build(explicit=True)
        self.assertEqual(entry["round_reasons"], ["named by the operator"])

    def test_each_outstanding_reason(self) -> None:
        """Findings on the head, and a newer maintainer comment, each count."""
        self.review = {"verdict": "findings"}
        self.assertIn(
            "Copilot's review of the head has findings",
            self.build(explicit=False)["round_reasons"],
        )
        self.review = None
        self.newer = True
        reasons = self.build(explicit=False)["round_reasons"]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(all("maintainer commented" in r for r in reasons))

    def test_round_cap_and_safety(self) -> None:
        """The cap, a foreign pull request and an unusable repository all skip."""
        self.rounds = (revisions.MAX_ROUNDS, [], "t")
        with self.assertRaisesRegex(select.Skip, "round cap"):
            self.build(explicit=True)
        self.rounds = (3, [], "t")
        self.pull = None
        with self.assertRaisesRegex(select.Skip, "not an open pull request"):
            self.build(explicit=True)
        self.pull = pull()
        self.meta["public"] = False
        with self.assertRaisesRegex(select.Skip, "not public"):
            self.build(explicit=True)

    def test_next_round_number_and_summaries(self) -> None:
        """Round numbering continues from the bot's markers."""
        self.rounds = (2, [{"round": 1, "body": "r1"}, {"round": 2, "body": "r2"}], "t")
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
            entries = select.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
            )
        self.assertEqual(calls, [40, 41, 9])
        self.assertEqual([e["pull_request"]["number"] for e in entries], [40, 41])
        self.assertEqual(skipped["revision"], 1)
        self.assertEqual(named[0]["issue"], "org/beta#9")


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
