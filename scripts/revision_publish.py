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


def published_head(check: dict[str, Any], result: dict[str, Any]) -> bool:
    """Whether the pull request still heads at the chain this round published.

    The compare-and-swap guards the branch move alone. Another run may
    advance the pull request between the swap and these writes, and
    this round's replies would then resolve threads and replace a
    newer description with an older one. A moved head, or one that
    cannot be read, leaves the writes undone with a warning; the
    publication itself stands.
    """
    commits = cast("list[Any]", result.get("commits") or [])
    tip = str(commits[-1])
    warnings = cast("list[str]", result["warnings"])
    try:
        head = review.pull_head(str(check["repository"]), int(check["pull_number"]))
    except github.GitHubError as exc:
        warnings.append(
            "could not read the pull request head before responding: "
            f"{github.safe_message(exc)}; no reply posted"
        )
        return False
    if head == tip:
        return True
    warnings.append(
        f"the pull request head moved from {tip[:7]} to {head[:7]} after the "
        "push; no reply posted and the description left alone"
    )
    return False


def run_retitle(args: argparse.Namespace) -> None:
    """``publish.py retitle``: set a revision's title before its branch moves.

    The Semantic Pull Request check runs on the push and compares the
    title with the commit subject, so the title must lead the push
    (AGENTS.md section 10.1). Only a proposed revision whose title
    differs writes, and only while the pull request heads at the
    selected commit or at this run's own published chain on a retry;
    a head another session moved keeps its title. What the write
    replaced goes to ``--record`` so a push that then fails with the
    branch unchanged can take the title back.
    """
    check = model.load_json(args.check, "check")
    if check.get("task") != "revise" or check.get("verdict") != "proposed":
        print("retitle: nothing to do")
        return
    repository = str(check["repository"])
    number = int(check["pull_number"])
    title = str(check.get("pr_title") or "")
    head = review.pull_head(repository, number)
    if head != str(check["head_sha"]):
        chain = already_moved(check)
        if chain is None or head != chain[-1]:
            print(f"retitle: head moved to {head[:7]}; title left alone")
            return
    current = github.api_object(f"repos/{repository}/pulls/{number}").get("title")
    if not title or title == current:
        print("retitle: title unchanged")
        return
    # The record goes down before the write: GitHub may apply the
    # title and lose the reply, and the restore must still know what
    # to give back.
    record = {
        "repository": repository,
        "pull_number": number,
        "head_sha": head,
        "previous": current if isinstance(current, str) else "",
        "title": title,
    }
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    github.api_write("PATCH", f"repos/{repository}/pulls/{number}", {"title": title})
    print("retitle: title set")


def run_restore_title(args: argparse.Namespace) -> None:
    """``publish.py restore-title``: take back a title the push did not earn.

    Runs after the push whatever its outcome, and after a retitle that
    failed, since GitHub may apply the title and lose the reply. A
    publication keeps the title; a failure that confirmed the branch
    unchanged, or a push that reached no result, gives the old title
    back when the new one is in place, provided the
    head is still the one the retitle saw and the title is still the
    one it set, so another session's work is never touched. An
    unknown swap outcome restores nothing, since the branch may hold
    the new chain.
    """
    if not args.record.is_file():
        print("restore-title: nothing was retitled")
        return
    record = model.load_json(args.record, "retitle record")
    result = model.load_json(args.result, "result") if args.result.is_file() else None
    if result is not None:
        verdict = result.get("verdict")
        reasons = " ".join(
            str(r) for r in cast("list[Any]", result.get("reasons") or [])
        )
        if verdict == "proposed":
            print("restore-title: published; the title stands")
            return
        if verdict == "publish-failed" and "unknown" in reasons:
            print("restore-title: swap outcome unknown; the title stands")
            return
    repository = str(record["repository"])
    number = int(record["pull_number"])
    head = review.pull_head(repository, number)
    if head != str(record["head_sha"]):
        print(f"restore-title: head moved to {head[:7]}; title left alone")
        return
    current = github.api_object(f"repos/{repository}/pulls/{number}").get("title")
    if current != record["title"]:
        print("restore-title: title changed since; left alone")
        return
    github.api_write(
        "PATCH", f"repos/{repository}/pulls/{number}", {"title": record["previous"]}
    )
    print("restore-title: previous title restored")


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
    if answering and verdict == "proposed" and not published_head(check, result):
        answering = False
    if answering:
        repository = str(check["repository"])
        number = int(check["pull_number"])
        marker = reply_marker(
            int(check.get("round") or 0), str(check.get("session") or "")
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
