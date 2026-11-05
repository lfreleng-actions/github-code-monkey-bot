# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Whether a pull request can merge into its default branch, read until it settles.

GitHub computes ``mergeable`` in the background: the first read after
a push, or after the base moves, usually answers ``null`` while that
job runs. ``settle`` reads again after a bounded backoff until the
answer is a boolean for the head the caller expects, and gives up as
``None``, never as mergeable.

A conflicting head gets no ``pull_request`` CI, since GitHub cannot
build its merge ref, so its checks look missing rather than failed.
The selection reads this to give such a pull request a round
(DESIGN.md section 20.1); the publish job reads it after the review
gate to report the head honestly (sections 8 and 20.3). Every read
here is of a public repository and needs no permission.
"""

from __future__ import annotations

import argparse
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import bot_github as github
import copilot_review as review
import issue_reads as reads
import proposal_model as model
from proposal_policy import PublishError

# Seconds between reads while ``mergeable`` is null: about a minute.
BACKOFF_SECONDS = (2, 4, 8, 16, 32)
# GitHub's ``mergeable_state`` is a short lowercase word; anything
# else is not recorded.
STATE_RE = re.compile(r"[a-z_]{1,32}")


@dataclass(frozen=True)
class Mergeability:
    """GitHub's answer: a boolean once settled, None when it never was."""

    mergeable: bool | None
    state: str | None
    warning: str | None = None


def settle(
    repository: str,
    number: int,
    head_sha: str,
    *,
    sleep: Callable[[float], None] | None = None,
) -> Mergeability:
    """Read the pull request until ``mergeable`` is a boolean for ``head_sha``, bounded.

    An answer for another head, a stale read or a push since, counts
    as unsettled and is read again. A read that fails, or no boolean
    for ``head_sha`` after the last backoff, is ``None`` with a
    warning: unknown, never mergeable.
    """
    state: str | None = None
    seen: str | None = None
    for delay in (*BACKOFF_SECONDS, None):
        try:
            data = github.api_object(f"repos/{repository}/pulls/{number}")
        except github.GitHubError as exc:
            return Mergeability(
                None,
                state,
                f"could not read whether pull request #{number} can merge: "
                f"{github.safe_message(exc)}",
            )
        seen = head_of(data)
        if seen == head_sha:
            raw = data.get("mergeable_state")
            state = raw if isinstance(raw, str) and STATE_RE.fullmatch(raw) else None
            value = data.get("mergeable")
            if isinstance(value, bool):
                return Mergeability(value, state)
        if delay is None:
            break
        (sleep or time.sleep)(delay)
    reads_made = len(BACKOFF_SECONDS) + 1
    if seen != head_sha:
        return Mergeability(
            None,
            None,
            f"pull request #{number} did not show head {head_sha[:12]} "
            f"after {reads_made} reads, so its mergeability is unknown",
        )
    return Mergeability(
        None,
        state,
        f"GitHub had not settled whether pull request #{number} can merge after "
        f"{reads_made} reads",
    )


def head_of(data: dict[str, Any]) -> str | None:
    """The head commit a pull request read names, or None when it names none."""
    head = data.get("head")
    sha = cast("dict[str, Any]", head).get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) and github.SHA_RE.fullmatch(sha) else None


def behind_by(repository: str, base_sha: str, head_sha: str) -> int:
    """How many commits ``head_sha`` lacks of ``base_sha``, from GitHub's compare."""
    base = urllib.parse.quote(base_sha, safe="")
    head = urllib.parse.quote(head_sha, safe="")
    data = github.api_object(f"repos/{repository}/compare/{base}...{head}")
    value = data.get("behind_by")
    if type(value) is not int or value < 0:
        raise github.GitHubError("compare: missing or invalid 'behind_by'")
    return value


def selected_base(check: dict[str, Any]) -> str:
    """The default branch head the selection read: ``rebase_sha`` or ``checkout_sha``.

    A revision records it as ``rebase_sha``; an issue entry's
    ``checkout_sha`` is its ``base_sha``.
    """
    key = "rebase_sha" if check.get("task") == "revise" else "base_sha"
    value = check.get(key)
    return value if isinstance(value, str) and github.SHA_RE.fullmatch(value) else ""


def probe(
    result: dict[str, Any],
    check: dict[str, Any],
    *,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Record whether the published pull request can merge, and how far behind it is.

    Adds ``mergeable``, ``mergeable_state``, ``base_behind_by`` and
    ``base_moved_during_run`` to the result, with a warning for each
    read that failed. Nothing here decides; the comment and the next
    selection read ``mergeable`` alone.
    """
    target = review.published(result)
    if target is None:
        return
    repository, number = target
    warnings = cast("list[Any]", result.setdefault("warnings", []))
    head = review.published_head(result)
    if head is None:
        settled = Mergeability(
            None, None, f"no published head to read pull request #{number} against"
        )
    else:
        settled = settle(repository, number, head, sleep=sleep)
    if settled.warning is not None:
        warnings.append(settled.warning)
    result["mergeable"] = settled.mergeable
    result["mergeable_state"] = settled.state
    result["base_behind_by"] = None
    result["base_moved_during_run"] = None
    default_branch = str(check.get("default_branch") or "")
    result["default_branch"] = default_branch or None
    if not default_branch:
        warnings.append("no default branch recorded to compare the head with")
        return
    try:
        current = reads.branch_head(repository, default_branch)
        selected = selected_base(check)
        if selected:
            result["base_moved_during_run"] = current != selected
        if head is not None:
            result["base_behind_by"] = behind_by(repository, current, head)
    except github.GitHubError as exc:
        warnings.append(
            f"could not compare the head with {default_branch}: "
            f"{github.safe_message(exc)}"
        )


def main(argv: list[str] | None = None) -> None:
    """Read mergeability after the review gate and write the result back."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--check", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = model.load_json(args.result, "result")
        check = model.load_json(args.check, "check")
        if review.published(result) is None:
            print("no pull request to probe")
            return
        probe(result, check)
        review.write_result(args.output, result)
    except (OSError, ValueError, PublishError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"merge probe: {message}\n")
    print(
        f"mergeable: {result['mergeable']}; state: {result['mergeable_state']}; "
        f"behind by: {result['base_behind_by']}; "
        f"base moved: {result['base_moved_during_run']}"
    )


if __name__ == "__main__":
    main()
