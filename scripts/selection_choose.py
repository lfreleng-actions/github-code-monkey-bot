# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Choose the entries a run works: the expensive checks, the cap and the budget.

Revision entries come first, since a round on an open pull request
comes before new work in the same repository; issue candidates
follow, each read in full the moment it is admitted. The one-per-
repository rule binds new issues alone (DESIGN.md section 6): every
round of a repository is taken, and a repository with an entry gets
no new issue in the same run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import fork_orgs as forks
import issue_reads as reads
from fork_orgs import ForkOrgError
from issue_reads import SelectionError

BRANCH_PREFIX = "code-monkey/issue-"
# GitHub expands at most 256 jobs from one matrix; a larger selection
# would fail at the author job rather than run.
MATRIX_LIMIT = 256
# The verifier refuses a selection.json over 16 MiB. Issue bodies and
# comments are bounded per issue, but 256 of them could still exceed
# that, so the cumulative serialised size stops the selection first,
# with headroom for the header fields and indentation.
MAX_SELECTION_BYTES = 12 * 1024 * 1024


class Budget:
    """The cap and the size budget, shared by both kinds, and the one-per-repository rule."""

    def __init__(self, max_pull_requests: int, *, one_per_repository: bool) -> None:
        """Start empty."""
        # One entry yields at most one pull request, so capping the
        # entries a run works caps the pull requests it can raise.
        self.cap = (
            min(max_pull_requests, MATRIX_LIMIT) if max_pull_requests else MATRIX_LIMIT
        )
        self.one_per_repository = one_per_repository
        # Each repository with an entry, and the first entry it took.
        self.seen: dict[str, str] = {}
        self.keys: set[str] = set()
        self.chosen: list[dict[str, Any]] = []
        self.used_bytes = 0

    def taken(self, repository: str) -> str | None:
        """The entry that took the repository, where the rule applies to an issue."""
        return self.seen.get(repository) if self.one_per_repository else None

    def full(self) -> bool:
        """Whether the cap or the size budget is spent."""
        return len(self.chosen) >= self.cap or self.used_bytes >= MAX_SELECTION_BYTES

    def duplicate(self, key: str) -> bool:
        """Whether an entry with this key is already chosen."""
        return key in self.keys

    def admit(self, entry: dict[str, Any]) -> bool:
        """Take the entry unless it would break the size budget."""
        size = len(json.dumps(entry, indent=2).encode("utf-8"))
        if self.used_bytes + size > MAX_SELECTION_BYTES:
            return False
        self.used_bytes += size
        self.seen.setdefault(
            str(entry["repository"]), f"{entry['repository']}#{entry['number']}"
        )
        self.keys.add(str(entry["key"]))
        self.chosen.append(entry)
        return True


def choose(
    ranked: list[dict[str, Any]],
    *,
    fork_orgs: dict[str, Any],
    max_pull_requests: int,
    skipped: dict[str, int],
    one_per_repository: bool = True,
    named: bool = False,
    skipped_issues: list[dict[str, str]] | None = None,
    revisions: list[tuple[str, Callable[[], dict[str, Any] | None]]] | None = None,
    reviser: Callable[[dict[str, Any], str, str], dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Run the expensive checks and cap; keep one issue per repository on a scan.

    ``revisions`` build a round each, taken first: a round on an open
    pull request comes before new work in the same repository. Each
    builder runs when the budget has room for it, so a pull request
    past the cap costs no reads and can fail none. The one-per-
    repository rule does not hold a round back: rounds share no state,
    so every bot pull request with work outstanding gets one. It
    holds back a new issue of a repository already chosen, by a round
    or an issue, before any read.
    ``reviser`` turns an issue whose earlier attempt is open into a
    revision entry, or returns None to skip it as before.
    ``named`` marks issues the operator named, which a closed earlier
    pull request does not keep out.
    ``skipped_issues`` collects a reason per skipped issue: every skip
    when the operator named the issues, so an operator learns why a
    named issue is missing from the matrix, and on a scan the issues
    the one-per-repository rule held back, with the target chosen.
    """

    def skip(
        candidate: dict[str, Any], counter: str, reason: str, chosen: str = ""
    ) -> None:
        skipped[counter] += 1
        if skipped_issues is not None and (named or chosen):
            reference = f"{candidate['repository']}#{candidate['number']}"
            record = {"issue": reference, "reason": reason}
            skipped_issues.append(record | ({"chosen": chosen} if chosen else {}))

    budget = Budget(max_pull_requests, one_per_repository=one_per_repository)
    for _, build in revisions or []:
        if budget.full():
            skipped["cap"] += 1
            continue
        entry = build()
        if entry is None:
            continue
        if budget.duplicate(str(entry["key"])):
            skip(entry, "duplicate", "already chosen under another name")
        elif not budget.admit(entry):
            skip(entry, "cap", "over the pull request cap")
    for candidate in ranked:
        repo = str(candidate["repository"])
        if budget.duplicate(f"{candidate['repo_name']}-{candidate['number']}"):
            skip(candidate, "duplicate", "already chosen under another name")
            continue
        taken = budget.taken(repo)
        if taken is not None:
            reason = f"another target in {repo} was chosen this run"
            skip(candidate, "one_per_repo", reason, taken)
            continue
        if budget.full():
            skip(candidate, "cap", "over the pull request cap")
            continue
        number = int(candidate["number"])
        branch = f"{BRANCH_PREFIX}{number}"
        # The publisher pushes into a fork in the bot pool, never the
        # target (DESIGN.md section 4.3); a prior attempt lives there.
        try:
            fork_org = forks.resolve(fork_orgs, repo.partition("/")[0])
        except ForkOrgError as exc:
            raise SelectionError(str(exc)) from exc
        fork_repository = f"{fork_org}/{candidate['repo_name']}"
        # An open pull request from the fork branch is an attempt a
        # human has not processed yet: a round on it when revisions are
        # on, a skip otherwise. A merged one finished the issue, and a
        # closed one is an attempt a human rejected: a scan leaves both
        # alone, while a named issue overrides a closed one, since
        # naming it is the operator's decision (DESIGN.md section 6),
        # and the publisher resets that closed attempt's branch. A fork
        # branch with no pull request is an orphan from a publish that
        # failed after pushing: select the issue so the publisher can
        # adopt the branch and finish.
        prior = reads.prior_attempt(repo, fork_repository, branch)
        if prior is not None and prior.kind == "pull_request":
            if prior.state == "open":
                revision = reviser(candidate, fork_org, branch) if reviser else None
                if revision is None:
                    skip(candidate, "attempted", prior.describe())
                elif not budget.admit(revision):
                    skip(candidate, "cap", "over the selection size budget")
                continue
            if prior.state == "merged" or not named:
                skip(candidate, "attempted", prior.describe())
                continue
        if reads.has_open_linked_pr(repo, number):
            skip(candidate, "linked_pr", "an open pull request is linked to close it")
            continue
        comments, dropped, truncated = reads.filtered_comments(repo, number)
        base_sha = reads.branch_head(repo, str(candidate["default_branch"]))
        entry = {
            "key": f"{candidate['repo_name']}-{number}",
            "task": "issue",
            **{k: v for k, v in candidate.items() if k != "assignees"},
            "base_sha": base_sha,
            "checkout_sha": base_sha,
            "branch": branch,
            "fork_org": fork_org,
            "fork_repository": fork_repository,
            "resume": prior is not None and prior.kind == "branch",
            # Set only on the named path past a closed pull request:
            # the one attempt whose branch the publisher may reset.
            "retry_of": (
                prior.number if prior is not None and prior.state == "closed" else None
            ),
            "comments": comments,
            "comments_dropped": dropped,
            "comments_truncated": truncated,
        }
        if not budget.admit(entry):
            skip(candidate, "cap", "over the selection size budget")
    return budget.chosen
