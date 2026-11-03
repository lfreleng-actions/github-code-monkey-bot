# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Recognise a closed attempt's branch, the one fork branch the publisher resets.

A named issue may be retried after a human closed the bot's pull
request unmerged (DESIGN.md sections 5 and 6), and that attempt's
branch, which GitHub keeps on close, then blocks the new create. The
publisher moves it back to the base only for the closed pull request
the selection authorised, and only from the tip checked here.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any, NamedTuple, cast

import bot_github as github
import branch_chain as chains
import branch_swap as swap
import issue_reads as reads
from proposal_policy import Rejection


class ClosedAttempt(NamedTuple):
    """The closed pull request the fork branch is left from, and its checked tip."""

    number: int
    sha: str


def closed_attempt(
    repository: str, branch: str, bot_login: str, fork_repository: str
) -> ClosedAttempt | None:
    """The closed pull request whose leftover the fork branch is, or None.

    The branch counts as that attempt's leftover only when every pull
    request from it on the target is closed and unmerged, the bot
    opened each one, and the branch still sits at the head of one of
    them: nothing on it is newer than the work a human closed, and
    the closed pull request keeps those commits. Every page is read,
    so an older open, merged or foreign pull request cannot hide
    behind a closed one. Anything else is not the bot's to reset.
    """
    fork_owner = fork_repository.partition("/")[0]
    head = urllib.parse.quote(f"{fork_owner}:{branch}", safe="")
    heads: dict[str, int] = {}
    for data in github.api_list(
        f"repos/{repository}/pulls?state=all&head={head}&per_page=100"
    ):
        head_data = data.get("head")
        head_info = (
            cast("dict[str, Any]", head_data) if isinstance(head_data, dict) else {}
        )
        head_repo = head_info.get("repo")
        head_name = (
            cast("dict[str, Any]", head_repo).get("full_name")
            if isinstance(head_repo, dict)
            else None
        )
        if not isinstance(head_name, str) or head_name.lower() != (
            fork_repository.lower()
        ):
            continue
        user = data.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        if reads.pull_state(data) != "closed" or login != bot_login:
            return None
        sha = head_info.get("sha")
        number = data.get("number")
        if isinstance(sha, str) and type(number) is int:
            # The newest pull request at a head names it, as in
            # prior_attempt(), which picks the retry_of this must match.
            heads[sha] = max(number, heads.get(sha, number))
    if not heads:
        return None
    tip = reads.branch_head(fork_repository, branch)
    number = heads.get(tip)
    return ClosedAttempt(number, tip) if number is not None else None


def create_fresh_branch(
    check: dict[str, Any],
    result: dict[str, Any],
    create: Callable[[str, str, str], None],
) -> ClosedAttempt | None:
    """Create the bot branch with ``create``, or reset a named retry's closed attempt.

    Returns the closed attempt when its branch was reset rather than
    created, so a later failure restores the branch, not deletes it.
    A branch that is neither raises the ``Rejection`` as before.
    """
    fork, branch = str(check["fork_repository"]), str(check["branch"])
    try:
        create(fork, branch, str(check["base_sha"]))
    except Rejection:
        closed = reset_closed_attempt(check, result)
        if closed is None:
            raise
        return closed
    return None


def reset_closed_attempt(
    check: dict[str, Any], result: dict[str, Any]
) -> ClosedAttempt | None:
    """Reset the existing bot branch when it is the named retry's closed attempt.

    Only a named retry carries ``retry_of``, the closed pull request
    the trusted selection admitted the issue past. A scan carries none
    and meets an existing branch as before, and so does a branch left
    by any other pull request: None tells the caller to treat it as
    any existing branch. A head that moved since the check is refused
    by ``reset_branch``. Returns the closed attempt the branch was
    reset from.
    """
    retry_of = check.get("retry_of")
    if type(retry_of) is not int:
        return None
    fork, branch = str(check["fork_repository"]), str(check["branch"])
    closed = closed_attempt(
        str(check["repository"]), branch, str(check["bot_login"]), fork
    )
    if closed is None or closed.number != retry_of:
        return None
    reset_branch(fork, branch, closed.sha, str(check["base_sha"]), result)
    cast("list[str]", result["warnings"]).append(
        f"reset the branch of closed pull request #{closed.number} "
        f"from {closed.sha[:7]} to the base to retry"
    )
    return closed


def restore_closed_attempt(
    check: dict[str, Any], closed: ClosedAttempt, progress: list[str]
) -> str:
    """Put a reset branch back at the closed attempt's head; say how it ended.

    Called when the replay after a reset failed. This run did not
    create the branch, so it never deletes it: the move goes from the
    head this run left (``replay_tip``) to the closed attempt's head,
    by the same compare-and-swap as the reset. A push since then makes
    it refuse, and the branch is kept as it is. Restored, the branch
    is again the closed attempt's leftover, so a later named run can
    retry.
    """
    fork, branch = str(check["fork_repository"]), str(check["branch"])
    left = replay_tip(check, progress)
    restored = (
        f"branch restored to closed pull request #{closed.number}'s head "
        f"{closed.sha[:7]}"
    )
    try:
        github.graphql(
            swap.UPDATE_REFS, {"input": ref_update(fork, branch, left, closed.sha)}
        )
    except github.GitHubError as exc:
        try:
            head = reads.branch_head(fork, branch)
        except github.GitHubError:
            head = None
        if head != closed.sha:
            return (
                f"branch {branch} kept: restoring closed pull request "
                f"#{closed.number}'s head from {left[:7]} failed: {exc}"
            )
    return restored


def replay_tip(check: dict[str, Any], progress: list[str]) -> str:
    """The head the failed replay left, read back from the branch.

    ``progress`` holds the commits whose reply arrived, but a commit
    can land and lose its reply, so the branch is read: when it holds
    a leading part of the proposal's chain, the bot's own verified
    commits on the base and at least those in ``progress``, its tip
    is what this run left. Anything else, or a failed read, falls
    back to ``progress``, and the move's compare-and-swap refuses.
    """
    base = str(check["base_sha"])
    fallback = progress[-1] if progress else base
    try:
        chain = chains.landed_chain(
            str(check["fork_repository"]),
            str(check["branch"]),
            base,
            cast("list[dict[str, Any]]", check["commits"]),
            str(check["bot_login"]),
        )
    except github.GitHubError:
        return fallback
    if chain is None or chain[: len(progress)] != progress:
        return fallback
    return chain[-1] if chain else base


def ref_update(fork: str, branch: str, before: str, after: str) -> dict[str, Any]:
    """The ``updateRefs`` input moving the branch from ``before`` to ``after`` only."""
    return {
        "repositoryId": swap.repository_id(fork),
        "refUpdates": [
            {
                "name": f"refs/heads/{branch}",
                "afterOid": after,
                "beforeOid": before,
                "force": True,
            }
        ],
    }


def reset_branch(
    fork: str, branch: str, checked: str, base_sha: str, result: dict[str, Any]
) -> None:
    """Move the branch from the checked tip to the base, or refuse if it moved.

    ``updateRefs`` applies the move only while the branch still holds
    ``checked``, so a push after the check is never overwritten; the
    branch is never deleted. A lost reply is read back from the
    branch, as a revision round's swap is (branch_swap.reconcile_swap):
    a moved head is a ``Rejection``, an unchanged one a failure.
    """
    try:
        github.graphql(
            swap.UPDATE_REFS, {"input": ref_update(fork, branch, checked, base_sha)}
        )
    except github.GitHubError as exc:
        swap.reconcile_swap(fork, branch, checked, base_sha, result, exc)
