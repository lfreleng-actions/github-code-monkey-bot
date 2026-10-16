# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The Copilot review classifier follows the organisation definition.

AGENTS.md section 11.5: a review of the head is clean when there is
no unresolved thread, no suppressed finding in the overview, and the
assessment is green or blue. Anything else is findings; a review
that never arrived is pending. The bodies are real ones, copied
verbatim (``review_fixtures``), since the format varies.
"""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
verdicts = import_module("review_verdict")
BODIES = import_module("review_fixtures").BODIES

SUPPRESSED_SUMMARY = (
    "\n<details>\n<summary>Suppressed comments (1)</summary>\n\n"
    "**scripts/x.py:12**\n* Something a reviewer would want to know.\n"
    "</details>\n"
)
SUPPRESSED_HEADING = (
    "\n### Suppressed comments (2)\n\n"
    "**scripts/x.py:12**\n* One.\n\n**scripts/y.py:3**\n* Two.\n"
)


class RealBodiesTest(unittest.TestCase):
    """Every real body classifies as the maintainer expects."""

    def test_green_and_blue_without_threads_are_clean(self) -> None:
        """Approval, and a blue review with nothing outstanding, are clean."""
        for name in ("v2-green-clean", "v2-blue-clean"):
            with self.subTest(name=name):
                verdict = verdicts.classify(BODIES[name], unresolved_threads=0)
                self.assertEqual(verdict.verdict, "clean")
                self.assertEqual(verdict.suppressed, 0)
                self.assertEqual(verdict.reasons, [])
        self.assertEqual(
            verdicts.classify(BODIES["v2-green-clean"], 0).assessment, "approval"
        )
        self.assertEqual(
            verdicts.classify(BODIES["v2-blue-clean"], 0).assessment, "closer-look"
        )

    def test_threads_make_any_assessment_findings(self) -> None:
        """A blue review with a thread, and every yellow review, are findings."""
        for name, threads in (
            ("v2-blue-with-findings", 1),
            ("v2-yellow-open-findings", 1),
            ("v2-overview-yellow", 1),
        ):
            with self.subTest(name=name):
                verdict = verdicts.classify(BODIES[name], unresolved_threads=threads)
                self.assertEqual(verdict.verdict, "findings")
                self.assertEqual(verdict.unresolved_threads, threads)
                self.assertTrue(any("thread" in r for r in verdict.reasons))
        yellow = verdicts.classify(BODIES["v2-yellow-open-findings"], 0)
        self.assertEqual(yellow.verdict, "findings")
        self.assertEqual(yellow.assessment, "changes")
        self.assertTrue(any("changes" in r.lower() for r in yellow.reasons))

    def test_legacy_bodies_have_no_assessment(self) -> None:
        """The older overview format never counts as clean."""
        for name, threads in (("legacy-no-comments", 0), ("legacy-comments", 4)):
            with self.subTest(name=name):
                verdict = verdicts.classify(BODIES[name], unresolved_threads=threads)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIsNone(verdict.assessment)
                self.assertIn("no assessment", verdict.reasons)

    def test_legacy_suppressed_is_counted(self) -> None:
        """A suppressed finding under the summary form counts."""
        verdict = verdicts.classify(BODIES["legacy-suppressed"], unresolved_threads=0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertEqual(verdict.suppressed, 1)
        self.assertTrue(any("suppressed" in r for r in verdict.reasons))


class DerivedBodiesTest(unittest.TestCase):
    """Variants the real sample lacks, derived from the real bodies."""

    def test_v2_with_suppressed_summary_is_findings(self) -> None:
        """A green review loses clean to one suppressed finding."""
        verdict = verdicts.classify(BODIES["v2-green-clean"] + SUPPRESSED_SUMMARY, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertEqual(verdict.assessment, "approval")
        self.assertEqual(verdict.suppressed, 1)

    def test_suppressed_heading_form_is_counted(self) -> None:
        """The ``### Suppressed comments (N)`` heading form counts entries."""
        verdict = verdicts.classify(BODIES["v2-blue-clean"] + SUPPRESSED_HEADING, 0)
        self.assertEqual(verdict.suppressed, 2)
        self.assertEqual(verdict.verdict, "findings")

    def test_stated_count_is_a_floor(self) -> None:
        """When the heading claims more than the entries listed, believe the claim."""
        text = BODIES["v2-green-clean"] + SUPPRESSED_HEADING.replace("(2)", "(5)")
        self.assertEqual(verdicts.classify(text, 0).suppressed, 5)

    def test_older_approval_wording_is_clean(self) -> None:
        """``Ready to approve`` is the older green wording."""
        text = BODIES["v2-green-clean"].replace(
            "Approval recommended", "Ready to approve"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(verdict.assessment, "approval")

    def test_green_with_threads_is_findings(self) -> None:
        """Approval with unresolved threads is still work outstanding."""
        verdict = verdicts.classify(BODIES["v2-green-clean"], unresolved_threads=2)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn("2 unresolved review threads", verdict.reasons)


class UntrustedTextTest(unittest.TestCase):
    """The body is text from outside; it is bounded and read by fixed patterns."""

    def test_fenced_code_cannot_supply_the_assessment(self) -> None:
        """A heading inside a backtick or tilde fence is an example, not a verdict."""
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                text = (
                    BODIES["legacy-no-comments"]
                    + f"\n{fence}markdown\n### 🟢 Approval recommended\n{fence}\n"
                )
                verdict = verdicts.classify(text, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn("no assessment", verdict.reasons)

    def test_indented_heading_is_not_an_assessment(self) -> None:
        """Four leading spaces make a code block in Markdown, not a heading."""
        text = BODIES["legacy-no-comments"] + "\n    ### 🟢 Approval recommended\n"
        self.assertIn("no assessment", verdicts.classify(text, 0).reasons)

    def test_fenced_code_cannot_hide_suppressed_findings(self) -> None:
        """A fence around the suppressed block does not remove the findings."""
        text = (
            BODIES["v2-green-clean"]
            + "\n```\n"
            + SUPPRESSED_HEADING
            + "```\n"
            + SUPPRESSED_SUMMARY
        )
        self.assertGreaterEqual(verdicts.classify(text, 0).suppressed, 1)

    def test_empty_or_missing_body_is_findings(self) -> None:
        """No text means no assessment, never clean."""
        for body in ("", None):
            with self.subTest(body=body):
                verdict = verdicts.classify(body, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn("no assessment", verdict.reasons)

    def test_body_over_the_bound_is_findings(self) -> None:
        """A body the classifier cannot read whole is never clean.

        Reading the first 64 KiB alone could pass an assessment and
        miss suppressed findings behind it.
        """
        filler = "x" * (verdicts.MAX_BODY_BYTES + 10)
        for text in (
            filler + "\n### 🟢 Approval recommended\n",
            BODIES["v2-green-clean"] + filler + SUPPRESSED_SUMMARY,
        ):
            with self.subTest(text=text[:20]):
                verdict = verdicts.classify(text, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn("review body over 65536 bytes", verdict.reasons)
        self.assertEqual(
            verdicts.classify("x" * 10 + "\n### 🟢 Approval recommended\n", 0).verdict,
            "clean",
        )

    def test_closing_fence_takes_no_trailing_text(self) -> None:
        """A fence line with text after it opens or continues a block; it closes none."""
        text = (
            BODIES["legacy-no-comments"]
            + "\n```markdown\nAn example reads:\n```markdown\n"
            + "### 🟢 Approval recommended\n```\n"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn("no assessment", verdict.reasons)

    def test_longer_closing_fence_closes_the_block(self) -> None:
        """Markdown closes a fence with a run at least as long; findings after it count."""
        text = BODIES["v2-green-clean"] + "\n```\nexample\n````\n" + SUPPRESSED_SUMMARY
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.suppressed, 1)
        self.assertEqual(verdict.verdict, "findings")

    @unittest.expectedFailure
    def test_line_breaks_do_not_make_a_heading(self) -> None:
        """A heading is one line; a break inside it is not an assessment."""
        for text in (
            "###\n🟢 Approval recommended\n",
            "### 🟢\nApproval recommended\n",
        ):
            with self.subTest(text=text):
                verdict = verdicts.classify(text, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn("no assessment", verdict.reasons)

    @unittest.expectedFailure
    def test_crlf_bodies_classify_like_lf_bodies(self) -> None:
        """Windows line endings change nothing: the fence closes, the findings count."""
        text = (
            BODIES["v2-green-clean"] + "\n```\nexample\n```\n" + SUPPRESSED_SUMMARY
        ).replace("\n", "\r\n")
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.suppressed, 1)
        self.assertEqual(verdict.verdict, "findings")
        self.assertEqual(
            verdicts.classify(
                BODIES["v2-green-clean"].replace("\n", "\r\n"), 0
            ).verdict,
            "clean",
        )

    def test_earliest_heading_is_the_assessment(self) -> None:
        """With conflicting headings the first one in the body decides."""
        text = (
            "<!-- ccr-overview-v2 -->\n\n### 🟡 Changes recommended\n\nFix it.\n\n"
            "### 🟢 Approval recommended\n"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.assessment, "changes")
        self.assertEqual(verdict.verdict, "findings")

    def test_negative_or_odd_thread_counts_are_refused(self) -> None:
        """A thread count must be a non-negative integer."""
        for value in (-1, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verdicts.classify(BODIES["v2-green-clean"], value)  # type: ignore[arg-type]


class PendingAndRecordTest(unittest.TestCase):
    """A review that never arrived, and the record every verdict serialises to."""

    def test_pending_carries_its_reason(self) -> None:
        """Pending names why; it is never clean."""
        verdict = verdicts.pending("no review of the head within 15 minutes")
        self.assertEqual(verdict.verdict, "pending")
        self.assertIsNone(verdict.assessment)
        self.assertEqual(verdict.reasons, ["no review of the head within 15 minutes"])

    def test_record_shape(self) -> None:
        """The record holds fixed labels and counts, never body text."""
        verdict = verdicts.classify(BODIES["v2-blue-with-findings"], 1)
        record = verdict.to_json(review_id=5425770192, commit_id="a" * 40)
        self.assertEqual(
            set(record),
            {
                "verdict",
                "assessment",
                "unresolved_threads",
                "suppressed",
                "reasons",
                "review_id",
                "commit_id",
            },
        )
        self.assertEqual(record["verdict"], "findings")
        self.assertEqual(record["review_id"], 5425770192)
        self.assertNotIn("Signature-sensitive", str(record))


if __name__ == "__main__":
    unittest.main()
