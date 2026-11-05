# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The bot branch's head on the fork, read to settle what a write did."""

from __future__ import annotations

import urllib.parse
from typing import Any, cast

import bot_github as github
from proposal_policy import PublishError


def branch_tip(fork: str, branch: str) -> str | None:
    """The bot branch's head commit, or None when the branch is gone.

    The heads namespace alone: a tag of the same name is not the
    branch. A 404 is absence; any other read failure is the caller's.
    """
    quoted = urllib.parse.quote(branch, safe="/")
    try:
        data = github.api_object(f"repos/{fork}/git/ref/heads/{quoted}")
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return None
        raise
    target = data.get("object")
    if not isinstance(target, dict):
        raise github.GitHubError(f"branch {branch} has no object")
    return github.require_sha(cast("dict[str, Any]", target), "sha", "branch head")


def replayed_head(
    fork: str, branch: str, parent: str, tree: str, message: str
) -> str | None:
    """The branch head when it is the commit a lost mutation made, else None.

    ``createCommitOnBranch`` is a compare-and-swap on ``parent``: a
    head still there means the mutation did not land, and it may be
    sent again. A head one commit on, with this commit's tree and
    message, is that mutation landing. Any other head is someone
    else's write, and a branch gone is no landing either: neither is
    "not landed", so both fail the replay.
    """
    head = branch_tip(fork, branch)
    if head == parent:
        return None
    if head is None:
        raise PublishError(f"branch {branch} vanished during the replay")
    data = github.api_object(f"repos/{fork}/git/commits/{head}")
    parents = [
        cast("dict[str, Any]", item).get("sha")
        for item in cast("list[Any]", data.get("parents") or [])
        if isinstance(item, dict)
    ]
    found = data.get("tree")
    found_tree = (
        cast("dict[str, Any]", found).get("sha") if isinstance(found, dict) else None
    )
    if (
        parents != [parent]
        or found_tree != tree
        or str(data.get("message") or "").rstrip("\n") != message.rstrip("\n")
    ):
        raise PublishError(
            f"branch {branch} moved to {head[:7]}, not this replay's commit"
        )
    return head
