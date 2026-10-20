# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The sections of a Copilot review body that count findings the threads miss.

Suppressed findings, and findings on code unchanged since the last
review, appear in the body alone: Copilot opens no thread for either.
The counts are read here from the visible text ``review_verdict``
hands over, and a details section the classifier does not know fails
the body closed, since the format is Copilot's and moves under us
(DESIGN.md section 8 step 11).
"""

from __future__ import annotations

import re

# Digits beyond this are no count GitHub would write.
MAX_COUNT_DIGITS = 9

# Suppressed findings sit under a heading or a <summary>; each entry
# opens with the bold path and line. The strong tags the real bodies
# use change nothing a reader sees and may fall anywhere, so they come
# off before the count is read, one around an entry read as asterisks
# first; whitespace and emphasis may sit around and between the
# label's words.
STRONG_TAGS = re.compile(r"</?strong>", re.I)
STRONG_ENTRY = re.compile(r"<strong>([^<*\n]+:\d+)</strong>", re.I)
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
LETTERS = re.compile(r"[^a-z]")
ANY_TAG_TEXT = re.compile(r"<[^<>]*>")
SUPPRESSED_LABEL = "suppressedcomments"
# Indentation of spaces and tabs alone: a class crossing newlines
# would rescan every blank line after the section once per line.
# An entry may sit under a list marker or a quote, bold by asterisks
# or underscores, a strong tag around it having been read as
# asterisks; the stated count is a floor, so each entry counts
# whatever the heading says.
SUPPRESSED_ENTRY = re.compile(
    r"^[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+|>[ \t]*)*"
    # A path may hold underscores, a pair included; the underscore
    # form ends at the pair that follows the line number, whatever
    # comes after it, so long as it is not a third underscore.
    # Bold, or bold and italic, by asterisks or underscores.
    r"(?:\*{2,3}`?[^*\n]+:\d+`?\*{2,3}|_{2,3}[^\n]+?:\d+_{2,3}(?!_))",
    re.M,
)
# Hashes inside a strong tag are text; with the tag stripped they would
# read as a heading and end a section early, so such a line fails the
# count closed instead.
STRONG_HEADING = re.compile(r"<strong>\s*#{1,6}(?:[ \t]|</strong>)", re.I)
SUPPRESSED_END = re.compile(
    r"^ {0,3}#{1,6}(?:[ \t]|$)|^ {0,3}</details>[ \t]*$", re.M | re.I
)
# Findings on code unchanged since the last review sit under a
# summary of their own, in two forms, and the body says "No open
# issues" above them. The count is read as the suppressed one is.
PREVIOUSLY_MISSED = re.compile(
    r"<summary>\s*(?:<strong>\s*)?(?:(\d+)\s+previously\s+missed\s+issues?"
    r"|previously\s+missed\s*\((\d+)\))",
    re.I,
)
# Where one count section ends and the next begins: a summary that is
# a count rather than a finding's badged title.
NEXT_COUNT = re.compile(
    r"<summary>\s*(?:<strong>\s*)?(?:\d+\s+(?:open|resolved|previously)"
    r"|open\s*\(|resolved\s*\(|previously\s+missed\s*\(|suppressed\s+comments)",
    re.I,
)
# The details sections the real bodies write: the counts, and a
# finding's own title under its severity badge. The format is
# Copilot's and moves under us, so any other section is one the
# classifier does not read and fails the body closed, rather than
# wait for a rule of its own.
KNOWN_SUMMARY = re.compile(
    r"^(?:\d+\s+open\s+(?:issues?|findings?)|open\s*\(\d+\)"
    r"|\d+\s+resolved\s+since\s+last\s+review|resolved\s*\(\d+\)"
    r"|suppressed\s+comments\s*\(\d+\)"
    r"|\d+\s+previously\s+missed\s+issues?|previously\s+missed\s*\(\d+\)"
    r"|what\s+changed\s+in\s+this\s+pr)$",
    re.I,
)
# A summary's text runs to its close; a nested opener fails the body
# closed elsewhere, so the scan need not look past one, which keeps a
# run of openers linear.
SUMMARY_TEXT = re.compile(
    r"<summary>((?:[^<]|<(?!/?summary>))*)(?:</summary>|(?=<summary>)|\Z)", re.I
)
FINDING_BADGE = re.compile(r'alt="(?:low|medium|high|critical)\s+severity"', re.I)
UNREADABLE_SECTION = "review body holds a section the classifier does not read"
INLINE_CLOSER = re.compile(r"\A(?:[ \t]*</summary>)?[ \t]*</details>", re.I)


def suppressed_count(text: str) -> tuple[int, bool]:
    """Suppressed findings: the entries listed, or the stated count if larger.

    The second value says a stated count was unreadable, which is a
    finding in itself rather than a crash.
    """
    total = 0
    unreadable = False
    # A strong tag around an entry's path and line is bold as a pair
    # of asterisks is, and is read as them; every other strong tag,
    # which may split the label or wrap its count, comes off.
    if STRONG_HEADING.search(text):
        unreadable = True
    text = STRONG_TAGS.sub("", STRONG_ENTRY.sub(r"**\1**", text))
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
        # empty section: nothing after it is the section's.
        if INLINE_CLOSER.match(block):
            block = ""
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


def previously_missed_count(text: str) -> tuple[int, bool]:
    """Findings on unchanged code, from either form of the section.

    The second value says a count was unreadable, a finding in itself.
    """
    total = 0
    unreadable = False
    stripped = STRONG_TAGS.sub("", text)
    for head in PREVIOUSLY_MISSED.finditer(stripped):
        digits = head.group(1) or head.group(2)
        # The stated count is a floor: the badged finding titles listed
        # under it, to the next count section or the body's end, count
        # whatever it says.
        rest = stripped[head.end() :]
        end = NEXT_COUNT.search(rest)
        block = rest[: end.start()] if end else rest
        listed = len(FINDING_BADGE.findall(block))
        if len(digits) > MAX_COUNT_DIGITS:
            unreadable = True
            total += listed
            continue
        total += max(int(digits), listed)
    return total, unreadable


def unknown_sections(text: str) -> bool:
    """Whether the body holds a details section the classifier does not know.

    A known section is one of the counts, or a finding's own title,
    which carries a severity badge; the rest of a finding's body is
    prose, not a section.
    """
    for found in SUMMARY_TEXT.finditer(text):
        inner = found.group(1)
        if FINDING_BADGE.search(inner):
            continue
        label = " ".join(ANY_TAG_TEXT.sub("", inner).split())
        if not KNOWN_SUMMARY.match(label):
            return True
    return False
