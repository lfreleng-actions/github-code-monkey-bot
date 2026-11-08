# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The round caps a revision round's result reads (DESIGN.md section 20.1).

The issue comment, the run report and the antagonistic review
nomination all read a result through these rules, so none offers a
round the selector would refuse.
"""

from __future__ import annotations

from typing import Any

import revision_reads as revisions
from review_outcome import checks_failed, review_verdict


def cap_of(result: dict[str, Any]) -> int:
    """The rounds per segment the selection ran with; the default when unrecorded."""
    value = result.get("max_rounds")
    return value if type(value) is int and value > 0 else revisions.MAX_ROUNDS


def round_of(result: dict[str, Any]) -> int:
    """The revision round a result records, or 0 for a first proposal."""
    value = result.get("round")
    return value if type(value) is int and value > 0 else 0


def segment_of(result: dict[str, Any]) -> int:
    """The round's place in its antagonistic segment, as the selection counted it.

    A result that records none caps on the global round, as it did
    before segments, so a missing count never lifts the cap.
    """
    value = result.get("segment_round")
    return value if type(value) is int and value > 0 else round_of(result)


# Why a round offers no further one, as the nomination records it.
AT_CAP = "the round is at the cap where a human takes over"


def spent_of(result: dict[str, Any]) -> int:
    """The rounds the ceiling counts after this one, recounted at its comment."""
    counted = revisions.rounds_spent(round_of(result), result.get("stale_rounds"))
    recounted = result.get("rounds_spent")
    return max(counted, recounted) if type(recounted) is int else counted


def capped(result: dict[str, Any]) -> bool:
    """Whether the round leaves the pull request at the cap with work undone.

    The selector skips a pull request at the cap, the run's rounds in
    an antagonistic segment (ten by default) or sixty in all, until
    the next hand-back starts a segment, so every outcome at the cap
    but a clean proposal that can merge with no known failing check
    needs a human; that one is finished. Ten superseded rounds stop
    it the same way, hand-back or not. A superseded round counts
    toward no segment's cap, only the ceiling and that guard.
    """
    if superseded(result):
        return spent_of(result) >= revisions.MAX_TOTAL_ROUNDS or guard_reached(result)
    if not guard_reached(result) and not revisions.round_cap_reached(
        spent_of(result), segment_of(result), cap_of(result)
    ):
        return False
    return (
        result.get("verdict") != "proposed"
        or review_verdict(result) != "clean"
        or result.get("mergeable") is False
        or checks_failed(result)
    )


def superseded(result: dict[str, Any]) -> bool:
    """Whether the round lost a race with another run's push (DESIGN.md 20.1)."""
    return result.get("superseded") is True


def stale_of(result: dict[str, Any]) -> int:
    """The superseded rounds once this one has run, recounted at its comment.

    ``stale_rounds`` counts those before it, which another run's
    footer can raise; a superseded round adds itself.
    """
    value = result.get("stale_rounds")
    count = value if type(value) is int and value > 0 else 0
    return count + 1 if superseded(result) else count


def guard_reached(result: dict[str, Any]) -> bool:
    """Whether the superseded rounds' guard stops the next selection, whatever the outcome."""
    return stale_of(result) >= revisions.MAX_STALE_ROUNDS


def at_cap(result: dict[str, Any]) -> bool:
    """Whether the selector offers the pull request no round after this one."""
    return (
        spent_of(result) >= revisions.MAX_TOTAL_ROUNDS
        or guard_reached(result)
        or capped(result)
    )


def cap_notice(result: dict[str, Any]) -> str:
    """The notice for a capped round: the segment's cap, the ceiling's or the guard's."""
    if spent_of(result) >= revisions.MAX_TOTAL_ROUNDS:
        return (
            f" This was the round cap of {revisions.MAX_TOTAL_ROUNDS} over every"
            " antagonistic review round: the workflow selects this pull request"
            " no further, and a human must take over."
        )
    if superseded(result):
        return f" This was superseded round {stale_of(result)}: a human must look."
    if guard_reached(result):
        return (
            f" {stale_of(result)} superseded rounds have run on this pull request:"
            " the workflow selects it no further, and a human must look."
        )
    return (
        f" This was the round cap of {cap_of(result)} per antagonistic review"
        " round: the workflow selects this pull request no further until the"
        " next antagonistic review hands it back, and until then a human must"
        " take over."
    )
