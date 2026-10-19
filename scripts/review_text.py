# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""One line of a Copilot review body, as the classifier may read it.

The body is text from outside. Inline code is prose and is blanked
before any check; everything else the real review bodies never carry,
raw HTML outside a known shape, a comment, a stray fence, a reference
definition, a character reference, fails the body closed rather than
be parsed, since each needs another rule and Copilot writes none of
them (DESIGN.md section 8 step 11). ``review_verdict`` reads the body
line by line through ``read_line``.
"""

from __future__ import annotations

import html.entities
import re

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
DRESSED_TAG = re.compile(r"</?(" + "|".join(sorted(BARE_TAGS)) + r")(?![>])", re.I)
# A block-level tag opening a list item or a blockquote starts an HTML
# block inside the container, where a heading is text; the guard above
# reads the line start alone, so the body fails closed. The real
# bodies' list items open with <picture>, inline HTML, and no other
# tag; and any tag alone on the item's line, the badge's own
# included, opens a block of the kind Markdown reads from a lone tag,
# so that fails closed too. The badges carry their text on the line.
BLOCK_TAGS = frozenset({"details", "summary", "source"})
# One or more container prefixes, a list marker with its space or a
# quote marker with or without one, nested in any order and read in
# one linear pass; the real bodies nest none.
CONTAINER = r"^ {0,3}(?:(?:[-+*]|\d{1,9}[.)])[ \t]+|>[ \t]*)+"
CONTAINER_BLOCK = re.compile(
    CONTAINER + r"</?(" + "|".join(sorted(BLOCK_TAGS)) + r")\b", re.I
)
# The tag's attributes may quote a >, so the pattern reads quoted
# values whole before it looks for the close.
CONTAINER_LONE_TAG = re.compile(
    CONTAINER + r"""</?[A-Za-z](?:"[^"]*"|'[^']*'|[^<>"'])*>[ \t]*$"""
)
# A character reference could spell a label, a tag or a delimiter the
# classifier reads literally; the real bodies write none.
# A numeric reference renders with or without its semicolon, as does
# a legacy entity name HTML knows without one.
LEGACY_ENTITIES = frozenset(
    name for name in html.entities.html5 if not name.endswith(";")
)
CHARACTER_REFERENCE = re.compile(
    r"&(?:#[0-9]{1,7};?|#[xX][0-9a-fA-F]{1,6};?|[A-Za-z][A-Za-z0-9]{1,31};)"
)
# Legacy names expand with whatever follows them, so the longest is
# tried first and the text need not end there.
NAMED_REFERENCE = re.compile(
    "&(?:" + "|".join(sorted(LEGACY_ENTITIES, key=len, reverse=True)) + ")"
)
# The shape of every tag the real bodies write: a name, then attributes
# with plain double-quoted values or none. A value holding a quote, a
# backtick, an entity or an angle bracket, or an unquoted value, is a
# shape the classifier does not read.
TAG_SHAPE = re.compile(
    r"""<(?:/?[A-Za-z][A-Za-z0-9-]*)(?:\s+[A-Za-z-]+(?:="[^"<>`&']*")?)*\s*/?>"""
)
ANY_TAG = re.compile(r"<[A-Za-z/][^<>]*>")
# A backtick inside a quoted attribute value would pair with a later
# backtick as a code span and mask what lies between; read on the
# line as written, before any masking.
ATTRIBUTE_BACKTICK = re.compile(
    r"""<[A-Za-z](?:[^<>`"']|"[^"`>]*"|'[^'`>]*')*(?:"[^">]*`|'[^'>]*`)"""
)
# A tag indented past where a block may open: under a list item it
# opens a block the guard, which reads column zero alone, cannot place.
INDENTED_TAG = re.compile(r"^(?=(?: {4}|[ \t]*\t))[ \t]*</?[A-Za-z]")
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
# At the line start, opening a list item or a quote, or indented
# under one: each is a definition to Markdown.
LINK_REFERENCE = re.compile(
    r"^[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+|>[ \t]*)*\[(?:\\.|[^\]\\])*\]:"
)
# A fence run indented past where a fence may open or close: under a
# list item it closes a fence the parser, which tracks no container
# indentation, would otherwise never end.
# The indentation is read once, by lookahead, so a line of whitespace
# costs one pass rather than one per split of it.
INDENTED_FENCE = re.compile(r"^(?=(?: {4}|[ \t]*\t))[ \t]*(?:`{3,}|~{3,})[ \t]*$")
# A heading opening a quote or a list item, or indented under one,
# renders as a heading; the classifier reads headings at the line
# start alone, so these fail the body closed.
CONTAINER_HEADING = re.compile(
    r"^(?: {0,3}(?:(?:[-+*]|\d{1,9}[.)])[ \t]+|>[ \t]*)+|(?=(?: {4}|[ \t]*\t))[ \t]*)"
    r"#{1,6}[ \t]"
)
# A line opening a bracket it never closes may begin a reference label
# that spans lines; the definition is then LINK_REFERENCE's to miss.
OPEN_BRACKET = re.compile(
    r"^[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+|>[ \t]*)*\[(?:\\.|[^\]\\])*$"
)


def tag_left_open(line: str) -> bool:
    """Whether a tag starts on the line and no unquoted > closes it.

    One pass: each tag is walked once to its close, and a < met inside
    a tag outside quotes is a nested opener the real bodies never
    write, read as a tag left open.
    """
    i = 0
    while True:
        start = TAG_START.search(line, i)
        if start is None:
            return False
        quote: str | None = None
        closed = False
        i = start.end()
        while i < len(line):
            char = line[i]
            i += 1
            if quote:
                if char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char == ">":
                closed = True
                break
            elif char == "<":
                return True
        if not closed:
            return True


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
    if CHARACTER_REFERENCE.search(masked) or NAMED_REFERENCE.search(masked):
        problems.add(UNREADABLE_MARKDOWN)
    if INDENTED_TAG.match(line) or any(
        # The tag's own < survives the masking when it lies outside a
        # closed span; inside one the whole tag is a quoted example.
        masked[found.start()] == "<"
        for found in ATTRIBUTE_BACKTICK.finditer(line)
    ):
        problems.add(UNREADABLE_HTML)
    if OPEN_BRACKET.match(masked) or CONTAINER_HEADING.match(masked):
        problems.add(UNREADABLE_MARKDOWN)
    for tag in ANY_TAG.finditer(masked):
        if not TAG_SHAPE.fullmatch(tag.group(0)):
            problems.add(UNREADABLE_HTML)
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
