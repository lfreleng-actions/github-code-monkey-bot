# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A bot pull request's review threads, as the packet carries them.

A thread counts when Copilot, a listed antagonistic review App or a
maintainer opened it. The packet carries every unresolved thread that
counts, and a resolved one only while it holds a maintainer's word
after the round's watermark, marked ``resolved``: the publisher
resolves a thread in the round that answers it, so a maintainer's
reply to that answer lands on a resolved thread. Each carries the ids
a reply and a resolve need, the opener as data, and the newest later
comments from those authors, so the agent reads the discussion a
finding grew and the selection hears a maintainer's word on the
thread itself (DESIGN.md section 20).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import bot_github as github
import issue_reads as reads
from thread_comments import (
    COMMENT_FIELDS as COMMENT_FIELDS,
)
from thread_comments import (
    COMMENTS_PER_PAGE as COMMENTS_PER_PAGE,
)
from thread_comments import (
    COPILOT_LOGIN as COPILOT_LOGIN,
)
from thread_comments import (
    MAX_THREAD_BODY as MAX_THREAD_BODY,
)
from thread_comments import (
    MAX_THREAD_PAGES as MAX_THREAD_PAGES,
)
from thread_comments import (
    MAX_THREAD_REPLIES as MAX_THREAD_REPLIES,
)
from thread_comments import (
    THREAD_COMMENTS_QUERY as THREAD_COMMENTS_QUERY,
)
from thread_comments import (
    answer_marks,
    comment_author,
    comment_fields,
    eligible_replies,
    first_comment,
    maintainer_marks,
    thread_comments,
    trimmed_replies,
)
from thread_comments import (
    read_thread as read_thread,
)
from thread_pending import held, watermark_only

# Threads carried to the agent: a bound on count and total bytes, as
# for issue comments, since each body is text from outside.
MAX_THREADS = 50
MAX_THREAD_BYTES = 128 * 1024
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


@dataclass(frozen=True)
class ThreadPacket:
    """The threads the packet carries, and what the whole set said."""

    threads: list[dict[str, Any]]
    # Threads that count but the bounds left out.
    dropped: int
    # Whether a maintainer opened or answered any thread that counts,
    # kept or dropped, resolved or not, after the watermark the caller
    # gave.
    maintainer_since: bool
    # Resolved threads among the kept and dropped, there for a
    # maintainer's word alone: they are no unresolved finding.
    resolved: int = 0
    # Resolved threads heard though their maintainer's word is older
    # than the watermark: no reply of the bot's on the thread answers
    # it, or the round that carried it left it unanswered.
    unanswered: int = 0


def unresolved_threads(
    repository: str,
    number: int,
    since: str | None = None,
    reviewers: frozenset[str] = frozenset(),
    bot: str = "",
    pending: str | None = None,
) -> ThreadPacket:
    """The review threads worth answering, bounded, read whole first.

    A thread counts when its opening comment comes from Copilot, an
    App in ``reviewers`` or a maintainer. Each carries the ids the publisher needs to reply and
    resolve, and the body as data for the agent. Every thread that
    counts is read before the bounds apply, so a maintainer's word
    after ``since`` is heard whichever thread holds it, and the
    threads holding one go first into the packet: the instruction
    that brings a round reaches the agent. A resolved thread is
    carried only when it holds such a word, so one with nothing new
    adds nothing to the packet or the bounds.

    The watermark moves past a word the bounds left out, so a resolved
    thread is also heard while no reply of ``bot``, the App's login,
    answers a maintainer's word: a reply answers the words its round
    read, up to the ``seen`` its marker carries; one the bot never replied on has the
    watermark alone, so those go first, and when the bounds cannot hold
    them all the read raises ``SelectionError`` rather than lose one.
    A round that carried one and posted no reply on it records the
    word as its footer's ``pending``; such a thread is heard again
    while its word is no older than ``pending``.
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
            thread = thread_entry(node, reviewers, bot)
            if thread is not None:
                found.append(thread)
        page = threads.get("pageInfo")
        if not isinstance(page, dict) or not cast("dict[str, Any]", page).get(
            "hasNextPage"
        ):
            break
        after = str(cast("dict[str, Any]", page).get("endCursor"))
    spoke = [maintainer_spoke(t, since) for t in found]
    owed = [
        t["resolved"] and (t["maintainer_unanswered"] or held(t, pending))
        for t in found
    ]
    spoken = [t for t, s, o in zip(found, spoke, owed, strict=True) if s or o]
    quiet = [t for t, s in zip(found, spoke, strict=True) if not (s or t["resolved"])]
    spoken.sort(key=lambda t: not watermark_only(t))
    kept, dropped = bounded(spoken + quiet)
    lost = sum(1 for t in spoken if watermark_only(t)) - sum(
        1 for t in kept if watermark_only(t)
    )
    if lost:
        raise reads.SelectionError(
            f"{lost} resolved review thread(s) with a maintainer's word the bot"
            " never answered exceed the packet bounds"
        )
    resolved = sum(1 for t in spoken if t["resolved"])
    late = sum(1 for s, o in zip(spoke, owed, strict=True) if o and not s)
    return ThreadPacket(kept, dropped, any(spoke), resolved, late)


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


def thread_entry(
    node: Any, reviewers: frozenset[str] = frozenset(), bot: str = ""
) -> dict[str, Any] | None:
    """One thread in packet form, or None when it does not count.

    A resolved thread is returned too, marked ``resolved``, so the
    caller can hear a maintainer's reply on it; the caller decides
    whether it is carried. ``maintainer_unanswered`` says a
    maintainer's newest word is answered by no reply from ``bot``, and
    ``maintainer_answered`` that a reply from ``bot`` answers it: one
    whose round read the thread after the word (``answer_marks``).
    """
    if not isinstance(node, dict):
        return None
    data = cast("dict[str, Any]", node)
    resolved = data.get("isResolved")
    if type(resolved) is not bool:
        return None
    comment = first_comment(data.get("comments"))
    if comment is None:
        return None
    login = comment_author(comment, reviewers)
    if login is None:
        return None
    thread_id = data.get("id")
    comment_id = comment.get("databaseId")
    if not isinstance(thread_id, str) or type(comment_id) is not int:
        return None
    line = data.get("line")
    opener = comment_fields(comment, login)
    comments = thread_comments(thread_id, cast("dict[str, Any]", data["comments"]))
    eligible = eligible_replies(comments, comment_id, reviewers)
    replies = trimmed_replies(eligible)
    marks = maintainer_marks([opener, *eligible])
    return {
        "thread_id": thread_id,
        "comment_id": comment_id,
        "path": str(data.get("path") or ""),
        "line": line if type(line) is int else None,
        "outdated": data.get("isOutdated") is True,
        "resolved": resolved,
        **opener,
        "replies": replies,
        "replies_dropped": len(replies) < len(eligible),
        **marks,
        **answer_marks(comments, bot),
    }
