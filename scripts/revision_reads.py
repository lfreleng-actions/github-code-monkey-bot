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
# Threads carried to the agent: a bound on count and total bytes, as
# for issue comments, since each body is text from outside.
MAX_THREADS = 50
MAX_THREAD_BYTES = 128 * 1024
MAX_THREAD_BODY = 8 * 1024
# Replies carried per thread: the newest from Copilot or a maintainer,
# so the agent reads the discussion a finding grew, not the opener alone.
MAX_THREAD_REPLIES = 10
MAX_REVIEW_BODY = verdicts.MAX_BODY_BYTES
MAX_ROUNDS = 10
# Review threads whose opening comment comes from Copilot or a
# maintainer count as work; a passer-by's thread does not. GraphQL
# names the reviewer without the [bot] suffix REST gives it.
THREAD_AUTHORS = reads.COMMENT_ASSOCIATIONS
COPILOT_LOGIN = review.REVIEWER_LOGIN.removesuffix("[bot]")
THREADS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      reviewThreads(first: 50, after: $after) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 1) {{
            nodes {{ databaseId body authorAssociation author {{ login }} }}
          }}
          replies: comments(last: {MAX_THREAD_REPLIES}) {{
            totalCount
            nodes {{ databaseId body authorAssociation author {{ login }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""


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


def unresolved_threads(
    repository: str, number: int
) -> tuple[list[dict[str, Any]], int]:
    """The unresolved review threads worth answering, bounded, and the count dropped.

    A thread counts when its opening comment comes from Copilot or a
    maintainer. Each carries the ids the publisher needs to reply and
    resolve, and the body as data for the agent.
    """
    owner, _, name = repository.partition("/")
    kept: list[dict[str, Any]] = []
    dropped = 0
    total = 0
    after: str | None = None
    while True:
        data = github.graphql(
            THREADS_QUERY,
            {"owner": owner, "name": name, "number": number, "after": after},
            read=True,
        )
        threads = thread_connection(data)
        for node in cast("list[Any]", threads.get("nodes") or []):
            thread = thread_entry(node)
            if thread is None:
                continue
            size = thread_size(thread)
            if len(kept) >= MAX_THREADS or total + size > MAX_THREAD_BYTES:
                dropped += 1
                continue
            total += size
            kept.append(thread)
        page = threads.get("pageInfo")
        if not isinstance(page, dict) or not cast("dict[str, Any]", page).get(
            "hasNextPage"
        ):
            return kept, dropped
        after = str(cast("dict[str, Any]", page).get("endCursor"))


def thread_connection(data: dict[str, Any]) -> dict[str, Any]:
    """The reviewThreads connection out of a GraphQL reply."""
    repository = data.get("repository")
    pull = (
        cast("dict[str, Any]", repository).get("pullRequest")
        if isinstance(repository, dict)
        else None
    )
    threads = (
        cast("dict[str, Any]", pull).get("reviewThreads")
        if isinstance(pull, dict)
        else None
    )
    if not isinstance(threads, dict):
        raise github.GitHubError("review thread query returned no data")
    return cast("dict[str, Any]", threads)


def thread_size(thread: dict[str, Any]) -> int:
    """The bytes a thread costs the packet: its opener and every reply."""
    bodies = [str(thread["body"])] + [
        str(reply["body"]) for reply in cast("list[dict[str, Any]]", thread["replies"])
    ]
    return sum(len(body.encode("utf-8")) for body in bodies)


def thread_entry(node: Any) -> dict[str, Any] | None:
    """One unresolved thread in packet form, or None when it does not count."""
    if not isinstance(node, dict):
        return None
    data = cast("dict[str, Any]", node)
    if data.get("isResolved") is not False:
        return None
    comment = first_comment(data.get("comments"))
    if comment is None:
        return None
    login = comment_author(comment)
    if login is None:
        return None
    thread_id = data.get("id")
    comment_id = comment.get("databaseId")
    if not isinstance(thread_id, str) or type(comment_id) is not int:
        return None
    line = data.get("line")
    replies, more = thread_replies(data.get("replies"), comment_id)
    return {
        "thread_id": thread_id,
        "comment_id": comment_id,
        "path": str(data.get("path") or ""),
        "line": line if type(line) is int else None,
        "outdated": data.get("isOutdated") is True,
        "author": login,
        "body": comment_text(comment),
        "replies": replies,
        "replies_dropped": more,
    }


def first_comment(comments: Any) -> dict[str, Any] | None:
    """The opening comment out of a comments connection, or None."""
    nodes = (
        cast("dict[str, Any]", comments).get("nodes")
        if isinstance(comments, dict)
        else None
    )
    first = cast("list[Any]", nodes)[0] if isinstance(nodes, list) and nodes else None
    return cast("dict[str, Any]", first) if isinstance(first, dict) else None


def comment_author(comment: dict[str, Any]) -> str | None:
    """The login of a comment that counts, Copilot's or a maintainer's, else None."""
    author = comment.get("author")
    login = (
        cast("dict[str, Any]", author).get("login")
        if isinstance(author, dict)
        else None
    )
    if not isinstance(login, str):
        return None
    if (
        login != COPILOT_LOGIN
        and comment.get("authorAssociation") not in THREAD_AUTHORS
    ):
        return None
    return login


def comment_text(comment: dict[str, Any]) -> str:
    """A comment's body as bounded packet text."""
    body = comment.get("body")
    return reads.truncate_utf8(body if isinstance(body, str) else "", MAX_THREAD_BODY)


def thread_replies(
    connection: Any, opener_id: int
) -> tuple[list[dict[str, Any]], bool]:
    """The thread's newest replies that count, and whether older ones were cut.

    GitHub answers the ``last: N`` of the thread's comments, the opener
    among them on a short thread; the opener is dropped here, a reply
    from anyone but Copilot or a maintainer is not carried, and the
    bound holds whatever the reply holds.
    """
    if not isinstance(connection, dict):
        return [], False
    data = cast("dict[str, Any]", connection)
    nodes = data.get("nodes")
    entries = cast("list[Any]", nodes) if isinstance(nodes, list) else []
    replies: list[dict[str, Any]] = []
    for item in entries[-MAX_THREAD_REPLIES:]:
        if not isinstance(item, dict):
            continue
        comment = cast("dict[str, Any]", item)
        if comment.get("databaseId") == opener_id:
            continue
        login = comment_author(comment)
        if login is None:
            continue
        replies.append({"author": login, "body": comment_text(comment)})
    total = data.get("totalCount")
    more = type(total) is int and total > MAX_THREAD_REPLIES + 1
    return replies, more


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
