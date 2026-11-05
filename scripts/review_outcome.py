# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What a result says about Copilot's review, in the comment and its marker.

The review record descends from the publish job's own reads, but the
report reads every ``result.json`` field as hostile (docs/DESIGN.md
18.4), so a record of any unrecognised shape reads as pending.
"""

from __future__ import annotations

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
