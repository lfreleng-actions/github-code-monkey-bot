# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Whether a target lets its sessions use ``sudo`` and containers.

The author job runs its session with both disabled unless the target
carries a regular file named ``.enable-sudo-and-containers`` at the
root of its default branch (DESIGN.md section 4.1). The author job is
untrusted and hardens its runner before any checkout, so the select
job decides here and records the answer on each entry.

The marker is read at the entry's ``checkout_sha``: the default-branch
commit an issue builds on, or the default-branch head a revision round
may rebase onto. Never a bot branch or a pull request head, so no
round can turn the switch on for itself.
"""

from __future__ import annotations

from typing import Any, cast

import bot_github as github
from issue_reads import SHA_RE

MARKER = ".enable-sudo-and-containers"
FIELD = "sudo_and_containers"
# Git's blob modes for a regular file. A symlink is 120000, a
# directory 040000 and a submodule 160000; none of them counts.
REGULAR_MODES = frozenset({"100644", "100755"})


def opted_in(repository: str, commit: str) -> bool:
    """Whether the root tree at ``commit`` holds the marker as a regular file.

    Reads the git tree rather than the contents endpoint, which serves
    a symlink's target as though it were the file. Any failed read
    propagates, as the selection's other reads of a target do.
    """
    if not SHA_RE.fullmatch(commit):
        raise github.GitHubError(f"marker read needs a commit SHA, not {commit!r}")
    reply = github.api_object(f"repos/{repository}/git/trees/{commit}")
    entries = reply.get("tree")
    if not isinstance(entries, list):
        raise github.GitHubError(f"root tree of {repository} has no entry list")
    for raw in cast("list[Any]", entries):
        if isinstance(raw, dict):
            entry = cast("dict[str, Any]", raw)
            if entry.get("path") == MARKER:
                return (
                    entry.get("type") == "blob" and entry.get("mode") in REGULAR_MODES
                )
    if reply.get("truncated"):
        raise github.GitHubError(f"root tree of {repository} was truncated")
    return False


def attach(chosen: list[dict[str, Any]]) -> None:
    """Record on each entry whether its target opted in."""
    for entry in chosen:
        entry[FIELD] = opted_in(str(entry["repository"]), str(entry["checkout_sha"]))
