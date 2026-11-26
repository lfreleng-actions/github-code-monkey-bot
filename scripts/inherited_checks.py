# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Failing checks a bot pull request inherited from its default branch.

When the default branch is red itself (an upstream tool release, an
expired image, a hook autoupdate), every bot pull request carries the
failure and no change of its own can fix it. The selection compares
each failing check on the head with the checks of the same name, a
check run's name or a status's context, on the default branch's head
(DESIGN.md section 20.1). A failing defect whose name the default
branch head also fails, as a defect, is inherited: it brings no
round. A name the default branch head lacks, passes, failed at the
egress agent alone, carries annotations the bound left unread, or has
yet to finish stays the pull request's own: the comparison fails
closed.

Names come from the code under review: data, bounded and escaped
wherever they reach a comment or the report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import check_reads as checks
import proposal_policy as policy
import stale_statuses as stale

# Inherited check names one report line lists.
MAX_LISTED = 10


def failing_checks(
    repository: str,
    head_sha: str,
    rule: stale.Rule = stale.OFF,
    *,
    default_sha: str | None = None,
) -> checks.CheckPacket:
    """The head's failing checks, as ``check_reads.failing_checks`` reads them.

    With ``default_sha``, the default branch head, a head with a
    failure has that commit's checks read too (``baseline``); each
    entry then carries ``inherited``, and the packet counts the
    inherited ones apart from ``outstanding``.
    """
    live, late = rule.split(checks.head_runs(repository, head_sha))
    shared: frozenset[str] = frozenset()
    running: list[str] = []
    if default_sha and any(checks.is_failing(run) for run in live):
        shared, running = baseline(repository, default_sha, live, rule)
    packet = checks.failing_of(
        repository, live, inherited=shared if default_sha else None
    )
    return replace(
        packet,
        stale=rule.entries(late),
        default_sha=default_sha or None,
        default_running=running,
    )


def baseline(
    repository: str,
    default_sha: str,
    runs: list[dict[str, Any]],
    rule: stale.Rule = stale.OFF,
) -> tuple[frozenset[str], list[str]]:
    """The names failing in ``runs`` the default branch head fails, and those it runs.

    The default branch head is read as the head is, bounded and
    failing closed the same way, its stale statuses set apart. A name
    fails there when a check of that name failed as a defect and none
    of that name has yet to finish; annotations are read only for the
    failing checks there whose name ``runs`` fails, and a check whose
    annotations the bound left partly unread is not counted as failing
    there, since an unread one may show the egress agent failed. The
    running names come back sorted and bounded, as the packet carries
    them.
    """
    names = {str(run.get("name") or "") for run in runs if checks.is_failing(run)}
    theirs, _ = rule.split(checks.head_runs(repository, default_sha))
    running = {
        name
        for run in theirs
        if (name := str(run.get("name") or "")) in names and checks.is_unsettled(run)
    }
    failing: set[str] = set()
    for run in theirs:
        name = str(run.get("name") or "")
        if name not in names or name in running | failing or not checks.is_failing(run):
            continue
        annotations, unread = checks.run_annotations(repository, run)
        # An unread annotation may be the egress agent's: fail closed.
        if not unread and checks.kind_of(annotations) == checks.DEFECT:
            failing.add(name)
    shown = [checks.text(name) for name in sorted(running)[: checks.MAX_CHECKS]]
    return frozenset(failing), shown


def skip_reason(default_branch: str) -> str:
    """The skip for a head whose only failures the default branch fails too."""
    return f"failing checks inherited from {default_branch}"


def sentence(names: Any, branch: Any, sha: Any) -> str:
    """The inherited checks as one comment-safe sentence, or empty.

    Names the default branch and, when ``sha`` is a full commit SHA,
    the head the selection read them on. No trailing period.
    """
    listed = policy.failing_checks_text(
        list(cast("list[Any]", names)) if isinstance(names, list) else []
    )
    if not listed:
        return ""
    where = "the default branch"
    if isinstance(branch, str) and branch.strip():
        where = f"`{policy.comment_safe(branch[:100]).replace('`', '')}`"
    if isinstance(sha, str) and policy.SHA_RE.fullmatch(sha):
        where += f" at {sha[:12]}"
    return f"Failing checks inherited from {where}, not this pull request's: {listed}"


def result_sentence(result: dict[str, Any]) -> str:
    """The inherited checks a round's result records, as ``sentence`` words them."""
    return sentence(
        result.get("inherited_checks"),
        result.get("default_branch"),
        result.get("inherited_sha"),
    )


def report_lines(
    results: list[dict[str, Any]], cell: Callable[[Any], str]
) -> list[str]:
    """The rounds whose head also failed checks the default branch fails.

    Listed apart from the failing checks, since no change of the pull
    request's own fixes them; at most ``MAX_LISTED`` names a line.
    ``cell`` makes each line one cell's worth of text, as the report's
    other lines are.
    """
    listed: list[str] = []
    for item in results:
        names = item.get("inherited_checks")
        names = list(cast("list[Any]", names)) if isinstance(names, list) else []
        text = result_sentence({**item, "inherited_checks": names[:MAX_LISTED]})
        if not text:
            continue
        if len(names) > MAX_LISTED:
            text += f" and {len(names) - MAX_LISTED} more"
        label = cell(f"{item.get('repository')}#{item.get('issue')}")
        url = cell(item.get("pull_request_url") or "—")
        listed.append(f"- {label}: {url}: {cell(text)}")
    if not listed:
        return []
    return [
        "Revision rounds whose head also failed checks the default branch head "
        "fails: those are the default branch's to fix, not the pull request's.",
        "",
        *listed,
        "",
    ]
