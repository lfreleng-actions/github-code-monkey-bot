# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read back how much of a proposal's replayed chain a fork branch holds.

Adopting an existing branch (branch_refs.branch_matches) and putting
a reset branch back after a failed replay (stale_branch) both judge
the branch by the same rule: each commit is the one the replay of
the checked proposal would have made, on the recorded base.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, cast

import bot_github as github
import proposal_policy as policy


def landed_chain(
    repository: str,
    branch: str,
    base_sha: str,
    expected: list[dict[str, Any]],
    bot_login: str,
) -> list[str] | None:
    """The branch's commits when they are a leading part of the replay's chain.

    The branch must sit on the recorded base with no other history and
    carry at most as many commits as the proposal, each the one the
    replay makes at that place: its parent the one before it, the
    checked tree and message, the bot as author and Verified by
    GitHub. It may stop short of the last: a replay that failed part
    way, perhaps after a commit landed whose reply was lost, leaves
    such a branch. Anything else is not this run's work and yields
    None; the commits come back otherwise, the empty list included.
    """
    # The bot branch carries a slash; the compare path needs it encoded.
    encoded = urllib.parse.quote(branch, safe="")
    compare = github.api_object(f"repos/{repository}/compare/{base_sha}...{encoded}")
    merge_base = compare.get("merge_base_commit")
    base = (
        cast("dict[str, Any]", merge_base).get("sha")
        if isinstance(merge_base, dict)
        else None
    )
    commits = compare.get("commits")
    ahead = compare.get("ahead_by")
    if (
        base != base_sha
        or compare.get("behind_by") != 0
        or type(ahead) is not int
        or not 0 <= ahead <= len(expected)
        or not isinstance(commits, list)
        or len(cast("list[Any]", commits)) != ahead
    ):
        return None
    parent = base_sha
    chain: list[str] = []
    for actual, wanted in zip(
        cast("list[Any]", commits), expected[:ahead], strict=True
    ):
        if not isinstance(actual, dict):
            return None
        data = cast("dict[str, Any]", actual)
        if not chain_link(data, parent, str(wanted.get("tree", ""))):
            return None
        parent = str(data.get("sha"))
        if not policy.SHA_RE.fullmatch(parent):
            return None
        chain.append(parent)
        author = data.get("author")
        inner = data.get("commit")
        if not isinstance(author, dict) or not isinstance(inner, dict):
            return None
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
            return None
    return chain


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
