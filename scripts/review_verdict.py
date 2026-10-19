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
# What stands in for a closed span: a letter opens nothing in Markdown.
SPAN_FILL = "x"
COMMENT_OPEN = "<!--"
# <!DOCTYPE, <![CDATA[ and <?xml: raw HTML blocks the classifier
# does not read, as it does not read comments.
DECLARATION = re.compile(r"<[!?]")
# The one HTML comment Copilot writes: the format marker, on a line of
# its own. Any other comment fails closed (``visible``).
MARKER_LINE = re.compile(r"^[ \t]*<!-- ccr-overview-v\d+ -->[ \t]*$")
UNREADABLE_COMMENT = "review body holds an HTML comment the classifier does not read"
UNREADABLE_SPAN = "review body holds a code span the classifier does not read"
UNREADABLE_HTML = "review body holds HTML the classifier does not read"
UNREADABLE_MARKDOWN = "review body holds Markdown the classifier does not read"
# The tags the real review bodies use (tests/review_fixtures.py). Any
# other raw HTML fails closed: a block could hold an example heading
# or a fence line, and the classifier does not parse HTML.
KNOWN_TAGS = frozenset(
    {"a", "details", "img", "picture", "source", "strong", "sub", "summary"}
)
HTML_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)")
# A line opening with a tag the real bodies use, open or close, starts
# an HTML block: lines after it, to the next blank line, are HTML and
# no heading or fence. Any other tag fails closed (KNOWN_TAGS).
BLOCK_TAG = re.compile(r"^ {0,3}</?(" + "|".join(sorted(KNOWN_TAGS)) + r")\b", re.I)
# The tags SUPPRESSED_HEAD reads, in the bare form the real bodies
# write; a variant with whitespace or attributes would slip past that
# pattern, so it fails the body closed instead.
BARE_TAGS = frozenset({"summary", "strong"})
DRESSED_TAG = re.compile(r"<(" + "|".join(sorted(BARE_TAGS)) + r")(?![>])", re.I)
# A block-level tag opening a list item or a blockquote starts an HTML
# block inside the container, where a heading is text; the guard above
# reads the line start alone, so the body fails closed. The real
# bodies' list items open with <picture>, inline HTML, and no other
# tag; and any tag alone on the item's line, the badge's own
# included, opens a block of the kind Markdown reads from a lone tag,
# so that fails closed too. The badges carry their text on the line.
BLOCK_TAGS = frozenset({"details", "summary", "source"})
CONTAINER = r"^ {0,3}(?:[-+*]|\d{1,9}[.)]|>)[ \t]*"
CONTAINER_BLOCK = re.compile(
    CONTAINER + r"</?(" + "|".join(sorted(BLOCK_TAGS)) + r")\b", re.I
)
CONTAINER_LONE_TAG = re.compile(CONTAINER + r"</?[A-Za-z][^<>]*>[ \t]*$")
# A character reference could spell a label, a tag or a delimiter the
# classifier reads literally; the real bodies write none.
CHARACTER_REFERENCE = re.compile(
    r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});"
)
# A tag the line does not close holds a multi-line attribute, which
# may carry anything; the real bodies close every tag on its line. A
# > inside a quoted value closes nothing, so quotes are tracked.
TAG_START = re.compile(r"<[A-Za-z/]")
MARKDOWN_SHAPE = re.compile(r"^ {0,3}(?:#{1,6}[ \t]|`{3,}|~{3,})")
# A backslash makes the backtick after it literal; an escaped one is
# no span delimiter, and the classifier does not parse escapes.
ESCAPED_BACKTICK = "\\`"
# A fence run anywhere but where FENCE_OPEN reads it: in a list item
# or a blockquote it opens a fence the classifier does not track, and
# its close would read as an opener hiding the rest of the body. The
# real bodies open every fence at the line start.
STRAY_FENCE = re.compile(r"`{3,}|~{3,}")
# A link reference definition's title may run over the lines after
# it, taking a heading with it; the real bodies define none.
LINK_REFERENCE = re.compile(r"^ {0,3}\[[^\]]*\]:")
# Digits beyond this are no count GitHub would write.
MAX_COUNT_DIGITS = 9

# Suppressed findings sit under a heading or a <summary>; each entry
# opens with the bold path and line.
SUPPRESSED_HEAD = re.compile(
    r"(?:^ {0,3}#{2,4}\s+|<summary>\s*(?:<strong>\s*)?)[*_]{0,3}"
    r"Suppressed\s+comments\s*\\?\((\d+)\\?\)",
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


def tag_left_open(line: str) -> bool:
    """Whether a tag starts on the line and no unquoted > closes it."""
    for start in TAG_START.finditer(line):
        quote: str | None = None
        closed = False
        for char in line[start.end() :]:
            if quote:
                if char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char == ">":
                closed = True
                break
        if not closed:
            return True
    return False


def mask_spans(line: str) -> tuple[str, bool]:
    """The line with each closed code span blanked, and whether one is left open.

    Inline code is prose: whatever a closed span holds is text to the
    reader, so the HTML and comment checks run on the line without
    it. The span is replaced by a run of a letter, not spaces, since
    spaces could indent what follows into a heading or a code block
    the writer's line never had. A span left open may run on to later
    lines; it is reported and the rest of the line is checked as
    written.
    """
    parts: list[str] = []
    left_open = False
    i = 0
    while True:
        ticks = BACKTICKS.search(line, i)
        if ticks is None:
            parts.append(line[i:])
            return "".join(parts), left_open
        run = ticks.group()
        close = line.find(run, ticks.end())
        while close >= 0 and (
            (close + len(run) < len(line) and line[close + len(run)] == "`")
            or line[close - 1] == "`"
        ):
            close = line.find(run, close + 1)
        parts.append(line[i : ticks.start()])
        if close < 0:
            left_open = True
            parts.append(run)
            i = ticks.end()
        else:
            end = close + len(run)
            parts.append(SPAN_FILL * (end - ticks.start()))
            i = end


def read_line(line: str) -> tuple[str, set[str]]:
    """The line as the classifier reads it, and what on it it will not read past.

    Inline code is prose, so a comment opener or a tag inside a closed
    span is fine, and the line comes back with its closed spans
    blanked: a quoted heading or label inside one names no assessment
    and counts no finding. A span left open on its line may run on to
    later lines and show or hide anything, and a comment opener
    outside code may hide anything to its close: either fails the
    body closed, as do an escaped backtick, a fence run off the line
    start, a link reference definition and any HTML tag the real
    bodies never carry. Parsing these instead is a losing game, since
    each needs another rule, and Copilot writes none of them.
    """
    problems: set[str] = set()
    masked, left_open = mask_spans(line)
    if left_open or ESCAPED_BACKTICK in line:
        problems.add(UNREADABLE_SPAN)
    if STRAY_FENCE.search(masked) or LINK_REFERENCE.match(masked):
        problems.add(UNREADABLE_MARKDOWN)
    if CHARACTER_REFERENCE.search(masked):
        problems.add(UNREADABLE_MARKDOWN)
    if CONTAINER_BLOCK.match(masked) or CONTAINER_LONE_TAG.match(masked):
        problems.add(UNREADABLE_HTML)
    if DRESSED_TAG.search(masked):
        problems.add(UNREADABLE_HTML)
    for tag in HTML_TAG.findall(masked):
        if tag.lower() not in KNOWN_TAGS:
            problems.add(UNREADABLE_HTML)
    if tag_left_open(masked):
        problems.add(UNREADABLE_HTML)
    for opener in DECLARATION.finditer(masked):
        # A declaration, processing instruction or CDATA block hides
        # what follows as a comment does; outside a code span, that is
        # HTML the classifier does not read.
        if masked.startswith(COMMENT_OPEN, opener.start()):
            problems.add(UNREADABLE_COMMENT)
        else:
            problems.add(UNREADABLE_HTML)
    return masked, problems


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
    for line in text.split("\n"):
        if fence is not None:
            if fence_closes(line, *fence):
                fence = None
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
                lines.append(read_into(line, problems))
                continue
        if BLOCK_TAG.match(line):
            in_html = True
            lines.append(read_into(line, problems))
            continue
        opened = FENCE_OPEN.match(line)
        if opened:
            run = opened.group(1) or opened.group(2)
            fence = (run[0], len(run))
            continue
        if MARKER_LINE.match(line):
            continue
        lines.append(read_into(line, problems))
    return "\n".join(lines), sorted(problems)


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
