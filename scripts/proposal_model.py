# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The verdict record the publisher builds and the report renders."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import hook_runs
import proposal_policy as policy
import review_outcome as outcome
from proposal_policy import Identity, PublishError
from workflow_grant import KINDS, Grant

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


def fork_tree_url(check: dict[str, Any]) -> str:
    """Where the bot branch lives: in the fork, never the target."""
    return f"https://github.com/{check['fork_repository']}/tree/{check['branch']}"


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
    # Whether the chain changes the root .pre-commit-config.yaml, the
    # one protected path an operator switch unlocks (DESIGN.md 8).
    changes_precommit_config: bool = False
    # The labels that let the proposal change workflow files and the
    # root .pre-commit-config.yaml, or blank (workflow_grant.KINDS).
    workflow_edits_label: str = ""
    precommit_edits_label: str = ""
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
    # The pull request's own base as the selection recorded it; a
    # rebase moves ``base_sha`` but never this.
    pull_base_sha: str = ""
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
    # Superseded rounds before this one: the ceiling reads them with
    # ``round`` (DESIGN.md 20.1).
    stale_rounds: int = 0
    # When the pull request opened: the publisher's recount of the
    # round history scopes its footers there, as the selector did.
    pull_opened_at: str = ""
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
    # Whether the author job stood the revision round down because
    # another run moved the branch first: its footer then marks the
    # round stale, outside every segment's cap. Only the stand-down
    # sets the manifest field; the bundle step strips the agent's.
    superseded: bool = False
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
    # What the online zizmor audit found (DESIGN.md section 8): its
    # pass note; blank when the check never reached it.
    online_audit: str = ""
    # The listed issue authors the selection ran with: the publisher
    # re-reads the issue's author and last editor against them.
    issue_authors: list[str] = field(default_factory=lambda: cast("list[str]", []))
    # Whether the selection ran with the rule on; off only when it
    # recorded ONLY_KNOWN_AUTHORS false, and then the list is empty.
    only_known_authors: bool = True
    # Whether that list refused the entry: nothing writes to the target.
    author_unlisted: bool = False

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
            check.pull_base_sha = check.base_sha
            check.head_sha = str(pull["head_sha"])
            check.pull_number = int(pull["number"])
            check.pull_request_url = str(pull["url"])
            check.round = int(entry["round"])
            check.max_rounds = int(entry.get("max_rounds") or 0)
            segment = entry.get("segment_round")
            # An entry without one caps as the global round did.
            check.segment_round = segment if type(segment) is int else check.round
            stale = entry.get("stale_rounds")
            check.stale_rounds = stale if type(stale) is int else 0
            check.pull_opened_at = str(pull.get("created_at") or "")
            check.conflicted = pull.get("mergeable") is False
            check.threads = [
                {"thread_id": str(t["thread_id"]), "comment_id": int(t["comment_id"])}
                for t in cast("list[dict[str, Any]]", entry.get("threads") or [])
            ]
            # A run the egress agent failed is not one the round fixes.
            check.failing_checks = [
                str(c["name"])
                for c in cast("list[dict[str, Any]]", entry.get("checks") or [])
                if c.get("kind") != "infrastructure"
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
            "only_known_authors": self.only_known_authors,
            "issue_authors": self.issue_authors,
            "author_unlisted": self.author_unlisted,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "needs_workflows": self.needs_workflows,
            "changes_precommit_config": self.changes_precommit_config,
            "workflow_edits_label": self.workflow_edits_label,
            "precommit_edits_label": self.precommit_edits_label,
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
            "stale_rounds": self.stale_rounds,
            "pull_opened_at": self.pull_opened_at,
            "responses": self.responses,
            "round_summary": self.round_summary,
            "failing_checks": self.failing_checks,
            "checks_unsettled": self.checks_unsettled,
            "conflicted": self.conflicted,
            "superseded": self.superseded,
            "antagonistic_review": self.antagonistic_review,
            "collected_at": self.collected_at,
            "session": self.session,
            "retry_of": self.retry_of,
            "online_audit": self.online_audit,
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
    zizmor_token: str = ""
    # The entry's grants to change workflow files and the root
    # .pre-commit-config.yaml: the run's switch or the issue's label.
    workflow_grant: Grant = field(default_factory=Grant)
    precommit_grant: Grant = field(default_factory=Grant)


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
        lines += [
            "",
            f"Needs `workflows: write`: {check.needs_workflows}",
            "",
            f"Changes `{policy.PRECOMMIT_CONFIG}`: {check.changes_precommit_config}",
            "",
        ]
        for kind in KINDS:
            label = getattr(check, kind.label_field)
            if label:
                edits = kind.edits.capitalize()
                lines += [f"{edits} granted by the `{label}` label on the issue.", ""]
    if check.online_audit:
        lines += [policy.log_safe(check.online_audit), ""]
    return "\n".join(lines) + "\n"


def provenance_block(
    *,
    model: str,
    run_url: str,
    issue_url: str,
    base_sha: str,
    commands: list[dict[str, Any]],
    changes_precommit_config: bool = False,
    precommit_edits_label: str = "",
) -> str:
    """The block that heads every pull request body, before agent text.

    A change to the lint gate shows in the summary line as well as the
    list, so a reviewer sees it with the block collapsed; the list
    names the label that granted it, when one did.
    """
    summary = "Authored by an AI agent; human review required"
    if changes_precommit_config:
        summary += f"; changes the lint gate, {policy.PRECOMMIT_CONFIG}"
    lines = [
        "<details>",
        f"<summary>{summary}</summary>",
        "",
        f"- Issue: {issue_url}",
        f"- Model: `{model}` via GitHub Copilot CLI",
        f"- Run: {run_url or 'n/a'}",
        f"- Base: `{base_sha}`",
    ]
    if changes_precommit_config:
        granted = (
            f"; the `{precommit_edits_label}` label on the issue granted it"
            if precommit_edits_label
            else ""
        )
        lines.append(
            f"- Lint gate: this pull request changes `{policy.PRECOMMIT_CONFIG}`, "
            f"which decides the checks the repository runs{granted}; review "
            "every hook change"
        )
    lines.append("")
    if commands:
        lines += ["| Command | Exit |", "| --- | --- |"]
        for item in commands:
            # Agent text, one inert line: it cannot leave its cell or block.
            command = hook_runs.cell(str(item["command"]))
            code = item["exit_code"]
            lines.append(f"| `{command}` | {code if code is not None else '?'} |")
        lines.append("")
    lines += [
        "The `github-code-monkey-bot` workflow opened this pull request.",
        "Nobody has merged it; a maintainer must review it first.",
        policy.REVISION_DISCLOSURE,
        "</details>",
        "",
    ]
    return "\n".join(lines)
