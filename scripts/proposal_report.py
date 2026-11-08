# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Merge every per-issue ``result.json`` into the run's report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

import antagonistic_request as antagonistic
import proposal_model as model
import proposal_policy as policy
import report_outputs as outputs
import review_outcome
import revision_reads as revisions
import round_caps as caps
from proposal_policy import PublishError

SCHEMA = 1
REVIEW_COLUMNS = (*review_outcome.REVIEW_VERDICTS, "none")


def cell(value: Any, limit: int = 300) -> str:
    """One table cell: flattened to a line, bounded, pipes escaped.

    Results are not yet authenticated (docs/DESIGN.md 18.4), so every
    field may come from the author job; none may add a row or heading.
    """
    return policy.log_safe(str(value))[:limit].replace("|", "\\|")


def notes_of(value: Any) -> list[str]:
    """A result's reasons or warnings as text, whatever shape they arrived in."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in cast("list[Any]", value)]
    return [str(value)]


def report_row(item: dict[str, Any]) -> str:
    """One table row for a result."""
    verdict = str(item.get("verdict"))
    requests = model.usage_number(item.get("premium_requests"))
    output = item.get("pull_request_url") or item.get("branch_url") or "—"
    if item.get("dry_run") and verdict == "proposed":
        output = "dry run"
    notes = notes_of(item.get("reasons")) + notes_of(item.get("warnings"))
    # Choose the text first, then make it one cell: the title and the
    # reasons alike descend from agent output.
    text = "; ".join(notes) or str(item.get("pr_title") or "")
    label = f"{item.get('repository')}#{item.get('issue')}"
    round_number = item.get("round")
    if type(round_number) is int and round_number > 0:
        label += f" (round {round_number})"
    issue = cell(label)
    copilot = review_cell(item)
    return (
        f"| {issue} | {cell(verdict)} | {cell(output)} | {copilot} "
        f"| {'—' if requests is None else f'{requests:g}'} | {cell(text)} |"
    )


def review_cell(item: dict[str, Any]) -> str:
    """The review column: the verdict, and for findings the counts (issue #29).

    A pending review the workflow could not request says so, apart
    from one requested that did not arrive within the wait.

    A count of the wrong shape stays out of the table; the verdict
    alone is shown then.
    """
    verdict = review_outcome.review_verdict(item)
    if verdict is None:
        return "—"
    if verdict == "pending" and review_outcome.review_unrequested(item):
        return "pending (not requested)"
    if verdict != "findings":
        return verdict
    record = cast("dict[str, Any]", item["review"])
    parts: list[str] = []
    threads = record.get("unresolved_threads")
    if type(threads) is int and threads > 0:
        parts.append(f"{threads} thread{'s' if threads != 1 else ''}")
    suppressed = record.get("suppressed")
    if type(suppressed) is int and suppressed > 0:
        parts.append(f"{suppressed} suppressed")
    missed = record.get("previously_missed")
    if type(missed) is int and missed > 0:
        parts.append(f"{missed} previously missed")
    return f"findings ({', '.join(parts)})" if parts else "findings"


MERGE_COLUMNS = ("true", "false", "unknown", "none")
MERGE_NAMES = {
    "true": "mergeable",
    "false": "conflicting",
    "unknown": "unconfirmed",
    "none": "not probed",
}


def cap_detail(item: dict[str, Any]) -> str:
    """The report's word for a capped round, as the issue comment's notice has it."""
    if not caps.capped(item):
        return ""
    if caps.spent_of(item) >= revisions.MAX_TOTAL_ROUNDS:
        return (
            f"; round cap of {revisions.MAX_TOTAL_ROUNDS} reached in all,"
            " a human must take over"
        )
    if caps.guard_reached(item):
        return (
            f"; {revisions.MAX_STALE_ROUNDS} superseded rounds reached,"
            " a human must look"
        )
    return (
        "; round cap reached for this antagonistic review round, a human must take over"
    )


def conflict_lines(results: list[dict[str, Any]]) -> list[str]:
    """The pull requests whose head conflicts, listed apart from the ready ones."""
    conflicting = [item for item in results if item.get("mergeable") is False]
    if not conflicting:
        return []
    lines = [
        "Pull requests that conflict with their default branch: GitHub ran no "
        "CI on them, so they are not ready whatever their review says. Each "
        "needs a rebase: a later round makes it, or a human at the round cap.",
        "",
    ]
    for item in conflicting:
        label = cell(f"{item.get('repository')}#{item.get('issue')}")
        url = cell(item.get("pull_request_url") or "—")
        behind = item.get("base_behind_by")
        detail = (
            f", {behind} commit{'s' if behind != 1 else ''} behind"
            if type(behind) is int
            else ""
        )
        detail += cap_detail(item)
        lines.append(f"- {label}: {url}{detail}")
    return [*lines, ""]


CHECK_COLUMNS = (*review_outcome.CHECK_STATES, "not read")
# Failing check names a report line lists; the record keeps the rest.
MAX_LISTED_CHECKS = 10


def failing_check_lines(results: list[dict[str, Any]]) -> list[str]:
    """The pull requests published with failing checks, each with the names.

    A head whose wait ended with some checks still running counts when
    it already had failures, as the comment and the round cap treat it.
    Check names come from the code under review: each is bounded and
    escaped as the comment's are, then the line is one cell's worth of
    text, as every result field is.
    """
    failing = [item for item in results if review_outcome.checks_failed(item)]
    if not failing:
        return []
    lines = [
        "Pull requests published with failing checks on their head: the "
        "publish job reports CI and does not gate on it, so a revision "
        "round or a human must fix them.",
        "",
    ]
    for item in failing:
        label = cell(f"{item.get('repository')}#{item.get('issue')}")
        url = cell(item.get("pull_request_url") or "—")
        names = review_outcome.failing_check_names(item)
        listed = policy.failing_checks_text(list(names[:MAX_LISTED_CHECKS]))
        if len(names) > MAX_LISTED_CHECKS:
            listed += f" and {len(names) - MAX_LISTED_CHECKS} more"
        detail = f": {cell(listed)}" if listed else ""
        detail += cap_detail(item)
        lines.append(f"- {label}: {url}{detail}")
    return [*lines, ""]


def attempt_of(item: dict[str, Any]) -> int:
    """The run attempt a result came from; results without one count as 0."""
    value = item.get("run_attempt")
    return value if type(value) is int else 0


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
            cost = model.usage_number(item.get("premium_requests")) or 0.0
            sessions[session] = max(sessions.get(session, 0.0), cost)
    unattributed = sum(
        model.usage_number(item.get("premium_requests")) or 0.0
        for item in shown
        if not item.get("author_session")
    )
    return sum(sessions.values()) + unattributed


def load_results(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Every result under ``root``, the latest per key, and the unreadable ones."""
    unreadable: list[str] = []
    every: list[dict[str, Any]] = []
    latest: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for path in sorted(root.rglob("result.json")):
        try:
            item = model.load_json(path, str(path))
        except PublishError as exc:
            unreadable.append(str(exc))
            continue
        every.append(item)
        key = item.get("key")
        if not isinstance(key, str):
            unkeyed.append(item)
        elif attempt_of(item) >= attempt_of(latest.get(key, {})):
            # A retried publish entry uploads a result per attempt; the
            # latest one describes the issue, so count it once.
            latest[key] = item
    return every, [latest[key] for key in sorted(latest)] + unkeyed, unreadable


def write_outputs(
    args: argparse.Namespace, results: list[dict[str, Any]], lines: list[str]
) -> dict[str, Any]:
    """The pull request links as outputs, when an org is named (DESIGN.md 18.6).

    Returns the fields ``report.json`` gains and adds any warning to
    ``lines``.
    """
    org: str | None = getattr(args, "org", None)
    if org is None:
        return {}
    pulls, excluded = outputs.pull_request_urls(results, org)
    unmergeable = outputs.unmergeable_urls(results, org)
    failing = outputs.failing_check_urls(results, org)
    nominated = antagonistic.nominees(results, pulls)
    extra: dict[str, Any] = {
        "pull_requests": pulls,
        "unmergeable_pull_requests": unmergeable,
        "failing_check_pull_requests": failing,
        "antagonistic_pull_requests": nominated,
        "excluded_pull_request_urls": excluded,
    }
    if nominated:
        lines += [
            "Pull requests nominated for an antagonistic review, which the "
            "request job asks the review bot for when the switch is on: "
            + ", ".join(cell(pull) for pull in nominated)
            + ".",
            "",
        ]
    if excluded:
        # The refused values descend from agent output; only the
        # count is printed.
        lines += [
            f"The run's outputs left {excluded} pull request link"
            f"{'s' if excluded != 1 else ''} out: not a pull request "
            "in this organisation.",
            "",
        ]
        print(
            f"::warning::{excluded} pull request link(s) failed validation "
            "and were left out of the run's outputs"
        )
    github_output: Path | None = getattr(args, "github_output", None)
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(outputs.output_lines(pulls))
            handle.write(outputs.unmergeable_lines(unmergeable))
            handle.write(outputs.failing_check_lines(failing))
            handle.write(antagonistic.output_lines(nominated))
    return extra


def run_report(args: argparse.Namespace) -> None:
    """Merge every result.json into one table."""
    every, results, unreadable = load_results(args.results)
    lines = [
        "## Code monkey results",
        "",
        "| Issue | Verdict | Output | Copilot review | Premium requests | Detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    totals: dict[str, int] = dict.fromkeys(policy.VERDICTS, 0)
    review_totals: dict[str, int] = dict.fromkeys(REVIEW_COLUMNS, 0)
    merge_totals: dict[str, int] = dict.fromkeys(MERGE_COLUMNS, 0)
    check_totals: dict[str, int] = dict.fromkeys(CHECK_COLUMNS, 0)
    for item in results:
        verdict = str(item.get("verdict"))
        totals[verdict] = totals.get(verdict, 0) + 1
        review_totals[review_outcome.review_verdict(item) or "none"] += 1
        merge_totals[review_outcome.merge_token(item) or "none"] += 1
        check_totals[review_outcome.checks_state(item) or "not read"] += 1
        lines.append(report_row(item))
    spend = total_spend(every, results)
    for problem in unreadable:
        lines.append(f"| — | unreadable | — | — | — | {cell(problem)} |")
    if not results and not unreadable:
        lines.append("| — | — | — | — | — | no proposals |")
    lines += [
        "",
        f"Proposed {totals['proposed']}, abstained {totals['abstain']}, "
        f"handed off {totals.get('handoff', 0)}, "
        f"rejected {totals['rejected']}, failed {totals['author-failed']}, "
        f"publish failures {totals.get('publish-failed', 0)}; "
        f"premium requests {spend:g}.",
        "",
        "Copilot reviews: "
        + ", ".join(f"{name} {review_totals[name]}" for name in REVIEW_COLUMNS)
        + ".",
        "",
        "Pull request heads: "
        + ", ".join(
            f"{MERGE_NAMES[name]} {merge_totals[name]}" for name in MERGE_COLUMNS
        )
        + ".",
        "",
        "Head checks: "
        + ", ".join(f"{name} {check_totals[name]}" for name in CHECK_COLUMNS)
        + ".",
        "",
        *conflict_lines(results),
        *failing_check_lines(results),
        policy.REVISION_DISCLOSURE,
        "",
    ]
    extra = write_outputs(args, results, lines)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text("\n".join(lines), encoding="utf-8")
    args.output_json.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "totals": totals,
                "review_totals": review_totals,
                "merge_totals": merge_totals,
                "check_totals": check_totals,
                "premium_requests": spend,
                "unreadable": unreadable,
                "results": results,
                **extra,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
