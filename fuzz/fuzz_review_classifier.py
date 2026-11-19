# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fuzz the classifier that reads Copilot's review bodies.

A review body is text from outside the pipeline. The first byte of an
input picks a target and the rest is its payload. Each oracle checks
what the code or docs/DESIGN.md section 8 step 11 says, from the raw
input:

- ``review_verdict.classify`` returns a verdict for any body and
  raises only on a negative thread count, which it documents. Its
  record holds "labels and counts, never the text". It is clean only
  with no reason, no unresolved thread, no suppressed or previously
  missed finding, a body within 64 KiB ("a body the classifier
  cannot read whole is never clean") and an h3 in the body that
  states a clean assessment in the module's own words, the one the
  record names. It reads "a CRLF body as its LF twin". Put before
  any body, a ``🟡 Changes recommended`` h3 is "the earliest
  recognised assessment heading", and a section stating N suppressed
  or previously missed findings counts at least N: each leaves the
  body with findings. The body is read "after fenced code is
  removed", so inside a fence no run in it can close it reads as no
  assessment, counts nothing and gives findings.
- ``review_text.mask_spans`` and ``read_line``: "the line comes back
  with its closed spans blanked, a suppressed entry's path and line
  apart", by a letter and never a space, where "two runs of equal
  length bound a span"; the text outside every span comes back as
  written, and a run with no partner leaves a span open. "A span
  left open", "a span that looks like an entry but cannot be kept"
  and "an escaped backtick" each fail the body closed with the code
  span reason; only the four unreadable reasons the module names are
  reported.
- ``review_sections``: a count section's "stated count is a floor",
  and a body that names no such section counts nothing and fails
  nothing closed.
"""

from __future__ import annotations

import re
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from harness_support import Breach, arguments, as_text, on_path

on_path()
verdicts = import_module("review_verdict")
review_text = import_module("review_text")
sections = import_module("review_sections")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
BODIES: dict[str, str] = import_module("review_fixtures").BODIES

MAX_BODY_BYTES = 64 * 1024
LINE_ENDS = re.compile(r"\r\n|\r|\n")
SURROGATES = re.compile("[\ud800-\udfff]")
# The reasons a verdict may carry, as review_verdict, review_text and
# review_sections write them: fixed labels and counts.
REASONS = re.compile(
    r"review body holds (?:an HTML comment|a code span|HTML|Markdown"
    r"|a section) the classifier does not read"
    r"|(?:suppressed|previously missed) count unreadable"
    rf"|review body over {MAX_BODY_BYTES} bytes"
    r"|[1-9][0-9]* unresolved review threads?"
    r"|[1-9][0-9]* (?:suppressed|previously missed) findings?"
    r"|no assessment|assessment: changes recommended"
)
UNREADABLE_SPAN = "review body holds a code span the classifier does not read"
LINE_PROBLEMS = frozenset(
    {
        "review body holds an HTML comment the classifier does not read",
        UNREADABLE_SPAN,
        "review body holds HTML the classifier does not read",
        "review body holds Markdown the classifier does not read",
    }
)
ASSESSMENTS = (None, "approval", "closer-look", "changes")
# "🟢 Approval recommended (🟢 Ready to approve in older reviews) or
# 🔵 Needs a closer look", as the review_verdict docstring words them.
CLEAN_PHRASES = {
    "🟢": ("Approval recommended", "Ready to approve"),
    "🔵": ("Needs a closer look",),
}
PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ")


def clean_headings(body: str) -> set[str]:
    """The labels of the clean assessment h3s, in review_verdict's words."""
    found: set[str] = set()
    for line in LINE_ENDS.split(body):
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) > 3 or not stripped.startswith("###"):
            continue
        words = re.split(r"[ \t]+", stripped[3:])
        if len(words) < 3 or words[0] or words[1] not in CLEAN_PHRASES:
            continue
        rest = " ".join(words[2:])
        for phrase in CLEAN_PHRASES[words[1]]:
            after = rest[len(phrase) : len(phrase) + 1]
            if rest.startswith(phrase) and not (after.isalnum() or after == "_"):
                found.add("approval" if words[1] == "🟢" else "closer-look")
    return found


def oracle_verdict(body: str, threads: int, verdict: Any) -> None:
    """The record is labels and counts, and clean only on what makes it so."""
    if verdict.verdict not in ("clean", "findings") or verdict.assessment not in (
        ASSESSMENTS
    ):
        raise Breach(f"classify returned {verdict.verdict!r}, {verdict.assessment!r}")
    if verdict.unresolved_threads != threads:
        raise Breach("classify recorded another thread count")
    for reason in verdict.reasons:
        if not REASONS.fullmatch(reason):
            raise Breach(f"classify recorded text that is no label: {reason!r}")
    if (verdict.verdict == "clean") != (not verdict.reasons):
        raise Breach("classify's verdict disagrees with its reasons")
    if verdict.verdict == "clean" and (
        threads
        or verdict.suppressed
        or verdict.previously_missed
        or len(body.encode("utf-8", "surrogatepass")) > MAX_BODY_BYTES
        or verdict.assessment not in clean_headings(body)
    ):
        raise Breach("classify called a body clean that holds findings")


def oracle_twin(verdict: Any, twin: Any) -> None:
    """A body with CRLF line ends reads as its LF twin."""
    if verdict != twin:
        raise Breach(f"the CRLF twin reads differently: {verdict} != {twin}")


# Sections written here, not by the module: a stated count is a floor.
PREFIXES: dict[str, str] = {
    "changes": "### 🟡 Changes recommended\n\n",
    "suppressed": "### Suppressed comments ({})\n\n",
    "missed": "<summary>{} previously missed issues</summary>\n\n",
}


def oracle_prefixed(kind: str, stated: int, verdict: Any) -> None:
    """A body behind a changes heading or a stated count holds findings."""
    if verdict.verdict != "findings":
        raise Breach(f"classify called a body clean behind a {kind} section")
    if kind == "changes" and verdict.assessment != "changes":
        raise Breach(f"an earlier changes heading read as {verdict.assessment!r}")
    if kind == "suppressed" and verdict.suppressed < stated:
        raise Breach(f"{stated} suppressed findings read as {verdict.suppressed}")
    if kind == "missed" and verdict.previously_missed < stated:
        raise Breach(f"{stated} previously missed read as {verdict.previously_missed}")


def fenced(body: str) -> str:
    """The body inside a backtick fence longer than any run in it.

    A fence "closes on a run of the same character at least as long",
    so no line of the body can close this one, and the whole body is
    fenced code. An oversized body loses its lone surrogates when it is
    bounded, which can join two runs, so runs are measured without them.
    """
    runs = re.findall(r"`+", SURROGATES.sub("", body))
    longest = max((len(run) for run in runs), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{body}\n{fence}\n"


def oracle_fenced(verdict: Any) -> None:
    """A body read "after fenced code is removed" holds nothing when all fenced."""
    if verdict.verdict != "findings" or verdict.assessment is not None:
        raise Breach(
            f"a fenced body read as {verdict.verdict!r}, {verdict.assessment!r}"
        )
    if verdict.suppressed or verdict.previously_missed:
        raise Breach("a fenced body counted findings")


def backtick_runs(line: str) -> list[tuple[int, int]]:
    """The start and end of each maximal run of backticks on the line."""
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(line):
        if line[i] != "`":
            i += 1
            continue
        start = i
        while i < len(line) and line[i] == "`":
            i += 1
        runs.append((start, i))
    return runs


def entry_shaped(span: str, fence: int) -> bool:
    """A span holding a path and a line number, which the masking keeps."""
    head, colon, tail = span[fence:-fence].rpartition(":")
    return bool(colon) and tail.isdecimal() and not set(head) & set("`\n<>")


def entry_like(span: str, fence: int) -> bool:
    """A span that still reads as an entry: a line number, then spaces or tabs."""
    head, colon, tail = span[fence:-fence].rstrip(" \t").rpartition(":")
    return bool(colon) and tail.isdecimal() and "\n" not in head


def expected_mask(line: str) -> tuple[str, bool, bool]:
    """The masked line, a span left open and an entry unkept, from the line alone.

    A run opens a span that the next run of its own length closes; a
    run with no such partner is left open, and the scan goes on after
    it. A blanked span that reads as an entry could not be kept.
    """
    runs = backtick_runs(line)
    parts: list[str] = []
    left_open = False
    unkept = False
    i = 0
    index = 0
    while index < len(runs):
        start, end = runs[index]
        fence = end - start
        partners = (
            k for k in range(index + 1, len(runs)) if runs[k][1] - runs[k][0] == fence
        )
        closer = next(partners, None)
        if closer is None:
            left_open = True
            index += 1
            continue
        stop = runs[closer][1]
        span = line[start:stop]
        parts.append(line[i:start])
        kept = entry_shaped(span, fence)
        unkept = unkept or (not kept and entry_like(span, fence))
        parts.append(span if kept else "x" * len(span))
        i = stop
        index = closer + 1
    parts.append(line[i:])
    return "".join(parts), left_open, unkept


def oracle_mask(line: str, masked: str, left_open: bool, unkept: bool) -> None:
    """Each closed span is blanked by a letter or kept as an entry; nothing else changes."""
    if len(masked) != len(line):
        raise Breach("mask_spans changed the line's length")
    if "`" not in line and (masked != line or left_open or unkept):
        raise Breach("mask_spans changed a line with no code span")
    expected, expected_open, expected_unkept = expected_mask(line)
    if masked != expected:
        raise Breach(f"mask_spans gave {masked!r} for {line!r}, not {expected!r}")
    if left_open != expected_open:
        raise Breach(f"mask_spans said left open {left_open} for {line!r}")
    if unkept != expected_unkept:
        raise Breach(f"mask_spans said unkept {unkept} for {line!r}")


def oracle_line(line: str, masked: str, problems: set[str]) -> None:
    """``read_line`` reports only its named reasons, the span one when due."""
    if not problems <= LINE_PROBLEMS:
        raise Breach(f"read_line reported {problems - LINE_PROBLEMS}")
    expected, left_open, unkept = expected_mask(line)
    if masked != expected:
        raise Breach(f"read_line gave {masked!r} for {line!r}, not {expected!r}")
    unreadable = left_open or unkept or "\\`" in line
    if unreadable != (UNREADABLE_SPAN in problems):
        raise Breach(f"read_line gave the code span reason wrongly for {line!r}")
    if set(line) <= PLAIN and (problems or masked != line):
        raise Breach(f"read_line refused plain words: {line!r}")


def oracle_sections(text: str, stated: int, counts: tuple[Any, ...]) -> None:
    """A stated count is a floor, and a body naming no section counts nothing."""
    suppressed, missed, floor_suppressed, floor_missed, unknown = counts
    for count, unreadable in (suppressed, missed, floor_suppressed, floor_missed):
        if type(count) is not int or count < 0 or type(unreadable) is not bool:
            raise Breach(f"a count came back as {count!r}, {unreadable!r}")
    if floor_suppressed[0] < stated or floor_missed[0] < stated:
        raise Breach(f"a stated count of {stated} read as less")
    folded = text.casefold()
    letters = re.sub(r"[^a-z]", "", text.lower())
    if (
        "<" not in text
        and "suppressed" not in folded
        and "suppressed" not in letters
        and suppressed != (0, False)
    ):
        raise Breach(f"no suppressed section, yet {suppressed}")
    if "<summary>" not in folded and (missed != (0, False) or unknown):
        raise Breach(f"no summary, yet {missed}, unknown sections {unknown}")


def check_classify(payload: bytes) -> None:
    """``classify`` on a body, with 0 to 2 threads from the first byte."""
    threads = payload[0] % 3 if payload else 0
    body = as_text(payload[1:])
    verdict = verdicts.classify(body, threads)
    oracle_verdict(body, threads, verdict)
    if "\r" not in body:
        twin = body.replace("\n", "\r\n")
        if len(twin.encode("utf-8", "surrogatepass")) <= MAX_BODY_BYTES:
            oracle_twin(verdict, verdicts.classify(twin, threads))
    stated = 1 + (payload[0] // 3) % 9 if payload else 1
    for kind, prefix in PREFIXES.items():
        prefixed = verdicts.classify(prefix.format(stated) + body, threads)
        oracle_prefixed(kind, stated, prefixed)
    oracle_fenced(verdicts.classify(fenced(body), threads))
    try:
        verdicts.classify(body, -1)
    except ValueError:
        return
    raise Breach("classify took a negative thread count")


def check_line(payload: bytes) -> None:
    """``mask_spans`` and ``read_line`` on each line of the payload."""
    for line in as_text(payload).split("\n"):
        masked, left_open, unkept = review_text.mask_spans(line)
        oracle_mask(line, masked, left_open, unkept)
        read, problems = review_text.read_line(line)
        oracle_line(line, read, problems)


def check_sections(payload: bytes) -> None:
    """The section counts on a text, alone and under a stated count."""
    stated = payload[0] if payload else 0
    text = as_text(payload[1:])
    counts = (
        sections.suppressed_count(text),
        sections.previously_missed_count(text),
        sections.suppressed_count(f"### Suppressed comments ({stated})\n" + text),
        sections.previously_missed_count(
            f"<summary>{stated} previously missed issues</summary>\n" + text
        ),
        sections.unknown_sections(text),
    )
    oracle_sections(text, stated, counts)


TARGETS = (check_classify, check_line, check_sections)


def check(data: bytes) -> None:
    """The fuzzed function: one input, one target, its contract or ``Breach``."""
    if data:
        TARGETS[data[0] % len(TARGETS)](data[1:])


def seed(target: int, prefix: int, text: str) -> bytes:
    """An input for the target at that index, its first payload byte given."""
    return bytes([target, prefix]) + text.encode("utf-8", "surrogatepass")


SEEDS: tuple[bytes, ...] = (
    *(seed(0, 0, body) for body in BODIES.values()),
    seed(0, 1, BODIES["v2-green-clean"]),
    seed(0, 0, "### 🟢 Approval recommended\n\n```\n### 🟡 Changes recommended\n```"),
    seed(0, 0, "### 🟡 Changes recommended\n### 🟢 Approval recommended"),
    seed(0, 0, "```\n### 🟢 Approval recommended\n```"),
    seed(0, 4, "### 🟢 Approval recommended\n\n### Suppressed comments (1)"),
    seed(0, 0, "x" * (MAX_BODY_BYTES + 1)),
    seed(0, 0, "\ud800"),
    seed(0, 0, "`\ud800`\ud800`\n### 🟢 Approval recommended\n" + "x" * MAX_BODY_BYTES),
    seed(1, 0, "Plain words\n`code` and ``a`b`` and `open\n<!-- c --> &amp; \\`"),
    seed(1, 0, "**`scripts/x.py:12`** and `` `a:1` ``\n- <details>\n    ```"),
    seed(1, 0, "**`src/x.py:12 `** and `a<b:3`\n`c` \\` d"),
    seed(2, 2, BODIES["legacy-suppressed"]),
    seed(2, 3, BODIES["v2-blue-previously-missed-current"]),
    seed(2, 0, "<summary>Odd section</summary>\n### Suppressed comments (x)"),
)


def main() -> None:
    """Fuzz ``check`` under Atheris, from the seeds unless files are named."""
    import atheris  # type: ignore[import-not-found]

    fuzzer: Any = atheris
    fuzzer.instrument_all()
    fuzzer.Setup(arguments(sys.argv, SEEDS), check)
    fuzzer.Fuzz()


if __name__ == "__main__":
    main()
