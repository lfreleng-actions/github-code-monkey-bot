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
            raise PublishError(f"updateRefs failed: {exc}") from exc
        return commits
    finally:
        leftover = refs.delete_branch(fork, scratch)
        if leftover:
            # The swap outcome stands; the scratch branch costs a look.
            cast("list[Any]", check.setdefault("warnings", [])).append(leftover)


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
    has nothing to do and the review gate runs on the new head.
    ``fail`` is the publisher's own: it records the failure and raises.
    """
    target = str(check["repository"])
    fork = str(check["fork_repository"])
    try:
        refs.ensure_fork(target, str(check["fork_org"]), target.partition("/")[2])
        refs.sync_fork(fork, str(check["default_branch"]), str(check["base_sha"]))
        result["commits"] = move_branch(args.workdir / "clone", check)
    except Rejection as exc:
        result["verdict"] = "rejected"
        cast("list[str]", result["reasons"]).append(str(exc))
        return result
    except (PublishError, github.GitHubError) as exc:
        fail(args, result, f"{exc}; the pull request branch is unchanged", exc)
    cast("list[str]", result["warnings"]).extend(
        cast("list[str]", check.get("warnings") or [])
    )
    result["branch_url"] = (
        f"https://github.com/{check['fork_repository']}/tree/{check['branch']}"
    )
    result["pull_request_url"] = str(check["pull_request_url"])
    return result


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
    if (
        check.get("task") == "revise"
        and result.get("verdict") == "proposed"
        and result.get("commits")
    ):
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
