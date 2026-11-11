# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Nominating published pull requests for an antagonistic review (issue #138)."""

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
antagonistic = import_module("antagonistic_request")
comments = import_module("issue_comment")
reporting = import_module("proposal_report")

ORG = "lfreleng-actions"
HEAD = "a" * 40
URL = f"https://github.com/{ORG}/sbom-action/pull/41"
REPOSITORY = "lfreleng-actions/github-code-review-bot"


def clean(**changes: Any) -> dict[str, Any]:
    """A live published proposal whose Copilot review of the head is clean."""
    item: dict[str, Any] = {
        "schema": 1,
        "key": "sbom-action-40",
        "repository": f"{ORG}/sbom-action",
        "issue": 40,
        "verdict": "proposed",
        "reasons": [],
        "dry_run": False,
        "mode": "pull-requests",
        "commits": [HEAD],
        "pull_request_url": URL,
        "mergeable": True,
        "review": {
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
        "checks": {
            "state": "passing",
            "commit_id": HEAD,
            "failing": [],
            "dropped": 0,
            "unsettled": 0,
            "read_at": "2026-11-06T00:00:00Z",
            "reason": None,
        },
    }
    item.update(changes)
    return item


def with_review(**changes: Any) -> dict[str, Any]:
    """A clean result whose review record differs as given."""
    item = clean()
    item["review"] = {**item["review"], **changes}
    return item


def with_checks(**changes: Any) -> dict[str, Any]:
    """A clean result whose checks record differs as given."""
    item = clean()
    item["checks"] = {**item["checks"], **changes}
    return item


class NominationRuleTest(unittest.TestCase):
    """Only a live, clean, mergeable proposal below the cap is nominated."""

    def test_clean_published_proposal_is_nominated(self) -> None:
        """A clean review of the published head, passing checks: nominated."""
        self.assertIsNone(antagonistic.unmet(clean()))
        for state in ("running", "none"):
            with self.subTest(checks=state):
                self.assertIsNone(antagonistic.unmet(with_checks(state=state)))
        self.assertIsNone(antagonistic.unmet(clean(mergeable=None)))

    def test_infrastructure_checks_are_nominated(self) -> None:
        """A re-run of a job the egress agent failed is no reason to wait (#165)."""
        agent = {"name": "lint", "kind": "infrastructure"}
        item = with_checks(state="infrastructure", failing=[agent])
        self.assertIsNone(antagonistic.unmet(item))

    def test_everything_else_is_not_nominated(self) -> None:
        """Findings, pending, absent, failing, conflicting and the rest."""
        failing = with_checks(state="failing", failing=[{"name": "lint"}])
        running_red = with_checks(state="running", failing=[{"name": "lint"}])
        cases = {
            "findings": with_review(verdict="findings", unresolved_threads=1),
            "pending": with_review(verdict="pending"),
            "absent": clean(review=None),
            "malformed review": with_review(extra=True),
            "older head": with_review(commit_id="b" * 40),
            "failing checks": failing,
            "running with failures": running_red,
            "unknown checks": with_checks(commit_id="b" * 40),
            "conflict": clean(mergeable=False),
            "handoff": clean(verdict="handoff"),
            "rejected": clean(verdict="rejected"),
            "publish failure": clean(verdict="publish-failed"),
            "dry run": clean(dry_run=True),
            "no pull request": clean(pull_request_url=None),
            "ceiling": clean(round=60, segment_round=1),
        }
        for name, item in cases.items():
            with self.subTest(case=name):
                self.assertIsNotNone(antagonistic.unmet(item))

    def test_segment_cap_with_a_clean_review_is_nominated(self) -> None:
        """At the segment cap a clean head is finished, and asks for the next round."""
        self.assertIsNone(
            antagonistic.unmet(clean(round=10, segment_round=10, max_rounds=10))
        )


class StateTest(unittest.TestCase):
    """The ``antagonistic`` record the publish job writes."""

    def test_states(self) -> None:
        """Off, dry-run, skipped with a reason, or requested."""
        state = antagonistic.state
        self.assertEqual(
            state(clean(), switch=False, repository=REPOSITORY)["state"], "off"
        )
        self.assertEqual(
            state(clean(dry_run=True), switch=True, repository=REPOSITORY)["state"],
            "dry-run",
        )
        self.assertEqual(
            state(clean(), switch=True, repository=REPOSITORY),
            {"state": "requested", "reason": None},
        )
        skipped = [
            state(clean(), switch=True, repository=""),
            state(clean(mode="branches"), switch=True, repository=REPOSITORY),
            state(clean(mergeable=False), switch=True, repository=REPOSITORY),
        ]
        for record in skipped:
            with self.subTest(record=record):
                self.assertEqual(record["state"], "skipped")
                self.assertTrue(record["reason"])

    def test_publish_command_records_the_state(self) -> None:
        """The command writes the record back into result.json."""
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "result.json"
            for switch, expected in (("true", "requested"), ("false", "off")):
                path.write_text(json.dumps(clean()), encoding="utf-8")
                with self.subTest(switch=switch), redirect_stdout(io.StringIO()):
                    antagonistic.main(
                        [
                            "--result",
                            str(path),
                            "--output",
                            str(path),
                            "--switch",
                            switch,
                            "--repository",
                            REPOSITORY,
                        ]
                    )
                    record = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(record["antagonistic"]["state"], expected)


class CommentTest(unittest.TestCase):
    """The issue comment says "requested" when nominated, nothing otherwise."""

    SENTENCE = "An antagonistic review was requested from the review bot."

    def test_requested_is_said(self) -> None:
        """A new pull request and a revision round both say so."""
        record = {"state": "requested", "reason": None}
        for item in (clean(antagonistic=record), clean(antagonistic=record, round=2)):
            with self.subTest(round=item.get("round")):
                body = comments.comment_body(item, "https://run")
                self.assertIsNotNone(body)
                self.assertTrue(str(body).endswith(self.SENTENCE))
                self.assertNotIn("reviewed by the review bot", str(body))

    def test_other_states_say_nothing(self) -> None:
        """Off, skipped, dry-run and an absent record add no sentence."""
        for record in (
            {"state": "off", "reason": None},
            {"state": "skipped", "reason": "x"},
            {"state": "dry-run", "reason": None},
            None,
        ):
            with self.subTest(record=record):
                body = comments.comment_body(clean(antagonistic=record), "")
                self.assertNotIn("antagonistic", str(body))


class ReportNominationTest(unittest.TestCase):
    """The report recomputes the nomination and outputs ``owner/repo#N``."""

    def test_report_outputs_the_nominees(self) -> None:
        """A clean result is named; the rest, and a stray link, are not."""
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            results = {
                "a": clean(key="a"),
                "b": clean(key="b", mergeable=False, pull_request_url=URL[:-2] + "42"),
                "c": clean(
                    key="c", pull_request_url="https://github.com/other/x/pull/1"
                ),
                # The recorded state is never trusted.
                "d": clean(
                    key="d",
                    pull_request_url=URL[:-2] + "43",
                    review=None,
                    antagonistic={"state": "requested", "reason": None},
                ),
            }
            for name, item in results.items():
                directory = root / "results" / name
                directory.mkdir(parents=True)
                (directory / "result.json").write_text(
                    json.dumps(item), encoding="utf-8"
                )
            output = root / "github_output"
            output.touch()
            args = argparse.Namespace(
                results=root / "results",
                output_md=root / "report.md",
                output_json=root / "report.json",
                org=ORG,
                github_output=output,
            )
            with redirect_stdout(io.StringIO()):
                reporting.run_report(args)
            lines = output.read_text(encoding="utf-8").splitlines()
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            markdown = (root / "report.md").read_text(encoding="utf-8")
        expected = [f"{ORG}/sbom-action#41"]
        self.assertIn(
            f"antagonistic_pull_requests={json.dumps(expected, separators=(',', ':'))}",
            lines,
        )
        self.assertEqual(report["antagonistic_pull_requests"], expected)
        self.assertIn("nominated for an antagonistic review", markdown)

    def test_nominee_shape(self) -> None:
        """Only a whole pull request URL converts."""
        self.assertEqual(antagonistic.nominee(URL), f"{ORG}/sbom-action#41")
        for bad in (URL + "?x", "https://github.com/o/r/issues/1", URL[:-2] + "0"):
            with self.subTest(url=bad):
                self.assertIsNone(antagonistic.nominee(bad))


if __name__ == "__main__":
    unittest.main()
