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
import review_verdict as verdicts


def _proposed_comment(
    result: dict[str, Any], _reasons: str, _run_url: str
) -> str | None:
    url = result.get("pull_request_url") or result.get("branch_url")
    if not url:
        return None
    noun = "pull request" if result.get("pull_request_url") else "branch"
    text = f"🐒 An AI agent has proposed a change for this issue: {noun} {url}"
    review = review_sentence(result.get("review"))
    return f"{text}. {review}" if review else text


def count_of(value: Any) -> int:
    """A count from the review record, or zero for anything else."""
    return value if type(value) is int and value >= 0 else 0


def review_sentence(review: Any) -> str | None:
    """The Copilot review verdict in fixed words, or None without a record.

    Only fixed text and counts reach the comment: the review body is
    untrusted, and ``result.json`` carries none of it.
    """
    if not isinstance(review, dict):
        return None
    record = cast("dict[str, Any]", review)
    verdict = record.get("verdict")
    if verdict == verdicts.CLEAN:
        return "Copilot's review of its head commit is clean."
    if verdict == verdicts.FINDINGS:
        assessment = record.get("assessment")
        label = assessment if assessment in verdicts.ASSESSMENTS.values() else None
        return (
            "Copilot's review of its head commit has findings "
            f"({count_of(record.get('unresolved_threads'))} unresolved thread(s), "
            f"{count_of(record.get('suppressed'))} suppressed, "
            f"assessment: {label or verdicts.NO_ASSESSMENT}); a maintainer must "
            "act on them, since this pipeline cannot yet revise its own pull "
            "requests, so it is not ready to merge."
        )
    return (
        "No Copilot review of its head commit arrived within the wait; "
        "the review is pending, so it is not ready to merge."
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
    return template(result, reasons, run_url)


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
    marker = f"<!-- code-monkey {args.run_url} {result.get('verdict')} -->"
    bot_login = str(result.get("bot_login") or "")
    earlier = (
        posted(repository, issue, marker, args.since, bot_login) if bot_login else None
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
    repository: str, issue: int, marker: str, since: str, bot_login: str
) -> str | None:
    """The URL of this run's comment carrying ``marker``, if one exists.

    Searches the comments made since the run's selection, which bounds
    the read to what this run can have posted.
    """
    if not since:
        return None
    for page in range(1, 4):
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
            if login == bot_login and marker in str(entry.get("body") or ""):
                url = entry.get("html_url")
                return url if isinstance(url, str) else ""
        if len(entries) < 100:
            break
    return None
