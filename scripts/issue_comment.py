# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The one-line outcome comment the publisher posts on each issue."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any, cast

import bot_github as github
import issue_reads as reads
import proposal_model as model
import proposal_policy as policy

REVIEW_VERDICTS = ("clean", "findings", "pending")
REVIEW_FIELDS = frozenset(
    {
        "verdict",
        "assessment",
        "unresolved_threads",
        "suppressed",
        "previously_missed",
        "reasons",
        "review_id",
        "commit_id",
        "requested",
    }
)
ASSESSMENT_TEXT = {
    "approval": "approval recommended",
    "closer-look": "needs a closer look",
    "changes": "changes recommended",
}


def review_verdict(result: dict[str, Any]) -> str | None:
    """The Copilot verdict a result records, or None when no review was awaited.

    A record the publisher does not recognise, by shape or by verdict,
    reads as pending: whatever it says, it is not proof of a clean review.
    """
    record = result.get("review")
    if record is None:
        return None
    if not isinstance(record, dict):
        return "pending"
    data = cast("dict[str, Any]", record)
    # Every field, no more and no fewer: a record missing its counts or
    # the head it judged is not proof of a clean review either.
    if frozenset(data) != REVIEW_FIELDS or data.get("verdict") not in REVIEW_VERDICTS:
        return "pending"
    return str(data["verdict"])


def review_sentence(result: dict[str, Any]) -> str:
    """What the comment says about Copilot's review, if one was awaited."""
    verdict = review_verdict(result)
    if verdict is None:
        return ""
    if verdict == "clean":
        return ". The Copilot review of it is clean."
    if verdict == "pending":
        return (
            "; no Copilot review of its head arrived within the wait, so the"
            " review is pending and a maintainer should check for it."
        )
    record = cast("dict[str, Any]", result["review"])
    counts: list[str] = []
    threads = record.get("unresolved_threads")
    if type(threads) is int and threads:
        counts.append(
            f"{threads} unresolved review thread{'s' if threads != 1 else ''}"
        )
    suppressed = record.get("suppressed")
    if type(suppressed) is int and suppressed:
        counts.append(
            f"{suppressed} suppressed finding{'s' if suppressed != 1 else ''}"
        )
    missed = record.get("previously_missed")
    if type(missed) is int and missed:
        counts.append(f"{missed} previously missed finding{'s' if missed != 1 else ''}")
    assessment = ASSESSMENT_TEXT.get(str(record.get("assessment")), "no assessment")
    detail = "; ".join([*counts, f"assessment: {assessment}"])
    return (
        f". The Copilot review of it has findings ({detail}). "
        f"{policy.REVISION_DISCLOSURE}"
    )


MAX_ROUNDS = 10
CAP_NOTICE = (
    f" This was the round cap of {MAX_ROUNDS}: the workflow selects this"
    " pull request no further, and a human must take over."
)


def round_of(result: dict[str, Any]) -> int:
    """The revision round a result records, or 0 for a first proposal."""
    value = result.get("round")
    return value if type(value) is int and value > 0 else 0


def capped(result: dict[str, Any]) -> bool:
    """Whether the round leaves the pull request at the cap with work undone.

    The selector skips a pull request at the cap whatever its state,
    so every tenth-round outcome but a clean proposal needs a human;
    a clean one is finished.
    """
    if round_of(result) < MAX_ROUNDS:
        return False
    return result.get("verdict") != "proposed" or review_verdict(result) != "clean"


def published_head(result: dict[str, Any]) -> str:
    """The head the push left, named in the comment so a round is traceable.

    Later rounds replace the same branch, so the pull request URL
    alone would not say which head a round described (issue #19).
    """
    commits = result.get("commits")
    if not isinstance(commits, list) or not commits:
        return ""
    last = cast("list[Any]", commits)[-1]
    if not isinstance(last, str) or not github.SHA_RE.fullmatch(last):
        return ""
    return f" (head {last[:12]})"


def _proposed_comment(
    result: dict[str, Any], _reasons: str, _run_url: str
) -> str | None:
    url = result.get("pull_request_url") or result.get("branch_url")
    if not url:
        return None
    noun = "pull request" if result.get("pull_request_url") else "branch"
    where = f"{noun} {url}{published_head(result)}"
    round_number = round_of(result)
    if round_number:
        summary = policy.comment_safe(str(result.get("round_summary") or ""))
        opening = f"🐒 Round {round_number}: an AI agent revised {where}" + (
            f". {summary}" if summary else ""
        )
        return opening + review_sentence(result)
    opening = f"🐒 An AI agent has proposed a change for this issue: {where}"
    return opening + review_sentence(result)


def _handoff_comment(result: dict[str, Any], reasons: str, run_url: str) -> str:
    url = result.get("pull_request_url") or ""
    summary = policy.comment_safe(str(result.get("round_summary") or ""))
    text = (
        f"🐒 Round {round_of(result)}: an AI agent handed pull request {url} "
        f"back to a maintainer. It disputes every remaining finding and changed "
        f"nothing: {reasons}" + (f" {summary}" if summary else "")
    )
    responded = result.get("responded")
    failed: Any = (
        cast("dict[str, Any]", responded).get("failed")
        if isinstance(responded, dict)
        else 0
    )
    if type(failed) is int and failed > 0:
        # A reply that did not post is not on its thread; the run
        # report holds the warning that says why.
        text += (
            f" {failed} of its replies did not post (see the run: "
            f"{run_url or 'n/a'}); the rest are on the threads, which stay open"
        )
    else:
        text += " Its replies are on the threads, which stay open"
    if capped(result):
        return text + "."
    return (
        text
        + "; the workflow revises this pull request again once a maintainer comments."
    )


def _abstain_comment(_result: dict[str, Any], reasons: str, _run_url: str) -> str:
    return f"🐒 An AI agent looked at this issue and did not attempt it: {reasons}"


def _rejected_comment(_result: dict[str, Any], reasons: str, run_url: str) -> str:
    return (
        "🐒 An AI agent attempted this issue but its proposal failed a policy "
        f"check ({reasons}). Run: {run_url or 'n/a'}"
    )


def _failed_comment(_result: dict[str, Any], reasons: str, run_url: str) -> str:
    return (
        "🐒 An AI agent attempted this issue but did not finish "
        f"({reasons}). Run: {run_url or 'n/a'}"
    )


def _publish_failed_comment(_result: dict[str, Any], reasons: str, run_url: str) -> str:
    return (
        "🐒 An AI agent proposed a change for this issue but publishing it "
        f"failed ({reasons}). Run: {run_url or 'n/a'}"
    )


COMMENTS: dict[str, Callable[[dict[str, Any], str, str], str | None]] = {
    "proposed": _proposed_comment,
    "handoff": _handoff_comment,
    "abstain": _abstain_comment,
    "rejected": _rejected_comment,
    "author-failed": _failed_comment,
    "publish-failed": _publish_failed_comment,
}


def comment_body(result: dict[str, Any], run_url: str) -> str | None:
    """One line for the issue, or None when there is nothing worth saying.

    The reasons descend from the untrusted manifest, so they are cut
    to a size that keeps the whole comment inside GitHub's limit with
    the run URL intact.
    """
    template = COMMENTS.get(str(result.get("verdict")))
    if template is None:
        return None
    reasons = policy.comment_safe(
        "; ".join(str(r) for r in cast("list[Any]", result.get("reasons") or []))
    )
    if len(reasons) > policy.MAX_COMMENT_REASON:
        reasons = reasons[: policy.MAX_COMMENT_REASON - 1] + "…"
    text = template(result, reasons, run_url)
    if text is not None and capped(result):
        text += CAP_NOTICE
    return text


def review_token(result: dict[str, Any]) -> str:
    """The review as one marker token: verdict, then counts and assessment.

    A findings review names its thread and suppressed counts and its
    assessment too, so a rerun that resolved threads under the same
    verdict reads as a new outcome; one token, with no spaces, so a
    reader of the footer splits it as before.
    """
    verdict = review_verdict(result)
    if verdict != "findings":
        return str(verdict)
    record = cast("dict[str, Any]", result["review"])
    return (
        f"findings/{record.get('unresolved_threads')}/{record.get('suppressed')}"
        f"/{record.get('previously_missed')}/{record.get('assessment')}"
    )


# Pages of comments since the run's selection the search reads; a
# search that fills them all may have the latest outcome beyond them
# and proves nothing.
MAX_COMMENT_PAGES = 3


def run_comment(args: argparse.Namespace) -> None:
    """Post the outcome comment and record its URL in result.json."""
    result = model.load_json(args.result, "result")
    if result.get("dry_run"):
        print("dry run: no comment")
        return
    body = comment_body(result, args.run_url)
    if body is None:
        print("nothing to comment")
        return
    repository = github.require_str(result, "repository", "result")
    issue = github.require_int(result, "issue", "result")
    # The marker names the run and the outcome, the review verdict
    # included: a rerun that changed either posts again rather than
    # leaving the issue at odds with result.json.
    verdict = review_verdict(result)
    suffix = f" review={review_token(result)}" if verdict else ""
    if round_of(result):
        suffix += f" round={round_of(result)}"
    collected = result.get("collected_at")
    if isinstance(collected, str) and collected:
        # The watermark for the next round: when this run read the
        # comments, not when it wrote this one.
        suffix += f" since={collected}"
    marker = f"<!-- code-monkey {args.run_url} {result.get('verdict')}{suffix} -->"
    bot_login = str(result.get("bot_login") or "")
    earlier = (
        posted(repository, issue, marker, args.since, bot_login, run_url=args.run_url)
        if bot_login
        else None
    )
    if earlier is not None:
        # A retry after the POST succeeded but its reply was lost, or
        # after a later step failed: the outcome is already on record.
        print("outcome already commented")
        result["comment_url"] = earlier
    else:
        data = github.api_write(
            "POST",
            f"repos/{repository}/issues/{issue}/comments",
            {"body": f"{body}\n\n{marker}"},
        )
        result["comment_url"] = data.get("html_url")
    args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def posted(
    repository: str,
    issue: int,
    marker: str,
    since: str,
    bot_login: str,
    *,
    run_url: str,
) -> str | None:
    """The URL of this run's comment carrying ``marker``, if it is the latest.

    Searches the comments made since the run's selection, which bounds
    the read to what this run can have posted, and compares against
    the bot's latest outcome for this run alone: an outcome that
    changed and changed back is a new outcome to report, so an older
    matching marker does not suppress it.
    """
    if not since:
        return None
    # Every marker of this run opens the same way: the run URL, then
    # the outcome. The outcome part is what may change between attempts.
    prefix = f"<!-- code-monkey {run_url} "
    latest: dict[str, Any] | None = None
    for page in range(1, MAX_COMMENT_PAGES + 1):
        entries = reads.api_page(
            f"repos/{repository}/issues/{issue}/comments"
            f"?since={since}&per_page=100&page={page}"
        )
        for entry in entries:
            user = entry.get("user")
            login = (
                cast("dict[str, Any]", user).get("login")
                if isinstance(user, dict)
                else None
            )
            # Anyone can paste the marker; only the bot's own comment
            # proves the outcome is on record.
            if login != bot_login or prefix not in str(entry.get("body") or ""):
                continue
            if latest is None or int(entry.get("id") or 0) > int(latest.get("id") or 0):
                latest = entry
        if len(entries) < 100:
            break
    else:
        # Every page was full: the latest outcome may sit past the
        # read, so an older match is no proof and the comment posts.
        return None
    if latest is None or marker not in str(latest.get("body") or ""):
        return None
    url = latest.get("html_url")
    return url if isinstance(url, str) else ""
