# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The offline checks a revision round adds to a proposal (DESIGN.md section 20).

A revision's manifest may sit on either recorded base, names a reply
per review thread, and carries a round summary for the issue. Every
field is agent output: typed, bounded against the trusted selection,
and defused before a human reads it.
"""

from __future__ import annotations

from typing import Any, cast

import proposal_policy as policy
from proposal_model import Check
from proposal_policy import PublishError, Rejection

MAX_REPLY = 4_000
MAX_RESPONSES = 100
REPLY_PREFIX = "🐒 Reply from the AI agent that authored this pull request: "


def reply_marker(round_number: int, session: str) -> str:
    """The last line of a session's replies: what a rerun looks for before posting.

    The round alone would not do: a session whose issue comment never
    went up leaves no footer, so the next session takes the same round
    number and must not mistake the earlier replies for its own. The
    session is the run id: unique per run where two runs may share a
    read time, and the same across the run's retries.
    """
    return f"<!-- code-monkey reply round={round_number} session={session} -->"


def check_entry(data: dict[str, Any]) -> None:
    """The revision fields of a trusted selection entry must have their shapes."""
    if data.get("task") != "revise":
        return
    rebase = data.get("rebase_sha")
    if not isinstance(rebase, str) or not policy.SHA_RE.fullmatch(rebase):
        raise PublishError("selection rebase_sha is not a commit SHA")
    pull = data.get("pull_request")
    if not isinstance(pull, dict):
        raise PublishError("selection revision entry carries no pull request")
    pull_data = cast("dict[str, Any]", pull)
    head = pull_data.get("head_sha")
    if not isinstance(head, str) or not policy.SHA_RE.fullmatch(head):
        raise PublishError("selection pull request head_sha is not a commit SHA")
    if type(pull_data.get("number")) is not int or not isinstance(
        pull_data.get("url"), str
    ):
        raise PublishError("selection pull request lacks number or url")
    if type(data.get("round")) is not int or int(data["round"]) < 1:
        raise PublishError("selection revision round is not a positive integer")
    threads = data.get("threads")
    if not isinstance(threads, list):
        raise PublishError("selection revision entry carries no thread list")
    for thread in cast("list[Any]", threads):
        if (
            not isinstance(thread, dict)
            or not isinstance(cast("dict[str, Any]", thread).get("thread_id"), str)
            or type(cast("dict[str, Any]", thread).get("comment_id")) is not int
        ):
            raise PublishError("selection thread lacks thread_id or comment_id")


def accepted_bases(check: Check) -> tuple[str, ...]:
    """The commits a chain may descend from: the base, and for a round the rebase."""
    if check.task == "revise":
        return (check.base_sha, check.rebase_sha)
    return (check.base_sha,)


def choose_base(manifest: dict[str, Any], check: Check) -> None:
    """Record which recorded base the agent built on; any other is a rejection."""
    stated = manifest.get("base_sha")
    if check.task != "revise" or stated == check.base_sha:
        return
    if stated == check.rebase_sha:
        check.base_sha = check.rebase_sha
        check.rebased = True
        return
    raise Rejection(
        "manifest base_sha is neither the pull request's base nor the recorded "
        "default branch head"
    )


def read_responses(manifest: dict[str, Any], check: Check) -> None:
    """Type the replies and bind each to a thread the selection carried."""
    raw = manifest.get("responses")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise Rejection("manifest responses is not a list")
    entries = cast("list[Any]", raw)
    if len(entries) > MAX_RESPONSES:
        raise Rejection(f"manifest carries over {MAX_RESPONSES} responses")
    known = {(str(t["thread_id"]), int(t["comment_id"])): True for t in check.threads}
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise Rejection("manifest response is not an object")
        data = cast("dict[str, Any]", item)
        thread_id = data.get("thread_id")
        comment_id = data.get("comment_id")
        reply = data.get("reply")
        resolve = data.get("resolve")
        if not isinstance(thread_id, str) or type(comment_id) is not int:
            raise Rejection("manifest response lacks thread_id or comment_id")
        if (thread_id, comment_id) not in known:
            raise Rejection(f"manifest response names unknown thread {thread_id}")
        if thread_id in seen:
            raise Rejection(f"manifest answers thread {thread_id} twice")
        if not isinstance(reply, str) or not reply.strip():
            raise Rejection(f"manifest response for {thread_id} has no reply")
        if type(resolve) is not bool:
            raise Rejection(f"manifest response for {thread_id} has no boolean resolve")
        seen.add(thread_id)
        text = policy.defuse_mentions(reply.strip())[:MAX_REPLY]
        check.responses.append(
            {
                "thread_id": thread_id,
                "comment_id": comment_id,
                "reply": f"{REPLY_PREFIX}{text}\n\n"
                f"{reply_marker(check.round, check.session)}",
                "resolve": resolve,
            }
        )
    unanswered = len(check.threads) - len(seen)
    if unanswered:
        plural = "s" if unanswered != 1 else ""
        check.warnings.append(
            f"{unanswered} thread{plural} left unanswered; they stay open for a human"
        )


def read_summary(manifest: dict[str, Any], check: Check) -> None:
    """The round summary is the issue comment; required and bounded."""
    summary = manifest.get("round_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise Rejection("manifest carries no round_summary")
    check.round_summary = policy.defuse_mentions(summary.strip())[: policy.MAX_REASON]


def read_revision(manifest: dict[str, Any], check: Check) -> None:
    """Everything a proposed revision adds to the manifest."""
    if check.task != "revise":
        return
    read_responses(manifest, check)
    read_summary(manifest, check)


def read_handoff(manifest: dict[str, Any], check: Check) -> None:
    """A handoff: the agent disputes every finding and leaves the code alone.

    Only a revision may hand off, since there is a pull request to
    hand back. Its replies are read as for a proposal, and none may
    resolve a thread: a disputed finding stays open for a human.
    """
    if check.verdict != "handoff":
        return
    if check.task != "revise":
        raise Rejection("an issue session cannot hand off; it abstains")
    read_responses(manifest, check)
    read_summary(manifest, check)
    if any(response["resolve"] for response in check.responses):
        raise Rejection("a handoff resolves no thread")


def round_note(check: Check) -> str:
    """The line the publisher adds to the description for a round."""
    if check.task != "revise":
        return ""
    base = "rebased onto the default branch" if check.rebased else "on its base"
    return f"Round {check.round} of revision, {base}.\n\n"
