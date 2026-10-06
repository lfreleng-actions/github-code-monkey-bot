# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fork, the bot branch and its pull request, as GitHub holds them.

Branch writes address the fork (DESIGN.md section 4.3); the pull
request lives on the target with the fork branch as its head.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, cast

import monkey_github as github
import proposal_check as checks
import proposal_policy as policy
from proposal_policy import PublishError, Rejection

FORK_WAIT_SECONDS = 60
FORK_POLL_SECONDS = 5

COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
"""


def is_fork_of(data: dict[str, Any], target: str) -> bool:
    """Whether a repository object is a fork whose parent is ``target``."""
    parent = data.get("parent")
    name = (
        cast("dict[str, Any]", parent).get("full_name")
        if isinstance(parent, dict)
        else None
    )
    return (
        data.get("fork") is True
        and isinstance(name, str)
        and name.lower() == target.lower()
    )


def await_fork(fork: str) -> dict[str, Any]:
    """Poll for a fork GitHub is still creating; forking is asynchronous."""
    deadline = time.monotonic() + FORK_WAIT_SECONDS
    while True:
        try:
            return github.api_object(f"repos/{fork}")
        except github.GitHubError as exc:
            if not github.is_absent(exc) or time.monotonic() >= deadline:
                raise PublishError(f"fork {fork} did not appear: {exc}") from exc
        time.sleep(FORK_POLL_SECONDS)


def ensure_fork(target: str, fork_org: str, repo_name: str) -> str:
    """Find or create the target's fork in the fork organisation.

    A repository of that name which is not a fork of the target is a
    failure, never a push destination: the publisher writes only into
    forks it can prove are its own.
    """
    fork = f"{fork_org}/{repo_name}"
    try:
        data = github.api_object(f"repos/{fork}")
    except github.GitHubError as exc:
        if not github.is_absent(exc):
            raise PublishError(f"could not read fork {fork}: {exc}") from exc
        try:
            github.api_write(
                "POST",
                f"repos/{target}/forks",
                {"organization": fork_org, "default_branch_only": True},
            )
        except github.GitHubError as failure:
            raise PublishError(
                f"could not fork {target} into {fork_org}: {failure}"
            ) from failure
        data = await_fork(fork)
    if not is_fork_of(data, target):
        raise PublishError(f"{fork} exists but is not a fork of {target}")
    return fork


def sync_fork(fork: str, default_branch: str, base_sha: str) -> None:
    """Bring the fork's default branch up to date, then require the base.

    ``merge-upstream`` answers 409 when the fork's branch has diverged;
    the base commit may still be present, so the commit lookup, not
    the sync status, decides whether the branch can be created.
    """
    try:
        github.api_write(
            "POST", f"repos/{fork}/merge-upstream", {"branch": default_branch}
        )
    except github.GitHubError as exc:
        if exc.status != 409:
            raise PublishError(f"could not sync fork {fork}: {exc}") from exc
    try:
        github.api_object(f"repos/{fork}/commits/{base_sha}")
    except github.GitHubError as exc:
        # GitHub answers an unknown SHA with 422, a missing repo with 404.
        if github.is_absent(exc) or exc.status == 422:
            raise PublishError(f"base commit {base_sha} is not in fork {fork}") from exc
        raise PublishError(f"could not read {fork}: {exc}") from exc


def create_branch(repository: str, branch: str, base_sha: str) -> None:
    """Create the bot branch at the base; an existing branch is a rejection."""
    try:
        github.api_write(
            "POST",
            f"repos/{repository}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
    except github.GitHubError as exc:
        if exc.status == 422 and "already exists" in str(exc).lower():
            raise Rejection(f"branch {branch} already exists") from exc
        raise PublishError(str(exc)) from exc


def existing_pull_request(
    repository: str, branch: str, bot_login: str, fork_repository: str
) -> str | None:
    """The URL of an open pull request the bot opened from the fork branch.

    A rerun of a publish job whose first attempt opened the pull
    request and then failed later meets the branch it made. Only a
    pull request the bot itself opened, from the designated fork,
    counts as that earlier success: anyone else's is a conflict.
    """
    fork_owner = fork_repository.partition("/")[0]
    entries = github.api_page(
        f"repos/{repository}/pulls?state=open&head={fork_owner}:{branch}&per_page=20"
    )
    for data in entries:
        user = data.get("user")
        head = data.get("head")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        head_repo = (
            cast("dict[str, Any]", head).get("repo") if isinstance(head, dict) else None
        )
        head_name = (
            cast("dict[str, Any]", head_repo).get("full_name")
            if isinstance(head_repo, dict)
            else None
        )
        url = data.get("html_url")
        if (
            login == bot_login
            and isinstance(head_name, str)
            and head_name.lower() == fork_repository.lower()
            and isinstance(url, str)
        ):
            return url
    return None


def file_changes(clone: Path, commit: dict[str, Any]) -> dict[str, Any]:
    """Build the createCommitOnBranch fileChanges payload for one commit."""
    additions: list[dict[str, str]] = []
    for item in cast("list[dict[str, Any]]", commit["additions"]):
        blob = str(item["blob"])
        if not policy.SHA_RE.fullmatch(blob):
            raise PublishError("check.json carries a malformed blob id")
        content = cast(
            "bytes", checks.git(clone, "cat-file", "blob", blob, binary=True)
        )
        additions.append(
            {"path": str(item["path"]), "contents": base64.b64encode(content).decode()}
        )
    deletions = [{"path": path} for path in cast("list[str]", commit["deletions"])]
    return {"additions": additions, "deletions": deletions}


def replay_commits(clone: Path, check: dict[str, Any]) -> list[str]:
    """Send each commit through createCommitOnBranch on the fork, chaining the head."""
    repository = str(check["fork_repository"])
    head = str(check["base_sha"])
    created: list[str] = []
    for commit in cast("list[dict[str, Any]]", check["commits"]):
        payload = {
            "branch": {
                "repositoryNameWithOwner": repository,
                "branchName": str(check["branch"]),
            },
            "expectedHeadOid": head,
            "message": {
                "headline": str(commit["headline"]),
                "body": str(commit["body"]),
            },
            "fileChanges": file_changes(clone, commit),
        }
        try:
            data = github.graphql(COMMIT_MUTATION, {"input": payload})
        except github.GitHubError as exc:
            raise PublishError(f"createCommitOnBranch failed: {exc}") from exc
        result = data.get("createCommitOnBranch")
        node = (
            cast("dict[str, Any]", result).get("commit")
            if isinstance(result, dict)
            else None
        )
        if not isinstance(node, dict):
            raise PublishError("createCommitOnBranch returned no commit")
        oid = github.require_str(cast("dict[str, Any]", node), "oid", "commit")
        created.append(oid)
        head = oid
    return created


def open_pull_request(check: dict[str, Any]) -> tuple[str, str | None]:
    """Open the pull request on the target from the fork branch; label it if possible.

    Returns the URL and a warning when labelling did not happen, so
    the result records it instead of the log swallowing it.
    """
    repository = str(check["repository"])
    data = github.api_write(
        "POST",
        f"repos/{repository}/pulls",
        {
            "title": str(check["pr_title"]),
            "head": f"{check['fork_org']}:{check['branch']}",
            "base": str(check["default_branch"]),
            "body": str(check["pr_body"]),
            "maintainer_can_modify": True,
            "draft": False,
        },
    )
    url = github.require_str(data, "html_url", "pull request")
    number = github.require_int(data, "number", "pull request")
    try:
        github.api_object(f"repos/{repository}/labels/{policy.LABEL}")
        github.api_write(
            "POST",
            f"repos/{repository}/issues/{number}/labels",
            {"labels": [policy.LABEL]},
        )
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return url, None
        return url, f"could not label the pull request: {exc}"
    return url, None


def branch_matches(
    repository: str,
    branch: str,
    base_sha: str,
    expected: list[dict[str, Any]],
    bot_login: str,
) -> bool:
    """Whether a branch holds exactly the commits the replay would make.

    The branch must sit on the recorded base with no other history,
    carry as many commits as the proposal, each authored by the bot,
    Verified by GitHub, and bearing the checked message. Anything else
    is not this run's work, whatever its final tree.
    """
    compare = github.api_object(f"repos/{repository}/compare/{base_sha}...{branch}")
    merge_base = compare.get("merge_base_commit")
    base = (
        cast("dict[str, Any]", merge_base).get("sha")
        if isinstance(merge_base, dict)
        else None
    )
    commits = compare.get("commits")
    if (
        base != base_sha
        or compare.get("behind_by") != 0
        or compare.get("ahead_by") != len(expected)
        or not isinstance(commits, list)
        or len(cast("list[Any]", commits)) != len(expected)
    ):
        return False
    parent = base_sha
    for actual, wanted in zip(cast("list[Any]", commits), expected, strict=True):
        if not isinstance(actual, dict):
            return False
        data = cast("dict[str, Any]", actual)
        if not chain_link(data, parent, str(wanted.get("tree", ""))):
            return False
        parent = str(data.get("sha"))
        author = data.get("author")
        inner = data.get("commit")
        if not isinstance(author, dict) or not isinstance(inner, dict):
            return False
        commit = cast("dict[str, Any]", inner)
        verification = commit.get("verification")
        message = str(commit.get("message") or "")
        composed = f"{wanted['headline']}\n\n{wanted['body']}"
        if (
            cast("dict[str, Any]", author).get("login") != bot_login
            or not isinstance(verification, dict)
            or cast("dict[str, Any]", verification).get("verified") is not True
            or message.rstrip("\n") != composed.rstrip("\n")
        ):
            return False
    return True


def chain_link(data: dict[str, Any], parent: str, tree: str) -> bool:
    """Whether a compare-API commit has one parent, ``parent``, and ``tree``.

    Replay reproduces each verified commit's tree exactly, so a commit
    with the same message but other content, or another history, is
    not this run's work.
    """
    parents = data.get("parents")
    inner = data.get("commit")
    commit_tree = (
        cast("dict[str, Any]", inner).get("tree") if isinstance(inner, dict) else None
    )
    actual_tree = (
        cast("dict[str, Any]", commit_tree).get("sha")
        if isinstance(commit_tree, dict)
        else None
    )
    if not isinstance(parents, list) or len(cast("list[Any]", parents)) != 1:
        return False
    only = cast("list[Any]", parents)[0]
    parent_sha = (
        cast("dict[str, Any]", only).get("sha") if isinstance(only, dict) else None
    )
    return bool(tree) and parent_sha == parent and actual_tree == tree


def delete_branch(repository: str, branch: str) -> str | None:
    """Remove the bot branch after a failed publication; return a note on failure.

    A half-built branch would otherwise count as a prior attempt (the
    selector checks for the branch) and keep the issue out of every
    later run. Deletion is best effort: the original failure is what
    the caller reports, and a leftover branch is recorded beside it.
    """
    try:
        github.run_gh(
            ["api", "--method", "DELETE", f"repos/{repository}/git/refs/heads/{branch}"]
        )
    except github.GitHubError as exc:
        # After an ambiguous create the branch may never have existed;
        # GitHub answers 404 or 422 "Reference does not exist" then.
        if github.is_absent(exc) or "does not exist" in str(exc).lower():
            return None
        return f"branch {branch} could not be removed after the failure: {exc}"
    return None
