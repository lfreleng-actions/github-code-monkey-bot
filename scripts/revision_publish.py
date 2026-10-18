# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The writes a revision round makes: move the bot branch, answer the review.

The branch moves by compare-and-swap (DESIGN.md section 20): the new
chain is replayed onto a temporary branch in the fork, so every
commit is signed, and ``updateRefs`` then points the bot branch at
its head on the condition that the branch still holds the head the
selection recorded. A head that moved since is a rejection; nothing
is forced over work the run did not see. The replies, resolves and
the description edit use the pull request token on the target.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

import bot_github as github
import branch_refs as refs
import issue_reads as reads
import proposal_model as model
from proposal_policy import PublishError, Rejection

UPDATE_REFS = """
mutation($input: UpdateRefsInput!) {
  updateRefs(input: $input) { clientMutationId }
}
"""
RESOLVE_THREAD = """
mutation($id: ID!) {
  resolveReviewThread(input: {threadId: $id}) { thread { isResolved } }
}
"""


class SwapUnknown(PublishError):
    """The swap's reply was lost and the branch does not say what happened.

    The caller must not report the branch unchanged: a human reads the
    fork before the next round.
    """


def temporary_branch(check: dict[str, Any]) -> str:
    """Where the round's chain is built before the bot branch moves to it."""
    return f"{check['branch']}-r{int(check['round'])}"


def repository_id(repository: str) -> str:
    """The GraphQL node id ``updateRefs`` addresses."""
    data = github.api_object(f"repos/{repository}")
    return github.require_str(data, "node_id", "repository")


def move_branch(clone: Path, check: dict[str, Any]) -> list[str]:
    """Replay the chain onto a temporary branch, then swap the bot branch to it.

    Returns the new commits. The temporary branch is removed whatever
    happens after it exists; the bot branch is touched by the swap
    alone, and the swap refuses unless the branch still holds the
    recorded head.
    """
    fork = str(check["fork_repository"])
    branch = str(check["branch"])
    scratch = temporary_branch(check)
    expected = str(check["head_sha"])
    refs.create_branch(fork, scratch, str(check["base_sha"]))
    try:
        staged = dict(check, branch=scratch)
        commits = refs.replay_commits(clone, staged)
        payload = {
            "repositoryId": repository_id(fork),
            "refUpdates": [
                {
                    "name": f"refs/heads/{branch}",
                    "afterOid": commits[-1],
                    "beforeOid": expected,
                    "force": True,
                }
            ],
        }
        try:
            github.graphql(UPDATE_REFS, {"input": payload})
        except github.GitHubError as exc:
            message = str(exc)
            if "beforeOid" in message or "expected" in message.lower():
                raise Rejection(
                    f"head moved from {expected[:7]} since selection; nothing forced"
                ) from exc
            reconcile_swap(fork, branch, expected, commits[-1], check, exc)
        return commits
    finally:
        leftover = refs.delete_branch(fork, scratch)
        if leftover:
            # The swap outcome stands; the scratch branch costs a look.
            cast("list[Any]", check.setdefault("warnings", [])).append(leftover)


def reconcile_swap(
    fork: str,
    branch: str,
    expected: str,
    tip: str,
    check: dict[str, Any],
    exc: github.GitHubError,
) -> None:
    """Read the branch after a swap whose reply was an error, and decide.

    GitHub may apply ``updateRefs`` and then time out on the reply.
    The branch at the new tip means the move stands and publication
    goes on, with a warning; at the recorded head it means the move
    did not happen and the branch is unchanged. Any other head, or no
    answer, is an outcome this run cannot name, and it says so rather
    than claim the branch unchanged.
    """
    try:
        head = reads.branch_head(fork, branch)
    except github.GitHubError as lookup:
        raise SwapUnknown(
            f"updateRefs failed ({exc}) and the branch could not be read "
            f"({lookup}): outcome unknown"
        ) from exc
    if head == tip:
        cast("list[Any]", check.setdefault("warnings", [])).append(
            f"updateRefs answered with an error ({exc}) but the branch holds the "
            "new head; the reply was lost and the move stands"
        )
        return
    if head == expected:
        raise PublishError(f"updateRefs failed: {exc}") from exc
    raise SwapUnknown(
        f"updateRefs failed ({exc}) and the branch reads as {head[:7]}, neither "
        "head: outcome unknown"
    ) from exc


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


def run_respond(args: argparse.Namespace) -> None:
    """``publish.py respond``: answer the threads and update the description.

    Each write is its own attempt: one refusal is a warning and the
    others go on, so a maintainer sees every reply the agent wrote.
    """
    result = model.load_json(args.result, "result")
    check = model.load_json(args.check, "check")
    if result.get("key") != check.get("key"):
        raise PublishError("result.json and check.json describe different proposals")
    counts = {"replied": 0, "resolved": 0, "failed": 0}
    warnings = cast("list[str]", result.setdefault("warnings", []))
    verdict = result.get("verdict")
    # A proposal answers once its chain is on the branch; a handoff
    # answers without one, leaving every thread open for a human.
    answering = check.get("task") == "revise" and (
        (verdict == "proposed" and result.get("commits")) or verdict == "handoff"
    )
    if answering:
        repository = str(check["repository"])
        number = int(check["pull_number"])
        for response in cast("list[dict[str, Any]]", check.get("responses") or []):
            thread = str(response["thread_id"])
            try:
                reply(
                    repository,
                    number,
                    int(response["comment_id"]),
                    str(response["reply"]),
                )
                counts["replied"] += 1
                if response.get("resolve") is True:
                    resolve(repository, thread)
                    counts["resolved"] += 1
            except github.GitHubError as exc:
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
