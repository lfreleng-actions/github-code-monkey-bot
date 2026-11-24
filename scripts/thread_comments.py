# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The comments on one review thread, read whole and typed for the packet.

Which comments count (Copilot's, a listed App's, a maintainer's), how
each is carried, when a maintainer and the bot last spoke, and the
paged read of a thread's comments that ``thread_reads`` and the
publisher share (DESIGN.md section 20).
"""

from __future__ import annotations

import re
from typing import Any, cast

import bot_github as github
import copilot_review as review
import issue_reads as reads

MAX_THREAD_BODY = 8 * 1024
# Replies carried per thread: the newest from Copilot or a maintainer,
# so the agent reads the discussion a finding grew, not the opener alone.
MAX_THREAD_REPLIES = 10
# A thread's comments are read whole, a page of 100 at a time, so a
# maintainer's answer behind a crowd of passers-by is not missed; a
# thread past this many pages fails closed rather than guess.
COMMENTS_PER_PAGE = 100
MAX_THREAD_PAGES = 10
# Review threads whose opening comment comes from Copilot, a listed
# antagonistic review App or a maintainer count as work; a
# passer-by's thread does not. GraphQL names a bot without the [bot]
# suffix REST gives it, and types an App's author ``Bot``.
THREAD_AUTHORS = reads.COMMENT_ASSOCIATIONS
COPILOT_LOGIN = review.REVIEWER_LOGIN.removesuffix("[bot]")
# The ``seen=`` stamp ``revision_check.reply_marker`` ends a reply with:
# when its round's selection read the thread.
REPLY_SEEN_RE = re.compile(
    r"<!-- code-monkey reply round=\d+ session=\S+"
    r" seen=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) -->\s*\Z"
)
COMMENT_FIELDS = (
    "databaseId body authorAssociation createdAt author { login __typename }"
)

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


def is_bot(comment: dict[str, Any], login: str) -> bool:
    """Whether the App ``login`` wrote the comment, typed ``Bot``.

    A user who took the App's slug as a login is not the App, as
    ``comment_author`` holds.
    """
    author = comment.get("author")
    return (
        bool(login)
        and isinstance(author, dict)
        and cast("dict[str, Any]", author).get("login") == login
        and cast("dict[str, Any]", author).get("__typename") == "Bot"
    )


def answer_marks(comments: list[Any], bot: str) -> dict[str, bool]:
    """Whether a reply of the bot's on the thread answers a maintainer's word.

    ``maintainer_unanswered`` says the newest dated maintainer comment
    is answered by no reply from ``bot``, the App's login, and
    ``maintainer_answered`` that a reply answers it; neither holds when
    one of the two never spoke. A reply answers what its round read:
    each word dated up to the ``seen`` its marker carries, since a word
    posted while the round ran never reached the agent. A reply without
    one answers the words before it, ordered by date, then by place in
    the thread, so one posted in the same second is not taken as
    answered by it.
    """
    login = bot.removesuffix("[bot]")
    reach: tuple[str, int] | None = None
    last_word: tuple[str, int] | None = None
    for index, item in enumerate(comments):
        if not isinstance(item, dict):
            continue
        comment = cast("dict[str, Any]", item)
        created = comment.get("createdAt")
        if not isinstance(created, str):
            continue
        if is_bot(comment, login):
            answers = reply_reach(comment, created, index, len(comments))
            reach = max(reach or answers, answers)
        if (
            comment.get("authorAssociation") in THREAD_AUTHORS
            and comment_author(comment) is not None
        ):
            last_word = max(last_word or (created, index), (created, index))
    if reach is None or last_word is None:
        return {"maintainer_unanswered": False, "maintainer_answered": False}
    return {
        "maintainer_unanswered": last_word > reach,
        "maintainer_answered": last_word < reach,
    }


def reply_reach(
    comment: dict[str, Any], created: str, index: int, count: int
) -> tuple[str, int]:
    """The last (date, place) a bot reply answers: its ``seen``, else itself.

    A word dated ``seen`` sorts before the reach whatever its place:
    ``collected_at`` sits a second early, so a word of that second came
    before the read. A ``seen`` no earlier than the reply proves nothing
    more than the reply's own place does.
    """
    body = comment.get("body")
    found = REPLY_SEEN_RE.search(body) if isinstance(body, str) else None
    if found is None or found.group(1) >= created:
        return (created, index)
    return (found.group(1), count)


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
        page = comments_page(
            thread_id, str(cast("dict[str, Any]", info).get("endCursor"))
        )
        comments.extend(cast("list[Any]", page.get("nodes") or []))
    raise reads.SelectionError(
        f"thread {thread_id} holds more comments than the packet reads"
    )


def comments_page(thread_id: str, after: str | None) -> dict[str, Any]:
    """One page of a thread's comments, by the thread's node id."""
    data = github.graphql(
        THREAD_COMMENTS_QUERY, {"id": thread_id, "after": after}, read=True
    )
    node = data.get("node")
    connection = (
        cast("dict[str, Any]", node).get("comments") if isinstance(node, dict) else None
    )
    if not isinstance(connection, dict):
        raise github.GitHubError("thread comment query returned no data")
    return cast("dict[str, Any]", connection)


def read_thread(thread_id: str) -> list[Any]:
    """Every comment on a thread the caller knows by id alone."""
    return thread_comments(thread_id, comments_page(thread_id, None))


def first_comment(comments: Any) -> dict[str, Any] | None:
    """The opening comment out of a comments connection, or None."""
    nodes = (
        cast("dict[str, Any]", comments).get("nodes")
        if isinstance(comments, dict)
        else None
    )
    first = cast("list[Any]", nodes)[0] if isinstance(nodes, list) and nodes else None
    return cast("dict[str, Any]", first) if isinstance(first, dict) else None


def comment_author(
    comment: dict[str, Any], reviewers: frozenset[str] = frozenset()
) -> str | None:
    """The login of a comment that counts, else None.

    Copilot's counts, a maintainer's, and a listed App's when GraphQL
    types its author ``Bot``: a user who took the App's slug as a
    login is not the App.
    """
    author = comment.get("author")
    login = (
        cast("dict[str, Any]", author).get("login")
        if isinstance(author, dict)
        else None
    )
    if not isinstance(login, str):
        return None
    if login == COPILOT_LOGIN or comment.get("authorAssociation") in THREAD_AUTHORS:
        return login
    if login in reviewers and cast("dict[str, Any]", author).get("__typename") == "Bot":
        return login
    return None


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


def eligible_replies(
    comments: list[Any], opener_id: int, reviewers: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Every reply on the thread that counts, in thread order, each bounded.

    The opener is dropped, and a reply from anyone but Copilot, a
    listed App or a maintainer is not carried.
    """
    eligible: list[dict[str, Any]] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        comment = cast("dict[str, Any]", item)
        if comment.get("databaseId") == opener_id:
            continue
        login = comment_author(comment, reviewers)
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
