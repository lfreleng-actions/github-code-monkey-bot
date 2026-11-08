# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads for revision rounds: the bot's open pull requests and their state.

A revision candidate is an open pull request the bot opened from the
designated fork, on a ``code-monkey/issue-<n>`` branch. Each read here
types and bounds what it returns; the selection decides. See
DESIGN.md section 20.

An antagonistic review hands a pull request back with one review by a
listed App whose body ends with a hand-back marker naming the head
(DESIGN.md section 20.1); the review's body is the round's data.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, cast

import bot_github as github
import copilot_review as review
import issue_reads as reads
import review_verdict as verdicts

BRANCH_RE = re.compile(r"^code-monkey/issue-(?P<number>[1-9][0-9]*)$")
MAX_REVIEW_BODY = verdicts.MAX_BODY_BYTES
# Rounds per segment by default, the first publication's and each
# antagonistic hand-back's after it (DESIGN.md section 20.1); a run's
# ``max_rounds`` input may set another.
MAX_ROUNDS = 10
# The ceiling over every segment, whatever a run's cap: the first
# publication's ten and ten for each of the review bot's five
# antagonistic rounds. Footer history that goes wrong still ends the
# loop here.
MAX_TOTAL_ROUNDS = 60
# Superseded rounds per pull request: rounds that lost a race with
# another run's push count toward no segment's cap, only the
# ceiling above, so this bounds them apart.
# Racing runs, or a forged stand-down, stop here for a human.
MAX_STALE_ROUNDS = 10
# The last line of an antagonistic review that hands the pull request
# back. The review bot's trusted applier writes it after its footer;
# agent text there cannot forge it, since that bot disarms ``<``
# outside code. Matched against the body after trailing whitespace.
HANDBACK = re.compile(
    r"(?:\A|\n)<!-- antagonistic-handback round=(?P<round>[1-9][0-9]*)"
    r" head=(?P<head>[0-9a-f]{40}) run=(?P<run>[1-9][0-9]*) -->\Z"
)


def round_cap_reached(round_number: int, segment: int, cap: int = MAX_ROUNDS) -> bool:
    """Whether a pull request at these counts is at the round cap.

    ``round_number`` is the global round, ``segment`` the rounds in
    the current antagonistic segment and ``cap`` the run's rounds per
    segment. The selector asks with the rounds so far, the publisher
    with the round it ran: either way ``cap`` in a segment, or sixty
    in all, is the cap.
    """
    return segment >= cap or round_number >= MAX_TOTAL_ROUNDS


def rounds_spent(round_number: int, stale: object) -> int:
    """The rounds the ceiling counts once ``round_number`` has run.

    ``stale`` is the superseded rounds before it, as the selection
    counted them. A counted round raises the global round and a
    superseded one the stale count, so either way the sum is what the
    selector next compares with the ceiling (DESIGN.md 20.1).
    """
    return round_number + (stale if type(stale) is int and stale > 0 else 0)


def cap_reason(round_number: int, segment: int, cap: int = MAX_ROUNDS) -> str | None:
    """Why the selector skips a pull request at these counts, or None."""
    if not round_cap_reached(round_number, segment, cap):
        return None
    limit = MAX_TOTAL_ROUNDS if round_number >= MAX_TOTAL_ROUNDS else cap
    return f"round cap of {limit} reached; a human must take over"


def unanswered(
    handback: dict[str, Any] | None, since: str | None
) -> dict[str, Any] | None:
    """The hand-back when no round has answered it yet: posted after ``since``."""
    if handback is None or (
        since is not None and str(handback["submitted_at"]) <= since
    ):
        return None
    return handback


def bot_pull_request(
    repository: str, number: int, bot_login: str, fork_repository: str
) -> dict[str, Any] | None:
    """The pull request as a revision target, or None when it is not the bot's.

    Open, opened by the bot, head in the designated fork on a bot
    branch; anything else is not this workflow's to revise.
    """
    try:
        data = github.api_object(f"repos/{repository}/pulls/{number}")
    except github.GitHubError as exc:
        # A mistyped number is the operator's; the caller names the skip.
        if github.is_absent(exc):
            return None
        raise
    user = data.get("user")
    head = data.get("head")
    base = data.get("base")
    if not isinstance(user, dict) or not isinstance(head, dict):
        raise github.GitHubError("pull request reply lacks user or head")
    if not isinstance(base, dict):
        raise github.GitHubError("pull request reply lacks base")
    head_data = cast("dict[str, Any]", head)
    head_repo = head_data.get("repo")
    head_name = (
        cast("dict[str, Any]", head_repo).get("full_name")
        if isinstance(head_repo, dict)
        else None
    )
    ref = head_data.get("ref")
    branch = BRANCH_RE.match(ref) if isinstance(ref, str) else None
    if (
        data.get("state") != "open"
        or cast("dict[str, Any]", user).get("login") != bot_login
        or not isinstance(head_name, str)
        or head_name.lower() != fork_repository.lower()
        or branch is None
    ):
        return None
    mergeable = data.get("mergeable")
    return {
        "number": github.require_int(data, "number", "pull request"),
        "url": github.require_str(data, "html_url", "pull request"),
        "title": github.require_str(data, "title", "pull request"),
        "issue": int(branch.group("number")),
        "branch": str(ref),
        "head_sha": github.require_sha(head_data, "sha", "pull request head"),
        "base_ref": github.require_str(
            cast("dict[str, Any]", base), "ref", "pull request base"
        ),
        "mergeable": mergeable if isinstance(mergeable, bool) else None,
        "created_at": github.require_str(data, "created_at", "pull request"),
    }


def search_bot_pull_requests(org: str, bot_slug: str) -> list[dict[str, Any]]:
    """Every open pull request the App opened in the owner, from search."""
    raw = github.run_gh(
        [
            "search",
            "prs",
            "--owner",
            org,
            "--app",
            bot_slug,
            "--state",
            "open",
            "--limit",
            str(reads.SEARCH_LIMIT),
            "--json",
            "number,repository,createdAt",
        ],
        read=True,
    )
    parsed = github.decode_response(raw)
    if not isinstance(parsed, list):
        raise github.GitHubError("expected a pull request array from search")
    entries = cast("list[Any]", parsed)
    if len(entries) >= reads.SEARCH_LIMIT:
        raise reads.SelectionError("pull request search reached the ceiling")
    found: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise github.GitHubError("expected pull request objects from search")
        data = cast("dict[str, Any]", entry)
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise github.GitHubError("search entry without a repository")
        found.append(
            {
                "repository": github.require_str(
                    cast("dict[str, Any]", repository), "nameWithOwner", "search"
                ),
                "number": github.require_int(data, "number", "search pull request"),
                "created_at": github.require_str(data, "createdAt", "search"),
            }
        )
    return found


def merge_base(repository: str, base_ref: str, fork_owner: str, branch: str) -> str:
    """The commit the pull request's chain descends from."""
    base = urllib.parse.quote(base_ref, safe="")
    head = urllib.parse.quote(f"{fork_owner}:{branch}", safe="")
    data = github.api_object(f"repos/{repository}/compare/{base}...{head}")
    merge = data.get("merge_base_commit")
    if not isinstance(merge, dict):
        raise github.GitHubError("compare reply carries no merge base")
    return github.require_sha(cast("dict[str, Any]", merge), "sha", "merge base")


def latest_review(repository: str, number: int, head_sha: str) -> dict[str, Any] | None:
    """Copilot's review of the head with its verdict, or None when there is none."""
    found = review.head_review(repository, number, head_sha)
    if found is None:
        return None
    unresolved = review.unresolved_threads(repository, number)
    body = found.get("body")
    verdict = verdicts.classify(body if isinstance(body, str) else "", unresolved)
    review_id = found.get("id")
    return {
        "body": reads.truncate_utf8(
            body if isinstance(body, str) else "", MAX_REVIEW_BODY
        ),
        "review_id": review_id if type(review_id) is int else None,
        **verdict.to_json(
            review_id=review_id if type(review_id) is int else None, commit_id=head_sha
        ),
    }


def listed_app(user: Any, reviewers: frozenset[str]) -> str | None:
    """The login of a REST user that is a listed App, else None.

    REST names an App ``<slug>[bot]`` with type ``Bot``; a user who
    took the slug as a login is type ``User`` and does not count.
    """
    if not reviewers or not isinstance(user, dict):
        return None
    data = cast("dict[str, Any]", user)
    login = data.get("login")
    if data.get("type") != "Bot" or not isinstance(login, str):
        return None
    if not login.endswith("[bot]") or login.removesuffix("[bot]") not in reviewers:
        return None
    return login


def handback_marker(body: str, head_sha: str) -> tuple[int, int] | None:
    """The round and run a body's hand-back marker names for ``head_sha``, or None.

    The marker counts only as the body's last line; one mid-body, or
    one naming another head, is text.
    """
    found = HANDBACK.search(body.rstrip())
    if found is None or found.group("head") != head_sha:
        return None
    return int(found.group("round")), int(found.group("run"))


def handback_review(
    repository: str, number: int, head_sha: str, reviewers: frozenset[str]
) -> dict[str, Any] | None:
    """The newest antagonistic review of the head that hands it back, or None.

    It must be a submitted review by a listed App on ``head_sha``
    whose body ends with a hand-back marker naming that head. A
    listing that fails raises, as every revision read does, so no
    round runs without the findings it was sent for; an empty list
    turns the read off.
    """
    if not reviewers:
        return None
    newest: tuple[str, int, dict[str, Any], tuple[int, int]] | None = None
    for review_data in github.api_list(f"repos/{repository}/pulls/{number}/reviews"):
        login = listed_app(review_data.get("user"), reviewers)
        if login is None or review_data.get("commit_id") != head_sha:
            continue
        submitted = review_data.get("submitted_at")
        if review_data.get("state") == "PENDING" or not isinstance(submitted, str):
            continue
        review_id = review_data.get("id")
        body = review_data.get("body")
        if type(review_id) is not int or not isinstance(body, str):
            raise github.GitHubError(
                f"antagonistic review by {login} carries no id or readable body"
            )
        marker = handback_marker(body, head_sha)
        if marker is None:
            continue
        if newest is None or (submitted, review_id) > (newest[0], newest[1]):
            newest = (submitted, review_id, review_data, marker)
    if newest is None:
        return None
    submitted, review_id, review_data, (round_number, run) = newest
    body = str(review_data["body"])
    bounded = reads.truncate_utf8(body, MAX_REVIEW_BODY)
    return {
        "review_id": review_id,
        "round": round_number,
        "run": run,
        "head_sha": head_sha,
        "url": f"https://github.com/{repository}/pull/{number}"
        f"#pullrequestreview-{review_id}",
        "author": listed_app(review_data.get("user"), reviewers),
        "submitted_at": submitted,
        "body": bounded,
        "body_truncated": len(bounded) < len(body),
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
