# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads for revision rounds: the bot's open pull requests and their state.

A revision candidate is an open pull request the bot opened from the
designated fork, on a ``code-monkey/issue-<n>`` branch. Each read here
types and bounds what it returns; the selection decides. See
DESIGN.md section 20.
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
MAX_ROUNDS = 10


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
