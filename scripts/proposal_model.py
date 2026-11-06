# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The verdict record the publisher builds and the report renders."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import proposal_policy as policy
import review_outcome as outcome
from proposal_policy import Identity, PublishError

SCHEMA = 1


def load_json(path: Path, context: str) -> dict[str, Any]:
    """Parse a JSON object file, treating anything else as an error."""
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PublishError(f"{context}: unreadable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PublishError(f"{context}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


@dataclass
class Check:
    """Accumulates the verdict for one proposal."""

    key: str
    repository: str
    issue: int
    branch: str
    base_sha: str
    default_branch: str
    fork_org: str = ""
    fork_repository: str = ""
    verdict: str = "proposed"
    reasons: list[str] = field(default_factory=lambda: cast("list[str]", []))
    needs_workflows: bool = False
    commits: list[dict[str, Any]] = field(
        default_factory=lambda: cast("list[dict[str, Any]]", [])
    )
    pr_title: str = ""
    pr_body: str = ""
    files_changed: int = 0
    added_bytes: int = 0
    commands: list[dict[str, Any]] = field(
        default_factory=lambda: cast("list[dict[str, Any]]", [])
    )
    premium_requests: float | None = None
    bot_login: str = ""
    agent_seconds: int | None = None
    warnings: list[str] = field(default_factory=lambda: cast("list[str]", []))
    # A revision round (DESIGN.md section 20): the pull request being
    # revised, the other base a rebase may land on, and the trusted
    # thread ids a reply may name. Blank or empty for an issue.
    task: str = "issue"
    rebase_sha: str = ""
    rebased: bool = False
    head_sha: str = ""
    pull_number: int = 0
    pull_request_url: str = ""
    round: int = 0
    # The round cap the selection ran with (0: the default).
    max_rounds: int = 0
    # The round's place in its antagonistic segment, which the cap
    # reads; ``round`` stays the global number.
    segment_round: int = 0
    threads: list[dict[str, Any]] = field(
        default_factory=lambda: cast("list[dict[str, Any]]", [])
    )
    responses: list[dict[str, Any]] = field(
        default_factory=lambda: cast("list[dict[str, Any]]", [])
    )
    round_summary: str = ""
    # The names of the head's failing checks the selection carried:
    # what the round set out to fix besides the threads.
    failing_checks: list[str] = field(default_factory=lambda: cast("list[str]", []))
    # How many of the head's checks the selection found still running:
    # an empty ``failing_checks`` then proves nothing about CI.
    checks_unsettled: int = 0
    # Whether the selection found the head conflicting with the default
    # branch: the round exists, in part, to rebase it.
    conflicted: bool = False
    # The antagonistic review that handed the pull request back, as the
    # selection read it (``review_id``, ``round``, ``url``), or None.
    antagonistic_review: dict[str, Any] | None = None
    # When the selection read the issue's comments: the watermark the
    # round footer carries, so a comment posted during the run is new
    # work for the next round.
    collected_at: str = ""
    # The run's session: unique per run, the same across its retries,
    # and trusted; the scratch branch and the reply marker are its.
    session: str = ""
    # A named retry past a closed pull request (DESIGN.md section 6):
    # that pull request's number, the one whose branch the publisher
    # may reset. None for every other entry, a scan's included.
    retry_of: int | None = None

    @classmethod
    def from_entry(cls, key: str, entry: dict[str, Any]) -> Check:
        """Start a check from a trusted selection entry."""
        check = cls(
            key=key,
            repository=str(entry["repository"]),
            issue=int(entry["number"]),
            branch=str(entry["branch"]),
            base_sha=str(entry["base_sha"]),
            default_branch=str(entry["default_branch"]),
            fork_org=str(entry["fork_org"]),
            fork_repository=str(entry["fork_repository"]),
            task=str(entry.get("task") or "issue"),
        )
        check.collected_at = str(entry.get("collected_at") or "")
        check.session = str(entry.get("session") or "")
        retry_of = entry.get("retry_of")
        check.retry_of = retry_of if type(retry_of) is int else None
        if check.task == "revise":
            pull = cast("dict[str, Any]", entry["pull_request"])
            check.rebase_sha = str(entry["rebase_sha"])
            check.head_sha = str(pull["head_sha"])
            check.pull_number = int(pull["number"])
            check.pull_request_url = str(pull["url"])
            check.round = int(entry["round"])
            check.max_rounds = int(entry.get("max_rounds") or 0)
            segment = entry.get("segment_round")
            # An entry without one caps as the global round did.
            check.segment_round = segment if type(segment) is int else check.round
            check.conflicted = pull.get("mergeable") is False
            check.threads = [
                {"thread_id": str(t["thread_id"]), "comment_id": int(t["comment_id"])}
                for t in cast("list[dict[str, Any]]", entry.get("threads") or [])
            ]
            check.failing_checks = [
                str(c["name"])
                for c in cast("list[dict[str, Any]]", entry.get("checks") or [])
            ]
            check.checks_unsettled = outcome.unsettled_count(
                entry.get("checks_unsettled")
            )
            handback = entry.get("antagonistic_review")
            if isinstance(handback, dict):
                data = cast("dict[str, Any]", handback)
                check.antagonistic_review = {
                    key: data[key] for key in ("review_id", "round", "url")
                }
        return check

    def to_json(self) -> dict[str, Any]:
        """Serialise for check.json."""
        return {
            "schema": SCHEMA,
            "key": self.key,
            "bot_login": self.bot_login,
            "repository": self.repository,
            "issue": self.issue,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "default_branch": self.default_branch,
            "fork_org": self.fork_org,
            "fork_repository": self.fork_repository,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "needs_workflows": self.needs_workflows,
            "commits": self.commits,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "stats": {
                "files_changed": self.files_changed,
                "added_bytes": self.added_bytes,
            },
            "commands": self.commands,
            "premium_requests": self.premium_requests,
            "agent_seconds": self.agent_seconds,
            "warnings": self.warnings,
            "task": self.task,
            "rebase_sha": self.rebase_sha,
            "rebased": self.rebased,
            "head_sha": self.head_sha,
            "pull_number": self.pull_number,
            "pull_request_url": self.pull_request_url,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "segment_round": self.segment_round,
            "responses": self.responses,
            "round_summary": self.round_summary,
            "failing_checks": self.failing_checks,
            "checks_unsettled": self.checks_unsettled,
            "conflicted": self.conflicted,
            "antagonistic_review": self.antagonistic_review,
            "collected_at": self.collected_at,
            "session": self.session,
            "retry_of": self.retry_of,
        }


def read_usage(path: Path, check: Check) -> None:
    """Pull the spend figures from the CLI's usage file, if present."""
    if not path.is_file():
        return
    try:
        usage = load_json(path, "usage")
    except PublishError as exc:
        check.reasons.append(f"usage.json unreadable: {exc}")
        return
    cost = usage_number(usage.get("totalPremiumRequestCost"))
    if cost is not None:
        check.premium_requests = cost
    duration = usage_number(usage.get("totalApiDurationMs"))
    if duration is not None:
        check.agent_seconds = int(duration // 1000)


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


@dataclass(frozen=True)
class Context:
    """Everything the verification needs beyond the proposal itself."""

    proposal_dir: Path
    workdir: Path
    identity: Identity
    model: str
    run_url: str
    issue_url: str


def check_summary(check: Check) -> str:
    """Markdown for the step summary."""
    lines = [f"### {check.repository}#{check.issue} — `{check.verdict}`", ""]
    for reason in check.reasons:
        lines.append(f"- {policy.log_safe(reason)}")
    if check.commits:
        lines += ["", "| Commit | Files | Bytes |", "| --- | --- | --- |"]
        for commit in check.commits:
            files = len(commit["additions"]) + len(commit["deletions"])
            size = sum(int(item["size"]) for item in commit["additions"])
            headline = str(commit["headline"]).replace("|", "\\|")
            lines.append(f"| {headline} | {files} | {size} |")
        lines += ["", f"Needs `workflows: write`: {check.needs_workflows}", ""]
    return "\n".join(lines) + "\n"
