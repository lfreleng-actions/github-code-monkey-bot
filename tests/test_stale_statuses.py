# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A listed status left pending too long is stale, not waited for (issue #168).

pre-commit.ci queues jobs for hours when its infrastructure is slow;
past the run's ``stale_status_minutes`` its pending status no longer
holds a head unsettled, counts as no failure and is named in the
record, the comment and the report.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_commit_statuses import (  # noqa: E402
    HEAD,
    PRE_COMMIT,
    REPOSITORY,
    Clock,
    StatusCase,
    run,
    status,
)

antagonistic = import_module("antagonistic_request")
checks = import_module("check_reads")
head_checks = import_module("head_checks")
outcome = import_module("review_outcome")
reporting = import_module("proposal_report")
stale = import_module("stale_statuses")

OLD = "2020-01-01T00:00:00Z"
RULE = stale.Rule(frozenset({PRE_COMMIT}), 8)
REPO_ROOT = Path(__file__).resolve().parents[1]


def stamp(minutes_ago: float) -> str:
    """A GitHub timestamp ``minutes_ago`` minutes before now."""
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def pending(context: str = PRE_COMMIT, updated_at: str = OLD) -> dict[str, Any]:
    """A pending status last updated at ``updated_at``."""
    return {**status(context, "pending", "queued"), "updated_at": updated_at}


class PacketTest(StatusCase):
    """The select job's packet sets a stale status apart."""

    def test_an_old_listed_pending_status_is_stale(self) -> None:
        """Older than the bound: stale, not unsettled, not failing."""
        self.runs = [run(1, "Test")]
        self.statuses = [pending()]
        packet = checks.failing_checks(REPOSITORY, HEAD, RULE)
        self.assertEqual((packet.outstanding, packet.unsettled), (0, 0))
        self.assertEqual(
            packet.stale, [{"context": PRE_COMMIT, "updated_at": OLD, "minutes": 8}]
        )

    def test_a_young_listed_pending_status_is_unsettled(self) -> None:
        """Within the bound, the status is still waited for."""
        self.statuses = [pending(updated_at=stamp(2))]
        packet = checks.failing_checks(REPOSITORY, HEAD, RULE)
        self.assertEqual((packet.unsettled, packet.stale), (1, []))

    def test_an_unlisted_pending_status_is_unsettled_at_any_age(self) -> None:
        """Only the listed contexts can turn stale."""
        self.statuses = [pending("ci/other")]
        packet = checks.failing_checks(REPOSITORY, HEAD, RULE)
        self.assertEqual((packet.unsettled, packet.stale), (1, []))

    def test_a_failing_listed_status_is_failing_never_stale(self) -> None:
        """A settled status keeps its result whatever its age."""
        self.statuses = [{**status(), "updated_at": OLD}]
        packet = checks.failing_checks(REPOSITORY, HEAD, RULE)
        self.assertEqual((packet.outstanding, packet.stale), (1, []))

    def test_zero_minutes_turns_the_rule_off(self) -> None:
        """``0`` waits on a listed status as on any check."""
        self.statuses = [pending()]
        rule = stale.Rule(RULE.contexts, 0)
        packet = checks.failing_checks(REPOSITORY, HEAD, rule)
        self.assertEqual((packet.unsettled, packet.stale), (1, []))

    def test_no_rule_keeps_the_old_reading(self) -> None:
        """Without a rule a pending status is unsettled, as before."""
        self.statuses = [pending()]
        packet = checks.failing_checks(REPOSITORY, HEAD)
        self.assertEqual((packet.unsettled, packet.stale), (1, []))

    def test_a_check_run_of_a_listed_name_is_never_stale(self) -> None:
        """A run named as the context is a check run, not a status."""
        going = {**run(1, PRE_COMMIT), "status": "pending", "conclusion": None}
        self.runs = [{**going, "updated_at": OLD}]
        packet = checks.failing_checks(REPOSITORY, HEAD, RULE)
        self.assertEqual((packet.unsettled, packet.stale), (1, []))


class PublishRecordTest(StatusCase):
    """The publish job's poll stops waiting on a stale status."""

    def await_head(self) -> Any:
        """Await the fixture head's checks under the rule, counting reads."""
        clock = Clock()
        window = head_checks.Window.of(20, 60.0, clock, clock.sleep)
        found = head_checks.await_checks(REPOSITORY, HEAD, lambda: HEAD, window, RULE)
        return found, clock.now

    def test_the_poll_stops_and_records_the_stale_status(self) -> None:
        """Passing runs and a stale status: passing at once, the status listed."""
        self.runs = [run(1, "Test")]
        self.statuses = [pending()]
        found, waited = self.await_head()
        self.assertEqual(waited, 0)
        record = found.to_json(HEAD)
        self.assertEqual((record["state"], record["unsettled"]), ("passing", 0))
        self.assertEqual(record["stale"][0]["context"], PRE_COMMIT)
        self.assertEqual(record["stale"][0]["minutes"], 8)
        lines = head_checks.log_lines(found, HEAD)
        self.assertIn("; stale: 1", lines[0])
        self.assertIn(
            "::warning title=Checks::pre-commit.ci - pr still pending after "
            "8 minutes; not waited for",
            lines,
        )

    def test_the_state_comes_from_the_rest(self) -> None:
        """A failing run beside a stale status is failing; alone it is none."""
        self.runs = [run(1, "Test", "failure")]
        self.statuses = [pending()]
        self.assertEqual(self.await_head()[0].state, "failing")
        self.runs = []
        self.assertEqual(self.await_head()[0].state, "none")

    def test_a_young_status_is_still_waited_for(self) -> None:
        """Within the bound, the poll waits as before."""
        self.runs = [run(1, "Test")]
        self.statuses = [pending(updated_at=stamp(1))]
        found, waited = self.await_head()
        self.assertEqual((found.state, found.stale), ("running", []))
        self.assertGreater(waited, 0)


def with_stale(state: str = "passing", **extra: Any) -> dict[str, Any]:
    """A clean result whose checks record lists a stale status."""
    record = head_checks.HeadChecks(
        state, stale=RULE.entries([checks.status_run(pending(), HEAD)]), **extra
    ).to_json(HEAD)
    return {"commits": [HEAD], "checks": record}


class ReadersTest(unittest.TestCase):
    """The comment, the marker, the report and the nomination read the set."""

    def test_the_comment_names_the_status_and_the_minutes(self) -> None:
        """One sentence on any read state; none when the record is unread."""
        text = outcome.checks_sentence(with_stale())
        self.assertIn("Still pending after 8 minutes and not waited for", text)
        self.assertIn(PRE_COMMIT, text)
        bare = {
            "commits": [HEAD],
            "checks": head_checks.HeadChecks("passing").to_json(HEAD),
        }
        self.assertNotIn("Still pending", outcome.checks_sentence(bare))
        unread = with_stale()
        unread["checks"]["commit_id"] = "b" * 40
        self.assertEqual(stale.sentence(unread, outcome.checks_state(unread)), "")

    def test_the_marker_folds_the_stale_set_into_its_hash(self) -> None:
        """A rerun that saw the status settle posts again."""
        bare = {
            "commits": [HEAD],
            "checks": head_checks.HeadChecks("passing").to_json(HEAD),
        }
        self.assertEqual(outcome.checks_token(bare), "passing")
        token = outcome.checks_token(with_stale())
        self.assertRegex(token, r"^passing/[0-9a-f]{12}$")

    def test_a_record_with_stale_reads_as_its_state(self) -> None:
        """The new field is no malformed record."""
        self.assertEqual(outcome.checks_state(with_stale()), "passing")

    def test_stale_does_not_withhold_the_antagonistic_review(self) -> None:
        """Stale is neither failing nor unknown."""
        item = import_module("test_antagonistic_request").clean()
        item["checks"] = {**item["checks"], "stale": with_stale()["checks"]["stale"]}
        self.assertIsNone(antagonistic.unmet(item))

    def test_the_report_counts_heads_with_stale_statuses(self) -> None:
        """The checks line and report.json carry the count."""
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            item = import_module("test_antagonistic_request").clean()
            item["checks"] = {
                **item["checks"],
                "stale": with_stale()["checks"]["stale"],
            }
            (root / "results" / "a").mkdir(parents=True)
            (root / "results" / "a" / "result.json").write_text(
                json.dumps(item), encoding="utf-8"
            )
            md, out = root / "report.md", root / "report.json"
            reporting.run_report(
                argparse.Namespace(
                    results=root / "results", output_md=md, output_json=out
                )
            )
            self.assertIn("; with a stale status 1.", md.read_text(encoding="utf-8"))
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["stale_status_heads"], 1)


class LoadTest(unittest.TestCase):
    """The context file and the minutes parse strictly."""

    def test_the_shipped_list_names_pre_commit_ci(self) -> None:
        """The config holds the exact context observed."""
        listed = stale.load_contexts(REPO_ROOT / "config" / "stale-statuses.txt")
        self.assertEqual(listed, frozenset({PRE_COMMIT}))

    def test_comments_blank_lines_and_spaces(self) -> None:
        """'#' starts a comment; a context's inner spaces are kept."""
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "list.txt"
            path.write_text("# c\n\n  a b - c  # note\nd\n", encoding="utf-8")
            self.assertEqual(stale.load_contexts(path), frozenset({"a b - c", "d"}))
            path.write_text("x" * (stale.MAX_CONTEXT + 1), encoding="utf-8")
            with self.assertRaises(ValueError):
                stale.load_contexts(path)
            with self.assertRaises(ValueError):
                stale.load_contexts(Path(work) / "missing.txt")
        self.assertEqual(stale.load_contexts(None), frozenset())

    def test_minutes_are_a_non_negative_integer(self) -> None:
        """Zero and above parse; anything else fails."""
        self.assertEqual(stale.parse_minutes("0"), 0)
        self.assertEqual(stale.parse_minutes("30"), 30)
        for text in ("", "-1", "1.5", "eight", "8m", "٨"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                stale.parse_minutes(text)

    def test_an_unreadable_timestamp_is_not_stale(self) -> None:
        """A status without a usable ``updated_at`` is waited for."""
        now = datetime.now(UTC)
        for value in (None, "", "yesterday", 5):
            run_ = {**checks.status_run(pending(), HEAD), "updated_at": value}
            with self.subTest(value=value):
                self.assertFalse(RULE.is_stale(run_, now))
        self.assertTrue(RULE.is_stale(checks.status_run(pending(), HEAD), now))


if __name__ == "__main__":
    unittest.main()
