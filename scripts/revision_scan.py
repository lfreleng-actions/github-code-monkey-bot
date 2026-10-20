# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The scan for the bot's open pull requests, ranked and built on demand.

The search is one read; a round's packet is many. The scan reads
each hit's pull request and issue to rank the rounds by the issue's
priority and then the pull request's age (issue #19), and hands the
builders to the selection, which calls each when its budget has room
(DESIGN.md section 20.1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import fork_orgs as forks
import revision_reads as revisions
from fork_orgs import ForkOrgError
from issue_reads import SelectionError
from revision_select import Inputs, Prefetch, Round, Skip, build_entry, read_issue


def scan(
    org: str,
    *,
    bot: dict[str, Any],
    fork_orgs: dict[str, Any],
    skipped: dict[str, int],
    skipped_issues: list[dict[str, str]],
    allowed: Callable[[str], bool] | None = None,
) -> list[Round]:
    """A builder per bot pull request in the owner, ranked, built on demand.

    The search is one read; building an entry is many. The scan reads
    each hit's pull request and issue, two reads, to rank the rounds
    by the issue's priority and then the pull request's age, as issue
    #19 asks, and hands both on so the build repeats neither. The
    packet, the many reads, waits for the budget to call the builder,
    which returns the entry or None after recording the skip; the
    repository beside it lets the budget refuse a second round of one
    repository unread. ``allowed`` is the run's repository policy, by
    lower-cased name; a hit outside it gets no builder.
    """
    inputs = Inputs(org, bot, fork_orgs)
    slug = str(bot["login"]).removesuffix("[bot]")
    found = revisions.search_bot_pull_requests(org, slug)
    ranked: list[tuple[tuple[int, str, int], str, int, Prefetch]] = []
    for hit in sorted(found, key=lambda h: (str(h["created_at"]), int(h["number"]))):
        repo_name = str(hit["repository"]).partition("/")[2]
        if allowed is not None and not allowed(repo_name.lower()):
            continue
        reference = f"{hit['repository']}#{hit['number']}"
        number = int(hit["number"])
        try:
            prefetched = prefetch(inputs, repo_name, number)
        except Skip as exc:
            skipped["revision"] += 1
            skipped_issues.append({"issue": reference, "reason": str(exc)})
            continue
        rank = (
            priority_rank(prefetched.details.get("priority")),
            str(hit["created_at"]),
            number,
        )
        ranked.append((rank, repo_name, number, prefetched))
    builders: list[Round] = []
    for _, repo_name, number, prefetched in sorted(ranked, key=lambda r: r[0]):
        reference = f"{org}/{repo_name}#{number}"

        def build(
            repo_name: str = repo_name,
            number: int = number,
            reference: str = reference,
            prefetched: Prefetch = prefetched,
        ) -> dict[str, Any] | None:
            try:
                return build_entry(
                    inputs, repo_name, number, explicit=False, prefetched=prefetched
                )
            except Skip as exc:
                if str(exc) != "nothing outstanding":
                    skipped["revision"] += 1
                    skipped_issues.append({"issue": reference, "reason": str(exc)})
                return None

        builders.append((f"{org}/{repo_name}", build))
    return builders


def prefetch(inputs: Inputs, repo_name: str, number: int) -> Prefetch:
    """The pull request and its issue, read to rank a candidate; ``Skip`` when not ours."""
    repository = f"{inputs.org}/{repo_name}"
    try:
        fork_org = forks.resolve(inputs.fork_orgs, inputs.org)
    except ForkOrgError as exc:
        raise SelectionError(str(exc)) from exc
    pull = revisions.bot_pull_request(
        repository, number, str(inputs.bot["login"]), f"{fork_org}/{repo_name}"
    )
    if pull is None:
        raise Skip("not an open pull request the bot opened from its fork")
    return Prefetch(pull, read_issue(repository, int(pull["issue"])))


# The issue priorities the organisation uses, urgent first; an issue
# with none ranks last.
PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}


def priority_rank(priority: Any) -> int:
    """Where a priority sorts; an unknown or absent one sorts last."""
    if isinstance(priority, str) and priority in PRIORITY_RANK:
        return PRIORITY_RANK[priority]
    return len(PRIORITY_RANK)
