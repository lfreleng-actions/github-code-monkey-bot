# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The bot branch and its pull request, as GitHub holds them."""

from __future__ import annotations

from typing import Any, cast

import monkey_github as github
from proposal_policy import PublishError, Rejection


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


def existing_pull_request(repository: str, branch: str, bot_login: str) -> str | None:
    """The URL of an open pull request the bot opened from this repository's branch.

    A rerun of a publish job whose first attempt opened the pull
    request and then failed later meets the branch it made. Only a
    pull request the bot itself opened, from the target repository,
    counts as that earlier success: anyone else's is a conflict.
    """
    owner = repository.partition("/")[0]
    entries = github.api_page(
        f"repos/{repository}/pulls?state=open&head={owner}:{branch}&per_page=20"
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
            and head_name.lower() == repository.lower()
            and isinstance(url, str)
        ):
            return url
    return None


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
