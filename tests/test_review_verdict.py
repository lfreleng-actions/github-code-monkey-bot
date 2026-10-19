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

    def test_every_real_body_is_readable(self) -> None:
        """No real body, the current format included, trips a fail-closed rule.

        The format is Copilot's and moves under us: the effort footer
        moved into <sub> and a first review carries an <a> promotion.
        A body the classifier cannot read reports findings, so each
        format drift turned every clean review into findings until
        this guard; it holds every fixture to a reading.
        """
        for name, body in BODIES.items():
            with self.subTest(name=name):
                verdict = verdicts.classify(body, 0)
                self.assertFalse(
                    [r for r in verdict.reasons if "classifier does not read" in r],
                    verdict.reasons,
                )
        current = verdicts.classify(BODIES["v2-blue-clean-current"], 0)
        self.assertEqual(current.verdict, "clean")
        self.assertEqual(current.assessment, "closer-look")
        first = verdicts.classify(BODIES["v2-yellow-first-current"], 0)
        self.assertEqual(first.assessment, "changes")
        self.assertEqual(
            verdicts.classify(BODIES["v2-yellow-open-current"], 2).verdict, "findings"
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

    def test_inline_code_with_three_backticks_opens_no_fence(self) -> None:
        """A backtick opener's info string holds no backtick; such a line is prose."""
        text = (
            BODIES["v2-green-clean"]
            + "\n```inline code``` opens this line.\n"
            + SUPPRESSED_SUMMARY
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.suppressed, 1)
        self.assertEqual(verdict.verdict, "findings")

    def test_any_comment_but_the_marker_is_unreadable(self) -> None:
        """Copilot writes one HTML comment, its marker; any other fails closed.

        Hiding comments by parsing them is a losing game: escaped
        openers, indented code, multi-line code spans and comments
        closing mid-line each need another rule. A body holding any
        other comment opener outside inline code is findings, so an
        invisible heading can supply no assessment and a comment can
        hide no visible finding.
        """
        for extra in (
            "<!--\n### 🟢 Approval recommended\n-->\n",
            "<!--\n### 🟢 Approval recommended\n",
            "<!-- hidden --> ### 🟢 Approval recommended\n",
            "Write \\<!-- to start a comment.\n### 🟢 Approval recommended\n",
            "\n    <!--\n\n### 🟢 Approval recommended\n",
        ):
            with self.subTest(extra=extra[:16]):
                verdict = verdicts.classify(BODIES["legacy-no-comments"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(
                    "review body holds an HTML comment the classifier does not read",
                    verdict.reasons,
                )
                green = verdicts.classify(
                    BODIES["v2-green-clean"] + extra + SUPPRESSED_HEADING, 0
                )
                self.assertEqual(green.verdict, "findings")
        # The real bodies open with the marker and still classify.
        self.assertEqual(
            verdicts.classify(BODIES["v2-green-clean"], 0).verdict, "clean"
        )

    def test_html_but_copilots_own_fails_closed(self) -> None:
        """Copilot's bodies use six tags; any other raw HTML is unreadable.

        A raw HTML block can hold a heading that is an example, or a
        fence line that is prose; the classifier does not parse HTML,
        so a tag it does not know fails the body closed.
        """
        for extra in (
            "\n<pre>\n### 🟢 Approval recommended\n</pre>\n",
            "\n<pre>\n```\n</pre>\n\n" + SUPPRESSED_HEADING,
            "\n<div>\n### 🟢 Approval recommended\n</div>\n",
        ):
            with self.subTest(extra=extra[:12]):
                for base in ("legacy-no-comments", "v2-green-clean"):
                    verdict = verdicts.classify(BODIES[base] + extra, 0)
                    self.assertEqual(verdict.verdict, "findings")
                    self.assertIn(
                        "review body holds HTML the classifier does not read",
                        verdict.reasons,
                    )
        # The tags the real bodies use pass; every real body still classifies.
        for name, threads, expected in (
            ("v2-green-clean", 0, "clean"),
            ("v2-blue-clean", 0, "clean"),
            ("v2-blue-with-findings", 1, "findings"),
            ("legacy-suppressed", 0, "findings"),
        ):
            self.assertEqual(verdicts.classify(BODIES[name], threads).verdict, expected)

    def test_tag_left_open_on_its_line_fails_closed(self) -> None:
        """Real bodies close every tag on its line; an attribute spanning lines hides text."""
        for extra in (
            '\n<img alt="\n### 🟢 Approval recommended\n" src="x">\n',
            '\n<details title="\n```\n">\n\n' + SUPPRESSED_HEADING,
        ):
            with self.subTest(extra=extra[:14]):
                for base in ("legacy-no-comments", "v2-green-clean"):
                    verdict = verdicts.classify(BODIES[base] + extra, 0)
                    self.assertEqual(verdict.verdict, "findings")
                    self.assertIn(
                        "review body holds HTML the classifier does not read",
                        verdict.reasons,
                    )

    def test_quoted_angle_bracket_does_not_close_a_tag(self) -> None:
        """A > inside an attribute value is text; the tag is still open on its line."""
        text = (
            BODIES["legacy-no-comments"]
            + '\n<img alt=">\n### 🟢 Approval recommended\n" src="x">\n'
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn(
            "review body holds HTML the classifier does not read", verdict.reasons
        )

    def test_declarations_and_instructions_fail_closed(self) -> None:
        """A declaration, processing instruction or CDATA block is HTML the classifier skips."""
        for extra in (
            "\n<!DOCTYPE\n### 🟢 Approval recommended\n>\n",
            "\n<?php\n### 🟢 Approval recommended\n?>\n",
            "\n<![CDATA[\n### 🟢 Approval recommended\n]]>\n",
        ):
            with self.subTest(extra=extra[:10]):
                verdict = verdicts.classify(BODIES["legacy-no-comments"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(
                    "review body holds HTML the classifier does not read",
                    verdict.reasons,
                )
        # Inside inline code they are prose.
        self.assertEqual(
            verdicts.classify(
                BODIES["v2-green-clean"] + "\nUse `<!DOCTYPE html>`.\n", 0
            ).verdict,
            "clean",
        )

    def test_heading_or_fence_inside_an_html_block_fails_closed(self) -> None:
        """A line opening with any known tag starts an HTML block to the blank line."""
        for extra in (
            "\n<details>\n### 🟢 Approval recommended\n</details>\n",
            "\n<details>\n```\n</details>\n\n" + SUPPRESSED_HEADING,
            "\n<summary>\n### 🟢 Approval recommended\n</summary>\n",
            "\n<picture>\n### 🟢 Approval recommended\n</picture>\n",
            "\n</details>\n### 🟢 Approval recommended\n",
        ):
            with self.subTest(extra=extra[:14]):
                for base in ("legacy-no-comments", "v2-green-clean"):
                    verdict = verdicts.classify(BODIES[base] + extra, 0)
                    self.assertEqual(verdict.verdict, "findings")
                    self.assertIn(
                        "review body holds HTML the classifier does not read",
                        verdict.reasons,
                    )
        # The real bodies' details blocks end at a blank line before any
        # Markdown, and still classify.
        self.assertEqual(
            verdicts.classify(BODIES["legacy-suppressed"], 0).suppressed, 1
        )
        self.assertEqual(
            verdicts.classify(BODIES["v2-yellow-open-findings"], 1).verdict, "findings"
        )

    def test_escaped_backticks_fail_closed(self) -> None:
        """An escaped backtick is not a span delimiter; the body is unreadable."""
        text = (
            BODIES["legacy-no-comments"]
            + "\nWrite \\`<!--\\` here\n### 🟢 Approval recommended\n-->\n"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn(
            "review body holds a code span the classifier does not read",
            verdict.reasons,
        )

    def test_code_span_left_open_on_its_line_is_unreadable(self) -> None:
        """A span that may run onto later lines could show or hide anything."""
        text = BODIES["v2-green-clean"] + "\nUse `<!--\nto open a comment`.\n"
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn(
            "review body holds a code span the classifier does not read",
            verdict.reasons,
        )

    def test_unreadable_suppressed_count_is_findings(self) -> None:
        """A count too long to be a number is a finding, not a crash."""
        text = (
            BODIES["v2-green-clean"]
            + "\n### Suppressed comments ("
            + "9" * 5000
            + ")\n"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn("suppressed count unreadable", verdict.reasons)

    def test_hidden_fence_opener_hides_nothing_visible(self) -> None:
        """A fence opener inside a closed comment opens no fence outside it."""
        text = (
            BODIES["v2-green-clean"]
            + "\n<!--\n```\n-->\n\nVisible again.\n"
            + SUPPRESSED_HEADING
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")

    def test_inline_code_opens_no_comment(self) -> None:
        """A comment opener inside inline code is prose, and hides nothing."""
        text = (
            BODIES["v2-green-clean"]
            + "\nUse `<!--` to open a comment.\n"
            + SUPPRESSED_HEADING
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.suppressed, 2)
        self.assertEqual(verdict.verdict, "findings")

    def test_inline_code_holds_no_html(self) -> None:
        """A tag inside a closed code span is prose; the same tag outside is HTML."""
        for prose in (
            "\nPrefer a `<list>` over a `<ul>` here.\n",
            "\nThe opener `<details` lacks its close.\n",
            '\nWrite ``<foo attr="a>b">`` as the tag.\n',
        ):
            with self.subTest(prose=prose.strip()):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + prose, 0)
                self.assertEqual(verdict.verdict, "clean")
                self.assertEqual(verdict.reasons, [])
        for html in (
            "\nPrefer a <list> here.\n",
            "\nA `<list>` and a <list>.\n",
            "\nAn open `<list> runs on.\n",
        ):
            with self.subTest(html=html.strip()):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + html, 0)
                self.assertEqual(verdict.verdict, "findings")

    def test_comment_closing_mid_line_fails_closed(self) -> None:
        """A comment closing mid-line is still a comment that is not the marker."""
        text = (
            BODIES["legacy-no-comments"]
            + "\n<!-- hidden --> ### 🟢 Approval recommended\n"
            + "<!-- a -->\n### 🟢 Approval recommended\n"
        )
        # Neither comment is the marker, so the body fails closed.
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")

    def test_fence_off_the_line_start_fails_closed(self) -> None:
        """A fence inside a list item or blockquote is one the classifier does not track."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            "\n- ~~~\n  example\n  ~~~\n" + SUPPRESSED_HEADING,
            "\n1. ```\n   example\n   ```\n" + SUPPRESSED_HEADING,
            "\n> ```\n> ### 🟢 Approval recommended\n> ```\n",
            "\nSee ``` here.\n",
            "\n    ```\n" + SUPPRESSED_HEADING,
        ):
            with self.subTest(extra=extra.strip()[:12]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # A fence run inside a closed code span is prose, as the real
        # bodies' fences at the line start are code.
        for prose in (
            "\nWrite ```` ``` ```` to open a fence.\n",
            "\n```\n- ~~~\n```\n",
        ):
            with self.subTest(prose=prose.strip()[:12]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + prose, 0)
                self.assertEqual(verdict.verdict, "clean")

    def test_html_block_inside_a_container_fails_closed(self) -> None:
        """A block tag opening a list item or a quote starts a block the guard missed."""
        reason = "review body holds HTML the classifier does not read"
        for extra in (
            "\n- <details>\n  ### \U0001f7e2 Approval recommended\n  </details>\n",
            "\n1. <summary>\n   ### \U0001f7e2 Approval recommended\n",
            "\n> <details>\n> ### \U0001f7e2 Approval recommended\n",
            "\n- </details>\n  ### \U0001f7e2 Approval recommended\n",
        ):
            with self.subTest(extra=extra.strip()[:14]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # The real bodies' list-item badges are inline HTML, and classify.
        self.assertEqual(
            verdicts.classify(BODIES["v2-blue-with-findings"], 0).reasons, []
        )
        self.assertEqual(
            verdicts.classify(
                BODIES["v2-green-clean"] + '\n- <picture><img src="x"></picture> ok\n',
                0,
            ).verdict,
            "clean",
        )

    def test_summary_tag_variants_fail_closed(self) -> None:
        """A summary or strong tag in any form but the bare one is unread HTML.

        The suppressed-count pattern reads the bare tags the real
        bodies write; a variant it would miss must not read as clean.
        """
        reason = "review body holds HTML the classifier does not read"
        for extra in (
            "\n<summary >Suppressed comments (1)</summary>\n",
            '\n<summary class="findings">Suppressed comments (1)</summary>\n',
            "\n<summary><strong id=s>Suppressed comments (1)</strong></summary>\n",
            "\n<summary\n>Suppressed comments (1)</summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:22]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # The bare forms, and <details open>, are the real bodies' and
        # still count their findings.
        self.assertEqual(
            verdicts.classify(BODIES["legacy-suppressed"], 0).suppressed, 1
        )
        text = (
            BODIES["v2-green-clean"]
            + "\n<details open>\n<summary><strong>Suppressed comments (2)</strong></summary>\n"
        )
        self.assertEqual(verdicts.classify(text, 0).suppressed, 2)

    def test_suppressed_heading_reads_through_whitespace(self) -> None:
        """Space or a newline between the tags and the label still counts."""
        for extra in (
            "\n<summary> Suppressed comments (1)</summary>\n",
            "\n<summary><strong>\nSuppressed comments (1)\n</strong></summary>\n",
            "\n<details>\n<summary>\t<strong> Suppressed comments (1) </strong></summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:24]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.suppressed, 1)
                self.assertEqual(verdict.verdict, "findings")

    def test_only_spaces_and_tabs_make_a_blank_line(self) -> None:
        """A line of Unicode whitespace ends no HTML block, as Markdown has it."""
        for blank in ("\u00a0", "\u2003", "\u3000", " \u00a0 "):
            with self.subTest(blank=repr(blank)):
                text = (
                    BODIES["v2-overview-yellow"]
                    + f"\n<details>\n{blank}\n### \U0001f7e2 Approval recommended\n</details>\n"
                )
                self.assertEqual(verdicts.classify(text, 0).verdict, "findings")
        # Spaces and tabs do end it, and the heading after them shows.
        text = (
            BODIES["legacy-no-comments"]
            + "\n<details>\n \t \n### \U0001f7e2 Approval recommended\n"
        )
        self.assertEqual(verdicts.classify(text, 0).assessment, "approval")

    def test_standalone_tag_in_a_container_fails_closed(self) -> None:
        """A known tag alone on a list item's line opens a block; a badge does not."""
        reason = "review body holds HTML the classifier does not read"
        for extra in (
            "\n- <picture>\n  ### \U0001f7e2 Approval recommended\n  </picture>\n",
            "\n- <img src=x>\n  ### \U0001f7e2 Approval recommended\n",
            "\n> <strong>\n> ### \U0001f7e2 Approval recommended\n",
            "\n1. <picture> \n   ### \U0001f7e2 Approval recommended\n",
        ):
            with self.subTest(extra=extra.strip()[:14]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # The real bodies' badges carry text after the tag on the line.
        self.assertEqual(
            verdicts.classify(BODIES["v2-blue-with-findings"], 0).reasons, []
        )
        text = (
            BODIES["v2-green-clean"] + '\n- <picture><img src="x"></picture> [x](u)\n'
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")

    def test_character_references_fail_closed(self) -> None:
        """An entity could spell a label or a tag the classifier does not read."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            "\n<summary>Suppressed&#32;comments (1)</summary>\n",
            "\n<summary>Suppressed comments&#x20;(1)</summary>\n",
            "\n&lt;details&gt; and more\n",
            "\nA &nbsp; here.\n",
        ):
            with self.subTest(extra=extra.strip()[:20]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # An ampersand that starts no reference is prose, as in a
        # literal "A & B" or a URL query string.
        text = (
            BODIES["v2-green-clean"] + "\nA & B, see https://x/y?a=1&b=2 and `&amp;`.\n"
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")

    def test_suppressed_label_reads_across_a_line_break(self) -> None:
        """A newline inside the label still counts the findings."""
        text = (
            BODIES["v2-green-clean"]
            + "\n<details>\n<summary>Suppressed\ncomments (1)</summary>\n"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.suppressed, 1)
        self.assertEqual(verdict.verdict, "findings")

    def test_escaped_parentheses_in_the_suppressed_heading_count(self) -> None:
        """Markdown shows an escaped parenthesis as itself; the count still reads."""
        for extra in (
            "\n### Suppressed comments \\(1\\)\n",
            "\n<summary><strong>Suppressed comments \\(2\\)</strong></summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:30]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertGreater(verdict.suppressed, 0)
                self.assertEqual(verdict.verdict, "findings")

    def test_emphasised_suppressed_heading_counts(self) -> None:
        """Emphasis around the heading's label leaves the count readable."""
        for marks in ("*", "**", "***", "_", "__"):
            extra = f"\n### {marks}Suppressed comments (1){marks}\n"
            with self.subTest(marks=marks):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.suppressed, 1)
                self.assertEqual(verdict.verdict, "findings")

    def test_inline_code_counts_no_findings(self) -> None:
        """A quoted example inside a code span is prose to the counter too."""
        for prose in (
            "\nWrite `<summary>Suppressed comments (1)</summary>` for the block.\n",
            "\nThe heading reads `### Suppressed comments (3)` in v1.\n",
            "\nNot ``### \U0001f7e1 Changes recommended`` here.\n",
        ):
            with self.subTest(prose=prose.strip()[:20]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + prose, 0)
                self.assertEqual(verdict.reasons, [])
                self.assertEqual(verdict.suppressed, 0)
                self.assertEqual(verdict.verdict, "clean")

    def test_a_masked_span_makes_no_heading(self) -> None:
        """Blanking a code span must not leave a heading indented into existence.

        Three spaces then a hash is a heading to Markdown's reader of
        the blanked line, where the writer's line, code then a hash,
        is prose.
        """
        base = "<!-- ccr-overview-v2 -->\n\nSome prose.\n"
        self.assertEqual(verdicts.classify(base, 0).assessment, None)
        for prose in (
            "`x`### \U0001f7e2 Approval recommended\n",
            "`x` ## \U0001f7e2 Approval recommended\n",
        ):
            with self.subTest(prose=prose[:16]):
                verdict = verdicts.classify(base + prose, 0)
                self.assertIsNone(verdict.assessment)
                self.assertEqual(verdict.verdict, "findings")
        counted = verdicts.classify(
            BODIES["v2-green-clean"] + "\n`xy`### Suppressed comments (1)\n", 0
        )
        self.assertEqual(counted.suppressed, 0)
        self.assertEqual(counted.verdict, "clean")

    def test_a_lone_tag_with_a_quoted_bracket_still_fails_closed(self) -> None:
        """A > inside a quoted attribute does not make the lone tag inline."""
        for extra in (
            '\n- <picture title=">">\n  ### \U0001f7e2 Approval recommended\n  </picture>\n',
            "\n- <picture title='>'>\n  ### \U0001f7e2 Approval recommended\n",
            "\n> <a href='x>y'>\n> ### \U0001f7e2 Approval recommended\n",
        ):
            with self.subTest(extra=extra.strip()[:22]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(
                    "review body holds HTML the classifier does not read",
                    verdict.reasons,
                )

    def test_suppressed_label_emphasised_alone_counts(self) -> None:
        """Emphasis or strong around the label alone, count outside, still counts."""
        for extra in (
            "\n### **Suppressed comments** (1)\n",
            "\n### _Suppressed comments_ (2)\n",
            "\n<summary><strong>Suppressed comments</strong> (1)</summary>\n",
            "\n<summary><strong>Suppressed comments</strong>(3)</summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:30]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertGreater(verdict.suppressed, 0)
                self.assertEqual(verdict.verdict, "findings")

    def test_backticks_inside_an_html_block_fail_closed(self) -> None:
        """Inside raw HTML a backtick is text, so a span masks nothing there."""
        reason = "review body holds HTML the classifier does not read"
        for extra in (
            "\n<details>\n`<summary>Suppressed comments (1)</summary>`\n",
            "\n<details>\n`<!--`\n### \U0001f7e2 Approval recommended\n-->\n",
            "\n<details>`x`\n### \U0001f7e2 Approval recommended\n",
        ):
            with self.subTest(extra=extra.strip()[:22]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)

    def test_escaped_bracket_in_a_reference_label_fails_closed(self) -> None:
        """A label may escape a bracket; the definition is one all the same."""
        for extra in (
            '\n[example\\]label]: /target "\n### \U0001f7e2 Approval recommended\n"\n',
            '\n[a\\\\]: /t "\n### \U0001f7e2 Approval recommended\n"\n',
        ):
            with self.subTest(extra=extra.strip()[:18]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(
                    "review body holds Markdown the classifier does not read",
                    verdict.reasons,
                )

    def test_a_fence_closed_under_list_indentation_fails_closed(self) -> None:
        """A closer indented past three spaces closes a fence the parser never ends."""
        text = (
            BODIES["v2-green-clean"]
            + "\n- Example:\n\n  ```\n  example\n    ```\n\n"
            + SUPPRESSED_HEADING
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn(
            "review body holds Markdown the classifier does not read", verdict.reasons
        )
        # A fence closed where it opened still drops its content whole.
        text = (
            BODIES["v2-green-clean"]
            + "\n```\n    ### \U0001f7e1 Changes recommended\n```\n"
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")

    def test_semicolonless_numeric_references_fail_closed(self) -> None:
        """A numeric reference renders without its semicolon; it fails closed too."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            "\n<details>\n<summary>Suppressed&#32comments (1)</summary>\n",
            "\nSuppressed comments (&#49)\n",
            "\nA &#x20here.\n",
        ):
            with self.subTest(extra=extra.strip()[:24]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)

    def test_reference_definition_in_a_container_fails_closed(self) -> None:
        """A definition opening a list item or quote, or indented, is one too."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            '\n- [ref]: /target "\n  ### \U0001f7e2 Approval recommended\n  "\n',
            '\n1. [ref]: /target "\n   ### \U0001f7e2 Approval recommended\n   "\n',
            '\n> [ref]: /target "\n> ### \U0001f7e2 Approval recommended\n"\n',
            '\n- a\n\n  [ref]: /target "\n  ### \U0001f7e2 Approval recommended\n  "\n',
        ):
            with self.subTest(extra=extra.strip()[:16]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)

    def test_an_indented_fence_fails_closed(self) -> None:
        """A fence opened off column zero may end with its list; the parser cannot tell."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            # The list ends the fence in Markdown; the parser would not.
            "\n- Example:\n\n  ```\n  example\n" + SUPPRESSED_HEADING,
            # A tab-indented closer closes under a list and nowhere else.
            "\n```\nexample\n\t```\n" + SUPPRESSED_HEADING,
            "\n ```\nexample\n```\n",
        ):
            with self.subTest(extra=extra.strip()[:16]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # The real bodies open every fence at column zero, and still read.
        self.assertEqual(
            verdicts.classify(BODIES["legacy-suppressed"], 0).suppressed, 1
        )

    def test_legacy_named_references_fail_closed(self) -> None:
        """A legacy entity name renders without its semicolon; it fails closed too."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            "\n<details>\n<summary>Suppressed&nbspcomments (1)</summary>\n",
            "\nA &amp B.\n",
            "\nSee &lt here.\n",
        ):
            with self.subTest(extra=extra.strip()[:24]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # A name no entity table knows, an ampersand in prose and a URL
        # query string are not references.
        text = BODIES["v2-green-clean"] + "\nA & B, &foo and https://x/y?a=1&b=2&x=3.\n"
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")

    def test_tag_attributes_hold_only_plain_values(self) -> None:
        """An attribute value with a backtick, quote or entity is not a real body's."""
        reason = "review body holds HTML the classifier does not read"
        for extra in (
            '\nSee <a title="`"><details><summary>Suppressed comments (1)</summary>'
            '</details>`">\n',
            "\n<a title='`'>x</a> and `<b>`\n",
            '\n<img src="x" alt="a&amp;b">\n',
            "\n<a href=unquoted>x</a>\n",
        ):
            with self.subTest(extra=extra.strip()[:20]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # A tag quoted inside a genuine code span stays prose.
        text = (
            BODIES["v2-green-clean"]
            + '\nWrite `<foo attr="a>b">` or `<a title="x">`.\n'
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")

    def test_an_indented_html_block_fails_closed(self) -> None:
        """A tag indented past three spaces opens a block the guard cannot place."""
        reason = "review body holds HTML the classifier does not read"
        for extra in (
            "\n- Example:\n\n    <details>\n  ### \U0001f7e2 Approval recommended\n  </details>\n",
            "\n\t<details>\n### \U0001f7e2 Approval recommended\n",
            "\n     <summary>x</summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:16]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)

    def test_attribute_backtick_inside_a_code_span_is_prose(self) -> None:
        """A tag quoted whole inside a span, backtick and all, is an example."""
        text = (
            BODIES["v2-green-clean"]
            + '\nWrite ``<a title="`">`` literally, or ``<x y="`z`">`` too.\n'
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.reasons, [])
        self.assertEqual(verdict.verdict, "clean")
        # The genuine attribute backtick, outside any span, still fails closed.
        text = (
            BODIES["v2-green-clean"]
            + '\nSee <a title="`"><details><summary>Suppressed comments (1)</summary>'
            '</details>`">\n'
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "findings")

    def test_a_bracket_left_open_on_its_line_fails_closed(self) -> None:
        """A reference label may span lines; a line opening one unclosed fails closed."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            '\n[exam\nple]: /target "\n### \U0001f7e2 Approval recommended\n"\n',
            '\n- [exam\n  ple]: /target "\n  ### \U0001f7e2 Approval recommended\n  "\n',
        ):
            with self.subTest(extra=extra.strip()[:10]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # A link, a task item or a bracket in prose closes on its line.
        text = BODIES["v2-green-clean"] + "\n- [x] done, see [the docs](u) and [a].\n"
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")

    def test_backtick_in_a_later_attribute_fails_closed(self) -> None:
        """The attribute check reads past clean values to the one with a backtick."""
        tag = (
            'See <a href="x" title="`"><details><summary>Suppressed comments (1)'
            '</summary></details>`">'
        )
        verdict = verdicts.classify(BODIES["v2-green-clean"] + "\n" + tag + "\n", 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn(
            "review body holds HTML the classifier does not read", verdict.reasons
        )
        # The same tag quoted whole inside a span is an example.
        quoted = BODIES["v2-green-clean"] + '\nWrite ``<a href="x" title="`">`` here.\n'
        self.assertEqual(verdicts.classify(quoted, 0).verdict, "clean")

    def test_whitespace_before_the_strong_close_counts(self) -> None:
        """Space or a newline before </strong> leaves the count readable."""
        for extra in (
            "\n<summary><strong>Suppressed comments </strong> (1)</summary>\n",
            "\n<summary><strong>Suppressed comments\n</strong> (1)</summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:40]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.suppressed, 1)
                self.assertEqual(verdict.verdict, "findings")

    def test_dressed_closing_tags_fail_closed(self) -> None:
        """A closing summary or strong tag with whitespace is as unread as an opener."""
        for extra in (
            "\n<summary><strong>Suppressed comments</strong > (1)</summary>\n",
            "\n<summary>Suppressed comments (1)</summary\t>\n",
        ):
            with self.subTest(extra=extra.strip()[:40]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(
                    "review body holds HTML the classifier does not read",
                    verdict.reasons,
                )

    def test_suppressed_heading_at_every_level(self) -> None:
        """Any heading level names the section, and any heading ends it."""
        for level in (1, 5, 6):
            extra = f"\n{'#' * level} Suppressed comments (1)\n\n**scripts/x.py:12**\n"
            with self.subTest(level=level):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.suppressed, 1)
                self.assertEqual(verdict.verdict, "findings")
        # Adjacent sections under deep headings count apart: the stated
        # count rules each, and the entries of the second are not the first's.
        text = (
            BODIES["v2-green-clean"]
            + "\n##### Suppressed comments (1)\n\n**a.py:1**\n"
            + "\n###### Other\n\n**b.py:2**\n**c.py:3**\n"
        )
        self.assertEqual(verdicts.classify(text, 0).suppressed, 1)

    def test_strong_tags_anywhere_in_the_suppressed_label_count(self) -> None:
        """Strong tags, emphasis and whitespace may sit anywhere in the label."""
        for extra in (
            "\n### <strong>Suppressed comments (1)</strong>\n",
            "\n<summary><strong>Suppressed</strong> comments (1)</summary>\n",
            "\n<summary>Suppressed <strong>comments</strong> (1)</summary>\n",
            "\n### <strong>**Suppressed**</strong> _comments_ <strong>(1)</strong>\n",
        ):
            with self.subTest(extra=extra.strip()[:40]):
                verdict = verdicts.classify(
                    BODIES["v2-green-clean"] + extra + "\n**scripts/x.py:12**\n", 0
                )
                self.assertEqual(verdict.suppressed, 1)
                self.assertEqual(verdict.verdict, "findings")

    def test_strong_tags_split_a_word_or_wrap_the_count(self) -> None:
        """Strong tags are stripped before the count is read, wherever they fall."""
        for extra in (
            "\n<summary>Suppre<strong>ssed</strong> comments (1)</summary>\n",
            "\n<summary>Suppressed comments (<strong>1</strong>)</summary>\n",
            "\n### Suppressed comments <strong>(</strong>2<strong>)</strong>\n",
        ):
            with self.subTest(extra=extra.strip()[:40]):
                verdict = verdicts.classify(
                    BODIES["v2-green-clean"] + extra + "\n**scripts/x.py:12**\n", 0
                )
                self.assertGreater(verdict.suppressed, 0)
                self.assertEqual(verdict.verdict, "findings")

    def test_an_unreadable_suppressed_section_is_findings(self) -> None:
        """A heading or summary naming suppressed findings in a form unread is findings.

        The label's formatting has no end of variants; one the count
        cannot read is a finding in itself rather than a clean review.
        """
        for extra in (
            "\n### Suppressed comments (**1**)\n",
            "\n<summary>Sup**pressed** comments (1)</summary>\n",
            "\n<summary><sub>Suppressed comments (1)</sub></summary>\n",
            '\n<summary><a href="https://x/y">Suppressed comments (1)</a></summary>\n',
            "\n### Suppressed comments\n",
            # A summary mid-line, or spanning lines, or long, is read whole.
            "\n<details><summary><sub>Suppressed comments (1)</sub></summary></details>\n",
            "\n<summary><sub>Suppressed comments (1)</sub>"
            + " x" * 300
            + "</summary>\n",
            "\n<summary>"
            + " " * 500
            + "<sub>Suppressed comments (1)</sub></summary>\n",
            "\n<summary>\nSuppressed comments (**1**)\n</summary>\n",
            "\n<summary>\n<sub>Suppressed comments (1)</sub>\n</summary>\n",
        ):
            with self.subTest(extra=extra.strip()[:40]):
                verdict = verdicts.classify(
                    BODIES["v2-green-clean"] + extra + "\n**scripts/x.py:12**\n", 0
                )
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn("suppressed count unreadable", verdict.reasons)
        # The word in prose, or quoted in code, is no section.
        text = (
            BODIES["v2-green-clean"]
            + "\nNothing was suppressed; see `### Suppressed comments (1)`.\n"
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")
        # A readable label across lines, or mid-line, counts and is not unread.
        for extra in (
            "\n<summary>\nSuppressed comments (1)\n</summary>\n",
            "\n<details><summary>Suppressed comments (1)</summary></details>\n",
        ):
            with self.subTest(extra=extra.strip()[:30]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + extra, 0)
                self.assertEqual(verdict.suppressed, 1)
                self.assertNotIn("suppressed count unreadable", verdict.reasons)

    def test_a_summary_inside_a_heading_line_is_still_read(self) -> None:
        """A heading's text does not hide the summary it carries."""
        text = (
            BODIES["v2-green-clean"]
            + "\n### Review notes <details><summary><sub>Suppressed comments (1)</sub>"
            "</summary></details>\n\n**x.py:1**\n"
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn("suppressed count unreadable", verdict.reasons)
        # A readable one in the same place counts.
        text = (
            BODIES["v2-green-clean"]
            + "\n### Notes <summary>Suppressed comments (2)</summary>\n"
        )
        self.assertEqual(verdicts.classify(text, 0).suppressed, 2)

    def test_setext_headings_fail_closed(self) -> None:
        """An underline makes the line above a heading the parser does not read."""
        reason = "review body holds Markdown the classifier does not read"
        for extra in (
            "\nSuppressed comments (1)\n===\n\n**x.py:1**\n",
            "\nSuppressed comments (1)\n---\n\n**x.py:1**\n",
            "\n\U0001f7e2 Approval recommended\n=\n",
            "\n  text\n  - - -\n",
        ):
            with self.subTest(extra=extra.strip()[:24]):
                verdict = verdicts.classify(BODIES["v2-overview-yellow"] + extra, 0)
                self.assertEqual(verdict.verdict, "findings")
                self.assertIn(reason, verdict.reasons)
        # A rule after a blank line, and a table's separator row, are
        # the real bodies' and stay prose.
        text = (
            BODIES["v2-green-clean"]
            + "\nA paragraph.\n\n---\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        )
        self.assertEqual(verdicts.classify(text, 0).verdict, "clean")
        self.assertEqual(
            verdicts.classify(BODIES["v2-yellow-first-current"], 0).reasons[:1],
            ["assessment: changes recommended"],
        )

    def test_a_long_hyphen_run_classifies_in_bounded_time(self) -> None:
        """A run of hyphens that is no underline must not stall the classifier."""
        import time

        text = BODIES["v2-green-clean"] + "\ntext\n" + "-" * 5000 + "x\n"
        started = time.monotonic()
        verdict = verdicts.classify(text, 0)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(verdict.verdict, "clean")

    def test_link_reference_definition_fails_closed(self) -> None:
        """A reference definition may swallow the lines after it as a title."""
        text = (
            BODIES["v2-overview-yellow"]
            + '\n[ref]: /url "\n### 🟢 Approval recommended\n"\n'
        )
        verdict = verdicts.classify(text, 0)
        self.assertEqual(verdict.verdict, "findings")
        self.assertIn(
            "review body holds Markdown the classifier does not read",
            verdict.reasons,
        )
        # Inside a closed code span, or as a link in a list, it is prose.
        for prose in (
            "\nUse `[ref]: /url` for a reference.\n",
            "\n- [A finding](https://x/y): fixed.\n",
        ):
            with self.subTest(prose=prose.strip()[:12]):
                verdict = verdicts.classify(BODIES["v2-green-clean"] + prose, 0)
                self.assertEqual(verdict.verdict, "clean")

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
