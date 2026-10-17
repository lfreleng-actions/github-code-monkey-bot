# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Revision candidates: the bot's open pull requests with work outstanding.

A revision entry sends a session back to a pull request the bot
opened, to answer its review and push the same branch forward. It
carries what the issue entry carries, plus the pull request, its
unresolved threads, Copilot's verdict on the head, the rounds so
far and the maintainers' comments on the pull request, all as data.
Both bases are recorded: the chain's own, and the default branch's
head, for a rebase (DESIGN.md section 20).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import bot_github as github
import explicit_issues
import fork_orgs as forks
import issue_reads as reads
import repository_reads as repositories
import revision_reads as revisions
import round_reads as rounds_of
from fork_orgs import ForkOrgError
from issue_reads import SelectionError

TASK = "revise"


class Skip(Exception):
    """The pull request cannot be revised; the message says why."""


def outstanding(
    pull: dict[str, Any],
    verdict: dict[str, Any] | None,
    threads: list[dict[str, Any]],
    *,
    repository: str,
    issue: int,
    since: str | None,
) -> list[str]:
    """Why the pull request needs a round, by the organisation definition.

    Work is outstanding when a thread from Copilot or a maintainer is
    unresolved, when Copilot's review of the head has findings, or
    when a maintainer commented on the issue or the pull request
    after the bot's last round. A maintainer's word on a review thread
    after that round, an opener or a reply, counts as a comment, since
    the issue and pull request comments do not carry it. A head with
    no review yet is left for the gate to await, and a clean head with
    nothing else is done.
    """
    reasons: list[str] = []
    if threads:
        reasons.append(f"{len(threads)} unresolved review thread(s)")
    if verdict is not None and verdict.get("verdict") == "findings":
        reasons.append("Copilot's review of the head has findings")
    for number, what in ((issue, "issue"), (int(pull["number"]), "pull request")):
        if rounds_of.newer_member_comment(repository, number, since):
            reasons.append(f"a maintainer commented on the {what} since the last round")
    if maintainer_spoke_on_a_thread(threads, since):
        reasons.append("a maintainer commented on a review thread since the last round")
    return reasons


def maintainer_spoke_on_a_thread(
    threads: list[dict[str, Any]], since: str | None
) -> bool:
    """Whether a maintainer opened or answered an unresolved thread after ``since``."""
    for thread in threads:
        replies = cast("list[dict[str, Any]]", thread.get("replies") or [])
        for comment in (thread, *replies):
            created = comment.get("created_at")
            if (
                comment.get("maintainer") is True
                and isinstance(created, str)
                and (since is None or created > since)
            ):
                return True
    return False


def build_entry(
    org: str,
    repo_name: str,
    pull_number: int,
    *,
    bot: dict[str, Any],
    fork_orgs: dict[str, Any],
    explicit: bool,
) -> dict[str, Any]:
    """The selection entry for one bot pull request, or raise ``Skip``.

    ``explicit`` means an operator named it: the work-outstanding
    test does not apply, since the operator decided, but the safety
    tests and the round cap do.
    """
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
    pull = revisions.bot_pull_request(
        repository, pull_number, str(bot["login"]), fork_repository
    )
    if pull is None:
        raise Skip("not an open pull request the bot opened from its fork")
    issue = int(pull["issue"])
    history = rounds_of.round_history(repository, issue, str(bot["login"]))
    highest, rounds, since = history.highest, history.summaries, history.since
    if highest >= revisions.MAX_ROUNDS:
        raise Skip(
            f"round cap of {revisions.MAX_ROUNDS} reached; a human must take over"
        )
    threads, threads_dropped = revisions.unresolved_threads(repository, pull_number)
    verdict = revisions.latest_review(repository, pull_number, str(pull["head_sha"]))
    if not explicit:
        reasons = outstanding(
            pull, verdict, threads, repository=repository, issue=issue, since=since
        )
        if not reasons:
            raise Skip("nothing outstanding")
        # After a handoff the threads the agent disputed stay open on
        # purpose; a maintainer's word, not the threads, resumes rounds.
        if history.last_verdict == "handoff" and not any(
            "maintainer commented" in reason for reason in reasons
        ):
            raise Skip("handed back to a maintainer; awaiting their comment")
    else:
        reasons = ["named by the operator"]
    details = issue_packet(repository, issue)
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
            "number": pull["number"],
            "url": pull["url"],
            "title": pull["title"],
            "head_sha": pull["head_sha"],
            "mergeable": pull["mergeable"],
            "created_at": pull["created_at"],
        },
        "round": highest + 1,
        "round_reasons": reasons,
        "rounds": rounds,
        "threads": threads,
        "threads_dropped": threads_dropped,
        "review": verdict,
        **conversation(repository, issue, pull_number, since),
    }


def issue_packet(repository: str, issue: int) -> dict[str, Any]:
    """The issue's details for the entry, after its own safety tests."""
    try:
        details = reads.issue_details(repository, issue)
    except github.GitHubError as exc:
        if not github.is_absent(exc):
            raise
        raise Skip(f"issue #{issue} is gone") from exc
    if details["state"] != "open":
        raise Skip(f"issue #{issue} is closed")
    if "no-agent" in details["labels"]:
        raise Skip(f"issue #{issue} carries the no-agent label")
    return {k: v for k, v in details.items() if k not in ("state", "assignees")}


def conversation(
    repository: str, issue: int, pull_number: int, since: str | None
) -> dict[str, Any]:
    """The maintainers' comments, on the issue and the pull request, old and new."""
    comments, dropped, truncated = reads.filtered_comments(repository, issue)
    pr_comments, pr_dropped, _ = reads.filtered_comments(repository, pull_number)
    new_comments, new_dropped = rounds_of.comments_since(repository, issue, since)
    new_pr_comments, new_pr_dropped = rounds_of.comments_since(
        repository, pull_number, since
    )
    return {
        "comments": comments,
        "comments_dropped": dropped,
        "comments_truncated": truncated,
        "pr_comments": pr_comments,
        "pr_comments_dropped": pr_dropped,
        "new_comments": new_comments,
        "new_comments_dropped": new_dropped,
        "new_pr_comments": new_pr_comments,
        "new_pr_comments_dropped": new_pr_dropped,
    }


def pull_request_for_branch(
    repository: str, fork_org: str, branch: str, bot_login: str
) -> int | None:
    """The number of the bot's open pull request from the fork branch, if any."""
    entries = reads.api_page(
        f"repos/{repository}/pulls?state=open&head={fork_org}:{branch}&per_page=20"
    )
    for data in entries:
        user = data.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        number = data.get("number")
        if login == bot_login and type(number) is int:
            return number
    return None


Builder = Callable[[], dict[str, Any] | None]


def scan(
    org: str,
    *,
    bot: dict[str, Any],
    fork_orgs: dict[str, Any],
    skipped: dict[str, int],
    skipped_issues: list[dict[str, str]],
    allowed: Callable[[str], bool] | None = None,
) -> list[Builder]:
    """A builder per bot pull request in the owner, oldest first, read on demand.

    The search is one read; building an entry is many. Each builder
    reads its pull request when called and returns the entry, or None
    after recording the skip, so the caller's budget decides what is
    read at all. ``allowed`` is the run's repository policy, by
    lower-cased name; a hit outside it gets no builder.
    """
    slug = str(bot["login"]).removesuffix("[bot]")
    found = revisions.search_bot_pull_requests(org, slug)
    builders: list[Builder] = []
    for hit in sorted(found, key=lambda h: (str(h["created_at"]), int(h["number"]))):
        repo_name = str(hit["repository"]).partition("/")[2]
        if allowed is not None and not allowed(repo_name.lower()):
            continue
        reference = f"{hit['repository']}#{hit['number']}"
        number = int(hit["number"])

        def build(
            repo_name: str = repo_name, number: int = number, reference: str = reference
        ) -> dict[str, Any] | None:
            try:
                return build_entry(
                    org, repo_name, number, bot=bot, fork_orgs=fork_orgs, explicit=False
                )
            except Skip as exc:
                if str(exc) != "nothing outstanding":
                    skipped["revision"] += 1
                    skipped_issues.append({"issue": reference, "reason": str(exc)})
                return None

        builders.append(build)
    return builders


class Rounds:
    """The revision entries a run works, built on demand with one set of inputs."""

    def __init__(
        self,
        org: str,
        *,
        bot: dict[str, Any],
        fork_orgs: dict[str, Any],
        enabled: bool,
        explicit: bool,
        skips: tuple[dict[str, int], list[dict[str, str]]],
        allowed: Callable[[str], bool] | None = None,
    ) -> None:
        """Remember what every build needs; ``skips`` are the counters and the named list."""
        self.org = org
        self.bot = bot
        self.fork_orgs = fork_orgs
        self.enabled = enabled
        self.explicit = explicit
        self.skipped, self.skipped_issues = skips
        self.allowed = allowed

    def named(self, pulls: list[tuple[str, int]]) -> list[Builder]:
        """A builder per pull request the operator named; a miss is a named skip."""
        builders: list[Builder] = []
        for repo_name, number in pulls:

            def build(
                repo_name: str = repo_name, number: int = number
            ) -> dict[str, Any] | None:
                try:
                    return build_entry(
                        self.org,
                        repo_name,
                        number,
                        bot=self.bot,
                        fork_orgs=self.fork_orgs,
                        explicit=True,
                    )
                except Skip as exc:
                    self.skipped["revision"] += 1
                    self.skipped_issues.append(
                        {
                            "issue": f"{self.org}/{repo_name}#{number}",
                            "reason": str(exc),
                        }
                    )
                    return None

            builders.append(build)
        return builders

    def scan(self) -> list[Builder]:
        """Builders for the owner's bot pull requests, when enabled."""
        if not self.enabled:
            return []
        return scan(
            self.org,
            bot=self.bot,
            fork_orgs=self.fork_orgs,
            skipped=self.skipped,
            skipped_issues=self.skipped_issues,
            allowed=self.allowed,
        )

    def for_issue(
        self, candidate: dict[str, Any], fork_org: str, branch: str
    ) -> dict[str, Any] | None:
        """A round on the issue's open pull request, or None to skip it as before."""
        if not self.enabled:
            return None
        number = pull_request_for_branch(
            str(candidate["repository"]), fork_org, branch, str(self.bot["login"])
        )
        if number is None:
            return None
        try:
            return build_entry(
                self.org,
                str(candidate["repo_name"]),
                number,
                bot=self.bot,
                fork_orgs=self.fork_orgs,
                explicit=self.explicit,
            )
        except Skip:
            return None
