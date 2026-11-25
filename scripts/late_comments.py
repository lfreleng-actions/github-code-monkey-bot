# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Maintainer comments a round never read: posted after its read watermark.

A round reads its pull request when its run selects (``collected_at``,
the footer's ``since=``). A maintainer who comments while the round
runs is heard by no one: the round's packet was read before it, and a
named dispatch to act on it is held by the running round (DESIGN.md
section 6 "Overlapping runs"). So once the round publishes, the
trusted publish job reads again, over the same authors the selector's
work-outstanding test counts (``revision_select.outstanding``): a
listed author's comment on the issue, a maintainer's on the pull
request, and a maintainer's word on a review thread, each after the
watermark. The answer is recorded in ``result.json`` as
``late_comments``: where a maintainer spoke (``issue``, ``pull
request``, ``review thread``), an empty list when nowhere, and None
when it found nowhere but a read failed, or no read ran.

The result's word goes no further than the continuation's select job
re-checks (DESIGN.md section 20.6): a late comment continues the pull
request and withholds the antagonistic review, which a forged result
could already withhold. Each place is read on its own: a read that
fails adds a warning and keeps what the others found, so it never
erases a comment one of them saw, and when they found nothing it
records None, which changes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import bot_github as github
import copilot_review as review
import issue_authors as authors
import issue_reads as reads
import revision_reads as revisions
import round_caps as caps
import round_reads
import thread_reads
from selection_inputs import load_reviewers

FIELD = "late_comments"
PLACES = ("issue", "pull request", "review thread")
# The antagonistic review Apps whose threads count, as the selector reads them.
REVIEWERS = (
    Path(__file__).resolve().parents[1] / "config" / "antagonistic-reviewers.txt"
)
FAILURES = (
    github.GitHubError,
    reads.SelectionError,
    authors.AuthorListError,
    OSError,
    ValueError,
)


def where(
    repository: str,
    issue: int,
    pull_number: int,
    since: str,
    result: dict[str, Any],
    reviewers: frozenset[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Where a maintainer spoke after ``since``, and the places it could not read.

    Each place is read on its own, so a failed read loses no place
    another read found. The issue's comments narrow to the listed
    authors the result records, as the packet and the selector narrow
    them; ``reviewers`` defaults to the trusted checkout's list.
    """

    def issue_read() -> bool:
        listed = authors.recorded_mode(result)
        return round_reads.newer_member_comment(repository, issue, since, listed)

    def pull_read() -> bool:
        return round_reads.newer_member_comment(repository, pull_number, since)

    def thread_read() -> bool:
        apps = configured_reviewers() if reviewers is None else reviewers
        packet = thread_reads.unresolved_threads(repository, pull_number, since, apps)
        return bool(packet.maintainer_since)

    found: list[str] = []
    failed: list[str] = []
    for place, read in zip(PLACES, (issue_read, pull_read, thread_read), strict=True):
        try:
            if read():
                found.append(place)
        except FAILURES as exc:
            failed.append(f"{place}: {github.safe_message(exc)}")
    return found, failed


def record(result: dict[str, Any], reviewers: frozenset[str] | None = None) -> None:
    """Record ``late_comments`` in the result, with one warning naming each failed read.

    Only a published pull request with a recorded watermark is read;
    anything else records None. A place a read found is recorded even
    when another read failed; None stands only for a read that found
    nothing and could not read every place.
    """
    result[FIELD] = None
    target = review.published(result)
    since = result.get("collected_at")
    issue = result.get("issue")
    if target is None or not isinstance(since, str) or not since:
        return
    if type(issue) is not int:
        return
    repository, number = target
    found, failed = where(repository, issue, number, since, result, reviewers)
    if failed:
        cast("list[Any]", result.setdefault("warnings", [])).append(
            "could not read maintainer comments after the round's watermark: "
            + "; ".join(failed)
        )
    result[FIELD] = found if found or not failed else None


def configured_reviewers() -> frozenset[str]:
    """The trusted checkout's reviewer list; none when it is missing."""
    return load_reviewers(REVIEWERS) if REVIEWERS.is_file() else frozenset()


def late(item: dict[str, Any]) -> list[str]:
    """Where the result records a maintainer comment the round never read.

    Only the three places this module writes count, in their order.
    """
    value = item.get(FIELD)
    if not isinstance(value, list):
        return []
    return [place for place in PLACES if place in cast("list[Any]", value)]


def answerable(item: dict[str, Any]) -> bool:
    """Whether the selector offers the pull request a round after this one.

    At a segment's cap it offers none until a hand-back starts the
    next segment, whatever the outcome; a superseded round counts
    toward no segment, as ``round_caps.capped`` reads it.
    """
    if caps.at_cap(item):
        return False
    if caps.superseded(item):
        return True
    return not revisions.round_cap_reached(
        caps.spent_of(item), caps.segment_of(item), caps.cap_of(item)
    )
