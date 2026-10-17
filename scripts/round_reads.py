# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The issue's comment history as a revision round reads it (DESIGN.md section 20).

The bot's footers on the issue count the rounds and set the
watermark a maintainer's comment must follow to be new; the
maintainers' comments since that watermark are the round's new
instructions. Every read spans the whole history or fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

import issue_reads as reads
from revision_reads import MAX_ROUNDS, MAX_THREAD_BODY

# The publisher's footer: the last line of its comment, nothing after
# it. A marker in the agent's summary text sits mid-body and is text.
FOOTER = re.compile(
    r"(?:^|\n)<!-- code-monkey \S+ (?P<verdict>\S+)(?: review=\S+)?"
    r"(?: round=(?P<round>\d+))?(?: since=(?P<since>\S+))? -->\s*\Z"
)
# Pages of issue comments the round history may span before the
# read fails closed rather than trust a partial count.
MAX_HISTORY_PAGES = 30


@dataclass(frozen=True)
class RoundHistory:
    """What the bot's footers on the issue say about the rounds so far."""

    highest: int
    summaries: list[dict[str, Any]]
    # The watermark a maintainer's comment must follow to be new: the
    # time the latest round read its packet, or failing that the
    # footer's own time.
    since: str | None
    # The verdict of the latest footer; ``handoff`` pauses the scan.
    last_verdict: str | None


def round_history(repository: str, issue: int, bot_login: str) -> RoundHistory:
    """Read the bot's footers on the issue: rounds, summaries, watermark, last verdict.

    The bot's own comments carry the footer; anyone else's do not
    count, and a marker in the body text is text. The first
    publication's footer carries no round: it sets the watermark and
    counts as round zero. A footer naming when its packet was read
    sets the watermark there, since the comment itself comes later
    than the input the agent saw. The whole history is read; one too
    long to read fails closed rather than yield a partial count.
    """
    highest = 0
    summaries: list[dict[str, Any]] = []
    latest: tuple[str, str, str | None] | None = None  # created, since, verdict
    for page in range(1, MAX_HISTORY_PAGES + 1):
        entries = reads.api_page(
            f"repos/{repository}/issues/{issue}/comments"
            f"?per_page={reads.COMMENTS_PER_PAGE}&page={page}"
        )
        for entry in entries:
            user = entry.get("user")
            login = (
                cast("dict[str, Any]", user).get("login")
                if isinstance(user, dict)
                else None
            )
            body = entry.get("body")
            if login != bot_login or not isinstance(body, str):
                continue
            found = FOOTER.search(body)
            if found is None:
                continue
            created = entry.get("created_at")
            if isinstance(created, str) and (latest is None or created > latest[0]):
                latest = (
                    created,
                    found.group("since") or created,
                    found.group("verdict"),
                )
            if found.group("round") is None:
                continue
            number = int(found.group("round"))
            highest = max(highest, number)
            summaries.append(
                {
                    "round": number,
                    "created_at": created if isinstance(created, str) else None,
                    "body": reads.truncate_utf8(body, MAX_THREAD_BODY),
                }
            )
        if len(entries) < reads.COMMENTS_PER_PAGE:
            return RoundHistory(
                highest,
                summaries[-MAX_ROUNDS:],
                latest[1] if latest else None,
                latest[2] if latest else None,
            )
    raise reads.SelectionError(
        f"{repository}#{issue} holds more comments than the round history reads"
    )


def rounds_so_far(
    repository: str, issue: int, bot_login: str
) -> tuple[int, list[dict[str, Any]], str | None]:
    """The round history as a tuple, for callers that need the three values."""
    history = round_history(repository, issue, bot_login)
    return history.highest, history.summaries, history.since


def member_comments_since(
    repository: str, number: int, since: str | None
) -> list[dict[str, Any]]:
    """Every maintainer comment after ``since``, across every page."""
    found: list[dict[str, Any]] = []
    for page in range(1, MAX_HISTORY_PAGES + 1):
        query = f"?per_page={reads.COMMENTS_PER_PAGE}&page={page}"
        if since:
            query += f"&since={since}"
        entries = reads.api_page(f"repos/{repository}/issues/{number}/comments{query}")
        for entry in entries:
            comment = reads.member_comment(entry)
            if comment is None:
                continue
            created = comment.get("created_at")
            if since is None or (isinstance(created, str) and created > since):
                found.append(comment)
        if len(entries) < reads.COMMENTS_PER_PAGE:
            return found
    raise reads.SelectionError(
        f"{repository}#{number} holds more comments than the round history reads"
    )


def newer_member_comment(repository: str, number: int, since: str | None) -> bool:
    """Whether a maintainer commented on the issue or pull request after ``since``.

    ``since`` is the bot's latest footer, round zero included; None
    means any maintainer comment counts.
    """
    return bool(member_comments_since(repository, number, since))


def comments_since(
    repository: str, number: int, since: str | None
) -> tuple[list[dict[str, Any]], int]:
    """The maintainers' comments since the last round, for the packet, bounded.

    The comment that caused the round must reach the agent whatever
    the issue's older comments number; the newest are kept.
    """
    found = member_comments_since(repository, number, since)
    kept: list[dict[str, Any]] = []
    total = 0
    for comment in reversed(found):
        size = len(str(comment["body"]).encode("utf-8"))
        if len(kept) >= reads.MAX_COMMENTS or total + size > reads.MAX_COMMENT_BYTES:
            break
        total += size
        kept.append(comment)
    kept.reverse()
    return kept, len(found) - len(kept)
