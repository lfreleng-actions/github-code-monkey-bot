# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The ceiling's rounds, recounted when a round's outcome is commented (DESIGN.md 20.1).

Racing runs select the same round on the same history, and the
selection's count misses what the other then posts: a counted
winner's round, or a superseded loser's. The comment step reads the
history again, this attempt's own footer left out, and adds this
round once. A nomination the recount puts at the cap is withdrawn,
since the selector would refuse its hand-back.
"""

from __future__ import annotations

from typing import Any, cast

import bot_github as github
import issue_reads as reads
import revision_reads as revisions
import round_reads as rounds_of
from round_caps import AT_CAP, at_cap


def count(value: object) -> int:
    """A recorded count, or 0 when it is missing or not a positive integer."""
    return value if type(value) is int and value > 0 else 0


def recount(result: dict[str, Any], run_url: str) -> None:
    """Recount the rounds the ceiling counts once this round has run, as they stand now.

    Sets ``rounds_spent``, what the selector next compares with the
    ceiling, and ``stale_rounds``, the superseded rounds before this
    one; neither falls below the selection's count. A first
    publication has no round to recount. A read that fails keeps the
    selection's count, with a warning. A ``requested`` nomination
    then at the cap becomes ``skipped``.
    """
    number = count(result.get("round"))
    bot_login = result.get("bot_login")
    if not number or not isinstance(bot_login, str) or not bot_login:
        return
    opened = result.get("pull_opened_at")
    collected = result.get("collected_at")
    try:
        history = rounds_of.round_history(
            github.require_str(result, "repository", "result"),
            github.require_int(result, "issue", "result"),
            bot_login,
            opened if isinstance(opened, str) and opened else None,
            count(result.get("pull_number")) or None,
            (run_url, collected if isinstance(collected, str) and collected else None),
        )
    except (github.GitHubError, reads.SelectionError) as exc:
        cast("list[str]", result.setdefault("warnings", [])).append(
            f"round history not recounted, the selection's count stands: {exc}"
        )
        return
    selected = revisions.rounds_spent(number, result.get("stale_rounds"))
    stale = max(count(result.get("stale_rounds")), history.stale)
    if result.get("superseded") is True:
        # A superseded round raises no round number: it adds one stale.
        spent = history.highest + stale + 1
    else:
        spent = max(history.highest, number) + stale
    result["stale_rounds"] = stale
    result["rounds_spent"] = max(spent, selected, count(result.get("rounds_spent")))
    record = result.get("antagonistic")
    if not isinstance(record, dict):
        return
    if cast("dict[str, Any]", record).get("state") == "requested" and at_cap(result):
        result["antagonistic"] = {"state": "skipped", "reason": AT_CAP}
