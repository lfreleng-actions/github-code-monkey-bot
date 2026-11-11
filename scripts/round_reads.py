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
from revision_reads import MAX_ROUNDS
from thread_reads import MAX_THREAD_BODY

# The publisher's footer: the last line of its comment, nothing after
# it. A marker in the agent's summary text sits mid-body and is text.
FOOTER = re.compile(
    r"(?:^|\n)<!-- code-monkey (?P<run>\S+) (?P<verdict>\S+)(?: review=\S+)?"
    r"(?: mergeable=(?:true|false|unknown))?"
    r"(?: checks=(?:passing|infrastructure|failing|running|none|unknown)"
    r"(?:/[0-9a-f]{12})?)?"
    r"(?: round=(?P<round>\d+))?(?: pr=(?P<pr>\d+))?(?: since=(?P<since>\S+))?"
    r"(?: handback=(?P<handback>\d+))?(?: antagonistic=(?P<antagonistic>\d+))?"
    r"(?: stale=(?P<stale>1))? -->\s*\Z"
)
# Pages of issue comments the round history may span before the
# read fails closed rather than trust a partial count.
MAX_HISTORY_PAGES = 30
# Footers whose round handled the maintainers' requests up to its
# read time: a published proposal, or a handoff that answered them.
HANDLED_VERDICTS = frozenset({"proposed", "handoff"})


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
    # Rounds in the current antagonistic segment: since the round that
    # first carried the latest hand-back (``handback=`` or
    # ``antagonistic=``), that round counted as the first, or every
    # round when none did.
    # The cap reads this; ``highest`` stays the global round number.
    segment: int
    # Every antagonistic review a round was sent for or answered: one
    # of these opens no new segment, however its time reads against
    # the watermark.
    anchored: frozenset[int] = frozenset()
    # Rounds that lost a race with another run's push (``stale=1``):
    # they count toward no segment's cap, only the sixty-round
    # ceiling, so a guard bounds them on their own.
    stale: int = 0
    # The run URLs whose footer records a published, counted round: a
    # continuation runs only when the run that dispatched it is here.
    published: frozenset[str] = frozenset()


def round_history(
    repository: str,
    issue: int,
    bot_login: str,
    opened_at: str | None = None,
    pull_number: int | None = None,
    exclude: tuple[str, str | None] | None = None,
) -> RoundHistory:
    """Read the bot's footers on the issue: rounds, summaries, watermark, last verdict.

    The bot's own comments carry the footer; anyone else's do not
    count, and a marker in the body text is text. ``pull_number`` and
    ``opened_at``, the pull request's number and creation time, scope
    the history to that pull request, so a named retry's new pull
    request starts its own round count, cap and watermark afresh: a
    footer naming another pull request (``pr=``) belongs to an earlier
    attempt a human closed, however late it was posted, and a footer
    naming none, a first publication's or one written before footers
    named their pull request, counts only from the opening on. The first
    publication's footer carries no round: it sets the watermark and
    counts as round zero. A footer naming when its packet was read
    sets the watermark there, since the comment itself comes later
    than the input the agent saw. The whole history is read; one too
    long to read fails closed rather than yield a partial count.

    The segment counts round numbers, not footers, so a round posted
    twice counts once: from the round that first carried the latest
    antagonistic review to the highest round of all. A round carries
    a review when its footer names it as answered (``antagonistic=``)
    or as the hand-back it was sent for (``handback=``), which a round
    that failed records too. A later round naming a review already
    carried, as a resumed handoff or a retried failure on the same
    head does, opens no segment.

    A footer marked ``stale=1`` belongs to a round another run's push
    superseded before it could publish: it is counted in ``stale``
    alone. It raises no round number, opens and lengthens no segment,
    anchors no review and adds no summary, so the race costs the pull
    request none of its segment's cap. The selector still adds
    ``stale`` to ``highest`` against the sixty-round ceiling.

    ``published`` holds the run of every ``proposed`` footer that is
    not stale: the runs whose round the publisher counted.

    ``exclude``, a run URL and the ``since`` its footer records, leaves
    out that one attempt's footers, so the publisher can count the
    other rounds and add its own exactly once, posted yet or not.
    """
    highest = 0
    # Each antagonistic review's first round, by review id.
    answered: dict[int, int] = {}
    summaries: list[dict[str, Any]] = []
    stale = 0
    published: set[str] = set()
    # since, created, verdict: handled rounds order by the read time
    # they record, so a retry of an older round posted after a newer
    # handoff rewinds nothing; creation orders two posts of one read.
    latest: tuple[str, str, str | None] | None = None
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
            if not in_scope(found, created, opened_at, pull_number):
                continue
            if exclude is not None and exclude == (
                found.group("run"),
                found.group("since"),
            ):
                continue
            verdict = found.group("verdict")
            # An attempt that published nothing handled no request: it
            # counts toward the cap below, but moves no watermark.
            if verdict in HANDLED_VERDICTS and isinstance(created, str):
                key = (found.group("since") or created, created)
                if latest is None or key > (latest[0], latest[1]):
                    latest = (key[0], created, verdict)
            if found.group("stale") is not None:
                stale += 1
                continue
            if verdict == "proposed":
                published.add(found.group("run"))
            if found.group("round") is None:
                continue
            number = int(found.group("round"))
            highest = max(highest, number)
            for field in ("handback", "antagonistic"):
                review = found.group(field)
                if review is not None:
                    first = answered.get(int(review), number)
                    answered[int(review)] = min(first, number)
            summaries.append(
                {
                    "round": number,
                    "created_at": created if isinstance(created, str) else None,
                    "body": reads.truncate_utf8(body, MAX_THREAD_BODY),
                }
            )
        if len(entries) < reads.COMMENTS_PER_PAGE:
            segment_start = max(answered.values(), default=0)
            return RoundHistory(
                highest,
                summaries[-MAX_ROUNDS:],
                latest[0] if latest else None,
                latest[2] if latest else None,
                highest - segment_start + 1 if segment_start else highest,
                frozenset(answered),
                stale,
                frozenset(published),
            )
    raise reads.SelectionError(
        f"{repository}#{issue} holds more comments than the round history reads"
    )


def in_scope(
    found: re.Match[str], created: Any, opened_at: str | None, pull_number: int | None
) -> bool:
    """Whether a footer belongs to the pull request ``round_history`` reads."""
    named = found.group("pr")
    if named is not None and pull_number is not None:
        return int(named) == pull_number
    return not opened_at or (isinstance(created, str) and created >= opened_at)


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
        if len(kept) >= reads.MAX_COMMENTS:
            break
        size = len(str(comment["body"]).encode("utf-8"))
        if total + size > reads.MAX_COMMENT_BYTES:
            # One oversized comment hides nothing smaller behind it.
            continue
        total += size
        kept.append(comment)
    kept.reverse()
    return kept, len(found) - len(kept)


def conversation(
    repository: str, issue: int, pull_number: int, since: str | None
) -> dict[str, Any]:
    """The maintainers' comments, on the issue and the pull request, old and new."""
    comments, dropped, truncated = reads.filtered_comments(repository, issue)
    pr_comments, pr_dropped, _ = reads.filtered_comments(repository, pull_number)
    new_comments, new_dropped = comments_since(repository, issue, since)
    new_pr_comments, new_pr_dropped = comments_since(repository, pull_number, since)
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
