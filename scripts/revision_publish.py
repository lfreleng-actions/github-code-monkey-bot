# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The writes a revision round makes: move the bot branch, answer the review.

The branch moves by the compare-and-swap in ``branch_swap`` (DESIGN.md
section 20.3), with the fork token. The replies, resolves and the
description edit use the pull request token on the target, and each
is read back before it is repeated, so a retry posts nothing twice.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any, NoReturn, cast

import bot_github as github
import branch_refs as refs
import copilot_review as review
import issue_reads as reads
import proposal_model as model
import thread_reads as threads_of
from branch_swap import SwapUnknown, already_moved, move_branch
from proposal_policy import PublishError, Rejection
from revision_check import reply_marker

RESOLVE_THREAD = """
mutation($id: ID!) {
  resolveReviewThread(input: {threadId: $id}) { thread { isResolved } }
}
"""


def reply(repository: str, pull_number: int, comment_id: int, body: str) -> None:
    """Post a reply on a review thread's opening comment."""
    github.api_write(
        "POST",
        f"repos/{repository}/pulls/{pull_number}/comments/{comment_id}/replies",
        {"body": body},
    )


def resolve(repository: str, thread_id: str) -> None:
    """Mark a review thread resolved."""
    del repository  # the node id is global; kept for a uniform signature
    github.graphql(RESOLVE_THREAD, {"id": thread_id})


def update_pull_request(
    repository: str, pull_number: int, title: str, body: str
) -> None:
    """Replace the pull request's title and body with the composed ones."""
    github.api_write(
        "PATCH",
        f"repos/{repository}/pulls/{pull_number}",
        {"title": title, "body": body},
    )


def run_push(
    args: argparse.Namespace,
    check: dict[str, Any],
    result: dict[str, Any],
    *,
    fail: Callable[[argparse.Namespace, dict[str, Any], str, Exception], NoReturn],
) -> dict[str, Any]:
    """Move the bot branch to the round's chain by compare-and-swap.

    The pull request already exists: the result names it so ``open``
    has nothing to do and the review gate runs on the new head. A
    handoff moves nothing and names the pull request for ``respond``.
    Revisions publish in pull-requests mode alone: a branches run
    must never move a live pull request's head. ``fail`` is the
    publisher's own: it records the failure and raises.
    """
    warnings = cast("list[str]", result["warnings"])
    # The check's own warnings, an unanswered thread above all, reach
    # the result whatever this run does with the branch; the move
    # may add a cleanup warning of its own after them.
    carried = list(cast("list[str]", check.get("warnings") or []))
    warnings.extend(carried)
    if result["verdict"] == "handoff":
        # The step holds no token for a handoff, so the head check
        # waits for respond, which has the pull request token.
        result["pull_request_url"] = str(check["pull_request_url"])
        return result
    if result["verdict"] != "proposed" or args.dry_run or args.mode == "select":
        return result
    if args.mode != "pull-requests":
        warnings.append(
            "a revision publishes in pull-requests mode alone; the pull request's "
            "branch is unchanged"
        )
        return result
    target = str(check["repository"])
    fork = str(check["fork_repository"])
    try:
        refs.ensure_fork(target, str(check["fork_org"]), target.partition("/")[2])
        refs.sync_fork(fork, str(check["default_branch"]), str(check["base_sha"]))
        published = already_moved(check)
        if published is not None:
            # A rerun after the swap succeeded and a later step failed:
            # the swap's beforeOid names the pre-round head, so a
            # replay would refuse the run's own work. The verified
            # chain on the branch is the publication; nothing moves.
            result["commits"] = published
            warnings.append("an earlier attempt already moved the branch to this round")
        else:
            result["commits"] = move_branch(args.workdir / "clone", check)
    except Rejection as exc:
        result["verdict"] = "rejected"
        cast("list[str]", result["reasons"]).append(str(exc))
        warnings.extend(added_warnings(check, len(carried)))
        return result
    except (PublishError, github.GitHubError) as exc:
        # fail() writes the result before raising: the cleanup warning
        # must be on it by then.
        warnings.extend(added_warnings(check, len(carried)))
        state = (
            ""
            if isinstance(exc, SwapUnknown)
            else "; the pull request branch is unchanged"
        )
        fail(args, result, f"{exc}{state}", exc)
    else:
        # A scratch branch the cleanup could not remove is reported
        # whatever the swap's outcome; the rejection path adds it too.
        warnings.extend(added_warnings(check, len(carried)))
    result["branch_url"] = (
        f"https://github.com/{check['fork_repository']}/tree/{check['branch']}"
    )
    result["pull_request_url"] = str(check["pull_request_url"])
    return result


def added_warnings(check: dict[str, Any], seen: int) -> list[str]:
    """The warnings the move added to the check beyond the ``seen`` it began with."""
    return list(cast("list[str]", check.get("warnings") or []))[seen:]


def handoff_head(check: dict[str, Any], result: dict[str, Any]) -> bool:
    """Whether the handoff may answer: the pull request head is the recorded one.

    A proposal's swap refuses a moved head by itself; a handoff moves
    nothing, so the guard is this read, with the pull request token.
    Replies written against code a maintainer has since replaced
    answer the wrong head, and the handoff's footer would pause the
    rounds on a head the agent never weighed: a moved head is a
    rejection. A head that cannot be read is a failure, not a
    handoff taken on trust; the result records it before the raise.
    """
    expected = str(check["head_sha"])
    try:
        head = review.pull_head(str(check["repository"]), int(check["pull_number"]))
    except github.GitHubError as exc:
        result["verdict"] = "publish-failed"
        cast("list[str]", result["reasons"]).append(
            f"could not read the pull request head: {github.safe_message(exc)}"
        )
        return False
    if head == expected:
        return True
    result["verdict"] = "rejected"
    result["pull_request_url"] = None
    cast("list[str]", result["reasons"]).append(
        f"head moved from {expected[:7]} to {head[:7]} since selection; the "
        "handoff answers code the agent did not see"
    )
    return False


def answered(thread_id: str, bot_login: str, marker: str) -> bool:
    """Whether the bot's reply for this round is already on the thread.

    A rerun after the replies went up and a later step failed must
    not post them again. The round marker is the reply's last line;
    a bot-authored comment ending with it is this round's reply, while
    an older round's, or anyone quoting the marker mid-body, is not.
    """
    login = bot_login.removesuffix("[bot]")
    for item in threads_of.read_thread(thread_id):
        if not isinstance(item, dict):
            continue
        comment = cast("dict[str, Any]", item)
        author = comment.get("author")
        body = comment.get("body")
        if (
            isinstance(author, dict)
            and cast("dict[str, Any]", author).get("login") == login
            and isinstance(body, str)
            and body.rstrip().endswith(marker)
        ):
            return True
    return False


def answer_thread(
    repository: str, number: int, response: dict[str, Any], counts: dict[str, int]
) -> None:
    """Post one reply unless this round's is already there, then resolve if asked.

    The resolve runs whether or not the reply was posted now: one lost
    after the reply is its own retry, and resolving a resolved thread
    changes nothing.
    """
    marker = str(response["marker"])
    if answered(str(response["thread_id"]), str(response["bot_login"]), marker):
        counts["already"] += 1
    else:
        reply(repository, number, int(response["comment_id"]), str(response["reply"]))
        counts["replied"] += 1
    if response.get("resolve") is True:
        resolve(repository, str(response["thread_id"]))
        counts["resolved"] += 1


def run_respond(args: argparse.Namespace) -> None:
    """``publish.py respond``: answer the threads and update the description.

    Each write is its own attempt: one refusal is a warning and the
    others go on, so a maintainer sees every reply the agent wrote.
    """
    result = model.load_json(args.result, "result")
    check = model.load_json(args.check, "check")
    if result.get("key") != check.get("key"):
        raise PublishError("result.json and check.json describe different proposals")
    counts = {"replied": 0, "already": 0, "resolved": 0, "failed": 0}
    warnings = cast("list[str]", result.setdefault("warnings", []))
    verdict = result.get("verdict")
    # A proposal answers once its chain is on the branch; a handoff
    # answers without one, leaving every thread open for a human.
    answering = check.get("task") == "revise" and (
        (verdict == "proposed" and result.get("commits")) or verdict == "handoff"
    )
    if answering and verdict == "handoff" and not handoff_head(check, result):
        answering = False
    if answering:
        repository = str(check["repository"])
        number = int(check["pull_number"])
        marker = reply_marker(
            int(check.get("round") or 0), str(check.get("collected_at") or "")
        )
        for response in cast("list[dict[str, Any]]", check.get("responses") or []):
            thread = str(response["thread_id"])
            try:
                answer_thread(
                    repository,
                    number,
                    dict(response, marker=marker, bot_login=str(check["bot_login"])),
                    counts,
                )
            except (github.GitHubError, reads.SelectionError) as exc:
                # A refused write, or a thread too long for the bounded
                # read: this thread is left alone and the rest go on.
                counts["failed"] += 1
                warnings.append(f"thread {thread}: {github.safe_message(exc)}")
        if verdict == "proposed":
            try:
                update_pull_request(
                    repository, number, str(check["pr_title"]), str(check["pr_body"])
                )
            except github.GitHubError as exc:
                warnings.append(
                    f"could not update the description: {github.safe_message(exc)}"
                )
    result["responded"] = counts
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"respond: {counts}")
    if result.get("verdict") == "publish-failed":
        raise PublishError(str(cast("list[str]", result["reasons"])[-1]))
