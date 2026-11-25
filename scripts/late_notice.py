# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What the outcome comment says of a maintainer comment the round never read.

``late_comments`` records where a maintainer spoke after the round
read its pull request (DESIGN.md section 20.6). The outcome comment
names it and says what answers it: the next round only when the
continuation will dispatch one, and otherwise that an operator's
dispatch, or a human at a cap, is needed. The marker carries the same
places, so a retry that finds a comment the first attempt did not
posts a new outcome rather than reuse the old one.
"""

from __future__ import annotations

import argparse
from typing import Any

from late_comments import answerable, late
from review_outcome import review_verdict


def sentence(result: dict[str, Any], *, continuing: bool) -> str:
    """Name a maintainer comment this round never read, and what answers it.

    ``continuing`` is whether the caller dispatches the next round of
    a pull request the continuation names (``CODE_MONKEY_CONTINUE``).
    """
    places = late(result)
    if not places:
        return ""
    text = (
        f" A maintainer commented on the {join_places(places)} after this round"
        " read the pull request, so this round did not answer it"
    )
    if not answerable(result):
        return text + (
            "; no round follows at the round cap, so it needs an operator's"
            " dispatch or a human."
        )
    if not continuing:
        return text + (
            "; continuation is off, so it needs an operator's `revise_only` dispatch."
        )
    if review_verdict(result) == "pending":
        return text + (
            "; Copilot's review is pending, which stops the continuation,"
            " so it needs an operator's `revise_only` dispatch."
        )
    return text + "; the next round answers it."


def after(text: str | None, result: dict[str, Any], continuing: bool) -> str | None:
    """``text`` with the late-comment sentence added; None stays None."""
    return None if text is None else text + sentence(result, continuing=continuing)


def continues(args: argparse.Namespace) -> bool:
    """Whether the caller said it continues (``--continue-rounds true``)."""
    return getattr(args, "continue_rounds", "") == "true"


def join_places(places: list[str]) -> str:
    """``issue``, ``issue and pull request``, ``issue, pull request and review thread``."""
    if len(places) == 1:
        return places[0]
    return ", ".join(places[:-1]) + " and " + places[-1]


def marker_field(result: dict[str, Any]) -> str:
    """The marker's `` late=`` field, ``issue,pull-request``; nothing when none."""
    places = late(result)
    if not places:
        return ""
    return " late=" + ",".join(place.replace(" ", "-") for place in places)
