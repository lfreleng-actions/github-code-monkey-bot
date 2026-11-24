# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What a session's usage file says it cost and which models it ran on."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any, cast

# A model id from the CLI's usage file, which the untrusted author job
# writes: the shape alone makes it safe in a code span or a table cell.
MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
MAX_MODELS = 10


def usage_models(metrics: Any) -> tuple[list[str], int]:
    """The model ids a usage file's ``modelMetrics`` names, and the count dropped.

    Only the top-level keys count, each with an object for its value
    and an id's shape; at most ``MAX_MODELS`` stay, sorted. Anything
    else is dropped without a reason: it changes no verdict.
    """
    if not isinstance(metrics, dict):
        return [], 0
    entries = cast("dict[Any, Any]", metrics)
    valid = sorted(
        key
        for key, value in entries.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and MODEL_ID_RE.fullmatch(key)
    )
    kept = valid[:MAX_MODELS]
    return kept, len(entries) - len(kept)


def model_line(model: str, fusion_tier: str, models_used: Sequence[str]) -> str:
    """The provenance block's model line: what was asked for, and what ran.

    A fixed model names the models it routed to only when they differ
    from the one requested; a Copilot Fusion run names its tier.
    """
    routed = ", ".join(f"`{name}`" for name in models_used)
    if fusion_tier:
        asked = f"Copilot Fusion (`{fusion_tier}` tier)"
    else:
        asked = f"`{model}`"
        if set(models_used) <= {model}:
            routed = ""
    if routed:
        asked += f", routed to {routed}"
    return f"- Model: {asked} via GitHub Copilot CLI"


def usage_number(value: Any) -> float | None:
    """A finite, non-negative number from untrusted JSON, or None.

    Booleans are ints in Python and 1e309 parses to infinity; neither
    is a spend figure, and either would corrupt a total or make
    ``round`` raise inside the publisher.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # A JSON integer with hundreds of digits does not fit a float.
        return None
    if not math.isfinite(number) or number < 0 or number > 1e12:
        return None
    return number


def total_spend(every: list[dict[str, Any]], shown: list[dict[str, Any]]) -> float:
    """Premium requests the run paid for, each author session once.

    A rerun author job is a new session with its own cost, even when a
    later attempt supersedes its row; a publish retry reuses the same
    session. Results without a session count only where shown.
    """
    sessions: dict[str, float] = {}
    for item in every:
        session = item.get("author_session")
        if isinstance(session, str) and session:
            cost = usage_number(item.get("premium_requests")) or 0.0
            sessions[session] = max(sessions.get(session, 0.0), cost)
    unattributed = sum(
        usage_number(item.get("premium_requests")) or 0.0
        for item in shown
        if not item.get("author_session")
    )
    return sum(sessions.values()) + unattributed


def shown_models(value: Any) -> list[str]:
    """The model ids of a result's ``models_used`` fit to show (issue #240).

    A result is not yet authenticated (docs/DESIGN.md 18.4), so only
    ids of a model id's shape count, at most ``MAX_MODELS`` of them.
    """
    if not isinstance(value, list):
        return []
    names = cast("list[Any]", value)
    return [n for n in names if isinstance(n, str) and MODEL_ID_RE.fullmatch(n)][
        :MAX_MODELS
    ]
