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
# spaces of indentation, as Markdown allows) opening with the colour,
# all on one line.
ASSESSMENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "approval",
        re.compile(
            r"^ {0,3}###[ \t]+🟢[ \t]+(?:Approval recommended|Ready to approve)\b",
            re.M,
        ),
    ),
    (
        "closer-look",
        re.compile(r"^ {0,3}###[ \t]+🔵[ \t]+Needs a closer look\b", re.M),
    ),
    ("changes", re.compile(r"^ {0,3}###[ \t]+🟡[ \t]+Changes recommended\b", re.M)),
)
CLEAN_ASSESSMENTS = frozenset({"approval", "closer-look"})

# A fenced code block opens with an unindented (up to three spaces)
# run of three or more backticks or tildes; a backtick opener's info
# string holds no backtick, as Markdown requires, so a line of inline
# code opens nothing. It closes on a run of the same character at
# least as long with nothing but whitespace after it.
FENCE_OPEN = re.compile(r"^ {0,3}(?:(`{3,})[^`]*|(~{3,}).*)$")
# Inline code: a run of backticks closed by a run of the same length.
BACKTICKS = re.compile(r"`+")
COMMENT_OPEN = "<!--"
COMMENT_CLOSE = "-->"
# Digits beyond this are no count GitHub would write.
MAX_COUNT_DIGITS = 9

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


def bounded(body: str | None) -> tuple[str, bool]:
    """The body as text, and whether it exceeded the size the classifier reads."""
    if not isinstance(body, str):
        return "", False
    raw = body.encode("utf-8")
    if len(raw) <= MAX_BODY_BYTES:
        return body, False
    return raw[:MAX_BODY_BYTES].decode("utf-8", "ignore"), True


def fence_closes(line: str, char: str, length: int) -> bool:
    """Whether a line closes an open fence of ``char`` repeated ``length`` times."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    run = len(stripped) - len(stripped.lstrip(char))
    return run >= length and stripped[run:].strip(" \t") == ""


def visible_span(line: str, start: int, in_comment: bool) -> tuple[str, bool]:
    """The part of one line a reader sees, tracking inline code and comments.

    Inline code is kept whole, so a comment opener inside it is prose;
    a comment hides to its close, on this line or a later one.
    """
    out: list[str] = []
    i = start
    while i < len(line):
        if in_comment:
            close = line.find(COMMENT_CLOSE, i)
            if close < 0:
                return "".join(out), True
            i = close + len(COMMENT_CLOSE)
            in_comment = False
            continue
        ticks = BACKTICKS.search(line, i)
        comment = line.find(COMMENT_OPEN, i)
        if ticks is None and comment < 0:
            out.append(line[i:])
            break
        if ticks is not None and (comment < 0 or ticks.start() < comment):
            run = ticks.group()
            close = line.find(run, ticks.end())
            while close >= 0 and (
                (close + len(run) < len(line) and line[close + len(run)] == "`")
                or (close > 0 and line[close - 1] == "`")
            ):
                close = line.find(run, close + 1)
            if close < 0:
                out.append(line[i : ticks.end()])
                i = ticks.end()
            else:
                out.append(line[i : close + len(run)])
                i = close + len(run)
            continue
        out.append(line[i:comment])
        i = comment + len(COMMENT_OPEN)
        in_comment = True
    return "".join(out), in_comment


def visible(text: str) -> str:
    """The body as a reader sees it: no fenced code, no HTML comments.

    One pass in document order, so a fence opener inside a comment
    opens nothing, a comment opener inside inline code opens nothing,
    and an example heading inside either is no verdict.
    """
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for line in text.split("\n"):
        if fence is not None:
            if fence_closes(line, *fence):
                fence = None
            continue
        if not in_comment:
            opened = FENCE_OPEN.match(line)
            if opened:
                run = opened.group(1) or opened.group(2)
                fence = (run[0], len(run))
                continue
        shown, in_comment = visible_span(line, 0, in_comment)
        lines.append(shown)
    return "\n".join(lines)


def assessment_of(text: str) -> str | None:
    """The earliest recognised assessment heading in the body, or None."""
    earliest: tuple[int, str] | None = None
    for label, pattern in ASSESSMENTS:
        found = pattern.search(text)
        if found and (earliest is None or found.start() < earliest[0]):
            earliest = (found.start(), label)
    return earliest[1] if earliest else None


def suppressed_count(text: str) -> tuple[int, bool]:
    """Suppressed findings: the entries listed, or the stated count if larger.

    The second value says a stated count was unreadable, which is a
    finding in itself rather than a crash.
    """
    total = 0
    unreadable = False
    for head in SUPPRESSED_HEAD.finditer(text):
        digits = head.group(1)
        rest = text[head.end() :]
        end = SUPPRESSED_END.search(rest)
        block = rest[: end.start()] if end else rest
        listed = len(SUPPRESSED_ENTRY.findall(block))
        if len(digits) > MAX_COUNT_DIGITS:
            unreadable = True
            total += listed
            continue
        total += max(int(digits), listed)
    return total, unreadable


def classify(body: str | None, unresolved_threads: int) -> Verdict:
    """Apply the section 11.5 definition to a review body and its threads."""
    if type(unresolved_threads) is not int or unresolved_threads < 0:
        raise ValueError("unresolved_threads must be a non-negative integer")
    text, oversized = bounded(body)
    # One line-ending convention, so a CRLF body reads as its LF twin.
    text = visible(text.replace("\r\n", "\n").replace("\r", "\n"))
    assessment = assessment_of(text)
    suppressed, unreadable = suppressed_count(text)
    reasons: list[str] = []
    if unreadable:
        reasons.append("suppressed count unreadable")
    if oversized:
        # The part past the cap may hold suppressed findings; a body
        # the classifier cannot read whole is never clean.
        reasons.append(f"review body over {MAX_BODY_BYTES} bytes")
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
