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

from review_text import (
    BLOCK_TAG,
    CONTAINER_PREFIX,
    INDENTED_FENCE,
    MARKDOWN_SHAPE,
    MARKER_LINE,
    UNREADABLE_HTML,
    UNREADABLE_MARKDOWN,
    read_line,
)

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
# Digits beyond this are no count GitHub would write.
MAX_COUNT_DIGITS = 9

# Suppressed findings sit under a heading or a <summary>; each entry
# opens with the bold path and line. The strong tags the real bodies
# use change nothing a reader sees and may fall anywhere, so they are
# stripped before the count is read; whitespace and emphasis may sit
# around and between the label's words.
STRONG_TAGS = re.compile(r"</?strong>", re.I)
LABEL_NOISE = r"(?:\s|[*_])"
SUPPRESSED_HEAD = re.compile(
    # One space after the hashes; the noise reads the rest, so the two
    # never split a run of whitespace between them.
    r"(?:^ {0,3}#{1,6}[ \t]|<summary>)"
    + LABEL_NOISE
    + r"*Suppressed"
    + LABEL_NOISE
    + r"+comments"
    + LABEL_NOISE
    + r"*\\?\((\d+)\\?\)",
    re.M | re.I,
)
# A heading or summary line that names suppressed findings in any
# form, letters alone considered: one SUPPRESSED_HEAD cannot read is
# a count unreadable, a finding in itself, since the label's
# formatting has no end of variants and a clean verdict must not
# rest on missing one.
# A heading reads to its line's end; a summary, wherever it starts,
# reads to its close across lines, or to the body's end, which the
# 64 KiB bound on the body already keeps short.
# Two scans, since a summary may sit inside a heading's line and one
# pattern would read the heading alone.
SUPPRESSED_MENTIONS = (
    re.compile(r"^ {0,3}#{1,6}[ \t](?P<text>.*)$", re.M),
    re.compile(r"<summary>(?P<text>[\s\S]*?)(?:</summary>|\Z)", re.I),
)
# A setext underline makes the line above a heading; the classifier
# reads ATX headings alone, so one fails the body closed. A rule after
# a blank line, as the real bodies write it, is no underline.
# One linear run each way: nested repetitions over the same character
# would backtrack exponentially on a long run that is no underline.
# Any indentation: a list continuation indents past three spaces.
SETEXT_UNDERLINE = re.compile(r"^[ \t]*(?:=+[ \t]*|-[ \t-]*)$")
LETTERS = re.compile(r"[^a-z]")
ANY_TAG_TEXT = re.compile(r"<[^<>]*>")
SUPPRESSED_LABEL = "suppressedcomments"
# Indentation of spaces and tabs alone: a class crossing newlines
# would rescan every blank line after the section once per line.
# An entry may sit under a list marker or a quote; the stated count is
# a floor, so each entry counts whatever the heading says.
SUPPRESSED_ENTRY = re.compile(
    r"^[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+|>[ \t]*)*\*\*[^*\n]+:\d+\*\*", re.M
)
# A heading ends the section; a space, a tab or the line's end alone
# follows its hashes, as Markdown has it, so hashes before other
# whitespace are prose and end nothing.
# A details closer ends it on a line of its own, as the real bodies
# write it; one mid-line is text or a title and ends nothing.
SUPPRESSED_END = re.compile(
    r"^ {0,3}#{1,6}(?:[ \t]|$)|^ {0,3}</details>[ \t]*$", re.M | re.I
)
INLINE_CLOSER = re.compile(r"\A(?:[ \t]*</summary>)?[ \t]*</details>", re.I)


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


def visible(text: str) -> tuple[str, list[str]]:
    """The body as a reader sees it, and the reasons it may not be.

    One pass in document order: fenced code is dropped whole, the
    format marker is dropped, and every other line is scanned for
    what the classifier will not read past.
    """
    lines: list[str] = []
    problems: set[str] = set()
    fence: tuple[str, int] | None = None
    in_html = False
    previous = ""
    in_summary = False
    for line in text.split("\n"):
        if in_summary and ("`" in line or "[" in line):
            # A summary open across lines is one summary: a span or a
            # link in it could hide part of its label, as on one line.
            problems.add(UNREADABLE_MARKDOWN)
        # Under a container prefix the underline reads the same, so
        # both lines are read with their prefix removed.
        bare = CONTAINER_PREFIX.sub("", line, count=1)
        if fence is None and previous.strip(" \t") and SETEXT_UNDERLINE.match(bare):
            problems.add(UNREADABLE_MARKDOWN)
        previous = CONTAINER_PREFIX.sub("", line, count=1)
        if fence is not None:
            if fence_closes(line, *fence):
                fence = None
            elif INDENTED_FENCE.match(line) and line.lstrip(" \t").startswith(
                fence[0] * fence[1]
            ):
                # A closer the parser cannot see as one: inside a list
                # it ends the fence, and what follows would be dropped
                # as code here, so the body fails closed.
                problems.add(UNREADABLE_MARKDOWN)
            continue
        if in_html:
            # Inside an HTML block a heading or fence is HTML text,
            # not Markdown; the classifier does not read it either way.
            if MARKDOWN_SHAPE.match(line):
                problems.add(UNREADABLE_HTML)
            # Markdown ends an HTML block at a line of spaces and tabs
            # alone; a line of other whitespace is text inside it.
            in_html = line.strip(" \t") != ""
            if in_html:
                lines.append(read_html_line(line, problems))
                in_summary = summary_open(lines[-1], in_summary, problems)
                continue
        if BLOCK_TAG.match(line):
            in_html = True
            lines.append(read_html_line(line, problems))
            in_summary = summary_open(lines[-1], in_summary, problems)
            continue
        opened = FENCE_OPEN.match(line)
        if opened:
            if in_summary:
                # A fence inside a summary would drop its label as
                # code; the real bodies fence nothing there.
                problems.add(UNREADABLE_MARKDOWN)
            if line[0] != opened.group(0).lstrip(" ")[0]:
                # Indented, the fence may belong to a list the parser
                # does not track and end with it; the real bodies open
                # every fence at column zero.
                problems.add(UNREADABLE_MARKDOWN)
            run = opened.group(1) or opened.group(2)
            fence = (run[0], len(run))
            continue
        if MARKER_LINE.match(line):
            continue
        lines.append(read_into(line, problems))
        in_summary = summary_open(lines[-1], in_summary, problems)
    return "\n".join(lines), sorted(problems)


def summary_open(masked: str, before: bool, problems: set[str]) -> bool:
    """Whether a <summary> stands open after this line, given its state before.

    A summary opened while one is open is nested, which the real
    bodies never write and the label scan would misread; it fails the
    body closed.
    """
    lower = masked.lower()
    depth = int(before)
    for opener in re.finditer(r"</?summary>", lower):
        if opener.group().startswith("</"):
            depth = max(depth - 1, 0)
        elif depth:
            problems.add(UNREADABLE_HTML)
        else:
            depth = 1
    return depth > 0


def read_html_line(line: str, problems: set[str]) -> str:
    """Read a line inside an HTML block, where a backtick is text, not a span.

    A span would mask a tag or a comment opener the block shows as
    written, so a backtick there fails the body closed; the line is
    otherwise read as any other.
    """
    if "`" in line:
        problems.add(UNREADABLE_HTML)
    return read_into(line, problems)


def read_into(line: str, problems: set[str]) -> str:
    """Read one line, adding its problems to ``problems``; return the readable text."""
    masked, found = read_line(line)
    problems |= found
    return masked


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
    text = STRONG_TAGS.sub("", text)
    readable = {head.start() for head in SUPPRESSED_HEAD.finditer(text)}
    for pattern in SUPPRESSED_MENTIONS:
        for mention in pattern.finditer(text):
            content = ANY_TAG_TEXT.sub("", mention.group("text"))
            letters = LETTERS.sub("", content.lower())
            if letters.startswith(SUPPRESSED_LABEL) and mention.start() not in readable:
                unreadable = True
    for head in SUPPRESSED_HEAD.finditer(text):
        digits = head.group(1)
        # Searched in place from the heading's end, so the line-start
        # anchor binds to real line starts and a heading's closing
        # hashes end no section.
        end = SUPPRESSED_END.search(text, head.end())
        block = text[head.end() : end.start() if end else len(text)]
        # A closer right after the summary, on its line, closes an
        # empty section and is no text of it.
        block = INLINE_CLOSER.sub("", block, count=1)
        listed = len(SUPPRESSED_ENTRY.findall(block))
        if "<details" in block.lower() or "</details" in block.lower():
            # A details block inside the section closes before the
            # section does, and a closer mid-line is text the parser
            # cannot place; the real bodies write neither there.
            unreadable = True
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
    text, problems = visible(text.replace("\r\n", "\n").replace("\r", "\n"))
    assessment = assessment_of(text)
    suppressed, unreadable = suppressed_count(text)
    reasons: list[str] = list(problems)
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
