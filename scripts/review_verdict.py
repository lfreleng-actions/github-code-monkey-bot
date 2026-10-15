# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Classify a Copilot review of a pull request head as the organisation does.

A pure function: it reads the review body and the count of unresolved
review threads, and returns a verdict with counts. No network, no
clock. The organisation guidance (``AGENTS.md`` section 11.5) defines
the verdict:

- ``clean`` when no review thread is unresolved, the body carries no
  suppressed findings, and its assessment is green or blue;
- ``findings`` otherwise, including a body with no recognised
  assessment, which section 11.5 requires;
- ``pending`` when no review of the head arrived at all.

The body is untrusted text. It is cut to a fixed size and read with
fixed patterns alone; nothing from it is copied into the verdict
beyond the fixed labels and counts below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast

MAX_BODY_CHARS = 64 * 1024
MAX_COUNT = 10_000

CLEAN = "clean"
FINDINGS = "findings"
PENDING = "pending"
VERDICTS = (CLEAN, FINDINGS, PENDING)

GREEN = "approval recommended"
BLUE = "needs a closer look"
YELLOW = "changes recommended"
ASSESSMENTS = {"\U0001f7e2": GREEN, "\U0001f535": BLUE, "\U0001f7e1": YELLOW}

NO_ASSESSMENT = "no assessment"

ASSESSMENT_RE = re.compile(
    r"^[ \t]{0,3}###[ \t]+(\U0001f7e2|\U0001f535|\U0001f7e1)", re.MULTILINE
)
# A heading or a <summary> that opens the suppressed section; GitHub
# writes both forms, with or without <strong> inside the summary.
SUPPRESSED_RE = re.compile(
    r"^[ \t]{0,3}(?:###[ \t]+|<summary>(?:<strong>)?[ \t]*)"
    r"Suppressed comments[ \t]*\((\d{1,5})\)",
    re.MULTILINE,
)
SUPPRESSED_END_RE = re.compile(r"^[ \t]{0,3}(?:</details>|#{1,3}[ \t])", re.MULTILINE)
ENTRY_RE = re.compile(r"^[ \t]{0,3}\*\*[^*\n]{1,512}:\d{1,7}\*\*[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Review:
    """What a Copilot review of one head commit says, in fixed fields."""

    verdict: str
    assessment: str | None = None
    unresolved_threads: int | None = None
    suppressed: int | None = None
    reasons: list[str] = field(default_factory=lambda: cast("list[str]", []))

    def to_json(
        self, review_id: int | None = None, commit_id: str | None = None
    ) -> dict[str, Any]:
        """The ``review`` record ``result.json`` carries (DESIGN.md 18.4)."""
        return {
            "verdict": self.verdict,
            "assessment": self.assessment,
            "unresolved_threads": self.unresolved_threads,
            "suppressed": self.suppressed,
            "reasons": list(self.reasons),
            "review_id": review_id,
            "commit_id": commit_id,
        }


def assessment_of(body: str) -> str | None:
    """The first green, blue or yellow ``###`` assessment heading, if any."""
    match = ASSESSMENT_RE.search(body)
    return ASSESSMENTS[match.group(1)] if match else None


def suppressed_of(body: str) -> int:
    """Count suppressed findings: one ``**path:line**`` entry per finding.

    Each suppressed section is read up to its ``</details>`` or the
    next heading. The larger of the stated count and the entries found
    counts, so a body that states findings it does not list is never
    read as having none.
    """
    total = 0
    for match in SUPPRESSED_RE.finditer(body):
        start = match.end()
        end = SUPPRESSED_END_RE.search(body, start)
        section = body[start : end.start() if end else len(body)]
        entries = len(ENTRY_RE.findall(section))
        total += max(int(match.group(1)), entries)
    return min(total, MAX_COUNT)


def classify(body: str, unresolved_threads: int) -> Review:
    """Classify one review of the head commit (AGENTS.md section 11.5)."""
    if type(unresolved_threads) is not int or unresolved_threads < 0:
        raise ValueError("unresolved_threads must be a non-negative integer")
    text = body[:MAX_BODY_CHARS]
    assessment = assessment_of(text)
    suppressed = suppressed_of(text)
    threads = min(unresolved_threads, MAX_COUNT)
    reasons: list[str] = []
    if assessment is None:
        reasons.append(NO_ASSESSMENT)
    elif assessment == YELLOW:
        reasons.append(YELLOW)
    if threads:
        reasons.append(f"{threads} unresolved review thread(s)")
    if suppressed:
        reasons.append(f"{suppressed} suppressed finding(s)")
    return Review(
        verdict=FINDINGS if reasons else CLEAN,
        assessment=assessment,
        unresolved_threads=threads,
        suppressed=suppressed,
        reasons=reasons,
    )


def pending() -> Review:
    """No review of the head arrived within the wait: never clean."""
    return Review(verdict=PENDING, reasons=["no Copilot review of the head arrived"])
