# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The compare-and-swap that moves a bot branch to a revision round's chain.

The new chain is replayed onto a temporary branch in the fork, so
every commit is signed, and ``updateRefs`` then points the bot branch
at its head on the condition that the branch still holds the head the
selection recorded (DESIGN.md section 20.3). A head that moved since
is a rejection; nothing is forced over work the run did not see.
Every reply GitHub may lose, the scratch branch's creation and the
swap itself, is read back from the branch rather than guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import bot_github as github
import branch_refs as refs
import issue_reads as reads
from proposal_policy import PublishError, Rejection

UPDATE_REFS = """
mutation($input: UpdateRefsInput!) {
  updateRefs(input: $input) { clientMutationId }
}
"""  # The one updateRefs error that says the swap did not apply: the
# branch no longer held beforeOid. Nothing looser, since "unexpected
# EOF" is a transport error that may follow an applied swap.
MISMATCH = re.compile(r"beforeOid|\bexpected\b[^.]*\bto point to\b", re.I)


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
    replayed = prepare_scratch(fork, scratch, check)
    try:
        staged = dict(check, branch=scratch)
        commits = (
            replayed if replayed is not None else refs.replay_commits(clone, staged)
        )
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
            reconcile_swap(fork, branch, expected, commits[-1], check, exc)
        return commits
    finally:
        leftover = refs.delete_branch(fork, scratch)
        if leftover:
            # The swap outcome stands; the scratch branch costs a look.
            cast("list[Any]", check.setdefault("warnings", [])).append(leftover)


def prepare_scratch(fork: str, scratch: str, check: dict[str, Any]) -> list[str] | None:
    """Create the scratch branch at the base, or make sense of one already there.

    GitHub may create the ref and lose the reply, and an earlier
    attempt may have left the ref when its own cleanup failed. The
    branch at the base is this round's fresh scratch, resumed; one
    carrying the round's verified chain is an earlier attempt's
    finished replay, adopted and returned so no second replay runs;
    one holding anything else is nobody's to delete, so it is named
    in a warning and the move fails. No branch after a failed create
    is a plain failure.
    """
    base = str(check["base_sha"])
    warnings = cast("list[Any]", check.setdefault("warnings", []))
    try:
        refs.create_branch(fork, scratch, base)
        return None
    except (Rejection, PublishError, github.GitHubError) as exc:
        # create_branch wraps a failed call in PublishError and an
        # existing branch in Rejection; either may follow a create that
        # applied, so the branch, not the error, says what happened.
        cause: Exception = exc
    try:
        head = reads.branch_head(fork, scratch)
    except github.GitHubError as lookup:
        if github.is_absent(lookup):
            raise PublishError(
                f"scratch branch {scratch} could not be created: {cause}"
            ) from cause
        raise PublishError(
            f"scratch branch {scratch} could not be read ({lookup}) after its "
            f"creation answered {cause}"
        ) from cause
    if head == base:
        warnings.append(
            f"scratch branch {scratch} already stood at the base ({cause}); "
            "resumed as this attempt's"
        )
        return None
    chain = refs.branch_matches(
        fork,
        scratch,
        base,
        cast("list[dict[str, Any]]", check["commits"]),
        str(check["bot_login"]),
    )
    if chain is not None:
        warnings.append(
            f"scratch branch {scratch} already carried the round's chain; "
            "an earlier attempt's replay, adopted"
        )
        return chain
    warnings.append(
        f"scratch branch {scratch} holds commits this run did not make and is "
        "kept for a human to inspect"
    )
    raise PublishError(
        f"scratch branch {scratch} exists with commits this run did not make"
    ) from cause


def reconcile_swap(
    fork: str,
    branch: str,
    expected: str,
    tip: str,
    check: dict[str, Any],
    exc: github.GitHubError,
) -> None:
    """Read the branch after a swap whose reply was an error, and decide.

    GitHub may apply ``updateRefs`` and then lose the reply, so the
    branch, not the error text, says what happened. The branch at the
    new tip means the move stands and publication goes on, with a
    warning; at the recorded head it means the move did not happen
    and the branch is unchanged. Elsewhere, the compare-and-swap
    refusal means someone moved the branch, a rejection; any other
    error there, or no answer at all, is an outcome this run cannot
    name, and it says so rather than claim the branch unchanged.
    """
    refused = MISMATCH.search(str(exc)) is not None
    moved = Rejection(f"head moved from {expected[:7]} since selection; nothing forced")
    try:
        head = reads.branch_head(fork, branch)
    except github.GitHubError as lookup:
        if refused:
            raise moved from exc
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
    if refused:
        raise moved from exc
    raise SwapUnknown(
        f"updateRefs failed ({exc}) and the branch reads as {head[:7]}, neither "
        "head: outcome unknown"
    ) from exc


def already_moved(check: dict[str, Any]) -> list[str] | None:
    """The round's chain when the bot branch already holds it, else None.

    The same proof the new-issue path asks of an existing branch: on
    the round's base, as many commits as the proposal, each the bot's,
    Verified and bearing the checked message. Before the swap the
    branch holds the earlier round on its own base and fails the
    proof; any other head fails it too and goes to the swap, which
    refuses what moved.
    """
    return refs.branch_matches(
        str(check["fork_repository"]),
        str(check["branch"]),
        str(check["base_sha"]),
        cast("list[dict[str, Any]]", check["commits"]),
        str(check["bot_login"]),
    )
