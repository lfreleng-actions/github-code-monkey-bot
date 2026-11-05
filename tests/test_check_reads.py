# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A revision's failing checks: which runs count, their annotations, the bounds."""

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
reads = import_module("issue_reads")

REPOSITORY = "org/alpha"
HEAD = "a" * 40


def run(
    identity: int,
    name: str,
    conclusion: str | None = "failure",
    *,
    status: str = "completed",
    annotations: int = 0,
    summary: str = "Job failed",
) -> dict[str, Any]:
    """A check run as the REST API returns it."""
    return {
        "id": identity,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": "github-actions"},
        "details_url": f"https://github.com/{REPOSITORY}/actions/runs/1/job/{identity}",
        "output": {
            "title": f"{name} failed",
            "summary": summary,
            "annotations_count": annotations,
        },
    }


def note(
    level: str, message: str = "Process completed with exit code 2."
) -> dict[str, Any]:
    """An annotation as the REST API returns it."""
    return {
        "annotation_level": level,
        "path": ".github",
        "start_line": 198,
        "message": message,
    }


class ChecksCase(unittest.TestCase):
    """The check run and annotation reads patched with fixtures."""

    def setUp(self) -> None:
        """No runs and no annotations until a test adds them."""
        self.runs: list[dict[str, Any]] = []
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.endpoints: list[str] = []
        patch.object(github, "api_object", side_effect=self.runs_page).start()
        patch.object(reads, "api_page", side_effect=self.notes_page).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def page_of(endpoint: str) -> tuple[int, int]:
        """The page number and size an endpoint asks for."""
        query = dict(part.split("=") for part in endpoint.split("?")[1].split("&"))
        return int(query["page"]), int(query["per_page"])

    def runs_page(self, endpoint: str) -> dict[str, Any]:
        """One page of the fixture runs."""
        self.endpoints.append(endpoint)
        page, size = self.page_of(endpoint)
        batch = self.runs[(page - 1) * size : page * size]
        return {"total_count": len(self.runs), "check_runs": batch}

    def notes_page(self, endpoint: str) -> list[dict[str, Any]]:
        """One page of a fixture run's annotations."""
        self.endpoints.append(endpoint)
        identity = int(endpoint.split("/check-runs/")[1].split("/")[0])
        page, size = self.page_of(endpoint)
        return self.notes.get(identity, [])[(page - 1) * size : page * size]

    def read(self) -> Any:
        """The packet for the fixture head."""
        return checks.failing_checks(REPOSITORY, HEAD)


class SelectionTest(ChecksCase):
    """Failing runs count; every other conclusion and state does not."""

    def test_no_check_runs(self) -> None:
        """A head with no runs carries an empty list and brings no round."""
        packet = self.read()
        self.assertEqual(packet.checks, [])
        self.assertEqual(packet.dropped, 0)
        self.assertEqual(packet.outstanding, 0)
        self.assertIn("filter=latest", self.endpoints[0])
        self.assertIn(f"commits/{HEAD}/check-runs", self.endpoints[0])

    def test_failing_conclusions_kept_others_ignored(self) -> None:
        """failure, timed_out, startup_failure count; the rest do not."""
        self.runs = [
            run(1, "fails", "failure"),
            run(2, "slow", "timed_out"),
            run(3, "boot", "startup_failure"),
            run(4, "ok", "success"),
            run(5, "meh", "neutral"),
            run(6, "skip", "skipped"),
            run(7, "stop", "cancelled"),
            run(8, "going", None, status="in_progress"),
            run(9, "queued", None, status="queued"),
        ]
        packet = self.read()
        self.assertEqual([c["name"] for c in packet.checks], ["boot", "fails", "slow"])
        self.assertEqual(packet.outstanding, 3)
        self.assertEqual(packet.dropped, 0)
        first = packet.checks[0]
        self.assertEqual(
            set(first),
            {
                "name",
                "conclusion",
                "app",
                "details_url",
                "title",
                "summary",
                "annotations",
            },
        )
        self.assertEqual(first["conclusion"], "startup_failure")
        self.assertEqual(first["app"], "github-actions")
        self.assertEqual(first["title"], "boot failed")
        # A run whose output counts no annotation costs no read.
        self.assertFalse(any("annotations" in e for e in self.endpoints))

    @unittest.expectedFailure
    def test_unfinished_runs_count_as_unsettled_and_nowhere_else(self) -> None:
        """Queued and in-progress runs are unsettled; a success counts nowhere."""
        self.runs = [
            run(1, "going", None, status="in_progress"),
            run(2, "queued", None, status="queued"),
            run(3, "ok", "success"),
        ]
        packet = self.read()
        self.assertEqual(packet.unsettled, 2)
        self.assertEqual(packet.checks, [])
        self.assertEqual(packet.outstanding, 0)
        self.assertEqual(packet.dropped, 0)
        self.runs = [run(3, "ok", "success")]
        self.assertEqual(self.read().unsettled, 0)

    def test_excluded_names_are_carried_but_not_outstanding(self) -> None:
        """The exclusion list keeps a run out of the count that brings a round."""
        self.runs = [run(1, "external"), run(2, "ours")]
        with patch.object(checks, "EXCLUDED", frozenset({"external"})):
            packet = self.read()
        self.assertEqual(len(packet.checks), 2)
        self.assertEqual(packet.outstanding, 1)

    def test_runs_are_paginated_and_fail_closed_past_the_bound(self) -> None:
        """Every page is read; a head beyond the page bound is an error."""
        self.runs = [run(i, f"r{i:03}", "success") for i in range(1, 150)]
        self.runs.append(run(150, "last"))
        self.assertEqual([c["name"] for c in self.read().checks], ["last"])
        self.runs = [
            run(i, f"r{i}", "success")
            for i in range(checks.RUNS_PER_PAGE * checks.MAX_RUN_PAGES + 1)
        ]
        with self.assertRaises(reads.SelectionError):
            self.read()

    def test_malformed_reply_is_an_error(self) -> None:
        """A reply without the runs array is not read as no runs."""
        with patch.object(github, "api_object", return_value={"total_count": 0}):
            with self.assertRaises(github.GitHubError):
                self.read()

    def test_non_object_run_is_an_error(self) -> None:
        """A null or scalar entry in the runs array is not read as no failure."""
        for entry in (None, "run", 1):
            reply = {"total_count": 1, "check_runs": [entry]}
            with patch.object(github, "api_object", return_value=reply):
                with self.assertRaises(github.GitHubError):
                    self.read()


class AnnotationTest(ChecksCase):
    """Failure and warning annotations are carried; notices are dropped."""

    def test_notices_dropped(self) -> None:
        """A notice is not a fault and costs no place in the packet."""
        self.runs = [run(1, "lint", annotations=3)]
        self.notes[1] = [note("notice"), note("warning", "w"), note("failure", "f")]
        packet = self.read()
        self.assertEqual(
            packet.checks[0]["annotations"],
            [
                {"level": "warning", "path": ".github", "line": 198, "message": "w"},
                {"level": "failure", "path": ".github", "line": 198, "message": "f"},
            ],
        )
        self.assertEqual(packet.dropped, 0)

    def test_annotation_bound_counts_the_rest(self) -> None:
        """At most ten per run; the rest count as dropped."""
        self.runs = [run(1, "lint", annotations=13)]
        self.notes[1] = [note("failure", str(i)) for i in range(13)]
        packet = self.read()
        self.assertEqual(len(packet.checks[0]["annotations"]), checks.MAX_ANNOTATIONS)
        self.assertEqual(packet.dropped, 3)

    def test_unread_annotations_count_as_dropped(self) -> None:
        """Past the page bound the unread ones count, whatever their level."""
        total = checks.ANNOTATIONS_PER_PAGE * checks.MAX_ANNOTATION_PAGES + 5
        self.runs = [run(1, "lint", annotations=total)]
        self.notes[1] = [note("notice") for _ in range(total)]
        packet = self.read()
        self.assertEqual(packet.checks[0]["annotations"], [])
        self.assertEqual(packet.dropped, 5)


class BoundsTest(ChecksCase):
    """Run count, text and total bytes bound the packet; each cut is counted."""

    def test_run_bound(self) -> None:
        """At most thirty failing runs; the rest count and still bring a round."""
        self.runs = [run(i, f"r{i:02}") for i in range(35)]
        packet = self.read()
        self.assertEqual(len(packet.checks), checks.MAX_CHECKS)
        self.assertEqual(packet.dropped, 5)
        self.assertEqual(packet.outstanding, 35)

    @unittest.expectedFailure
    def test_unsettled_count_is_whole_whatever_the_bounds_keep(self) -> None:
        """Every unfinished run counts, past the failing-run bound too."""
        self.runs = [run(i, f"r{i:02}") for i in range(35)]
        self.runs += [
            run(100 + i, f"q{i:02}", None, status="queued") for i in range(40)
        ]
        packet = self.read()
        self.assertEqual(len(packet.checks), checks.MAX_CHECKS)
        self.assertEqual(packet.unsettled, 40)
        self.assertEqual(packet.outstanding, 35)

    def test_message_and_summary_truncated_utf8_safe(self) -> None:
        """Two KiB per message and summary, never splitting a character."""
        long = "é" * checks.MAX_TEXT
        self.runs = [run(1, "lint", annotations=1, summary=long)]
        self.notes[1] = [note("failure", long)]
        entry = self.read().checks[0]
        for text in (entry["summary"], entry["annotations"][0]["message"]):
            self.assertLessEqual(len(text.encode("utf-8")), checks.MAX_TEXT)
            self.assertEqual(set(text), {"é"})

    def test_total_bound_trims_annotations_then_runs(self) -> None:
        """Past 32 KiB annotations go first, then whole runs, each counted."""
        big = "x" * checks.MAX_TEXT
        self.runs = [
            run(i, f"r{i:02}", annotations=10, summary=big) for i in range(1, 6)
        ]
        for i in range(1, 6):
            self.notes[i] = [note("failure", big) for _ in range(10)]
        packet = self.read()
        sizes = sum(checks.entry_size(c) for c in packet.checks)
        self.assertLessEqual(sizes, checks.MAX_CHECK_BYTES)
        lost_runs = 5 - len(packet.checks)
        carried = sum(len(c["annotations"]) for c in packet.checks)
        lost_notes = 10 * len(packet.checks) - carried
        self.assertGreater(lost_runs, 0)
        self.assertGreater(lost_notes, 0)
        # A carried run's lost annotations count each; a lost run, once.
        self.assertEqual(packet.dropped, lost_notes + lost_runs)
        self.assertEqual(packet.outstanding, 5)


if __name__ == "__main__":
    unittest.main()
