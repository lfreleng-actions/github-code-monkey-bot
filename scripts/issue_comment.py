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
import write_recovery as recovery
from review_outcome import (
    merge_sentence,
    merge_token,
    review_after_merge,
    review_sentence,
    review_token,
    review_verdict,
)


def outcome_text(opening: str, result: dict[str, Any]) -> str:
    """The opening, then the merge state when it is in doubt, then the review."""
    token = merge_token(result)
    if token in (None, "true"):
        return opening + review_sentence(result)
    text = opening.rstrip(".") + "." + merge_sentence(result)
    text += review_after_merge(result)
    if token == "false" and not capped(result):
        text += " The next round rebases it."
    return text


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
    so every tenth-round outcome but a clean proposal that can merge
    needs a human; a clean one that can merge is finished.
    """
    if round_of(result) < MAX_ROUNDS:
        return False
    return (
        result.get("verdict") != "proposed"
        or review_verdict(result) != "clean"
        or result.get("mergeable") is False
    )


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
        failing = policy.failing_checks_text(
            list(cast("list[Any]", result.get("failing_checks") or []))
        )
        if failing:
            opening = (
                opening.rstrip(".")
                + f". It set out to fix the failing checks: {failing}"
            )
        answered = handback_text(result)
        if answered:
            opening = opening.rstrip(".") + f". {answered}"
        return outcome_text(opening, result)
    opening = f"🐒 An AI agent has proposed a change for this issue: {where}"
    return outcome_text(opening, result)


def _handoff_comment(result: dict[str, Any], reasons: str, run_url: str) -> str:
    url = result.get("pull_request_url") or ""
    summary = policy.comment_safe(str(result.get("round_summary") or ""))
    text = (
        f"🐒 Round {round_of(result)}: an AI agent handed pull request {url} "
        f"back to a maintainer. It disputes every remaining finding and changed "
        f"nothing: {reasons}" + (f" {summary}" if summary else "")
    )
    answered = handback_text(result)
    if answered:
        text = text.rstrip(".") + f". {answered}."
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


def handback_id(result: dict[str, Any]) -> int | None:
    """The id of the antagonistic review the round answered, or None."""
    handback = result.get("antagonistic_review")
    if not isinstance(handback, dict) or result.get("verdict") not in (
        "proposed",
        "handoff",
    ):
        return None
    review_id = cast("dict[str, Any]", handback).get("review_id")
    return review_id if type(review_id) is int and review_id > 0 else None


def handback_text(result: dict[str, Any]) -> str:
    """Name the antagonistic review the round answered, from the trusted record."""
    if handback_id(result) is None:
        return ""
    handback = cast("dict[str, Any]", result["antagonistic_review"])
    round_number = handback.get("round")
    url = handback.get("url")
    if type(round_number) is not int or not isinstance(url, str):
        return ""
    return f"It answers antagonistic review round {round_number} ({url})"


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
    merge = merge_token(result)
    if merge is not None:
        suffix += f" mergeable={merge}"
    if round_of(result):
        suffix += f" round={round_of(result)}"
    pull_number = result.get("pull_number")
    if type(pull_number) is int and pull_number > 0:
        # Scopes the round to its pull request: a late footer from a
        # closed attempt's round must not count against a retry's.
        suffix += f" pr={pull_number}"
    collected = result.get("collected_at")
    if isinstance(collected, str) and collected:
        # The watermark for the next round: when this run read the
        # comments, not when it wrote this one.
        suffix += f" since={collected}"
    handback = handback_id(result)
    if handback is not None:
        # The receiving side's key for an idempotent hand-back: which
        # antagonistic review this round consumed.
        suffix += f" antagonistic={handback}"
    marker = f"<!-- code-monkey {args.run_url} {result.get('verdict')}{suffix} -->"
    bot_login = str(result.get("bot_login") or "")

    def landed() -> dict[str, Any] | None:
        # With no bot login to match, posted() is given no window to read.
        since = args.since if bot_login else ""
        url = posted(repository, issue, marker, since, bot_login, run_url=args.run_url)
        return None if url is None else {"html_url": url}

    def write() -> dict[str, Any]:
        endpoint = f"repos/{repository}/issues/{issue}/comments"
        return github.api_write("POST", endpoint, {"body": f"{body}\n\n{marker}"})

    earlier = landed()
    if earlier is not None:
        # A retry after the POST succeeded but its reply was lost, or
        # after a later step failed: the outcome is already on record.
        print("outcome already commented")
        result["comment_url"] = earlier["html_url"]
    else:
        # Found by its marker alone, so a lost POST is never sent again.
        with recovery.recording(cast("list[str]", result.setdefault("warnings", []))):
            data = recovery.recover(
                write, landed, repeat_safe=False, label="issue comment"
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
