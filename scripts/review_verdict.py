# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Classify a Copilot review of a pull request head, offline.

The organisation's definition (AGENTS.md section 11.5): a review of
the head is **clean** when there is no unresolved review thread, no
suppressed finding in the overview, and the assessment is
``🟢 Approval recommended`` (``🟢 Ready to approve`` in older
reviews) or ``🔵 Needs a closer look``. ``🟡 Changes recommended``
is always **findings**, as is a body with no assessment at all,
which is what the older ``## Pull request overview`` format has. A
review that never arrived is **pending**.

The body is text from outside the pipeline. It is read to a bound,
with fixed patterns, after fenced code is removed, and the record
holds labels and counts alone, never the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_BODY_BYTES = 64 * 1024

# The assessment heading: an h3 at the start of a line (up to three
# spaces of indentation, as Markdown allows) opening with the colour.
ASSESSMENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "approval",
        re.compile(
            r"^ {0,3}###\s+🟢\s+(?:Approval recommended|Ready to approve)\b", re.M
        ),
    ),
    ("closer-look", re.compile(r"^ {0,3}###\s+🔵\s+Needs a closer look\b", re.M)),
    ("changes", re.compile(r"^ {0,3}###\s+🟡\s+Changes recommended\b", re.M)),
)
CLEAN_ASSESSMENTS = frozenset({"approval", "closer-look"})

# A fenced code block: an unindented (up to three spaces) run of three
# or more backticks or tildes, to the matching fence or the end.
FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[^\n]*$|\Z)", re.M | re.S
)

# Suppressed findings sit under a heading or a <summary>; each entry
# opens with the bold path and line.
SUPPRESSED_HEAD = re.compile(
    r"(?:^ {0,3}#{2,4}\s+|<summary>(?:<strong>)?)"
    r"Suppressed comments\s*\((\d+)\)",
    re.M | re.I,
)
SUPPRESSED_ENTRY = re.compile(r"^\s*\*\*[^*\n]+:\d+\*\*", re.M)
SUPPRESSED_END = re.compile(r"^ {0,3}#{1,4}\s|</details>", re.M)


@dataclass(frozen=True)
class Verdict:
    """What the review says about the head, in fixed terms."""

    verdict: str
    assessment: str | None
    unresolved_threads: int
    suppressed: int
    reasons: list[str] = field(default_factory=list)

    def to_json(
        self, *, review_id: int | None, commit_id: str | None
    ) -> dict[str, Any]:
        """The record result.json carries (DESIGN.md section 18.4)."""
        return {
            "verdict": self.verdict,
            "assessment": self.assessment,
            "unresolved_threads": self.unresolved_threads,
            "suppressed": self.suppressed,
            "reasons": list(self.reasons),
            "review_id": review_id,
            "commit_id": commit_id,
        }


def bounded(body: str | None) -> str:
    """The body as text, cut to the size the classifier reads."""
    if not isinstance(body, str):
        return ""
    return body.encode("utf-8")[:MAX_BODY_BYTES].decode("utf-8", "ignore")


def without_fences(text: str) -> str:
    """Drop fenced code: an example heading inside one is not a verdict."""
    return FENCE.sub("", text)


def assessment_of(text: str) -> str | None:
    """The first recognised assessment heading, or None."""
    for label, pattern in ASSESSMENTS:
        if pattern.search(text):
            return label
    return None


def suppressed_count(text: str) -> int:
    """Suppressed findings: the entries listed, or the stated count if larger."""
    total = 0
    for head in SUPPRESSED_HEAD.finditer(text):
        stated = int(head.group(1))
        rest = text[head.end() :]
        end = SUPPRESSED_END.search(rest)
        block = rest[: end.start()] if end else rest
        total += max(stated, len(SUPPRESSED_ENTRY.findall(block)))
    return total


def classify(body: str | None, unresolved_threads: int) -> Verdict:
    """Apply the section 11.5 definition to a review body and its threads."""
    if type(unresolved_threads) is not int or unresolved_threads < 0:
        raise ValueError("unresolved_threads must be a non-negative integer")
    text = without_fences(bounded(body))
    assessment = assessment_of(text)
    suppressed = suppressed_count(text)
    reasons: list[str] = []
    if unresolved_threads:
        plural = "s" if unresolved_threads != 1 else ""
        reasons.append(f"{unresolved_threads} unresolved review thread{plural}")
    if suppressed:
        plural = "s" if suppressed != 1 else ""
        reasons.append(f"{suppressed} suppressed finding{plural}")
    if assessment is None:
        reasons.append("no assessment")
    elif assessment not in CLEAN_ASSESSMENTS:
        reasons.append("assessment: changes recommended")
    verdict = "clean" if not reasons else "findings"
    return Verdict(verdict, assessment, unresolved_threads, suppressed, reasons)


def pending(reason: str) -> Verdict:
    """No review of the head arrived; never clean."""
    return Verdict("pending", None, 0, 0, [reason])
