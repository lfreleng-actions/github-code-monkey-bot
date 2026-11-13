# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which workflow files a proposal may change (DESIGN.md sections 8 and 20.3).

A new proposal may add, edit or delete a file under
``.github/workflows/`` only when the run's ``allow_workflow_edits``
switch, recorded in the trusted selection, is on. A revision round may
change only the workflow files its pull request already changes,
whatever the switch says: a round answers review findings on its pull
request and never widens it into another workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import proposal_policy as policy
from git_bounded import git
from proposal_model import Check
from proposal_policy import PublishError, Rejection

# Paths a reason names before it says how many more there are.
NAMED = 10
# Published commits read for the workflow writes a round carries over.
HISTORY = 64
# The mode git reports for a side of a change where the file is absent.
ABSENT = "000000"

# A path's state at one commit: its mode and blob.
State = tuple[str, str]
# One commit's change to a path: its state before and after.
Step = tuple[State, State]


def touched(commits: list[dict[str, Any]]) -> list[str]:
    """Every workflow path the chain adds, edits or deletes, sorted."""
    paths: set[str] = set()
    for commit in commits:
        paths.update(str(item["path"]) for item in commit["additions"])
        paths.update(str(path) for path in commit["deletions"])
    return sorted(p for p in paths if p.startswith(policy.WORKFLOW_PREFIX))


def named(paths: list[str]) -> str:
    """The paths for a reason, bounded so the reason stays readable."""
    shown = ", ".join(paths[:NAMED])
    rest = len(paths) - NAMED
    return f"{shown} and {rest} more" if rest > 0 else shown


def fork_url(fork_repository: str) -> str:
    """Where the pull request's head lives: the bot fork, read anonymously."""
    return f"https://github.com/{fork_repository}.git"


def fetch_pull_request(clone: Path, check: Check) -> None:
    """Fetch the pull request's recorded base and published head."""
    git(
        clone,
        "-c",
        "protocol.version=2",
        "fetch",
        "-q",
        "--depth=1",
        fork_url(check.fork_repository),
        check.pull_base_sha,
        check.head_sha,
    )


def workflow_diff(clone: Path, old: str, new: str) -> set[str]:
    """The workflow paths that differ between two commits."""
    raw = str(
        git(
            clone,
            "diff-tree",
            "-r",
            "-z",
            "--no-renames",
            "--name-only",
            old,
            new,
        )
    )
    return {p for p in raw.split("\0") if p.startswith(policy.WORKFLOW_PREFIX)}


def pull_request_workflows(clone: Path, check: Check) -> set[str]:
    """The workflow paths the pull request changes, base to published head.

    The base is the one the selection recorded for the pull request,
    not the rebase commit a round may have moved to, so the diff holds
    the pull request's own changes and none from the default branch.
    """
    fetch_pull_request(clone, check)
    return workflow_diff(clone, check.pull_base_sha, check.head_sha)


def published_commits(clone: Path, check: Check) -> list[str]:
    """The published pull request's own commits, oldest first.

    Walks the first-parent line from the published head back to the
    recorded base, at most HISTORY commits, so commits a merge from
    the default branch brings in are not the pull request's. A longer
    history yields fewer commits, which only rejects more.
    """
    git(
        clone,
        "-c",
        "protocol.version=2",
        "fetch",
        "-q",
        f"--depth={HISTORY}",
        fork_url(check.fork_repository),
        check.head_sha,
    )
    shas = str(
        git(
            clone,
            "rev-list",
            "--first-parent",
            "--no-merges",
            f"--max-count={HISTORY}",
            f"{check.pull_base_sha}..{check.head_sha}",
        )
    ).split()
    return shas[::-1]


def transitions(
    clone: Path, shas: list[str], paths: list[str]
) -> dict[str, list[Step]]:
    """Each commit's change to each of ``paths``, per path in commit order.

    A change is the path's mode and blob before and after the commit,
    read from the commit's raw diff against its parent. No pathspec is
    involved, so a path holding a glob character names that file alone.
    """
    found: dict[str, list[Step]] = {path: [] for path in paths}
    for sha in shas:
        fields = str(
            git(
                clone,
                "diff-tree",
                "-r",
                "-z",
                "--raw",
                "--no-renames",
                "--no-commit-id",
                sha,
            )
        ).split("\0")
        for meta, path in zip(fields[0::2], fields[1::2], strict=False):
            if path in found:
                old_mode, new_mode, old, new = meta.lstrip(":").split(" ")[:4]
                found[path].append(((old_mode, old), (new_mode, new)))
    return found


def merged(clone: Path, ours: State, base: State, theirs: State) -> State | None:
    """``base`` to ``theirs`` applied to ``ours``, or None when it does not apply.

    A three-way merge of the blobs, as a rebase makes it, so the change
    keeps its place in the file. A file added, deleted or changed in
    mode on either side, a conflict, or a file git cannot merge yields
    None.
    """
    if ABSENT in (ours[0], base[0], theirs[0]) or ours[0] != base[0]:
        return None
    try:
        oid = str(
            git(
                clone,
                "merge-file",
                "--object-id",
                "--diff-algorithm=histogram",
                ours[1],
                base[1],
                theirs[1],
            )
        ).strip()
    except PublishError:
        return None
    return (theirs[0], oid)


def replayed(clone: Path, start: State, published: list[Step]) -> list[Step]:
    """The published changes to one path, replayed in order from ``start``.

    ``start`` is the path as the round's base holds it. A change whose
    before-state is the current one applies as published; any other is
    merged onto it. Replay stops at the first change that does not
    apply, which only leaves fewer changes to match.
    """
    trusted: list[Step] = []
    state = start
    for before, after in published:
        result = after if state == before else merged(clone, state, before, after)
        if result is None:
            break
        trusted.append((state, result))
        state = result
    return trusted


def carried(chain: list[Step], trusted: list[Step]) -> bool:
    """Whether the chain's changes are trusted ones, in order, none repeated.

    A subsequence check: each trusted change can be used once, so a
    round cannot replay an edit and revert more often than the pull
    request made them.
    """
    remaining = iter(trusted)
    return all(step in remaining for step in chain)


def inherited(clone: Path, chain: list[Step], published: list[Step]) -> bool:
    """Whether the chain's changes to a path are the pull request's own.

    The chain sits on the round's base, so its first change to the path
    starts from the path as that base holds it.
    """
    if not chain:
        return False
    return carried(chain, replayed(clone, chain[0][0], published))


def check_workflows(clone: Path, check: Check, *, allowed: bool) -> None:
    """Reject a chain that changes a workflow file it may not change."""
    paths = touched(check.commits)
    if not paths:
        return
    if check.task != "revise":
        if not allowed:
            raise Rejection(
                "workflow edits are off for this run (allow_workflow_edits is "
                "false), so a new proposal may not add, change or delete files "
                f"under {policy.WORKFLOW_PREFIX}: {named(paths)}"
            )
        return
    changed = pull_request_workflows(clone, check)
    outside = [p for p in paths if p not in changed]
    if outside:
        # A path the pull request's net diff leaves alone may still
        # appear in commits the round carries over, such as an edit and
        # its revert. Those pass when the chain, taken whole, leaves the
        # path as its base had it and makes no change to it that the
        # published commits, replayed onto that base, did not already
        # make, in the same order and at the same place.
        net = workflow_diff(clone, check.base_sha, str(check.commits[-1]["sha"]))
        chain = transitions(clone, [str(c["sha"]) for c in check.commits], outside)
        published = transitions(clone, published_commits(clone, check), outside)
        outside = [
            p
            for p in outside
            if p in net or not inherited(clone, chain[p], published[p])
        ]
    if outside:
        raise Rejection(
            "a revision round may change only the workflow files its pull "
            f"request already changes; this one also changes {named(outside)}"
        )
