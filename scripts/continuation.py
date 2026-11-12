# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which published pull requests to continue with another revision round.

A run performs one round per pull request and ends. When a round's
publication left work outstanding, the scheduled caller dispatches
the next round at once rather than leave it for the hourly scan
(docs/DESIGN.md section 20.6). The next round is a new run whose
select job re-reads everything from GitHub and applies the scan's
tests, so the rule here only decides who starts it; a forged result
costs one select job there, or brings forward a round the scan would run.

The report job applies the rule to every result, over the pull
request links it already accepted, and never trusts a recorded state
but the antagonistic request, which can only stop a continuation.
"""

from __future__ import annotations

import json
from typing import Any, cast

import review_outcome
from antagonistic_request import nominee, requested
from round_caps import AT_CAP, at_cap


def outstanding(item: dict[str, Any]) -> list[str]:
    """What the published head left to do, by the result's own record."""
    reasons: list[str] = []
    if review_outcome.review_verdict(item) == "findings":
        head = review_outcome.published_commit(item)
        commit = cast("dict[str, Any]", item["review"]).get("commit_id")
        if head is not None and commit == head:
            reasons.append("Copilot's review of the head has findings")
    if review_outcome.checks_failed(item):
        reasons.append("the head has failing checks")
    if item.get("mergeable") is False:
        reasons.append("the head conflicts with the default branch")
    return reasons


def unmet(item: dict[str, Any]) -> str | None:
    """The first continuation condition a result fails, or None when it continues.

    Anything but a published proposal falls back to the schedule, so
    a run that fails never dispatches itself again.
    """
    if item.get("verdict") != "proposed":
        return "not a published proposal"
    if item.get("dry_run") is not False:
        return "a dry run"
    if not item.get("pull_request_url"):
        return "no pull request was opened"
    # The round history counts rounds by their outcome comments, so a
    # round with none would leave the next one the same number and
    # neither cap would advance.
    if not item.get("comment_url"):
        return "the outcome comment was not posted"
    if review_outcome.review_verdict(item) == "pending":
        return "Copilot's review is pending; the scan picks it up"
    if requested(item):
        return "an antagonistic review was requested; its hand-back continues"
    # The segment's cap, the sixty-round ceiling and the superseded
    # rounds' guard: the selector would refuse the next round.
    if at_cap(item):
        return AT_CAP
    if not outstanding(item):
        return "nothing outstanding"
    return None


def continuing(results: list[dict[str, Any]], urls: list[str]) -> list[str]:
    """``owner/repo#N`` for each validated URL whose result continues, in order.

    ``urls`` are the report's own validated links; a result counts only
    when its link is among them, so nothing outside the organisation
    or of another shape is ever named.
    """
    allowed = {url.lower() for url in urls}
    named: list[str] = []
    seen: set[str] = set()
    for item in results:
        url = item.get("pull_request_url")
        if not isinstance(url, str) or url.lower() not in allowed:
            continue
        if unmet(item) is not None:
            continue
        pull = nominee(url)
        if pull is not None and pull.lower() not in seen:
            seen.add(pull.lower())
            named.append(pull)
    return named


def output_lines(pulls: list[str]) -> str:
    """The ``$GITHUB_OUTPUT`` line for the continuing pull requests."""
    return f"continue_pull_requests={json.dumps(pulls, separators=(',', ':'))}\n"
