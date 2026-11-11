# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The revision rounds a run works, built on demand with one set of inputs.

``Rounds`` gathers the three ways a run finds a round: the pull
requests the operator named, the owner-wide scan (``revision_scan``)
and an issue's own open pull request. Each builds its entry through
``revision_select`` (DESIGN.md section 20.1). It sits apart from both
so neither has to import the other late to break a cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import revision_reads as revisions
from revision_reads import pull_request_for_branch
from revision_scan import Hold, Round, scan
from revision_select import Inputs, Skip, build_entry


class Rounds:
    """The revision entries a run works, built on demand with one set of inputs."""

    def __init__(
        self,
        org: str,
        *,
        bot: dict[str, Any],
        fork_orgs: dict[str, Any],
        enabled: bool,
        explicit: bool,
        skips: tuple[dict[str, int], list[dict[str, str]]],
        allowed: Callable[[str], bool] | None = None,
        reviewers: frozenset[str] = frozenset(),
        max_rounds: int = revisions.MAX_ROUNDS,
    ) -> None:
        """Remember what every build needs; ``skips`` are the counters and the named list."""
        self.org = org
        self.bot = bot
        self.fork_orgs = fork_orgs
        self.inputs = Inputs(org, bot, fork_orgs, reviewers, max_rounds)
        self.enabled = enabled
        self.explicit = explicit
        self.skipped, self.skipped_issues = skips
        self.allowed = allowed

    def named(self, pulls: list[tuple[str, int]]) -> list[Round]:
        """A builder per pull request the operator named; a miss is a named skip."""
        builders: list[Round] = []
        for repo_name, number in pulls:

            def build(
                repo_name: str = repo_name, number: int = number
            ) -> dict[str, Any] | None:
                try:
                    return build_entry(self.inputs, repo_name, number, explicit=True)
                except Skip as exc:
                    self.skipped["revision"] += 1
                    reference = f"{self.org}/{repo_name}#{number}"
                    self.skipped_issues.append({"issue": reference, "reason": str(exc)})
                    return None

            builders.append((f"{self.org}/{repo_name}", build))
        return builders

    def scan(self, held: Hold | None = None) -> list[Round]:
        """Builders for the owner's bot pull requests, when enabled, skipping ``held``."""
        if not self.enabled:
            return []
        return scan(
            self.org,
            bot=self.bot,
            fork_orgs=self.fork_orgs,
            skipped=self.skipped,
            skipped_issues=self.skipped_issues,
            allowed=self.allowed,
            reviewers=self.inputs.reviewers,
            max_rounds=self.inputs.max_rounds,
            held=held,
        )

    def for_issue(
        self, candidate: dict[str, Any], fork_org: str, branch: str
    ) -> dict[str, Any] | None:
        """A round on the issue's open pull request, or None to skip it as before."""
        if not self.enabled:
            return None
        number = pull_request_for_branch(
            str(candidate["repository"]), fork_org, branch, str(self.bot["login"])
        )
        if number is None:
            return None
        try:
            return build_entry(
                self.inputs, str(candidate["repo_name"]), number, explicit=self.explicit
            )
        except Skip:
            return None
