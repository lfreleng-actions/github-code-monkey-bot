# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What a result says about Copilot's review, in the comment and its marker.

The review record descends from the publish job's own reads, but the
report reads every ``result.json`` field as hostile (docs/DESIGN.md
18.4), so a record of any unrecognised shape reads as pending.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import proposal_policy as policy

REVIEW_VERDICTS = ("clean", "findings", "pending")
REVIEW_FIELDS = frozenset(
    {
        "verdict",
        "assessment",
        "unresolved_threads",
        "suppressed",
        "previously_missed",
        "reasons",
        "review_id",
        "commit_id",
        "requested",
    }
)
ASSESSMENT_TEXT = {
    "approval": "approval recommended",
    "closer-look": "needs a closer look",
    "changes": "changes recommended",
}


def review_verdict(result: dict[str, Any]) -> str | None:
    """The Copilot verdict a result records, or None when no review was awaited.

    A record the publisher does not recognise, by shape or by verdict,
    reads as pending: whatever it says, it is not proof of a clean review.
    """
    record = result.get("review")
    if record is None:
        return None
    if not isinstance(record, dict):
        return "pending"
    data = cast("dict[str, Any]", record)
    # Every field, no more and no fewer: a record missing its counts or
    # the head it judged is not proof of a clean review either.
    if frozenset(data) != REVIEW_FIELDS or data.get("verdict") not in REVIEW_VERDICTS:
        return "pending"
    return str(data["verdict"])


def review_unrequested(result: dict[str, Any]) -> bool:
    """Whether the record says the workflow could not request the review.

    Read from ``requested`` alone, so a record of any other shape
    still reads as pending without adding a field to it.
    """
    record = result.get("review")
    return (
        isinstance(record, dict)
        and cast("dict[str, Any]", record).get("requested") is False
    )


def review_sentence(result: dict[str, Any]) -> str:
    """What the comment says about Copilot's review, if one was awaited."""
    verdict = review_verdict(result)
    if verdict is None:
        return ""
    if verdict == "clean":
        return ". The Copilot review of it is clean."
    if verdict == "pending" and review_unrequested(result):
        return (
            "; the workflow could not request a Copilot review of it, so the"
            " review is pending and a maintainer should request one."
        )
    if verdict == "pending":
        return (
            "; no Copilot review of its head arrived within the wait, so the"
            " review is pending and a maintainer should check for it."
        )
    record = cast("dict[str, Any]", result["review"])
    counts: list[str] = []
    threads = record.get("unresolved_threads")
    if type(threads) is int and threads:
        counts.append(
            f"{threads} unresolved review thread{'s' if threads != 1 else ''}"
        )
    suppressed = record.get("suppressed")
    if type(suppressed) is int and suppressed:
        counts.append(
            f"{suppressed} suppressed finding{'s' if suppressed != 1 else ''}"
        )
    missed = record.get("previously_missed")
    if type(missed) is int and missed:
        counts.append(f"{missed} previously missed finding{'s' if missed != 1 else ''}")
    assessment = ASSESSMENT_TEXT.get(str(record.get("assessment")), "no assessment")
    detail = "; ".join([*counts, f"assessment: {assessment}"])
    return (
        f". The Copilot review of it has findings ({detail}). "
        f"{policy.REVISION_DISCLOSURE}"
    )


def review_token(result: dict[str, Any]) -> str:
    """The review as one marker token: verdict, then counts and assessment.

    A findings review names its thread and suppressed counts and its
    assessment too, so a rerun that resolved threads under the same
    verdict reads as a new outcome; a pending review the workflow could
    not request reads apart from one that did not arrive, since the
    comment says different things of them. One token, with no spaces,
    so a reader of the footer splits it as before.
    """
    verdict = review_verdict(result)
    if verdict == "pending" and review_unrequested(result):
        return "pending/not-requested"
    if verdict != "findings":
        return str(verdict)
    record = cast("dict[str, Any]", result["review"])
    return (
        f"findings/{record.get('unresolved_threads')}/{record.get('suppressed')}"
        f"/{record.get('previously_missed')}/{record.get('assessment')}"
    )


# A default branch name longer than this is named generically.
MAX_BRANCH_NAME = 100


def merge_token(result: dict[str, Any]) -> str | None:
    """The probe's answer as one marker word, or None when no probe ran.

    Anything but a boolean reads as ``unknown``: never as mergeable.
    """
    if "mergeable" not in result:
        return None
    value = result.get("mergeable")
    if value is True:
        return "true"
    return "false" if value is False else "unknown"


def branch_text(result: dict[str, Any]) -> str:
    """The default branch the probe compared with, in code, or a generic name."""
    name = result.get("default_branch")
    if not isinstance(name, str) or not name or len(name) > MAX_BRANCH_NAME:
        return "the default branch"
    safe = policy.comment_safe(name)
    if safe != name or "`" in name:
        return "the default branch"
    return f"`{name}`"


def merge_sentence(result: dict[str, Any]) -> str:
    """What the comment says first about a head that may not merge, or nothing."""
    token = merge_token(result)
    branch = branch_text(result)
    if token == "false":
        moved = (
            ", which moved while this run worked"
            if result.get("base_moved_during_run") is True
            else ""
        )
        return (
            f" The pull request conflicts with {branch}{moved}, so its CI has not run."
        )
    if token == "unknown":
        return (
            f" GitHub did not confirm whether the pull request can merge with "
            f"{branch}, so its CI may not have run."
        )
    return ""


def review_after_merge(result: dict[str, Any]) -> str:
    """The review sentence as a sentence of its own, after the merge state.

    A conflicting head is never called clean: Copilot's verdict is not
    the closing word on a head that cannot merge.
    """
    sentence = review_sentence(result)
    if not sentence:
        return ""
    if merge_token(result) == "false" and review_verdict(result) == "clean":
        return " Copilot's review of it raised no findings."
    body = sentence[2:]
    return f" {body[:1].upper()}{body[1:]}"


CHECK_STATES = ("passing", "failing", "running", "none", "unknown")
CHECK_FIELDS = frozenset(
    {"state", "commit_id", "failing", "dropped", "unsettled", "read_at", "reason"}
)
# Failing checks the comment names; the record and the report keep the rest.
MAX_NAMED_CHECKS = 10
# Hex digits of the failing checks' hash in the comment marker.
CHECKS_HASH_LENGTH = 12


def published_commit(result: dict[str, Any]) -> str | None:
    """The last commit the result records as published, or None."""
    commits = result.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    last = cast("list[Any]", commits)[-1]
    return last if isinstance(last, str) and last else None


def checks_state(result: dict[str, Any]) -> str | None:
    """The head's checks as the result records them, or None when none were awaited.

    Bound to the published head as the review is: a record of another
    head, of an unrecognised shape, or missing from a result whose
    review was awaited, as an older result's is, reads as ``unknown``.
    """
    record = result.get("checks")
    if record is None:
        return None if review_verdict(result) is None else "unknown"
    if not isinstance(record, dict):
        return "unknown"
    data = cast("dict[str, Any]", record)
    if frozenset(data) != CHECK_FIELDS or data.get("state") not in CHECK_STATES:
        return "unknown"
    head = published_commit(result)
    if head is None or data.get("commit_id") != head:
        return "unknown"
    return str(data["state"])


def failing_check_names(result: dict[str, Any]) -> list[str]:
    """The failing check names the record keeps, unescaped; empty when unread."""
    if checks_state(result) not in ("failing", "running"):
        return []
    entries = cast("dict[str, Any]", result["checks"]).get("failing")
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in cast("list[Any]", entries):
        name = (
            cast("dict[str, Any]", entry).get("name")
            if isinstance(entry, dict)
            else None
        )
        if isinstance(name, str) and name.strip():
            names.append(name)
    return names


def checks_token(result: dict[str, Any]) -> str | None:
    """The checks state as one marker word, or None when no read was recorded.

    As ``merge_token`` does for the probe, a result without the record
    adds nothing, so markers posted before it existed still match.
    When the record names failing checks, the word carries a short
    hash of them, ``<state>/<hex>``: a retry whose state held but
    whose failures changed is a new outcome and posts again.
    """
    if "checks" not in result:
        return None
    state = checks_state(result)
    names = failing_check_names(result)
    if not names:
        return state
    encoded = json.dumps(sorted(names)).encode("utf-8")
    return f"{state}/{hashlib.sha256(encoded).hexdigest()[:CHECKS_HASH_LENGTH]}"


def checks_failed(result: dict[str, Any]) -> bool:
    """Whether the record shows a failing check on the head, settled or not."""
    state = checks_state(result)
    return state == "failing" or (
        state == "running" and bool(failing_check_names(result))
    )


def failing_checks_named(result: dict[str, Any]) -> str:
    """The failing checks as one comment-safe list, bounded, or empty."""
    names = failing_check_names(result)
    text = policy.failing_checks_text(list(names[:MAX_NAMED_CHECKS]))
    more = len(names) - MAX_NAMED_CHECKS
    return f"{text} and {more} more" if text and more > 0 else text


def checks_sentence(result: dict[str, Any]) -> str:
    """What the comment says of the head's CI: failing checks, or nothing.

    The publish job reports CI and gates on nothing but the review, so
    a passing, absent or unread result adds no words.
    """
    state = checks_state(result)
    named = failing_checks_named(result)
    if state == "failing":
        return (
            f" Checks failed on its head: {named}."
            if named
            else " Checks failed on its head."
        )
    if state == "running" and named:
        return (
            f" Checks were still running on its head when the wait ended, and"
            f" some had failed: {named}."
        )
    return ""
