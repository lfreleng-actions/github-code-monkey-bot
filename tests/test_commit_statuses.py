# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Commit statuses read beside the check runs, as checks (issue #163).

pre-commit.ci and other services post to the Commit Status API, not
the Check Runs API; a failing status has to count as a failing check
in the revision packet and in the publish job's ``checks`` record.
"""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
checks = import_module("check_reads")
github = import_module("bot_github")
head_checks = import_module("head_checks")
reads = import_module("issue_reads")
outcome = import_module("review_outcome")

REPOSITORY = "org/alpha"
HEAD = "a" * 40
PRE_COMMIT = "pre-commit.ci - pr"


def run(identity: int, name: str, conclusion: str = "success") -> dict[str, Any]:
    """A completed check run on the head, as the REST API returns it."""
    return {
        "id": identity,
        "name": name,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"slug": "github-actions"},
        "details_url": f"https://github.com/{REPOSITORY}/actions/runs/1/job/{identity}",
        "output": {"title": name, "summary": "", "annotations_count": 0},
    }


def status(
    context: str = PRE_COMMIT,
    state: str = "failure",
    description: str = "checks completed with failures",
) -> dict[str, Any]:
    """A commit status, as the combined status endpoint returns it."""
    return {
        "context": context,
        "state": state,
        "description": description,
        "target_url": "https://results.pre-commit.ci/run/github/1/2/3",
    }


class StatusCase(unittest.TestCase):
    """The check run and combined status reads patched with fixtures."""

    def setUp(self) -> None:
        """No runs and no statuses until a test adds them."""
        self.runs: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.status_reply: dict[str, Any] | None = None
        self.endpoints: list[str] = []
        patch.object(github, "api_object", side_effect=self.reply).start()
        patch.object(reads, "api_page", side_effect=AssertionError("no notes")).start()
        self.addCleanup(patch.stopall)

    def reply(self, endpoint: str) -> dict[str, Any]:
        """The fixture runs or statuses, by endpoint."""
        self.endpoints.append(endpoint)
        if f"commits/{HEAD}/check-runs?" in endpoint:
            return {"total_count": len(self.runs), "check_runs": self.runs}
        if f"commits/{HEAD}/status?" in endpoint:
            if self.status_reply is not None:
                return self.status_reply
            return {
                "sha": HEAD,
                "state": "failure",
                "total_count": len(self.statuses),
                "statuses": self.statuses,
            }
        raise AssertionError(f"unexpected read of {endpoint}")


class PacketTest(StatusCase):
    """The select job's packet carries a failing status as a check."""

    def test_a_failing_status_alone_is_outstanding(self) -> None:
        """A failing status and no failing run brings a round, as a check entry."""
        self.runs = [run(1, "Test")]
        self.statuses = [status(), status("other", "success")]
        packet = checks.failing_checks(REPOSITORY, HEAD)
        self.assertEqual(packet.outstanding, 1)
        self.assertEqual(packet.unsettled, 0)
        [entry] = packet.checks
        self.assertEqual(entry["name"], PRE_COMMIT)
        self.assertEqual(entry["conclusion"], "failure")
        self.assertEqual(entry["title"], "checks completed with failures")
        self.assertEqual(entry["details_url"], status()["target_url"])
        self.assertEqual(entry["annotations"], [])
        self.assertEqual(entry["kind"], checks.DEFECT)
        read = [e for e in self.endpoints if "/status?" in e]
        self.assertEqual(len(read), 1)
        self.assertIn("per_page=100", read[0])
        self.assertNotIn("filter=latest", read[0])

    def test_an_error_status_is_failing(self) -> None:
        """``error`` fails as ``failure`` does."""
        self.statuses = [status(state="error")]
        packet = checks.failing_checks(REPOSITORY, HEAD)
        self.assertEqual(packet.outstanding, 1)
        self.assertEqual(packet.checks[0]["conclusion"], "error")

    def test_a_pending_status_leaves_the_head_unsettled(self) -> None:
        """A pending status is unsettled and no failure."""
        self.runs = [run(1, "Test")]
        self.statuses = [status(state="pending")]
        packet = checks.failing_checks(REPOSITORY, HEAD)
        self.assertEqual((packet.outstanding, packet.unsettled), (0, 1))
        self.assertEqual(packet.checks, [])

    def test_a_status_and_a_run_of_one_name_are_two_entries(self) -> None:
        """A run and a status sharing a name are not merged."""
        self.runs = [run(1, PRE_COMMIT, "failure")]
        self.statuses = [status()]
        packet = checks.failing_checks(REPOSITORY, HEAD)
        self.assertEqual(packet.outstanding, 2)
        self.assertEqual([e["name"] for e in packet.checks], [PRE_COMMIT] * 2)
        self.assertEqual(
            sorted(e["app"] or "" for e in packet.checks), ["", "github-actions"]
        )

    def test_the_bound_trims_context_and_description(self) -> None:
        """A context or description past the bound is cut to it."""
        long = "x" * (checks.MAX_TEXT + 50)
        self.statuses = [status(long, description=long)]
        [entry] = checks.failing_checks(REPOSITORY, HEAD).checks
        self.assertLessEqual(len(entry["name"].encode()), checks.MAX_TEXT)
        self.assertLessEqual(len(entry["title"].encode()), checks.MAX_TEXT)
        self.assertTrue(entry["name"].startswith("xxx"))

    def test_a_reply_without_statuses_fails_closed(self) -> None:
        """A malformed combined status is an error, not a clean head."""
        self.status_reply = {"sha": HEAD, "state": "pending"}
        with self.assertRaises(github.GitHubError):
            checks.failing_checks(REPOSITORY, HEAD)


class Clock:
    """A fake monotonic clock that sleeping advances."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock."""
        self.now += seconds


class PublishRecordTest(StatusCase):
    """The publish job's ``checks`` record counts a failing status."""

    def await_head(self) -> Any:
        """Await the fixture head's checks in a twenty-minute window."""
        clock = Clock()
        window = head_checks.Window.of(20, 60.0, clock, clock.sleep)
        return head_checks.await_checks(REPOSITORY, HEAD, lambda: HEAD, window)

    def test_a_failing_status_makes_the_record_failing(self) -> None:
        """Passing runs and a failed status record ``failing``."""
        self.runs = [run(1, "Test")]
        self.statuses = [status()]
        found = self.await_head()
        record = found.to_json(HEAD)
        self.assertEqual(record["state"], "failing")
        self.assertEqual(record["defects"], 1)
        self.assertEqual([e["name"] for e in record["failing"]], [PRE_COMMIT])
        warning = head_checks.annotation(found, HEAD)
        assert warning is not None
        self.assertIn(PRE_COMMIT, warning)

    def test_a_pending_status_is_running_at_the_deadline(self) -> None:
        """A status that stays pending is waited on, then ``running``."""
        self.runs = [run(1, "Test")]
        self.statuses = [status(state="pending")]
        found = self.await_head()
        self.assertEqual((found.state, found.unsettled), ("running", 1))

    def test_a_head_with_passing_runs_and_statuses_passes(self) -> None:
        """Successful runs and statuses record ``passing``."""
        self.runs = [run(1, "Test")]
        self.statuses = [status(state="success")]
        self.assertEqual(self.await_head().state, "passing")


class CountBoundTest(unittest.TestCase):
    """Readers of a recorded count accept every count the reads can make."""

    def most(self) -> int:
        """The most runs the reads keep plus one page of statuses."""
        return checks.RUNS_PER_PAGE * checks.MAX_RUN_PAGES + checks.STATUSES_PER_PAGE

    def test_the_bound_covers_every_run_and_status(self) -> None:
        """The most runs read plus one page of statuses is a valid count."""
        self.assertEqual(outcome.MAX_UNSETTLED, self.most())

    def test_a_count_past_the_runs_alone_survives(self) -> None:
        """1,000 runs still going and a pending status are 1,001 (#181 review)."""
        self.assertEqual(outcome.unsettled_count(1_001), 1_001)
        self.assertIn("1001 check(s)", outcome.unsettled_at_selection(1_001))
        self.assertEqual(outcome.unsettled_count(self.most() + 1), 0)
        record = head_checks.HeadChecks("failing", defects=1_050, infrastructure=1_001)
        result = {"commits": [HEAD], "checks": record.to_json(HEAD)}
        self.assertEqual(outcome.defect_count(result), 1_050)
        self.assertEqual(outcome.infrastructure_count(result), 1_001)


if __name__ == "__main__":
    unittest.main()
