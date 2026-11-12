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
import stale_statuses as stale

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


CHECK_STATES = ("passing", "infrastructure", "failing", "running", "none", "unknown")
# A failing run's kind when the egress agent, not the change, failed it.
INFRASTRUCTURE = "infrastructure"
CHECK_FIELDS = frozenset(
    {
        "state",
        "commit_id",
        "failing",
        "dropped",
        "unsettled",
        "infrastructure",
        "defects",
        "read_at",
        "reason",
        "stale",
    }
)
# A record written before the whole failing-run counts and the stale
# set existed; one may carry any of them without the others.
LEGACY_CHECK_FIELDS = CHECK_FIELDS - {"infrastructure", "defects", "stale"}
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
    if not LEGACY_CHECK_FIELDS <= frozenset(data) <= CHECK_FIELDS:
        return "unknown"
    if data.get("state") not in CHECK_STATES:
        return "unknown"
    head = published_commit(result)
    if head is None or data.get("commit_id") != head:
        return "unknown"
    return str(data["state"])


def recorded_check_names(result: dict[str, Any], *, infrastructure: bool) -> list[str]:
    """The record's failing check names of one kind, unescaped; empty when unread.

    An entry without a ``kind``, as an older record's, is a defect.
    """
    if checks_state(result) not in ("failing", INFRASTRUCTURE, "running"):
        return []
    entries = cast("dict[str, Any]", result["checks"]).get("failing")
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in cast("list[Any]", entries):
        if not isinstance(entry, dict):
            continue
        data = cast("dict[str, Any]", entry)
        if (data.get("kind") == INFRASTRUCTURE) != infrastructure:
            continue
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name)
    return names


def failing_check_names(result: dict[str, Any]) -> list[str]:
    """The failing defect check names the record keeps, unescaped."""
    return recorded_check_names(result, infrastructure=False)


def infrastructure_check_names(result: dict[str, Any]) -> list[str]:
    """The check names the egress agent failed, unescaped; they need a re-run."""
    return recorded_check_names(result, infrastructure=True)


def infrastructure_count(result: dict[str, Any]) -> int:
    """How many checks failed at the egress agent, kept or left out."""
    return recorded_count(result, infrastructure=True)


def defect_count(result: dict[str, Any]) -> int:
    """How many checks failed for a reason other than the egress agent."""
    return recorded_count(result, infrastructure=False)


def recorded_count(result: dict[str, Any], *, infrastructure: bool) -> int:
    """How many failing checks of one kind the record counts, kept or left out.

    The record's whole count, which the bounds on its names do not
    cut, or the names it kept when that is larger or absent, as in a
    record written before the count existed.
    """
    names = recorded_check_names(result, infrastructure=infrastructure)
    if checks_state(result) not in ("failing", INFRASTRUCTURE, "running"):
        return 0
    key = INFRASTRUCTURE if infrastructure else "defects"
    value = cast("dict[str, Any]", result["checks"]).get(key)
    whole = value if type(value) is int and 0 <= value <= MAX_UNSETTLED else 0
    return max(whole, len(names))


def checks_token(result: dict[str, Any]) -> str | None:
    """The checks state as one marker word, or None when no read was recorded.

    As ``merge_token`` does for the probe, a result without the record
    adds nothing, so markers posted before it existed still match.
    When the record names failing checks, the word carries a short
    hash of them, ``<state>/<hex>``: a retry whose state held but
    whose failures changed is a new outcome and posts again. Defect
    names alone hash as they always have; infrastructure ones join
    them as a second list. When the whole counts exceed the names
    kept, the counts join as well, so a failure past the bounds that
    appears or clears posts again; so do stale statuses' contexts.
    """
    if "checks" not in result:
        return None
    state = checks_state(result)
    names = failing_check_names(result)
    agent = infrastructure_check_names(result)
    counts = [defect_count(result), infrastructure_count(result)]
    beyond = counts[0] > len(names) or counts[1] > len(agent)
    late = stale.contexts(result, state)
    if not names and not agent and not beyond and not late:
        return state
    hashed: Any = [sorted(names), sorted(agent)] if agent else sorted(names)
    if beyond:
        hashed = [hashed, counts]
    if late:
        hashed = [hashed, late]
    encoded = json.dumps(hashed).encode("utf-8")
    return f"{state}/{hashlib.sha256(encoded).hexdigest()[:CHECKS_HASH_LENGTH]}"


def checks_failed(result: dict[str, Any]) -> bool:
    """Whether the record shows a failing defect check on the head, settled or not.

    A ``running`` head counts by the record's whole defect count, so a
    defect the bounds left out of its names still marks it failed.
    """
    state = checks_state(result)
    return state == "failing" or (state == "running" and defect_count(result) > 0)


def failing_checks_named(result: dict[str, Any]) -> str:
    """The failing defect checks as one comment-safe list, bounded, or empty."""
    return checks_named(failing_check_names(result), defect_count(result))


def checks_named(names: list[str], total: int = 0) -> str:
    """Check names as one comment-safe list, bounded, or empty.

    ``total`` counts checks the record left out as well, when it
    exceeds the names.
    """
    shown = names[:MAX_NAMED_CHECKS]
    text = policy.failing_checks_text(list(shown))
    more = max(total, len(names)) - len(shown)
    return f"{text} and {more} more" if text and more > 0 else text


# The most checks the selection reads on a head: ten pages of check
# runs and one of commit statuses (check_reads). A larger count is not
# one it recorded.
MAX_UNSETTLED = 1_100


def unsettled_count(value: Any) -> int:
    """A count of checks still running at selection, or 0 when it is not one."""
    return value if type(value) is int and 0 <= value <= MAX_UNSETTLED else 0


def unsettled_at_selection(value: Any) -> str:
    """The round's word on checks the selection found running, or empty.

    The selection-time counterpart of the publish-time sentence in
    ``checks_sentence``: a round sent while CI had not finished must
    not read as one sent to a green head.
    """
    count = unsettled_count(value)
    return f"{count} check(s) still running at selection" if count else ""


def checks_sentence(result: dict[str, Any]) -> str:
    """What the comment says of the head's CI: failing checks, or nothing.

    The publish job reports CI and gates on nothing but the review, so
    a passing, absent or unread result adds no words. Failures the
    egress agent caused are named apart, as needing a re-run, and a
    stale status as not waited for, on any read state.
    """
    state = checks_state(result)
    named = failing_checks_named(result)
    sentence = ""
    if state == "failing":
        sentence = (
            f" Checks failed on its head: {named}."
            if named
            else " Checks failed on its head."
        )
    elif state == "running" and defect_count(result):
        sentence = (
            f" Checks were still running on its head when the wait ended, and"
            f" some had failed: {named}."
            if named
            else f" Checks were still running on its head when the wait ended,"
            f" and {defect_count(result)} had failed."
        )
    late = stale.sentence(result, state)
    return sentence + infrastructure_sentence(result) + late


def infrastructure_sentence(result: dict[str, Any]) -> str:
    """The comment's word on failures the egress agent caused, or empty."""
    count = infrastructure_count(result)
    if not count:
        if checks_state(result) == INFRASTRUCTURE:
            return " Checks failed at the egress agent and need a re-run."
        return ""
    named = checks_named(infrastructure_check_names(result), count)
    return f" {count} check(s) failed at the egress agent and need a re-run" + (
        f": {named}." if named else "."
    )
