# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A bot pull request's unresolved review threads, as the packet carries them.

A thread counts when Copilot or a maintainer opened it. Each carries
the ids a reply and a resolve need, the opener as data, and the
newest later comments from Copilot or a maintainer, so the agent reads
the discussion a finding grew and the selection hears a maintainer's
word on the thread itself (DESIGN.md section 20).
"""

from __future__ import annotations

from typing import Any, cast

import bot_github as github
import copilot_review as review
import issue_reads as reads

# Threads carried to the agent: a bound on count and total bytes, as
# for issue comments, since each body is text from outside.
MAX_THREADS = 50
MAX_THREAD_BYTES = 128 * 1024
MAX_THREAD_BODY = 8 * 1024
# Replies carried per thread: the newest from Copilot or a maintainer,
# so the agent reads the discussion a finding grew, not the opener alone.
MAX_THREAD_REPLIES = 10
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
            nodes {{
              databaseId body authorAssociation createdAt author {{ login }}
            }}
          }}
          replies: comments(last: {MAX_THREAD_REPLIES}) {{
            totalCount
            nodes {{
              databaseId body authorAssociation createdAt author {{ login }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


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
        **comment_fields(comment, login),
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


def comment_fields(comment: dict[str, Any], login: str) -> dict[str, Any]:
    """Who wrote a comment that counts, whether a maintainer, when, and what.

    The selection reads ``maintainer`` and ``created_at`` to tell a
    maintainer's word on a thread after the last round from Copilot's
    or an older one; a comment GitHub dates oddly carries None, and
    the selection reads none as after the watermark, as it does for
    the issue comments.
    """
    created = comment.get("createdAt")
    return {
        "author": login,
        "maintainer": comment.get("authorAssociation") in THREAD_AUTHORS,
        "created_at": created if isinstance(created, str) else None,
        "body": comment_text(comment),
    }


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
        replies.append(comment_fields(comment, login))
    total = data.get("totalCount")
    more = type(total) is int and total > MAX_THREAD_REPLIES + 1
    return replies, more
