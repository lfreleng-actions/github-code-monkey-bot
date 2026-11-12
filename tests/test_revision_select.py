# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Revision selection: which bot pull requests get a round, and the entry."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
check_reads = import_module("check_reads")
reads = import_module("issue_reads")
repository_reads = import_module("repository_reads")
revisions = import_module("revision_reads")
round_reads = import_module("round_reads")
thread_reads = import_module("thread_reads")
stale_statuses = import_module("stale_statuses")
select = import_module("revision_select")
scanning = import_module("revision_scan")
rounding = import_module("revision_rounds")
github = import_module("bot_github")

HEAD = "a" * 40
BASE = "b" * 40
TIP = "c" * 40
BOT = {"login": "lf-code-monkey[bot]", "email": "e"}
FORK_ORGS: dict[str, Any] = {"default": "lfreleng-bot-forks", "organisations": {}}
APP = "lf-releng-antagonistic-review-bot"
REVIEWERS = frozenset({APP})
# The body shapes an antagonistic review takes (DESIGN.md section 20.1).
SUMMARY_ONLY = "The change never closes the file it opens.\n"
UNPLACED = "Summary.\n\n**src/x.py:12**: the loop is off by one.\n"
FALLBACK = (
    "GitHub refused the inline findings.\n\n"
    "**src/x.py:12**: off by one.\n**src/y.py:3**: unused import.\n"
)


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


def app_review(text: str, *, handback: bool = True) -> dict[str, Any]:
    """The listed App's REST review of the head, ending with its marker."""
    kind = "antagonistic-handback" if handback else "antagonistic-review"
    return {
        "id": 501,
        "user": {"login": f"{APP}[bot]", "type": "Bot"},
        "commit_id": HEAD,
        "state": "COMMENTED",
        "submitted_at": "2026-10-05T00:00:00Z",
        "body": f"{text}\n---\nFooter.\n<!-- {kind} round=2 head={HEAD} run=777 -->\n",
    }


def no_rounds() -> list[dict[str, Any]]:
    """An empty, typed round summary list."""
    return []


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
        self.checks = check_reads.CheckPacket([], 0, 0)
        self.rounds: tuple[int, list[dict[str, Any]], str | None, str | None] = (
            0,
            [],
            None,
            None,
        )
        # Rounds in the current antagonistic segment; None reads as
        # every round, as a history with no hand-back answered has it.
        self.segment: int | None = None
        # The review ids the history's footers carried.
        self.anchored: frozenset[int] = frozenset()
        # The runs whose footers record a published, counted round.
        self.published: frozenset[str] = frozenset()
        self.newer = False
        patch.object(
            repository_reads, "repository_meta", side_effect=self.meta_of
        ).start()
        patch.object(revisions, "bot_pull_request", side_effect=self.pull_of).start()
        patch.object(
            thread_reads, "unresolved_threads", side_effect=self.threads_of
        ).start()
        patch.object(revisions, "latest_review", side_effect=self.review_of).start()
        patch.object(check_reads, "failing_checks", side_effect=self.checks_of).start()
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

    def checks_of(self, *_args: Any) -> Any:
        """The fixture failing checks."""
        return self.checks

    def review_of(self, *_args: Any) -> dict[str, Any] | None:
        """The fixture verdict."""
        return self.review

    def rounds_of(self, *_args: Any) -> Any:
        """The fixture rounds."""
        highest, summaries, since, verdict = self.rounds
        segment = highest if self.segment is None else self.segment
        return round_reads.RoundHistory(
            highest,
            summaries,
            since,
            verdict,
            segment,
            self.anchored,
            published=self.published,
        )

    def newer_of(self, *_args: Any) -> bool:
        """Whether a maintainer commented since."""
        return self.newer

    def build(self, *, explicit: bool) -> dict[str, Any]:
        """Build the entry for the fixture pull request."""
        return select.build_entry(
            select.Inputs("org", BOT, FORK_ORGS), "alpha", 41, explicit=explicit
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

    def test_failing_check_alone_brings_a_round(self) -> None:
        """A clean head with a failing check is work outstanding for a scan."""
        named = self.build(explicit=True)
        self.assertEqual(named["checks"], [])
        self.assertEqual(named["checks_dropped"], 0)
        with self.assertRaisesRegex(select.Skip, "nothing outstanding"):
            self.build(explicit=False)
        failing: dict[str, Any] = {
            "name": "Test",
            "conclusion": "failure",
            "app": "github-actions",
            "details_url": "https://github.com/org/alpha/runs/1",
            "title": "",
            "summary": "",
            "annotations": [],
        }
        self.checks = check_reads.CheckPacket([failing], 2, 3)
        entry = self.build(explicit=False)
        self.assertEqual(entry["round_reasons"], ["failing checks"])
        self.assertEqual(entry["checks"], [failing])
        self.assertEqual(entry["checks_dropped"], 2)

    def test_running_checks_alone_skip_as_still_running(self) -> None:
        """A head whose CI has not finished is not read as a green one."""
        named = self.build(explicit=True)
        self.assertEqual(named["checks_unsettled"], 0)
        with self.assertRaisesRegex(select.Skip, "^nothing outstanding$"):
            self.build(explicit=False)
        self.checks = check_reads.CheckPacket([], 0, 0, 2)
        with self.assertRaisesRegex(select.Skip, "^checks still running$"):
            self.build(explicit=False)
        # The operator decided: a named pull request still gets its round.
        named = self.build(explicit=True)
        self.assertEqual(named["round_reasons"], ["named by the operator"])
        self.assertEqual(named["checks_unsettled"], 2)

    def test_stale_statuses_alone_are_nothing_outstanding(self) -> None:
        """A stale status is not checks still running; the packet names it."""
        late = [{"context": "pre-commit.ci - pr", "updated_at": "t", "minutes": 8}]
        self.checks = check_reads.CheckPacket([], 0, 0, stale=late)
        with self.assertRaisesRegex(select.Skip, "^nothing outstanding$"):
            self.build(explicit=False)
        self.threads = [thread()]
        rule = stale_statuses.Rule(frozenset({"pre-commit.ci - pr"}), 8)
        with patch.object(
            check_reads, "failing_checks", return_value=self.checks
        ) as read:
            entry = select.build_entry(
                select.Inputs("org", BOT, FORK_ORGS, stale=rule),
                "alpha",
                41,
                explicit=False,
            )
        self.assertEqual(read.call_args.args[2], rule)
        self.assertEqual(entry["checks_stale"], late)
        self.assertEqual(entry["checks_unsettled"], 0)

    def test_running_checks_ride_along_with_other_work(self) -> None:
        """With another reason the round goes ahead and carries the count."""
        self.threads = [thread()]
        self.checks = check_reads.CheckPacket([], 0, 0, 3)
        entry = self.build(explicit=False)
        self.assertEqual(entry["round_reasons"], ["1 unresolved review thread(s)"])
        self.assertEqual(entry["checks"], [])
        self.assertEqual(entry["checks_unsettled"], 3)

    def test_excluded_failing_checks_bring_no_round(self) -> None:
        """A failing check on the exclusion list is carried but is not work."""
        self.checks = check_reads.CheckPacket([{"name": "Excluded"}], 0, 0)
        with self.assertRaisesRegex(select.Skip, "nothing outstanding"):
            self.build(explicit=False)
        self.assertEqual(self.build(explicit=True)["checks"], [{"name": "Excluded"}])

    def test_infrastructure_only_failures_skip_as_needing_a_re_run(self) -> None:
        """Failures at the egress agent alone spend no round (#165)."""
        agent = {"name": "Lint", "kind": "infrastructure"}
        self.checks = check_reads.CheckPacket([agent], 0, 0, infrastructure=1)
        with self.assertRaisesRegex(
            select.Skip, "^infrastructure-only check failures; needs a re-run$"
        ):
            self.build(explicit=False)

    def test_infrastructure_failures_ride_along_with_other_work(self) -> None:
        """With a defect or a thread, the round goes ahead and carries them."""
        agent = {"name": "Lint", "kind": "infrastructure"}
        defect = {"name": "Test", "kind": "defect"}
        self.checks = check_reads.CheckPacket([agent, defect], 0, 1, infrastructure=1)
        entry = self.build(explicit=False)
        self.assertEqual(entry["round_reasons"], ["failing checks"])
        self.assertEqual(entry["checks"], [agent, defect])
        self.threads = [thread()]
        self.checks = check_reads.CheckPacket([agent], 0, 0, infrastructure=1)
        entry = self.build(explicit=False)
        self.assertEqual(entry["round_reasons"], ["1 unresolved review thread(s)"])
        self.assertEqual(entry["checks"], [agent])

    def test_failing_checks_do_not_resume_after_a_handoff(self) -> None:
        """After a handoff a maintainer's word, not a failing check, resumes."""
        self.rounds = (2, [], "2026-10-03T00:00:00Z", "handoff")
        self.checks = check_reads.CheckPacket([], 1, 1)
        with self.assertRaisesRegex(select.Skip, "handed"):
            self.build(explicit=False)

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

    def test_round_history_is_scoped_to_the_pull_request(self) -> None:
        """Footers count for this pull request alone, so a retry starts afresh."""
        with patch.object(
            round_reads, "round_history", side_effect=self.rounds_of
        ) as history:
            self.build(explicit=True)
        history.assert_called_once_with(
            "org/alpha", 7, BOT["login"], "2026-10-01T00:00:00Z", 41
        )

    def test_round_cap_and_safety(self) -> None:
        """The cap, a foreign pull request and an unusable repository all skip."""
        self.rounds = (revisions.MAX_ROUNDS, [], "t", "proposed")
        with self.assertRaisesRegex(select.Skip, "round cap of 10"):
            self.build(explicit=True)
        # An operator may raise the cap for a run; the entry records it.
        raised = select.Inputs("org", BOT, FORK_ORGS, max_rounds=12)
        entry = select.build_entry(raised, "alpha", 41, explicit=True)
        self.assertEqual((entry["round"], entry["max_rounds"]), (11, 12))
        self.rounds = (12, [], "t", "proposed")
        with self.assertRaisesRegex(select.Skip, "round cap of 12"):
            select.build_entry(raised, "alpha", 41, explicit=True)
        self.rounds = (revisions.MAX_ROUNDS, [], "t", "proposed")
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

    def test_scan_round_needs_a_trusted_author_or_the_approval(self) -> None:
        """An untrusted author's issue costs no round until a maintainer approves it."""
        self.threads = [thread()]
        details: dict[str, Any] = {
            "state": "open",
            "title": "T",
            "body": "B",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": None,
            "url": "u",
            "created_at": "t",
            "author_association": "CONTRIBUTOR",
        }
        with patch.object(reads, "issue_details", return_value=details):
            with self.assertRaisesRegex(select.Skip, "agent-approved"):
                self.build(explicit=False)
            details["author_association"] = None
            with self.assertRaisesRegex(select.Skip, "agent-approved"):
                self.build(explicit=False)
            details["labels"] = ["bug", "agent-approved", "no-agent"]
            with self.assertRaisesRegex(select.Skip, "no-agent"):
                self.build(explicit=False)
            details["labels"] = ["bug", "agent-approved"]
            self.assertEqual(self.build(explicit=False)["number"], 7)
            details["labels"] = ["bug"]
            details["author_association"] = "MEMBER"
            details["author_is_bot"] = True
            with self.assertRaisesRegex(select.Skip, "agent-approved"):
                self.build(explicit=False)
            details["author_is_bot"] = False
            for association in ("OWNER", "MEMBER"):
                details["author_association"] = association
                self.assertEqual(self.build(explicit=False)["number"], 7)

    def test_named_round_applies_no_author_rule(self) -> None:
        """Naming the pull request is the approval, whoever filed the issue."""
        details: dict[str, Any] = {
            "state": "open",
            "title": "T",
            "body": "B",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": None,
            "url": "u",
            "created_at": "t",
            "author_association": "NONE",
        }
        with patch.object(reads, "issue_details", return_value=details):
            self.assertEqual(self.build(explicit=True)["number"], 7)

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


class RequireOutstandingTest(RevisionCase):
    """A continuation names a pull request but asks for the scan's test (#167)."""

    def build_outstanding(self) -> dict[str, Any]:
        """Build the fixture pull request as a named continuation would."""
        return select.build_entry(
            select.Inputs("org", BOT, FORK_ORGS),
            "alpha",
            41,
            explicit=True,
            require_outstanding=True,
        )

    def test_nothing_outstanding_skips_with_the_scan_reason(self) -> None:
        """No session for a pull request whose state changed since the dispatch."""
        with self.assertRaisesRegex(select.Skip, "^nothing outstanding$"):
            self.build_outstanding()
        self.checks = check_reads.CheckPacket([], 0, 0, 2)
        with self.assertRaisesRegex(select.Skip, "^checks still running$"):
            self.build_outstanding()

    def test_work_outstanding_gives_the_scan_reasons(self) -> None:
        """The round's reasons are the scan's, not the operator's word."""
        self.threads = [thread()]
        entry = self.build_outstanding()
        self.assertEqual(entry["round_reasons"], ["1 unresolved review thread(s)"])

    def test_handoff_pause_holds(self) -> None:
        """A handed-back pull request waits for a maintainer, as in a scan."""
        self.threads = [thread()]
        self.rounds = (2, [], "2026-10-03T00:00:00Z", "handoff")
        with self.assertRaisesRegex(select.Skip, "handed back to a maintainer"):
            self.build_outstanding()
        self.assertEqual(self.build(explicit=True)["round"], 3)

    def test_the_scan_author_rule_and_the_cap_apply(self) -> None:
        """A continuation's target comes from an unauthenticated result.

        So it earns no operator waiver: an unapproved external issue is
        skipped as the scan skips it, and the cap still applies.
        """
        self.threads = [thread()]
        details: dict[str, Any] = {
            "state": "open",
            "title": "T",
            "body": "B",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": None,
            "url": "u",
            "created_at": "t",
            "author_association": "NONE",
        }
        with patch.object(reads, "issue_details", return_value=details):
            with self.assertRaisesRegex(select.Skip, "agent-approved"):
                self.build_outstanding()
            details["labels"] = ["bug", "agent-approved"]
            self.assertEqual(self.build_outstanding()["number"], 7)
        self.rounds = (revisions.MAX_ROUNDS, [], "t", "proposed")
        with self.assertRaisesRegex(select.Skip, "round cap of 10"):
            self.build_outstanding()


PARENT = "https://github.com/org/bot/actions/runs/42"


class ContinuedRoundTest(RevisionCase):
    """A continuation runs only after its parent's round was counted (#179)."""

    def build_continued(self, parent: str = PARENT) -> dict[str, Any]:
        """Build the fixture pull request as a continuation of ``parent``."""
        return select.build_entry(
            select.Inputs("org", BOT, FORK_ORGS),
            "alpha",
            41,
            explicit=True,
            require_outstanding=True,
            continues=parent,
        )

    def test_a_parent_with_no_counted_round_skips(self) -> None:
        """A forged result whose round posted no footer starts no session."""
        self.threads = [thread()]
        self.published = frozenset({"https://github.com/org/bot/actions/runs/41"})
        with self.assertRaisesRegex(
            select.Skip, "dispatched this continuation published no round here"
        ):
            self.build_continued()

    def test_a_parent_that_published_continues(self) -> None:
        """The parent's footer is on the pull request: the round goes ahead."""
        self.threads = [thread()]
        self.published = frozenset({PARENT})
        self.assertEqual(self.build_continued()["round"], 1)

    def test_no_parent_asks_for_no_footer(self) -> None:
        """A named run that continues nothing reads no parent."""
        self.threads = [thread()]
        self.assertEqual(self.build_continued(parent="")["round"], 1)


class NoReviewTest(RevisionCase):
    """A head Copilot never reviewed skips by name, not as nothing outstanding."""

    def setUp(self) -> None:
        """No review of the head."""
        super().setUp()
        self.review = None

    def test_no_review_and_nothing_else_skips_by_name(self) -> None:
        """No review, threads, failing checks or comments: its own skip."""
        with self.assertRaisesRegex(select.Skip, "^no Copilot review of the head$"):
            self.build(explicit=False)

    def test_a_refused_request_says_so(self) -> None:
        """The latest footer's ``pending/not-requested`` names the refusal."""
        refused = dataclasses.replace(
            self.rounds_of(), last_review="pending/not-requested"
        )
        with (
            patch.object(round_reads, "round_history", return_value=refused),
            self.assertRaisesRegex(
                select.Skip,
                "^no Copilot review of the head; the review request was refused$",
            ),
        ):
            self.build(explicit=False)

    def test_a_thread_still_brings_a_round(self) -> None:
        """Other work outstanding goes ahead as before."""
        self.threads = [thread()]
        entry = self.build(explicit=False)
        self.assertEqual(entry["round_reasons"], ["1 unresolved review thread(s)"])
        self.assertIsNone(entry["review"])

    def test_running_checks_skip_first(self) -> None:
        """A head just pushed waits for its checks before it reads as unreviewed."""
        self.checks = check_reads.CheckPacket([], 0, 0, 1)
        with self.assertRaisesRegex(select.Skip, "^checks still running$"):
            self.build(explicit=False)

    def test_a_named_pull_request_still_gets_its_round(self) -> None:
        """The operator decided."""
        entry = self.build(explicit=True)
        self.assertEqual(entry["round_reasons"], ["named by the operator"])

    def test_the_scan_records_the_skip(self) -> None:
        """Unlike nothing outstanding, the skip reaches ``skipped_issues``."""
        hits = [
            {
                "repository": "org/alpha",
                "number": 41,
                "created_at": "2026-10-02T00:00:00Z",
            }
        ]
        skipped = {"revision": 0}
        named: list[dict[str, str]] = []
        with patch.object(revisions, "search_bot_pull_requests", return_value=hits):
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
            )
        self.assertEqual([build() for _, build in builders], [None])
        self.assertEqual(skipped["revision"], 1)
        self.assertEqual(
            named,
            [{"issue": "org/alpha#41", "reason": "no Copilot review of the head"}],
        )


class PublishedRunsTest(unittest.TestCase):
    """The round history names the runs whose round the publisher counted."""

    def history(self, *footers: str) -> Any:
        """The history of the bot's footers on the issue, in order."""
        entries = [
            {
                "user": {"login": BOT["login"]},
                "body": f"r{n}\n\n{footer}",
                "created_at": f"2026-11-01T{n:02d}:00:00Z",
            }
            for n, footer in enumerate(footers)
        ]
        with patch.object(reads, "api_page", return_value=entries):
            return round_reads.round_history("org/alpha", 7, BOT["login"], None, 41)

    def test_published_holds_proposed_rounds_of_this_pull_request(self) -> None:
        """Not a rejection, a superseded round or another pull request's round."""
        found = self.history(
            "<!-- code-monkey https://r/0 proposed -->",
            "<!-- code-monkey https://r/1 proposed round=1 pr=41 -->",
            "<!-- code-monkey https://r/2 rejected round=2 pr=41 -->",
            "<!-- code-monkey https://r/3 proposed round=3 pr=41 stale=1 -->",
            "<!-- code-monkey https://r/4 proposed round=4 pr=40 -->",
            "<!-- code-monkey https://r/5 handoff round=5 pr=41 -->",
        )
        self.assertEqual(found.published, frozenset({"https://r/0", "https://r/1"}))

    def test_a_marker_in_another_users_comment_is_text(self) -> None:
        """Only the bot's own footer names a published run."""
        entries = [
            {
                "user": {"login": "someone"},
                "body": "x\n\n<!-- code-monkey https://r/9 proposed round=9 pr=41 -->",
                "created_at": "2026-11-01T00:00:00Z",
            }
        ]
        with patch.object(reads, "api_page", return_value=entries):
            found = round_reads.round_history("org/alpha", 7, BOT["login"], None, 41)
        self.assertEqual(found.published, frozenset())

    def test_last_review_is_the_latest_handled_footers(self) -> None:
        """A later failed round leaves the published head's review token."""
        found = self.history(
            "<!-- code-monkey https://r/0 proposed review=clean -->",
            "<!-- code-monkey https://r/1 proposed review=pending/not-requested"
            " round=1 pr=41 -->",
            "<!-- code-monkey https://r/2 author-failed round=2 pr=41 -->",
        )
        self.assertEqual(found.last_review, "pending/not-requested")
        self.assertIsNone(self.history().last_review)


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
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
            )
        # The scan reads each pull request and its issue, to rank them;
        # the packet, many reads, waits for the budget to call.
        self.assertEqual(calls, [40, 41, 9])
        self.assertEqual(skipped["revision"], 1)
        self.assertEqual(named[0]["issue"], "org/beta#9")
        entries = [entry for _, build in builders if (entry := build()) is not None]
        self.assertEqual(calls, [40, 41, 9])
        self.assertEqual([e["pull_request"]["number"] for e in entries], [40, 41])

    def test_scan_applies_the_configured_cap(self) -> None:
        """A scan's builds use the run's cap, not the default."""
        hits = [
            {
                "repository": "org/alpha",
                "number": 41,
                "created_at": "2026-10-02T00:00:00Z",
            }
        ]
        self.threads = [thread()]
        self.rounds = (revisions.MAX_ROUNDS, [], "t", "proposed")
        skipped = {"revision": 0}
        named: list[dict[str, str]] = []
        with patch.object(revisions, "search_bot_pull_requests", return_value=hits):
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
                max_rounds=12,
            )
            entry = builders[0][1]()
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual((entry["round"], entry["max_rounds"]), (11, 12))
            # The default cap stops the same pull request.
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
            )
            self.assertIsNone(builders[0][1]())
        self.assertIn("round cap of 10", named[-1]["reason"])
        # Rounds hands its cap to the scan.
        rounds = rounding.Rounds(
            "org",
            bot=BOT,
            fork_orgs=FORK_ORGS,
            enabled=True,
            explicit=False,
            skips=(skipped, named),
            max_rounds=12,
        )
        with (
            patch.object(revisions, "search_bot_pull_requests", return_value=hits),
            patch.object(rounding, "scan", wraps=scanning.scan) as scanned,
        ):
            rounds.scan()
        self.assertEqual(scanned.call_args.kwargs["max_rounds"], 12)

    def test_scan_ranks_by_the_issues_priority_then_age(self) -> None:
        """An urgent issue's round comes before an older low one's (issue #19)."""
        hits = [
            {
                "repository": "org/alpha",
                "number": 40,
                "created_at": "2026-10-01T00:00:00Z",
            },
            {
                "repository": "org/alpha",
                "number": 41,
                "created_at": "2026-10-02T00:00:00Z",
            },
            {
                "repository": "org/beta",
                "number": 9,
                "created_at": "2026-10-03T00:00:00Z",
            },
        ]
        priorities = {7: "Low", 8: "Urgent", -24: None}

        def per_pull(_repo: str, number: int, *_a: Any) -> dict[str, Any] | None:
            return pull(number=number, issue=number - 33)

        def per_issue(_repo: str, number: int) -> dict[str, Any]:
            return {
                "state": "open",
                "title": "T",
                "body": "B",
                "labels": [],
                "type": "Bug",
                "assignees": [],
                "priority": priorities[number],
                "url": f"https://github.com/org/x/issues/{number}",
                "created_at": "2026-09-01T00:00:00Z",
                "author_association": "MEMBER",
            }

        patch.object(revisions, "bot_pull_request", side_effect=per_pull).start()
        patch.object(reads, "issue_details", side_effect=per_issue).start()
        self.threads = [thread()]
        with patch.object(revisions, "search_bot_pull_requests", return_value=hits):
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped={"revision": 0},
                skipped_issues=[],
            )
        entries = [entry for _, build in builders if (entry := build()) is not None]
        # Urgent first, then Low, then the issue with no priority.
        self.assertEqual([e["pull_request"]["number"] for e in entries], [41, 40, 9])
        self.assertEqual([e["priority"] for e in entries], ["Urgent", "Low", None])

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
            builders = scanning.scan(
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

    def test_scan_passes_over_a_held_round_before_its_packet(self) -> None:
        """A hit another run holds, by pull request or issue, gets no builder.

        The ranking reads name both numbers; the packet's many reads,
        and any failure among them, never happen for a held round.
        """
        hits = [
            {"repository": "org/alpha", "number": 41, "created_at": "t1"},
            {"repository": "org/beta", "number": 9, "created_at": "t2"},
            {"repository": "org/gamma", "number": 50, "created_at": "t3"},
        ]
        self.threads = [thread()]
        asked: list[tuple[str, tuple[int, ...], str]] = []

        def per_pull(_repo: str, number: int, *_a: Any) -> dict[str, Any] | None:
            return pull(number=number, issue=number - 33)

        def held(repository: str, numbers: tuple[int, ...], reference: str) -> bool:
            asked.append((repository, numbers, reference))
            return repository != "org/gamma" and (8 in numbers or 9 in numbers)

        patch.object(revisions, "bot_pull_request", side_effect=per_pull).start()
        with (
            patch.object(revisions, "search_bot_pull_requests", return_value=hits),
            patch.object(scanning, "build_entry", wraps=select.build_entry) as built,
        ):
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped={"revision": 0},
                skipped_issues=[],
                held=held,
            )
            self.assertEqual([repository for repository, _ in builders], ["org/gamma"])
            built.assert_not_called()
            self.assertIsNotNone(builders[0][1]())
            self.assertEqual(built.call_args.args[1:3], ("gamma", 50))
        self.assertEqual(
            asked,
            [
                ("org/alpha", (41, 8), "org/alpha#41"),
                ("org/beta", (9, -24), "org/beta#9"),
                ("org/gamma", (50, 17), "org/gamma#50"),
            ],
        )

    def test_rounds_hand_their_hold_to_the_scan(self) -> None:
        """``Rounds.scan`` passes the hold it is given on to the scan."""

        def held(_repository: str, _numbers: tuple[int, ...], _reference: str) -> bool:
            return False

        rounds = rounding.Rounds(
            "org",
            bot=BOT,
            fork_orgs=FORK_ORGS,
            enabled=True,
            explicit=False,
            skips=({"revision": 0}, []),
        )
        with (
            patch.object(revisions, "search_bot_pull_requests", return_value=[]),
            patch.object(rounding, "scan", wraps=scanning.scan) as scanned,
        ):
            rounds.scan(held)
            rounds.scan()
        self.assertIs(scanned.call_args_list[0].kwargs["held"], held)
        self.assertIsNone(scanned.call_args_list[1].kwargs["held"])

    def test_scan_skips_an_untrusted_authors_round(self) -> None:
        """A scanned round on an unapproved external issue is a named skip."""
        hits = [{"repository": "org/alpha", "number": 41, "created_at": "t1"}]
        self.threads = [thread()]
        details: dict[str, Any] = {
            "state": "open",
            "title": "T",
            "body": "B",
            "labels": [],
            "type": "Bug",
            "assignees": [],
            "priority": None,
            "url": "u",
            "created_at": "t",
            "author_association": "NONE",
        }
        skipped = {"revision": 0}
        named: list[dict[str, str]] = []
        patch.object(reads, "issue_details", return_value=details).start()
        with patch.object(revisions, "search_bot_pull_requests", return_value=hits):
            builders = scanning.scan(
                "org",
                bot=BOT,
                fork_orgs=FORK_ORGS,
                skipped=skipped,
                skipped_issues=named,
            )
        self.assertIsNone(builders[0][1]())
        self.assertEqual(skipped["revision"], 1)
        self.assertEqual(named[0]["issue"], "org/alpha#41")
        self.assertIn("agent-approved", named[0]["reason"])


class PullRequestForBranchTest(unittest.TestCase):
    """The bot's open pull request from a fork branch, by number."""

    def test_finds_the_bots_pull_request(self) -> None:
        """Someone else's pull request from the same branch does not count."""
        entries = [
            {"user": {"login": "mallory"}, "number": 5},
            {"user": {"login": BOT["login"]}, "number": 6},
        ]
        with patch.object(reads, "api_page", return_value=entries) as page:
            number = revisions.pull_request_for_branch(
                "org/alpha", "lfreleng-bot-forks", "code-monkey/issue-7", BOT["login"]
            )
        self.assertEqual(number, 6)
        self.assertIn(
            "head=lfreleng-bot-forks:code-monkey/issue-7", page.call_args.args[0]
        )
        with patch.object(reads, "api_page", return_value=[]):
            self.assertIsNone(
                revisions.pull_request_for_branch("org/alpha", "f", "b", BOT["login"])
            )


class HandbackSelectionTest(RevisionCase):
    """A hand-back newer than the last round brings one; its body rides along."""

    def setUp(self) -> None:
        """No hand-back by default: the listing returns no reviews."""
        super().setUp()
        self.reviews: list[dict[str, Any]] = []
        patch.object(github, "api_list", side_effect=self.reviews_of).start()

    def reviews_of(self, endpoint: str) -> list[dict[str, Any]]:
        """The fixture reviews, for the pull request's review listing alone."""
        self.assertEqual(endpoint, "repos/org/alpha/pulls/41/reviews")
        return self.reviews

    def scan(self, max_rounds: int = revisions.MAX_ROUNDS) -> dict[str, Any]:
        """The entry a scan builds with the App listed, at the run's cap."""
        return select.build_entry(
            select.Inputs("org", BOT, FORK_ORGS, REVIEWERS, max_rounds),
            "alpha",
            41,
            explicit=False,
        )

    def test_hand_back_with_inline_findings(self) -> None:
        """The App's threads and its body reach the entry; the scan selects."""
        self.threads = [{**thread(), "author": APP}]
        self.reviews = [app_review(UNPLACED)]
        entry = self.scan()
        self.assertIn("antagonistic review round 2 handed back", entry["round_reasons"])
        self.assertEqual(entry["threads"][0]["author"], APP)
        handback = entry["antagonistic_review"]
        self.assertEqual(handback["review_id"], 501)
        self.assertEqual(
            handback["url"],
            "https://github.com/org/alpha/pull/41#pullrequestreview-501",
        )
        self.assertIn(UNPLACED, handback["body"])

    def test_body_only_findings_select(self) -> None:
        """A summary-only defect, an unplaced finding, the 422 fallback."""
        for text in (SUMMARY_ONLY, UNPLACED, FALLBACK):
            with self.subTest(text=text):
                self.reviews = [app_review(text)]
                entry = self.scan()
                self.assertEqual(
                    entry["round_reasons"], ["antagonistic review round 2 handed back"]
                )
                self.assertIn(text, entry["antagonistic_review"]["body"])

    def test_threads_reader_hears_the_list(self) -> None:
        """The thread read is given the listed Apps."""
        with patch.object(
            thread_reads,
            "unresolved_threads",
            return_value=thread_reads.ThreadPacket([], 0, False),
        ) as read:
            self.reviews = [app_review(SUMMARY_ONLY)]
            self.scan()
        self.assertEqual(read.call_args.args[3], REVIEWERS)

    def test_a_review_landing_between_reads_is_not_half_read(self) -> None:
        """A hand-back posted mid-read fails the read; the retry reads it whole.

        GitHub posts a review with its inline comments, so one landing
        between the reads would put its threads in the packet without
        its body. A round those threads start moves the head, and a
        hand-back of an older head never counts again, so its body
        findings would be lost: the read fails instead.
        """
        order: list[str] = []
        posted: list[bool] = [False]

        def reviews_of(_endpoint: str) -> list[dict[str, Any]]:
            order.append("reviews")
            found = [app_review(UNPLACED)] if posted[0] else []
            posted[0] = True
            return found

        def threads_of(*_args: Any) -> Any:
            order.append("threads")
            found = [{**thread(), "author": APP}] if posted[0] else []
            return thread_reads.ThreadPacket(found, 0, False)

        patch.object(github, "api_list", side_effect=reviews_of).start()
        patch.object(thread_reads, "unresolved_threads", side_effect=threads_of).start()
        with self.assertRaisesRegex(github.GitHubError, "changed during the read"):
            self.scan()
        self.assertEqual(order, ["reviews", "threads", "reviews"])
        entry = self.scan()
        self.assertEqual(entry["antagonistic_review"]["review_id"], 501)
        self.assertIn(UNPLACED, entry["antagonistic_review"]["body"])
        self.assertEqual(entry["threads"][0]["author"], APP)
        # Once a round moves the head, the old head's hand-back is gone.
        self.pull = pull(head_sha="f" * 40)
        self.assertIsNone(self.scan()["antagonistic_review"])

    def test_no_hand_back_brings_no_round(self) -> None:
        """An ``antagonistic-review`` marker hands nothing back."""
        self.reviews = [app_review(SUMMARY_ONLY, handback=False)]
        with self.assertRaisesRegex(select.Skip, "nothing outstanding"):
            self.scan()

    def test_a_hand_back_before_the_watermark_is_answered(self) -> None:
        """A hand-back the last round already read brings no round of its own."""
        self.reviews = [app_review(SUMMARY_ONLY)]
        self.rounds = (1, no_rounds(), "2026-10-06T00:00:00Z", "proposed")
        with self.assertRaisesRegex(select.Skip, "nothing outstanding"):
            self.scan()
        self.rounds = (1, no_rounds(), "2026-10-04T00:00:00Z", "proposed")
        self.assertEqual(
            self.scan()["round_reasons"], ["antagonistic review round 2 handed back"]
        )

    def test_failed_listing_fails_the_read(self) -> None:
        """A review listing that fails aborts the build; no round runs without it."""
        patch.object(
            github, "api_list", side_effect=github.GitHubError("listing failed")
        ).start()
        with self.assertRaises(github.GitHubError):
            self.scan()
        with self.assertRaises(github.GitHubError):
            select.build_entry(
                select.Inputs("org", BOT, FORK_ORGS, REVIEWERS),
                "alpha",
                41,
                explicit=True,
            )

    def test_unlisted_reads_nothing(self) -> None:
        """Without a listed App the entry carries no hand-back and lists nothing."""
        self.reviews = [app_review(SUMMARY_ONLY)]
        entry = self.build(explicit=True)
        self.assertIsNone(entry["antagonistic_review"])


class SegmentCapTest(RevisionCase):
    """Ten rounds per antagonistic segment, sixty in all (issue #135)."""

    # Before the fixture hand-back was submitted, so it is unanswered.
    SINCE = "2026-10-04T00:00:00Z"

    def setUp(self) -> None:
        """Work outstanding by default, so only the cap decides; no hand-back."""
        super().setUp()
        self.threads = [thread()]
        self.reviews: list[dict[str, Any]] = []
        self.listings = 0
        patch.object(github, "api_list", side_effect=self.reviews_of).start()

    def reviews_of(self, endpoint: str) -> list[dict[str, Any]]:
        """The fixture reviews, for the pull request's review listing alone."""
        self.assertEqual(endpoint, "repos/org/alpha/pulls/41/reviews")
        self.listings += 1
        return self.reviews

    def scan(self, max_rounds: int = revisions.MAX_ROUNDS) -> dict[str, Any]:
        """The entry a scan builds with the App listed, at the run's cap."""
        return select.build_entry(
            select.Inputs("org", BOT, FORK_ORGS, REVIEWERS, max_rounds),
            "alpha",
            41,
            explicit=False,
        )

    def history(self, highest: int, segment: int) -> None:
        """A round history at ``highest``, ``segment`` of them in this segment."""
        self.rounds = (highest, no_rounds(), self.SINCE, "proposed")
        self.segment = segment

    def test_ten_rounds_without_a_hand_back_cap(self) -> None:
        """The eleventh round is skipped, as before; the ninth goes ahead."""
        self.history(10, 10)
        with self.assertRaisesRegex(
            select.Skip, "round cap of 10 reached; a human must take over"
        ):
            self.scan()
        self.history(9, 9)
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (10, 10))

    def test_a_hand_back_starts_a_new_segment(self) -> None:
        """An accepted hand-back on the head: round 11 globally, 1 in its segment."""
        self.history(10, 10)
        self.reviews = [app_review(SUMMARY_ONLY)]
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (11, 1))
        self.assertEqual(entry["antagonistic_review"]["review_id"], 501)
        # Below the cap a hand-back starts a segment all the same.
        self.history(4, 4)
        self.assertEqual(self.scan()["segment_round"], 1)

    def test_a_segment_after_a_reset_caps_again(self) -> None:
        """Ten rounds after a reset cap again, until the next hand-back."""
        self.history(20, 10)
        with self.assertRaisesRegex(select.Skip, "round cap of 10 reached"):
            self.scan()
        self.history(15, 5)
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (16, 6))
        self.history(20, 10)
        self.reviews = [app_review(SUMMARY_ONLY)]
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (21, 1))

    def test_an_answered_hand_back_does_not_reset(self) -> None:
        """A hand-back the last round already read starts no segment."""
        self.reviews = [app_review(SUMMARY_ONLY)]
        self.rounds = (10, no_rounds(), "2026-10-06T00:00:00Z", "proposed")
        self.segment = 10
        with self.assertRaisesRegex(select.Skip, "round cap of 10 reached"):
            self.scan()

    def test_a_rejected_hand_back_does_not_reset(self) -> None:
        """Only a hand-back the #107 checks accept resets the count."""
        older = app_review(SUMMARY_ONLY)
        older["commit_id"] = "f" * 40
        not_last = app_review(SUMMARY_ONLY)
        not_last["body"] = f"{not_last['body']}Trailing text.\n"
        rejected = {
            "wrong App": {
                **app_review(SUMMARY_ONLY),
                "user": {"login": "some-other-app[bot]", "type": "Bot"},
            },
            "not a Bot": {
                **app_review(SUMMARY_ONLY),
                "user": {"login": f"{APP}[bot]", "type": "User"},
            },
            "older head": older,
            "marker not last": not_last,
        }
        self.history(10, 10)
        for name, review in rejected.items():
            with self.subTest(name=name):
                self.reviews = [review]
                with self.assertRaisesRegex(select.Skip, "round cap of 10 reached"):
                    self.scan()
        # With the source off, no listed App's hand-back counts at all.
        self.reviews = [app_review(SUMMARY_ONLY)]
        with self.assertRaisesRegex(select.Skip, "round cap of 10 reached"):
            self.build(explicit=True)

    def test_the_ceiling_holds_whatever_the_segment(self) -> None:
        """Sixty rounds in all skip, even with a fresh hand-back."""
        self.reviews = [app_review(SUMMARY_ONLY)]
        self.history(revisions.MAX_TOTAL_ROUNDS, 1)
        with self.assertRaisesRegex(
            select.Skip, "round cap of 60 reached; a human must take over"
        ):
            self.scan()
        self.history(revisions.MAX_TOTAL_ROUNDS - 1, 3)
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (60, 1))

    def test_the_ceiling_reads_no_hand_back(self) -> None:
        """At sixty rounds the skip comes first; a failing listing cannot abort it."""
        self.history(revisions.MAX_TOTAL_ROUNDS, 10)
        with (
            patch.object(
                github, "api_list", side_effect=github.GitHubError("listing failed")
            ),
            self.assertRaisesRegex(select.Skip, "round cap of 60 reached"),
        ):
            self.scan()

    def test_a_carried_hand_back_does_not_reset(self) -> None:
        """A hand-back a round already carried starts no segment, however fresh.

        Failed attempts move no watermark, so the hand-back stays newer
        than it; and one arriving while a round's selection read it is
        newer than that round's watermark too. Either way its first
        round anchored it, and the segment counts from there.
        """
        self.reviews = [app_review(SUMMARY_ONLY)]
        self.anchored = frozenset({501})
        # Ten failed attempts at the hand-back exhaust its segment.
        self.history(20, 10)
        with self.assertRaisesRegex(select.Skip, "round cap of 10 reached"):
            self.scan()
        self.history(15, 5)
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (16, 6))
        # Answered by a round whose watermark predates it, at a cap of one.
        self.rounds = (11, no_rounds(), "2026-10-04T23:59:59Z", "proposed")
        self.segment = 1
        with self.assertRaisesRegex(select.Skip, "round cap of 1 reached"):
            self.scan(max_rounds=1)
        # A review no round carried still resets the count.
        self.anchored = frozenset({499})
        entry = self.scan(max_rounds=1)
        self.assertEqual((entry["round"], entry["segment_round"]), (12, 1))

    def test_the_runs_cap_applies_per_segment(self) -> None:
        """A run's ``max_rounds`` caps each segment; the ceiling stays sixty."""
        self.history(14, 4)
        with self.assertRaisesRegex(select.Skip, "round cap of 4 reached"):
            self.scan(max_rounds=4)
        self.history(13, 3)
        entry = self.scan(max_rounds=4)
        self.assertEqual(
            (entry["round"], entry["segment_round"], entry["max_rounds"]), (14, 4, 4)
        )
        self.history(14, 4)
        self.reviews = [app_review(SUMMARY_ONLY)]
        entry = self.scan(max_rounds=4)
        self.assertEqual((entry["round"], entry["segment_round"]), (15, 1))
        self.history(revisions.MAX_TOTAL_ROUNDS, 5)
        with self.assertRaisesRegex(select.Skip, "round cap of 60 reached"):
            self.scan(max_rounds=50)

    def test_a_capped_pull_request_costs_one_read(self) -> None:
        """At the cap with no hand-back, the review listing is the only read."""
        self.history(10, 10)
        with (
            patch.object(thread_reads, "unresolved_threads") as threads,
            self.assertRaisesRegex(select.Skip, "round cap"),
        ):
            self.scan()
        threads.assert_not_called()
        self.assertEqual(self.listings, 1)


class SupersededGuardTest(RevisionCase):
    """Rounds lost to another run's push stop at their own limit (issue #154)."""

    def setUp(self) -> None:
        """Work outstanding, no hand-back, nine rounds counted."""
        super().setUp()
        self.threads = [thread()]
        self.rounds = (9, no_rounds(), "2026-10-04T00:00:00Z", "proposed")
        self.stale = 0
        patch.object(github, "api_list", return_value=[]).start()

    def rounds_of(self, *args: Any) -> Any:
        """The fixture history with ``stale`` superseded footers beside it."""
        return dataclasses.replace(super().rounds_of(*args), stale=self.stale)

    def scan(self) -> dict[str, Any]:
        """The entry a scan builds."""
        return select.build_entry(
            select.Inputs("org", BOT, FORK_ORGS), "alpha", 41, explicit=False
        )

    def test_superseded_rounds_skip_at_their_limit(self) -> None:
        """Below the limit the round goes ahead; at it, a human must look."""
        self.stale = revisions.MAX_STALE_ROUNDS - 1
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (10, 10))
        self.stale = revisions.MAX_STALE_ROUNDS
        with self.assertRaisesRegex(
            select.Skip, "too many superseded rounds; a human must look"
        ):
            self.scan()
        with self.assertRaisesRegex(select.Skip, "too many superseded rounds"):
            self.build(explicit=True)

    def test_the_ceiling_still_comes_first(self) -> None:
        """Sixty counted rounds skip with the ceiling's reason, whatever the rest."""
        self.rounds = (revisions.MAX_TOTAL_ROUNDS, no_rounds(), None, "proposed")
        self.stale = revisions.MAX_STALE_ROUNDS
        with self.assertRaisesRegex(select.Skip, "round cap of 60 reached"):
            self.scan()

    def test_the_ceiling_counts_superseded_rounds(self) -> None:
        """Counted and superseded rounds share the ceiling of sixty."""
        self.segment = 1
        self.rounds = (55, no_rounds(), "2026-10-04T00:00:00Z", "proposed")
        self.stale = 4
        entry = self.scan()
        self.assertEqual((entry["round"], entry["segment_round"]), (56, 2))
        self.stale = 5
        with self.assertRaisesRegex(
            select.Skip, "round cap of 60 reached; a human must take over"
        ):
            self.scan()

    def test_the_entry_carries_the_superseded_rounds(self) -> None:
        """The publisher reads them beside ``round`` against the ceiling."""
        self.stale = 4
        self.assertEqual(self.scan()["stale_rounds"], 4)


class ConflictSelectionTest(RevisionCase):
    """A head that conflicts with the default branch is work (issue #104)."""

    def settled(self, value: bool | None) -> Any:
        """Patch the bounded mergeability read to answer ``value``."""
        merge_state = import_module("merge_state")
        answer = merge_state.Mergeability(value, None, None)
        return patch.object(merge_state, "settle", return_value=answer)

    def test_conflict_alone_brings_a_round(self) -> None:
        """A clean, passing head that cannot merge is selected, and says why."""
        self.pull = pull(mergeable=False)
        entry = self.build(explicit=False)
        self.assertEqual(entry["round_reasons"], ["conflicts with the default branch"])
        self.assertIs(entry["pull_request"]["mergeable"], False)

    def test_lazy_null_is_read_again(self) -> None:
        """A null ``mergeable`` is settled before the outstanding test."""
        self.pull = pull(mergeable=None)
        with self.settled(False) as settle:
            entry = self.build(explicit=False)
        settle.assert_called_once_with("org/alpha", 41, HEAD)
        self.assertEqual(entry["round_reasons"], ["conflicts with the default branch"])
        self.assertIs(entry["pull_request"]["mergeable"], False)

    def test_unsettled_is_no_work_on_its_own(self) -> None:
        """A ``mergeable`` that never settles spends no round."""
        self.pull = pull(mergeable=None)
        with (
            self.settled(None),
            self.assertRaisesRegex(select.Skip, "nothing outstanding"),
        ):
            self.build(explicit=False)

    def test_a_settled_answer_is_not_read_again(self) -> None:
        """A boolean from the scan's read needs no second read."""
        with self.settled(None) as settle:
            with self.assertRaisesRegex(select.Skip, "nothing outstanding"):
                self.build(explicit=False)
        settle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
