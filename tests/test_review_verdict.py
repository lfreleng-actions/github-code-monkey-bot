# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The Copilot review classifier, offline, against real review bodies."""

from __future__ import annotations

import re
import sys
import unittest
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
verdicts = import_module("review_verdict")

# Real review bodies, verbatim from organisation pull requests.
BODIES: dict[str, str] = {
    # gerrit-clone-action#304, review 5366612856
    "v2-green-clean": """<!-- ccr-overview-v2 -->

## Copilot review overview

### 🟢 Approval recommended

No unresolved issues were identified; follow the stated merge order.

**Review effort:** Balanced
**Findings:** None""",
    # test-rust-project#11, review 5464621166
    "v2-blue-clean": """<!-- ccr-overview-v2 -->

### 🔵 Needs a closer look

The first live run performs an irreversible crates.io publication and therefore warrants final human review.

**0 open findings**

🧠 **Review effort:** Balanced""",
    # dependamerge#539, review 5425770192
    "v2-blue-with-findings": """<!-- ccr-overview-v2 -->

## Copilot review overview

### 🔵 Needs a closer look

Signature-sensitive asynchronous recovery needs human validation, particularly the unresolved refusal-handling gap.

**Review effort:** Balanced
**Findings:** 1 <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture>

<details open>
<summary><strong>Open (1)</strong></summary>

- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Recheck merge refusal after approval recovery](#discussion_r4193156909) · New
</details>""",
    # github-code-monkey-bot#4, review 5454189313
    "v2-yellow-open-findings": """<!-- ccr-overview-v2 -->

### 🟡 Changes recommended

The shared-module template PR must land before these copied changes are merged.

<details open>
<summary><strong>1 open finding</strong></summary>

- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Land template PR before refreshing copied shared files](#discussion_r4216991778) · New
</details>

🧠 **Review effort:** Balanced

---

Give feedback about Copilot approvals in [this survey](https://survey.alchemer.com/s3/9011660/CCR-Public-Preview-Autoapprove-feedback-survey) to enter a drawing for a $150 gift card.""",
    # dependamerge#543, review 5407070987
    "v2-overview-yellow": """<!-- ccr-overview-v2 -->

## Copilot review overview

### 🟡 Changes recommended

An empty required environment input bypasses the intended secret gate.

**Review effort:** Balanced
**Findings:** 1 <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture>

<details open>
<summary><strong>Open (1)</strong></summary>

- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Reject empty environment input before running the action](#discussion_r4178370628) · New
</details>""",
    # gerrit-clone-action#263, review 5022960157
    "legacy-no-comments": """## Pull request overview

Copilot reviewed 2 out of 3 changed files in this pull request and generated no new comments.""",
    # gerrit-clone-action#267, review 5049400713
    "legacy-comments": """## Pull request overview

Copilot reviewed 9 out of 9 changed files in this pull request and generated 4 comments.""",
    # gerrit-clone-action#263, review 5022650710
    "legacy-suppressed": """## Pull request overview

Copilot reviewed 2 out of 3 changed files in this pull request and generated 1 comment.




<details>
<summary>Suppressed comments (1)</summary>

**.github/workflows/testing.yaml:241**
* This sync also builds the editable project under PEP 517 isolation, where `hatchling` and `hatch-vcs` may be resolved independently of `uv.lock`. Keeping those packages in the dev group does not make the isolated build environment consume their locked artifacts, so this path still permits unpinned downloads. Please make the build environment consume locked build tools or otherwise constrain and verify those artifacts.
```
          uv sync --locked
```
</details>""",
}


def without_suppressed(body: str) -> str:
    """The legacy suppressed body with its suppressed section removed."""
    return body[: body.index("<details>")].rstrip() + "\n"


def as_v2(body: str) -> str:
    """A v2 overview carrying the legacy body's suppressed section.

    No sampled v2 body carries suppressed comments, so this one is
    derived from the legacy case rather than invented.
    """
    section = body[body.index("<details>") :]
    return BODIES["v2-green-clean"] + "\n\n" + section


class RealBodiesTest(unittest.TestCase):
    """Each sampled body classifies as the organisation defines it."""

    CASES = (
        ("v2-green-clean", 0, "clean", verdicts.GREEN, 0),
        ("v2-blue-clean", 0, "clean", verdicts.BLUE, 0),
        ("v2-blue-with-findings", 1, "findings", verdicts.BLUE, 0),
        ("v2-yellow-open-findings", 1, "findings", verdicts.YELLOW, 0),
        ("v2-overview-yellow", 1, "findings", verdicts.YELLOW, 0),
        ("legacy-no-comments", 0, "findings", None, 0),
        ("legacy-comments", 4, "findings", None, 0),
        ("legacy-suppressed", 1, "findings", None, 1),
    )

    def test_sampled_bodies(self) -> None:
        """Verdict, assessment and counts for every real body."""
        for name, threads, verdict, assessment, suppressed in self.CASES:
            with self.subTest(body=name):
                review = verdicts.classify(BODIES[name], threads)
                self.assertEqual(review.verdict, verdict)
                self.assertEqual(review.assessment, assessment)
                self.assertEqual(review.unresolved_threads, threads)
                self.assertEqual(review.suppressed, suppressed)

    def test_legacy_bodies_have_no_assessment(self) -> None:
        """The legacy format is never clean, even with nothing unresolved."""
        for name in ("legacy-no-comments", "legacy-comments", "legacy-suppressed"):
            with self.subTest(body=name):
                review = verdicts.classify(BODIES[name], 0)
                self.assertEqual(review.verdict, "findings")
                self.assertIn("no assessment", review.reasons)

    def test_blue_counts_only_without_threads(self) -> None:
        """Blue with an unresolved thread is findings; with none, clean."""
        body = BODIES["v2-blue-with-findings"]
        self.assertEqual(verdicts.classify(body, 1).verdict, "findings")
        self.assertEqual(verdicts.classify(body, 0).verdict, "clean")

    def test_open_findings_come_from_threads_not_the_body(self) -> None:
        """A body saying "Open (1)" with every thread resolved is not a count."""
        review = verdicts.classify(BODIES["v2-overview-yellow"], 0)
        self.assertEqual(review.unresolved_threads, 0)
        self.assertEqual(review.verdict, "findings")
        self.assertEqual(review.reasons, ["changes recommended"])

    def test_yellow_is_findings_without_threads(self) -> None:
        """Changes recommended always means more work."""
        review = verdicts.classify(BODIES["v2-yellow-open-findings"], 0)
        self.assertEqual(review.verdict, "findings")

    def test_green_with_a_thread_is_findings(self) -> None:
        """An unresolved thread outweighs a green assessment."""
        review = verdicts.classify(BODIES["v2-green-clean"], 2)
        self.assertEqual(review.verdict, "findings")
        self.assertIn("2 unresolved review thread(s)", review.reasons)


class SuppressedTest(unittest.TestCase):
    """Suppressed findings are counted from either heading form."""

    def test_summary_form(self) -> None:
        """``<summary>Suppressed comments (N)</summary>`` counts its entries."""
        self.assertEqual(verdicts.suppressed_of(BODIES["legacy-suppressed"]), 1)

    def test_heading_form(self) -> None:
        """``### Suppressed comments (N)`` counts the same way."""
        body = BODIES["legacy-suppressed"].replace(
            "<details>\n<summary>Suppressed comments (1)</summary>",
            "### Suppressed comments (1)",
        )
        self.assertNotIn("<summary>", body)
        self.assertEqual(verdicts.suppressed_of(body), 1)

    def test_entries_are_counted(self) -> None:
        """Two ``**path:line**`` entries count two, whatever the header says."""
        body = BODIES["legacy-suppressed"]
        entry = "**.github/workflows/testing.yaml:241**"
        doubled = body.replace(entry, entry + "\n* Another.\n\n**a.py:3**", 1)
        self.assertEqual(verdicts.suppressed_of(doubled), 2)

    def test_stated_count_is_a_floor(self) -> None:
        """A stated count with no parsable entries still counts."""
        body = BODIES["legacy-suppressed"].replace(
            "**.github/workflows/testing.yaml:241**", "path"
        )
        self.assertEqual(verdicts.suppressed_of(body), 1)

    def test_v2_body_with_suppressed_is_findings(self) -> None:
        """A green v2 overview with a suppressed section is not clean."""
        review = verdicts.classify(as_v2(BODIES["legacy-suppressed"]), 0)
        self.assertEqual(review.assessment, verdicts.GREEN)
        self.assertEqual(review.suppressed, 1)
        self.assertEqual(review.verdict, "findings")
        self.assertEqual(review.reasons, ["1 suppressed finding(s)"])

    def test_without_the_section_nothing_is_suppressed(self) -> None:
        """Removing the section removes the count."""
        body = without_suppressed(BODIES["legacy-suppressed"])
        self.assertEqual(verdicts.suppressed_of(body), 0)


class UntrustedBodyTest(unittest.TestCase):
    """The body is bounded and read with fixed patterns alone."""

    def test_older_green_wording(self) -> None:
        """``Ready to approve`` is the older green heading."""
        review = verdicts.classify("### \U0001f7e2 Ready to approve\n", 0)
        self.assertEqual(review.verdict, "clean")
        self.assertEqual(review.assessment, verdicts.GREEN)

    def test_assessment_must_be_a_heading(self) -> None:
        """An emoji in prose or a deeper heading is not an assessment."""
        for body in (
            "Text \U0001f7e2 Approval recommended",
            "#### \U0001f7e2 Approval recommended",
            "```\n    ### \U0001f7e2 Approval recommended\n```",
        ):
            with self.subTest(body=body):
                self.assertIsNone(verdicts.classify(body, 0).assessment)

    def test_oversized_body_is_cut(self) -> None:
        """An assessment past the size bound is never read."""
        body = "x" * verdicts.MAX_BODY_CHARS + "\n### \U0001f7e2 Approval recommended"
        review = verdicts.classify(body, 0)
        self.assertIsNone(review.assessment)
        self.assertEqual(review.verdict, "findings")

    def test_record_holds_fixed_fields_alone(self) -> None:
        """Nothing from the body text reaches the record."""
        body = "### \U0001f535 ::error::@alice\n<script>"
        record = verdicts.classify(body, 0).to_json(review_id=5, commit_id="c" * 40)
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
        text = repr(record)
        for fragment in ("::error::", "@alice", "<script>"):
            self.assertNotIn(fragment, text)
        self.assertEqual(record["verdict"], "clean")

    def test_thread_count_must_be_a_count(self) -> None:
        """A negative or non-integer thread count is a caller error."""
        for value in (-1, True, 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verdicts.classify("", value)

    def test_pending_is_never_clean(self) -> None:
        """No review of the head is pending, with nothing counted."""
        record = verdicts.pending().to_json()
        self.assertEqual(record["verdict"], "pending")
        self.assertIsNone(record["assessment"])
        self.assertIsNone(record["unresolved_threads"])

    def test_reasons_are_fixed_text(self) -> None:
        """Every reason matches one of the fixed forms."""
        allowed = re.compile(
            r"^(no assessment|changes recommended"
            r"|\d+ unresolved review thread\(s\)|\d+ suppressed finding\(s\))$"
        )
        for name, body in BODIES.items():
            with self.subTest(body=name):
                for reason in verdicts.classify(body, 3).reasons:
                    self.assertRegex(reason, allowed)


if __name__ == "__main__":
    unittest.main()
