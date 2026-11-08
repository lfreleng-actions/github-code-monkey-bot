# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A round another run's push superseded does not count (issue #154).

Its footer carries ``stale=1``; the round history leaves it out of
``highest``, the segment and the summaries, and counts it apart so
the selector can stop a pull request whose rounds keep losing races.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
round_reads = import_module("round_reads")
comments = import_module("issue_comment")
antagonistic = import_module("antagonistic_request")
reporting = import_module("proposal_report")
model = import_module("proposal_model")
revision_check = import_module("revision_check")
revisions = import_module("revision_reads")

REPO = "org/alpha"
BOT = "lf-code-monkey[bot]"
PULL = f"https://github.com/{REPO}/pull/41"
MOVED = "head moved from 3e819d2 to 79d8341 since selection; nothing forced"


def history(footers: list[str]) -> Any:
    """The round history the footers, posted by the bot in order, give."""
    entries = [
        {
            "user": {"login": BOT},
            "body": f"r{n}\n\n{footer}",
            "created_at": f"2026-11-01T{n:02d}:00:00Z",
        }
        for n, footer in enumerate(footers)
    ]
    with patch.object(reads, "api_page", return_value=entries):
        return round_reads.round_history(REPO, 7, BOT)


ROUNDS = ["<!-- code-monkey https://r/0 proposed -->"] + [
    f"<!-- code-monkey https://r/{n} proposed round={n} pr=41 "
    f"since=2026-11-01T{n:02d}:00:00Z -->"
    for n in range(1, 10)
]
STALE = (
    "<!-- code-monkey https://r/s rejected round=10 pr=41 "
    "since=2026-11-02T00:00:00Z stale=1 -->"
)


class StaleFooterTest(unittest.TestCase):
    """``round_history`` counts a superseded round apart."""

    def test_a_stale_footer_raises_nothing(self) -> None:
        """Round 9 stays the highest and the segment; the stale round is counted apart."""
        found = history([*ROUNDS, STALE])
        self.assertEqual((found.highest, found.segment), (9, 9))
        self.assertEqual(found.stale, 1)
        self.assertEqual([s["round"] for s in found.summaries][-1], 9)
        # The watermark stays the last handled round's read time.
        self.assertEqual(found.since, "2026-11-01T09:00:00Z")

    def test_a_stale_footer_opens_no_segment(self) -> None:
        """A stale round sent with a hand-back anchors nothing and resets nothing."""
        carried = STALE.replace(" stale=1", " handback=502 stale=1")
        found = history([*ROUNDS, carried])
        self.assertEqual((found.highest, found.segment), (9, 9))
        self.assertEqual(found.anchored, frozenset())
        self.assertEqual(found.stale, 1)
        # Nor does one extend a segment a real round opened.
        opened = (
            "<!-- code-monkey https://r/10 proposed round=10 pr=41 "
            "since=2026-11-02T00:00:00Z handback=501 antagonistic=501 -->"
        )
        later = carried.replace("round=10", "round=11").replace("502", "501")
        found = history([*ROUNDS, opened, later, later])
        self.assertEqual((found.highest, found.segment), (10, 1))
        self.assertEqual(found.stale, 2)

    def test_a_rejection_for_any_other_reason_counts(self) -> None:
        """Policy, payload and check rejections carry no token and still count."""
        rejected = STALE.replace(" stale=1", "")
        found = history([*ROUNDS, rejected])
        self.assertEqual((found.highest, found.segment), (10, 10))

    def test_the_stale_count_is_per_pull_request(self) -> None:
        """A stale footer of another pull request is not this one's."""
        other = STALE.replace("pr=41", "pr=40")
        with patch.object(
            reads,
            "api_page",
            return_value=[{"user": {"login": BOT}, "body": f"x\n\n{other}"}],
        ):
            found = round_reads.round_history(REPO, 7, BOT, None, 41)
        self.assertEqual(found.stale, 0)

    def test_the_footer_reads_the_token(self) -> None:
        """The footer pattern names the token, after every other field."""
        found = round_reads.FOOTER.search(
            STALE.replace(" stale=1", " handback=501 stale=1")
        )
        assert found is not None
        self.assertEqual((found.group("handback"), found.group("stale")), ("501", "1"))


def result(**overrides: Any) -> dict[str, Any]:
    """A revision round's result, superseded at publication."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "alpha-7",
        "bot_login": BOT,
        "repository": REPO,
        "issue": 7,
        "verdict": "rejected",
        "reasons": [MOVED],
        "warnings": [],
        "dry_run": False,
        "mode": "pull-requests",
        "commits": [],
        "pull_request_url": None,
        "round": 10,
        "segment_round": 10,
        "pull_number": 41,
        "collected_at": "2026-11-06T02:06:00Z",
        "superseded": True,
    }
    data.update(overrides)
    return data


def posted(data: dict[str, Any]) -> str:
    """The body ``run_comment`` posts for ``data``."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "result.json"
        path.write_text(json.dumps(data), encoding="utf-8")
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
    return str(write.call_args.args[2]["body"])


class StaleCommentTest(unittest.TestCase):
    """The publisher's comment marks a superseded round and reads well."""

    def test_a_superseded_rejection_writes_the_token(self) -> None:
        """The footer ends ``stale=1``, and the history counts it apart."""
        body = posted(result())
        self.assertTrue(
            body.endswith(
                "<!-- code-monkey https://run rejected round=10 pr=41 "
                "since=2026-11-06T02:06:00Z stale=1 -->"
            ),
            body,
        )
        self.assertIn(MOVED, body)
        self.assertIn("does not count toward the round cap", body)
        self.assertNotIn("round cap of 10", body)
        found = history([*ROUNDS, body.rpartition("\n\n")[2]])
        self.assertEqual((found.highest, found.stale), (9, 1))

    def test_a_stood_down_abstention_writes_the_token(self) -> None:
        """The author job's stand-down reads as a skip that does not count."""
        reason = "code-monkey/issue-7 in forks/alpha moved from 3e819d2 to 79d8341."
        body = posted(result(verdict="abstain", reasons=[reason]))
        self.assertTrue(body.endswith(" stale=1 -->"), body)
        self.assertTrue(
            body.startswith(
                "🐒 An AI agent looked at this issue and did not attempt it: "
                f"{reason} This round does not count toward the round cap"
            ),
            body,
        )

    def test_any_other_rejection_writes_no_token(self) -> None:
        """A policy rejection is not superseded: no token, and the cap applies."""
        body = posted(result(reasons=["symlink"], superseded=False))
        self.assertNotIn("stale=", body)
        self.assertIn("round cap of 10", body)
        self.assertNotIn("does not count", body)

    def test_a_superseded_round_is_never_capped(self) -> None:
        """Its round number is the next round's too: no cap notice for it."""
        self.assertFalse(comments.capped(result()))
        self.assertTrue(comments.capped(result(superseded=False)))


HEAD = "a" * 40


def published(**overrides: Any) -> dict[str, Any]:
    """A revision round's live proposal, its Copilot review of the head clean."""
    data = result(
        verdict="proposed",
        segment_round=1,
        reasons=[],
        commits=[HEAD],
        pull_request_url=PULL,
        mergeable=True,
        superseded=False,
        review={
            "requested": True,
            "verdict": "clean",
            "assessment": "approval",
            "unresolved_threads": 0,
            "suppressed": 0,
            "previously_missed": 0,
            "reasons": [],
            "review_id": 1,
            "commit_id": HEAD,
        },
        checks={
            "state": "passing",
            "commit_id": HEAD,
            "failing": [],
            "dropped": 0,
            "unsettled": 0,
            "read_at": "2026-11-06T00:00:00Z",
            "reason": None,
        },
    )
    data.update(overrides)
    return data


def entry(**overrides: Any) -> dict[str, Any]:
    """A revision selection entry as the check reads it."""
    data: dict[str, Any] = {
        "repository": REPO,
        "number": 7,
        "branch": "code-monkey/issue-7",
        "base_sha": "b" * 40,
        "default_branch": "main",
        "fork_org": "lfreleng-bot-forks",
        "fork_repository": "lfreleng-bot-forks/alpha",
        "task": "revise",
        "rebase_sha": "c" * 40,
        "pull_request": {"number": 41, "url": PULL, "head_sha": HEAD},
        "round": 55,
        "segment_round": 1,
        "threads": [],
    }
    data.update(overrides)
    return data


class CeilingConsumersTest(unittest.TestCase):
    """Every reader of the ceiling counts the superseded rounds too.

    After 54 counted rounds and five superseded ones, round 55 spends
    the sixtieth: the selector picks the pull request no further, so
    nothing downstream may offer it another round.
    """

    def test_the_entry_carries_the_stale_count_to_the_result(self) -> None:
        """Selection, check and result keep the superseded rounds before this one."""
        check = model.Check.from_entry("alpha-7", entry(stale_rounds=5))
        self.assertEqual(check.to_json()["stale_rounds"], 5)
        opened = {"number": 41, "url": PULL, "head_sha": HEAD, "created_at": "t0"}
        check = model.Check.from_entry("alpha-7", entry(pull_request=opened))
        self.assertEqual(check.to_json()["pull_opened_at"], "t0")
        self.assertEqual(
            model.Check.from_entry("alpha-7", entry()).to_json()["stale_rounds"], 0
        )

    def test_the_entry_shape_is_checked(self) -> None:
        """The count is an integer below its guard, within the ceiling."""
        for good in (0, 5, revisions.MAX_STALE_ROUNDS - 1):
            revision_check.check_entry(entry(round=50, stale_rounds=good))
        revision_check.check_entry(entry())
        for bad in ("1", True, None, -1, revisions.MAX_STALE_ROUNDS):
            with (
                self.subTest(bad=bad),
                self.assertRaises(revision_check.PublishError),
            ):
                revision_check.check_entry(entry(stale_rounds=bad))
        with self.assertRaises(revision_check.PublishError):
            revision_check.check_entry(entry(stale_rounds=6))

    def test_no_review_is_requested_at_the_ceiling(self) -> None:
        """A clean round 55 after five superseded rounds asks for no hand-back."""
        self.assertIsNone(antagonistic.unmet(published(round=55, stale_rounds=4)))
        self.assertEqual(
            antagonistic.unmet(published(round=55, stale_rounds=5)),
            "the round is at the cap where a human takes over",
        )

    def test_the_comment_and_report_state_the_ceiling(self) -> None:
        """A round with findings at the shared ceiling says a human must take over."""
        findings = published(
            round=55,
            stale_rounds=5,
            review={**published()["review"], "verdict": "findings"},
        )
        self.assertTrue(comments.capped(findings))
        self.assertIn("round cap of 60 over every", comments.cap_notice(findings))
        self.assertIn("round cap of 60 reached in all", reporting.cap_detail(findings))
        below = {**findings, "stale_rounds": 4}
        self.assertFalse(comments.capped(below))
        self.assertEqual(reporting.cap_detail(below), "")

    def test_a_superseded_round_at_a_limit_says_so(self) -> None:
        """The ceiling or the superseded rounds' guard stops the next round."""
        ceiling = result(round=56, segment_round=1, stale_rounds=4)
        self.assertTrue(comments.capped(ceiling))
        self.assertIn("round cap of 60 over every", posted(ceiling))
        guard = result(round=10, segment_round=1, stale_rounds=9)
        self.assertTrue(comments.capped(guard))
        body = posted(guard)
        self.assertIn("does not count toward the round cap", body)
        self.assertIn("superseded round 10", body)
        self.assertIn("10 superseded rounds reached", reporting.cap_detail(guard))
        self.assertFalse(comments.capped(result(stale_rounds=8)))


def footer(run: str, verdict: str, number: int, *extra: str) -> str:
    """A revision round's footer on pull request 41."""
    fields = " ".join((f"round={number}", "pr=41", f"since=2026-11-06T{run}", *extra))
    return f"<!-- code-monkey https://r/{run} {verdict} {fields} -->"


# 54 counted rounds and four superseded ones: the next selection picks
# round 55 with 59 rounds spent of the ceiling's sixty.
SPENT_59 = [footer(f"{n:02d}", "proposed", n) for n in range(1, 55)] + [
    footer(f"s{n}", "rejected", 55, "stale=1") for n in range(4)
]
WINNER = footer("win", "proposed", 55)
LOSER = footer("lose", "rejected", 55, "stale=1")


def recounted(
    data: dict[str, Any], footers: list[str], run: str
) -> tuple[str, dict[str, Any]]:
    """The body ``run_comment`` posts for ``data`` and the result it writes.

    The issue holds ``footers``, posted by the bot in order; ``run``
    names this attempt as its own footer would.
    """
    entries = [
        {"user": {"login": BOT}, "body": f"r\n\n{text}", "id": n}
        for n, text in enumerate(footers, 1)
    ]
    data = {**data, "collected_at": f"2026-11-06T{run}"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "result.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with (
            patch.object(reads, "api_page", return_value=entries),
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/1"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(
                argparse.Namespace(result=path, run_url=f"https://r/{run}", since="t")
            )
        written = json.loads(path.read_text(encoding="utf-8"))
    # A retry that finds its comment posted writes none.
    return (str(write.call_args.args[2]["body"]) if write.called else ""), written


class RacingRoundsTest(unittest.TestCase):
    """Two runs select round 55 after 54 counted and four superseded rounds.

    One publishes a counted round, the other loses the race: either
    way the history then spends the ceiling's sixty rounds, and each
    comment, whichever posts second, says a human must take over.
    """

    def test_a_loser_after_a_counted_winner_states_the_ceiling(self) -> None:
        """The loser counts the winner's round 55 and its own superseded one."""
        loser = result(round=55, segment_round=1, stale_rounds=4)
        self.assertFalse(comments.capped(loser))
        body, written = recounted(loser, [*SPENT_59, WINNER], "lose")
        self.assertEqual((written["rounds_spent"], written["stale_rounds"]), (60, 4))
        self.assertIn("round cap of 60 over every", body)
        self.assertTrue(body.endswith(" stale=1 -->"), body)
        self.assertIn("round cap of 60 reached in all", reporting.cap_detail(written))

    def test_a_winner_after_a_superseded_loser_states_the_ceiling(self) -> None:
        """The winner counts the loser's superseded round and its own round 55."""
        findings = published(
            round=55,
            stale_rounds=4,
            review={**published()["review"], "verdict": "findings"},
        )
        body, written = recounted(findings, [*SPENT_59, LOSER], "win")
        self.assertEqual((written["rounds_spent"], written["stale_rounds"]), (60, 5))
        self.assertIn("round cap of 60 over every", body)
        self.assertIn("round cap of 60 reached in all", reporting.cap_detail(written))

    def test_a_clean_winner_withdraws_its_nomination(self) -> None:
        """A hand-back the selector would refuse is neither offered nor said."""
        clean = published(
            round=55,
            stale_rounds=4,
            antagonistic={"state": "requested", "reason": None},
        )
        self.assertIsNone(antagonistic.unmet(clean))
        body, written = recounted(clean, [*SPENT_59, LOSER], "win")
        self.assertEqual(
            written["antagonistic"],
            {
                "state": "skipped",
                "reason": "the round is at the cap where a human takes over",
            },
        )
        self.assertNotIn("antagonistic review was requested", body)
        self.assertEqual(
            antagonistic.unmet(written),
            "the round is at the cap where a human takes over",
        )

    def test_the_round_counts_once_posted_or_not(self) -> None:
        """A retry that finds its own footer posted counts it once, not twice."""
        loser = result(round=55, segment_round=1, stale_rounds=4)
        for footers in (SPENT_59, [*SPENT_59, LOSER]):
            with self.subTest(posted=len(footers)):
                _, written = recounted(loser, footers, "lose")
                self.assertEqual(written["rounds_spent"], 59)
                self.assertEqual(written["stale_rounds"], 4)

    def test_no_race_leaves_the_count_as_selected(self) -> None:
        """Below the ceiling, the recount agrees with the selection."""
        findings = published(
            round=55,
            stale_rounds=4,
            review={**published()["review"], "verdict": "findings"},
        )
        body, written = recounted(findings, SPENT_59, "win")
        self.assertEqual(written["rounds_spent"], 59)
        self.assertNotIn("round cap of 60", body)

    def test_a_failed_read_keeps_the_selection_count(self) -> None:
        """No history to read is a warning, and the selection's count stands."""
        loser = result(round=56, segment_round=1, stale_rounds=4)
        with patch.object(
            round_reads, "round_history", side_effect=github.GitHubError("boom")
        ):
            import_module("round_recount").recount(loser, "https://r/lose")
        self.assertNotIn("rounds_spent", loser)
        self.assertIn("not recounted", loser["warnings"][-1])
        self.assertTrue(comments.capped(loser))

    def test_the_history_leaves_out_one_attempt(self) -> None:
        """``exclude`` drops the footers of that run's selection alone."""
        entries = [
            {"user": {"login": BOT}, "body": f"x\n\n{text}"}
            for text in (*SPENT_59, LOSER)
        ]
        with patch.object(reads, "api_page", return_value=entries):
            found = round_reads.round_history(
                REPO, 7, BOT, None, 41, ("https://r/lose", "2026-11-06Tlose")
            )
            other = round_reads.round_history(
                REPO, 7, BOT, None, 41, ("https://r/lose", "2026-11-06Tother")
            )
        self.assertEqual((found.highest, found.stale), (54, 4))
        self.assertEqual((other.highest, other.stale), (54, 5))


# Five counted rounds and nine superseded ones: the next selection
# picks round 6 with the guard's tenth superseded round still open.
STALE_9 = [footer(f"{n:02d}", "proposed", n) for n in range(1, 6)] + [
    footer(f"s{n}", "rejected", 6, "stale=1") for n in range(9)
]


class StaleGuardTest(unittest.TestCase):
    """A counted round reads the superseded rounds' guard too.

    Another run can post the tenth superseded footer while a counted
    round runs: the selector then skips the pull request, so nothing
    downstream may offer it another round, whatever the outcome.
    """

    def test_a_counted_round_at_the_guard_says_so(self) -> None:
        """A round with findings after ten superseded rounds needs a human."""
        findings = published(
            round=6,
            stale_rounds=10,
            review={**published()["review"], "verdict": "findings"},
        )
        self.assertTrue(comments.capped(findings))
        self.assertIn("10 superseded rounds have run", comments.cap_notice(findings))
        self.assertIn("10 superseded rounds reached", reporting.cap_detail(findings))
        below = {**findings, "stale_rounds": 9}
        self.assertFalse(comments.capped(below))
        self.assertEqual(reporting.cap_detail(below), "")

    def test_a_clean_round_at_the_guard_asks_for_no_review(self) -> None:
        """A clean proposal is finished, but no hand-back is offered."""
        clean = published(round=6, stale_rounds=10)
        self.assertFalse(comments.capped(clean))
        self.assertEqual(
            antagonistic.unmet(clean),
            "the round is at the cap where a human takes over",
        )
        self.assertIsNone(antagonistic.unmet(published(round=6, stale_rounds=9)))

    def test_a_winner_after_the_tenth_loser_withdraws_its_nomination(self) -> None:
        """The recount sees the loser's tenth superseded round."""
        clean = published(
            round=6,
            stale_rounds=9,
            antagonistic={"state": "requested", "reason": None},
        )
        _, written = recounted(
            clean, [*STALE_9, footer("lose", "rejected", 6, "stale=1")], "win"
        )
        self.assertEqual(written["stale_rounds"], 10)
        self.assertEqual(written["antagonistic"]["state"], "skipped")

    def test_a_handoff_at_the_guard_promises_no_round(self) -> None:
        """No maintainer's comment brings a round the guard refuses."""
        handoff = published(
            verdict="handoff",
            round=6,
            stale_rounds=10,
            review={**published()["review"], "verdict": "findings"},
        )
        body = posted(handoff)
        self.assertNotIn("once a maintainer comments", body)
        self.assertIn("10 superseded rounds have run", body)


if __name__ == "__main__":
    unittest.main()
