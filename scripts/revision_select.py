# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Revision candidates: the bot's open pull requests with work outstanding.

A revision entry sends a session back to a pull request the bot
opened, to answer its review and push the same branch forward. It
carries what the issue entry carries, plus the pull request, its
unresolved threads, Copilot's verdict on the head, an antagonistic
review that handed the head back, the head's failing checks, the
rounds so far and the maintainers' comments on the pull request, all
as data.
Both bases are recorded: the chain's own, and the default branch's
head, for a rebase (DESIGN.md section 20).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import bot_github as github
import check_reads
import explicit_issues
import fork_orgs as forks
import issue_authors as authors
import issue_reads as reads
import merge_state
import repository_reads as repositories
import revision_reads as revisions
import round_reads as rounds_of
import thread_reads
from fork_orgs import ForkOrgError
from issue_reads import SelectionError

TASK = "revise"


class Skip(Exception):
    """The pull request cannot be revised; the message says why."""


@dataclass(frozen=True)
class Inputs:
    """What every build shares: the owner, the bot, the forks and the reviewers."""

    org: str
    bot: dict[str, Any]
    fork_orgs: dict[str, Any]
    # The antagonistic review Apps whose threads and hand-backs count
    # (empty: none), and the rounds a pull request may receive.
    reviewers: frozenset[str] = frozenset()
    max_rounds: int = revisions.MAX_ROUNDS


@dataclass(frozen=True)
class Prefetch:
    """Reads a scan made to rank a candidate, handed on so the build repeats none."""

    pull: dict[str, Any]
    details: dict[str, Any]


@dataclass(frozen=True)
class Findings:
    """What the head's reviewers said: threads, verdicts, a hand-back and checks."""

    threads: thread_reads.ThreadPacket
    verdict: dict[str, Any] | None
    checks: check_reads.CheckPacket
    handback: dict[str, Any] | None = None


def read_findings(
    repository: str,
    pull: dict[str, Any],
    since: str | None,
    reviewers: frozenset[str] = frozenset(),
) -> Findings:
    """The unresolved threads, the head's reviews and its failing checks.

    The hand-back is read before the threads and again after every
    other read. GitHub posts a review together with its inline
    comments, so the threads read in between hold every finding the
    first read's hand-back placed on the diff. A hand-back posted
    between the reads would put its threads in the packet without its
    body, and a round they start moves the head past the review, so
    its body findings would never reach a round. The two reads must
    name the same review, or none; otherwise the read fails, as every
    revision read does, and the next run reads both together.
    """
    number, head = int(pull["number"]), str(pull["head_sha"])
    handback = revisions.handback_review(repository, number, head, reviewers)
    findings = Findings(
        thread_reads.unresolved_threads(repository, number, since, reviewers),
        revisions.latest_review(repository, number, head),
        check_reads.failing_checks(repository, head),
        handback,
    )
    recheck = revisions.handback_review(repository, number, head, reviewers)
    if review_id_of(recheck) != review_id_of(handback):
        raise github.GitHubError(
            f"antagonistic hand-back on {repository}#{number} changed during the"
            " read; retry so its threads and body are read together"
        )
    return findings


def review_id_of(handback: dict[str, Any] | None) -> int | None:
    """The hand-back's review id, or None when there is none."""
    return None if handback is None else int(handback["review_id"])


def outstanding(
    pull: dict[str, Any],
    findings: Findings,
    *,
    repository: str,
    since: str | None,
) -> list[str]:
    """Why the pull request needs a round, by the organisation definition.

    Work is outstanding when a thread from Copilot, a listed App or a
    maintainer is unresolved, when Copilot's review of the head has
    findings, when an antagonistic review of the head handed it back
    after the last round's watermark, when a check on the head
    failed, outside the exclusion list, when the head conflicts with
    the default branch, so no CI ran on it, or when a maintainer
    commented on the issue or the pull request after the bot's last
    round. A maintainer's word on a review thread after that round,
    an opener or a reply, counts as a comment, since the issue and
    pull request comments do not carry it; the thread
    reads heard it over every thread, the packet's bounds aside. A
    head with no review yet is left for the gate to await, and a clean
    head with nothing else is done, or skips as still running while
    its checks have yet to finish. A ``mergeable`` that never settled
    is no work on its own, so a GitHub hiccup cannot spend rounds.
    """
    packet, verdict = findings.threads, findings.verdict
    issue = int(pull["issue"])
    reasons: list[str] = []
    total = len(packet.threads) + packet.dropped
    if total:
        reasons.append(f"{total} unresolved review thread(s)")
    if verdict is not None and verdict.get("verdict") == "findings":
        reasons.append("Copilot's review of the head has findings")
    handback = revisions.unanswered(findings.handback, since)
    if handback is not None:
        reasons.append(f"antagonistic review round {handback['round']} handed back")
    if findings.checks.outstanding:
        reasons.append("failing checks")
    if pull.get("mergeable") is False:
        reasons.append("conflicts with the default branch")
    for number, what in ((issue, "issue"), (int(pull["number"]), "pull request")):
        if rounds_of.newer_member_comment(repository, number, since):
            reasons.append(f"a maintainer commented on the {what} since the last round")
    if packet.maintainer_since:
        reasons.append("a maintainer commented on a review thread since the last round")
    return reasons


def build_entry(
    inputs: Inputs,
    repo_name: str,
    pull_number: int,
    *,
    explicit: bool,
    prefetched: Prefetch | None = None,
) -> dict[str, Any]:
    """The selection entry for one bot pull request, or raise ``Skip``.

    ``explicit`` means an operator named it: the work-outstanding
    test and the author rule do not apply, since the operator decided,
    but the other safety tests and the round cap do. ``prefetched``
    carries the pull request and issue a scan read to rank it.
    """
    org, bot, fork_orgs = inputs.org, inputs.bot, inputs.fork_orgs
    meta = repositories.repository_meta(org, repo_name)
    obstacle = explicit_issues.repository_obstacle(meta)
    if obstacle is not None or meta is None:
        raise Skip(f"repository is {obstacle}")
    repository = f"{org}/{meta['name']}"
    try:
        fork_org = forks.resolve(fork_orgs, org)
    except ForkOrgError as exc:
        raise SelectionError(str(exc)) from exc
    fork_repository = f"{fork_org}/{meta['name']}"
    pull = (
        prefetched.pull
        if prefetched is not None
        else revisions.bot_pull_request(
            repository, pull_number, str(bot["login"]), fork_repository
        )
    )
    if pull is None:
        raise Skip("not an open pull request the bot opened from its fork")
    if pull.get("mergeable") is None:
        # GitHub computes it lazily; read again so a conflict counts.
        settled = merge_state.settle(repository, pull_number, str(pull["head_sha"]))
        pull = {**pull}
        pull["mergeable"] = settled.mergeable
    issue = int(pull["issue"])
    history = rounds_of.round_history(
        repository, issue, str(bot["login"]), str(pull["created_at"]), pull_number
    )
    highest, rounds, since = history.highest, history.summaries, history.since
    # Skips a capped pull request before the packet's reads.
    next_segment_round(repository, pull, history, inputs, None)
    findings = read_findings(repository, pull, since, inputs.reviewers)
    # The hand-back may have changed since the cap read it; decide on
    # the one the packet carries.
    segment_round = next_segment_round(repository, pull, history, inputs, findings)
    reasons = (
        ["named by the operator"]
        if explicit
        else scan_reasons(pull, findings, history, repository=repository)
    )
    details = packet_of(
        read_issue(repository, issue) if prefetched is None else prefetched.details,
        issue,
        explicit=explicit,
    )
    rebase_sha = reads.branch_head(repository, str(meta["default_branch"]))
    return {
        "key": f"{meta['name']}-{issue}",
        "task": TASK,
        "repository": repository,
        "repo_name": meta["name"],
        "number": issue,
        "default_branch": meta["default_branch"],
        **details,
        "base_sha": revisions.merge_base(
            repository, str(pull["base_ref"]), fork_org, str(pull["branch"])
        ),
        "rebase_sha": rebase_sha,
        "checkout_sha": rebase_sha,
        "branch": pull["branch"],
        "fork_org": fork_org,
        "fork_repository": fork_repository,
        "resume": False,
        "pull_request": {
            key: pull[key]
            for key in ("number", "url", "title", "head_sha", "mergeable", "created_at")
        },
        "round": highest + 1,
        "segment_round": segment_round,
        "stale_rounds": history.stale,
        "max_rounds": inputs.max_rounds,
        "round_reasons": reasons,
        "rounds": rounds,
        "threads": findings.threads.threads,
        "threads_dropped": findings.threads.dropped,
        "review": findings.verdict,
        "antagonistic_review": findings.handback,
        "checks": findings.checks.checks,
        "checks_dropped": findings.checks.dropped,
        "checks_unsettled": findings.checks.unsettled,
        **rounds_of.conversation(repository, issue, pull_number, since),
    }


def next_segment_round(
    repository: str,
    pull: dict[str, Any],
    history: rounds_of.RoundHistory,
    inputs: Inputs,
    findings: Findings | None,
) -> int:
    """The coming round's place in its antagonistic segment, or ``Skip`` at the cap.

    The cap is the run's ``max_rounds`` per segment. An unanswered
    hand-back of the head that no round has carried yet starts a new
    segment whatever the last one counted; only the sixty-round ceiling,
    superseded rounds included, and their guard of ten still stop it,
    and there nothing is read. Without ``findings`` the hand-back is
    read only at the cap, so a capped pull request costs that read alone.
    """
    highest, segment, cap = history.highest, history.segment, inputs.max_rounds
    if (spent := highest + history.stale) >= revisions.MAX_TOTAL_ROUNDS:
        raise Skip(str(revisions.cap_reason(spent, segment, cap)))
    if history.stale >= revisions.MAX_STALE_ROUNDS:
        raise Skip("too many superseded rounds; a human must look")
    if findings is None and not revisions.round_cap_reached(highest, segment, cap):
        return segment + 1
    handback = (
        findings.handback
        if findings is not None
        else revisions.handback_review(
            repository, int(pull["number"]), str(pull["head_sha"]), inputs.reviewers
        )
    )
    fresh = (
        revisions.unanswered(handback, history.since) is not None
        and review_id_of(handback) not in history.anchored
    )
    reason = revisions.cap_reason(highest, 0 if fresh else segment, cap)
    if reason is not None:
        raise Skip(reason)
    return 1 if fresh else segment + 1


def scan_reasons(
    pull: dict[str, Any],
    findings: Findings,
    history: rounds_of.RoundHistory,
    *,
    repository: str,
) -> list[str]:
    """Why a scan gives the pull request a round, or ``Skip`` when it does not."""
    reasons = outstanding(pull, findings, repository=repository, since=history.since)
    if not reasons:
        running = findings.checks.unsettled
        raise Skip("checks still running" if running else "nothing outstanding")
    # After a handoff the threads the agent disputed stay open on
    # purpose; a maintainer's word, not the threads or the checks,
    # resumes rounds.
    if history.last_verdict == "handoff" and not any(
        "maintainer commented" in reason for reason in reasons
    ):
        raise Skip("handed back to a maintainer; awaiting their comment")
    return reasons


def read_issue(repository: str, issue: int) -> dict[str, Any]:
    """The issue's details, or ``Skip`` when the issue is gone."""
    try:
        return reads.issue_details(repository, issue)
    except github.GitHubError as exc:
        if not github.is_absent(exc):
            raise
        raise Skip(f"issue #{issue} is gone") from exc


def packet_of(details: dict[str, Any], issue: int, *, explicit: bool) -> dict[str, Any]:
    """The entry's view of the issue, after its safety tests."""
    if details["state"] != "open":
        raise Skip(f"issue #{issue} is closed")
    if "no-agent" in details["labels"]:
        raise Skip(f"issue #{issue} carries the no-agent label")
    if not explicit and not authors.admits(details):
        raise Skip(f"issue #{issue}'s author needs the {authors.APPROVAL_LABEL} label")
    return {k: v for k, v in details.items() if k not in ("state", "assignees")}
