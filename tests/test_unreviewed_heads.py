# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bot pull request heads with no Copilot review, in the summary and the report.

A head Copilot never reviewed, with nothing else outstanding, skips
by name (issue #169). The selection summary lists those heads under
their own heading, and the report repeats them from the select job's
file, which it validates before printing.
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
from types import ModuleType
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
reporting = import_module("proposal_report")
selection_outputs = import_module("selection_outputs")

ORG = "lfreleng-actions"
NO_REVIEW = "no Copilot review of the head"
REFUSED = f"{NO_REVIEW}; the review request was refused"


def heads() -> ModuleType:
    """Import the module inside each test, so its absence fails one test only."""
    return import_module("unreviewed_heads")


def skips() -> list[dict[str, str]]:
    """A scan's skipped issues: two unreviewed heads among other skips."""
    return [
        {"issue": f"{ORG}/alpha#41", "reason": NO_REVIEW},
        {"issue": f"{ORG}/beta#9", "reason": "checks still running"},
        {"issue": f"{ORG}/gamma#3", "reason": REFUSED},
    ]


def selection(skipped_issues: list[dict[str, str]]) -> dict[str, Any]:
    """A minimal selection document carrying ``skipped_issues``."""
    return {
        "mode": "pull-requests",
        "dry_run": False,
        "model": "m",
        "max_pull_requests": 1,
        "categories": ["bugs"],
        "candidates_seen": 0,
        "skipped": {"revision": len(skipped_issues)},
        "skipped_issues": skipped_issues,
        "issues": [],
        "exclusions": [],
    }


class ReasonTest(unittest.TestCase):
    """The skip's reason, from the latest footer's review token."""

    def test_reason_names_a_refused_request(self) -> None:
        """Only ``pending/not-requested`` adds the refusal."""
        self.assertEqual(heads().reason(None), NO_REVIEW)
        self.assertEqual(heads().reason("pending"), NO_REVIEW)
        self.assertEqual(heads().reason("clean"), NO_REVIEW)
        self.assertEqual(heads().reason("pending/not-requested"), REFUSED)

    def test_entries_keep_the_unreviewed_skips_in_order(self) -> None:
        """Every other skip is left out."""
        self.assertEqual(
            heads().entries(skips()),
            [
                {"issue": f"{ORG}/alpha#41", "reason": NO_REVIEW},
                {"issue": f"{ORG}/gamma#3", "reason": REFUSED},
            ],
        )


class SelectionSummaryTest(unittest.TestCase):
    """The step summary lists the heads under their own heading, with a count."""

    def test_summary_counts_and_lists_the_heads(self) -> None:
        """The heading carries both counts; each head is a row."""
        text = selection_outputs.summary_markdown(selection(skips()))
        self.assertIn(
            "### Heads with no Copilot review: 2 (1 with the request refused)", text
        )
        self.assertIn(f"| {ORG}/alpha#41 | no review yet |", text)
        self.assertIn(f"| {ORG}/gamma#3 | review request refused |", text)

    def test_summary_without_heads_has_no_heading(self) -> None:
        """A run with none says nothing of them."""
        text = selection_outputs.summary_markdown(selection([]))
        self.assertNotIn("no Copilot review", text)

    def test_write_outputs_writes_the_heads_for_the_report(self) -> None:
        """The select job reads them from a file of their own."""
        with tempfile.TemporaryDirectory() as work:
            out = Path(work)
            selection_outputs.write_outputs(out, selection(skips()), b"")
            written = json.loads(
                (out / "unreviewed-heads.json").read_text(encoding="utf-8")
            )
        self.assertEqual(written, heads().entries(skips()))


class ReportTest(unittest.TestCase):
    """``publish.py report`` validates ``--unreviewed-heads``, then lists."""

    def setUp(self) -> None:
        """An empty results tree in a scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        (self.root / "results").mkdir()

    def report(
        self, raw: str | None, path: Path | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Run the report with ``raw`` as the select job's file."""
        if raw is not None:
            path = self.root / "unreviewed-heads.json"
            path.write_text(raw, encoding="utf-8")
        args = argparse.Namespace(
            results=self.root / "results",
            output_md=self.root / "report.md",
            output_json=self.root / "report.json",
            org=ORG,
            unreviewed_heads=path,
        )
        with redirect_stdout(io.StringIO()):
            reporting.run_report(args)
        markdown = (self.root / "report.md").read_text(encoding="utf-8")
        document = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        return markdown, document

    def test_report_lists_the_heads(self) -> None:
        """Valid entries are listed and recorded in report.json."""
        markdown, document = self.report(json.dumps(heads().entries(skips())))
        self.assertIn(
            "### Heads with no Copilot review: 2 (1 with the request refused)",
            markdown,
        )
        self.assertIn(f"| {ORG}/gamma#3 | review request refused |", markdown)
        self.assertEqual(document["unreviewed_heads"], heads().entries(skips()))

    def test_report_refuses_what_it_cannot_vouch_for(self) -> None:
        """Another owner, an unknown reason or a bad shape is counted, not printed."""
        raw = json.dumps(
            [
                {"issue": "other/x#1", "reason": NO_REVIEW},
                {"issue": f"{ORG}/a#2", "reason": "| injected |"},
                {"issue": f"{ORG}/a#3\n## heading", "reason": NO_REVIEW},
                "text",
                {"issue": f"{ORG}/a#5", "reason": []},
                {"issue": f"{ORG}/a#6", "reason": {}},
                {"issue": f"{ORG}/ok#4", "reason": NO_REVIEW},
            ]
        )
        markdown, document = self.report(raw)
        self.assertEqual(
            document["unreviewed_heads"],
            [{"issue": f"{ORG}/ok#4", "reason": NO_REVIEW}],
        )
        self.assertNotIn("other/x", markdown)
        self.assertNotIn("injected", markdown)
        self.assertNotIn("## heading", markdown)
        self.assertIn("6 unreviewed-head entries failed validation", markdown)

    def test_report_without_heads_says_nothing_of_them(self) -> None:
        """No file, an empty one or an empty array adds no heading."""
        for raw in (None, "", "[]"):
            with self.subTest(raw=raw):
                markdown, _ = self.report(raw)
                self.assertNotIn("no Copilot review", markdown)

    def test_an_unreadable_file_is_counted(self) -> None:
        """A path the report cannot read costs the list, not the report."""
        markdown, document = self.report(None, self.root / "missing.json")
        self.assertEqual(document["unreviewed_heads"], [])
        self.assertIn("1 unreviewed-head entry failed validation", markdown)


if __name__ == "__main__":
    unittest.main()
