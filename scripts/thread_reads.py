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

from dataclasses import dataclass
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
# A thread's comments are read whole, a page of 100 at a time, so a
# maintainer's answer behind a crowd of passers-by is not missed; a
# thread past this many pages fails closed rather than guess.
COMMENTS_PER_PAGE = 100
MAX_THREAD_PAGES = 10
# Review threads whose opening comment comes from Copilot or a
# maintainer count as work; a passer-by's thread does not. GraphQL
# names the reviewer without the [bot] suffix REST gives it.
THREAD_AUTHORS = reads.COMMENT_ASSOCIATIONS
COPILOT_LOGIN = review.REVIEWER_LOGIN.removesuffix("[bot]")
COMMENT_FIELDS = "databaseId body authorAssociation createdAt author { login }"
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
          comments(first: {COMMENTS_PER_PAGE}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ {COMMENT_FIELDS} }}
          }}
        }}
      }}
    }}
  }}
}}
"""
THREAD_COMMENTS_QUERY = f"""
query($id: ID!, $after: String) {{
  node(id: $id) {{
    ... on PullRequestReviewThread {{
      comments(first: {COMMENTS_PER_PAGE}, after: $after) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {COMMENT_FIELDS} }}
      }}
    }}
  }}
}}
"""


@dataclass(frozen=True)
class ThreadPacket:
    """The unresolved threads the packet carries, and what the whole set said."""

    threads: list[dict[str, Any]]
    # Threads that count but the bounds left out.
    dropped: int
    # Whether a maintainer opened or answered any thread that counts,
    # kept or dropped, after the watermark the caller gave.
    maintainer_since: bool


def unresolved_threads(
    repository: str, number: int, since: str | None = None
) -> ThreadPacket:
    """The unresolved review threads worth answering, bounded, read whole first.

    A thread counts when its opening comment comes from Copilot or a
    maintainer. Each carries the ids the publisher needs to reply and
    resolve, and the body as data for the agent. Every thread that
    counts is read before the bounds apply, so a maintainer's word
    after ``since`` is heard whichever thread holds it, and the
    threads holding one go first into the packet: the instruction
    that brings a round reaches the agent.
    """
    owner, _, name = repository.partition("/")
    found: list[dict[str, Any]] = []
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
            if thread is not None:
                found.append(thread)
        page = threads.get("pageInfo")
        if not isinstance(page, dict) or not cast("dict[str, Any]", page).get(
            "hasNextPage"
        ):
            break
        after = str(cast("dict[str, Any]", page).get("endCursor"))
    spoken = [t for t in found if maintainer_spoke(t, since)]
    quiet = [t for t in found if not maintainer_spoke(t, since)]
    kept, dropped = bounded(spoken + quiet)
    return ThreadPacket(kept, dropped, bool(spoken))


def bounded(threads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The first threads that fit the count and byte bounds, and the count left out."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    total = 0
    for thread in threads:
        size = thread_size(thread)
        if len(kept) >= MAX_THREADS or total + size > MAX_THREAD_BYTES:
            dropped += 1
            continue
        total += size
        kept.append(thread)
    return kept, dropped


def maintainer_spoke(thread: dict[str, Any], since: str | None) -> bool:
    """Whether a maintainer opened or answered the thread after ``since``.

    The thread records the latest maintainer word over its whole
    history, trimmed replies included. None for ``since`` means any
    maintainer's word counts, and a comment GitHub dates oddly counts
    as after the watermark, as the issue comments do.
    """
    if thread.get("maintainer_undated") is True:
        return True
    latest = thread.get("maintainer_latest")
    if not isinstance(latest, str):
        return False
    return since is None or latest > since


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
    opener = comment_fields(comment, login)
    eligible = eligible_replies(
        thread_comments(thread_id, cast("dict[str, Any]", data["comments"])),
        comment_id,
    )
    replies = trimmed_replies(eligible)
    return {
        "thread_id": thread_id,
        "comment_id": comment_id,
        "path": str(data.get("path") or ""),
        "line": line if type(line) is int else None,
        "outdated": data.get("isOutdated") is True,
        **opener,
        "replies": replies,
        "replies_dropped": len(replies) < len(eligible),
        **maintainer_marks([opener, *eligible]),
    }


def maintainer_marks(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """When a maintainer last spoke on the thread, read before any trimming.

    ``maintainer_latest`` is the newest dated maintainer comment and
    ``maintainer_undated`` says one carried no date; the selection
    reads these, not the bounded replies, to hear a maintainer.
    """
    dated = [
        str(c["created_at"])
        for c in comments
        if c.get("maintainer") is True and isinstance(c.get("created_at"), str)
    ]
    undated = any(
        c.get("maintainer") is True and c.get("created_at") is None for c in comments
    )
    return {
        "maintainer_latest": max(dated) if dated else None,
        "maintainer_undated": undated,
    }


def thread_comments(thread_id: str, first_page: dict[str, Any]) -> list[Any]:
    """Every comment on the thread, the first page's and the pages after it."""
    comments = list(cast("list[Any]", first_page.get("nodes") or []))
    page = first_page
    for _ in range(MAX_THREAD_PAGES):
        info = page.get("pageInfo")
        if not isinstance(info, dict) or not cast("dict[str, Any]", info).get(
            "hasNextPage"
        ):
            return comments
        after = str(cast("dict[str, Any]", info).get("endCursor"))
        data = github.graphql(
            THREAD_COMMENTS_QUERY, {"id": thread_id, "after": after}, read=True
        )
        node = data.get("node")
        connection = (
            cast("dict[str, Any]", node).get("comments")
            if isinstance(node, dict)
            else None
        )
        if not isinstance(connection, dict):
            raise github.GitHubError("thread comment query returned no data")
        page = cast("dict[str, Any]", connection)
        comments.extend(cast("list[Any]", page.get("nodes") or []))
    raise reads.SelectionError(
        f"thread {thread_id} holds more comments than the packet reads"
    )


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


def eligible_replies(comments: list[Any], opener_id: int) -> list[dict[str, Any]]:
    """Every reply on the thread that counts, in thread order, each bounded.

    The opener is dropped, and a reply from anyone but Copilot or a
    maintainer is not carried.
    """
    eligible: list[dict[str, Any]] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        comment = cast("dict[str, Any]", item)
        if comment.get("databaseId") == opener_id:
            continue
        login = comment_author(comment)
        if login is None:
            continue
        eligible.append(comment_fields(comment, login))
    return eligible


def trimmed_replies(eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``MAX_THREAD_REPLIES`` replies the packet carries, in thread order.

    A maintainer's word outranks a reviewer's: the newest maintainer
    replies fill the bound first, the newest of Copilot's the rest,
    so a run of review replies pushes no instruction out.
    """
    if len(eligible) <= MAX_THREAD_REPLIES:
        return eligible
    ranked = sorted(
        range(len(eligible)),
        key=lambda i: (eligible[i].get("maintainer") is not True, -i),
    )
    chosen = sorted(ranked[:MAX_THREAD_REPLIES])
    return [eligible[i] for i in chosen]
