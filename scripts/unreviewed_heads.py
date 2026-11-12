# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bot pull request heads Copilot never reviewed (DESIGN.md section 20.1).

A head with no Copilot review and nothing else outstanding skips by
its own reason, not as nothing outstanding, so the selection records
it. The selection summary lists those heads under their own heading,
and the select job hands them to the report as a file in its
evidence artifact, which the report validates before printing: a run
of them, or of refused requests, is how a human notices a lapsed
review token.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

# The skip for a head with no Copilot review and nothing else outstanding.
NO_REVIEW = "no Copilot review of the head"
# Appended when the bot's latest footer records the request refused.
REFUSED = f"{NO_REVIEW}; the review request was refused"
# The footer's review token for a request GitHub refused (review_outcome).
REFUSED_TOKEN = "pending/not-requested"
REASONS = {NO_REVIEW: "no review yet", REFUSED: "review request refused"}


def reason(last_review: str | None) -> str:
    """The skip's reason, from the latest footer's review token."""
    return REFUSED if last_review == REFUSED_TOKEN else NO_REVIEW


def entries(skipped_issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """The skipped issues that are unreviewed heads, in order."""
    return [entry for entry in skipped_issues if entry.get("reason") in REASONS]


def section(found: list[dict[str, str]]) -> list[str]:
    """The heading, its counts and a row per head; nothing when there are none."""
    if not found:
        return []
    refused = sum(entry["reason"] == REFUSED for entry in found)
    lines = [
        "",
        f"### Heads with no Copilot review: {len(found)}"
        f" ({refused} with the request refused)",
        "",
        "Nothing outstanding but the review: the scan leaves these for"
        " Copilot or a human. Many refused requests mean the review token"
        " lapsed.",
        "",
        "| Pull request | State |",
        "| --- | --- |",
    ]
    lines += [f"| {entry['issue']} | {REASONS[entry['reason']]} |" for entry in found]
    return lines


def read(path: Path | None) -> str | None:
    """The file's text; an unreadable one reads as text no entry survives."""
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable"


def accepted(raw: str | None, org: str | None) -> tuple[list[dict[str, str]], int]:
    """The select job's entries that name a pull request in ``org``, and the rest.

    The output crosses jobs, so an entry counts only when its reference
    is a whole ``org/repo#N`` and its reason one of the two the selector
    writes; anything else is counted, never copied.
    """
    if not raw:
        return [], 0
    try:
        loaded: Any = json.loads(raw)
    except ValueError:
        return [], 1
    if not isinstance(loaded, list):
        return [], 1
    items = cast("list[Any]", loaded)
    if org is None:
        return [], len(items)
    pattern = re.compile(
        rf"{re.escape(org)}/[A-Za-z0-9_.-]+#[1-9][0-9]*", re.IGNORECASE | re.ASCII
    )
    kept: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, Any]", item)
        issue, why = entry.get("issue"), entry.get("reason")
        if (
            isinstance(issue, str)
            and pattern.fullmatch(issue)
            and isinstance(why, str)
            and why in REASONS
        ):
            kept.append({"issue": issue, "reason": why})
    return kept, len(items) - len(kept)


def report_lines(found: list[dict[str, str]], invalid: int) -> list[str]:
    """The report's section: the heads, then a count of entries left out."""
    lines = section(found)
    if invalid:
        many = invalid != 1
        lines += [
            "",
            f"{invalid} unreviewed-head entr{'ies' if many else 'y'} failed validation"
            f" and {'were' if many else 'was'} left out.",
        ]
    return [*lines, ""] if lines else []
