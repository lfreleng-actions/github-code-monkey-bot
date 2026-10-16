# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Named issues: the operator's list, read directly, safety filters alone.

The workflow's splitter accepts the operator's spellings and
publishes each issue as ``owner/repo#N``; this module reads that
form, looks each issue up without a scan, and applies the filters
the pipeline cannot work around. The triage signals that rank a scan
(Type, labels, assignee, category) do not apply to an issue a
maintainer named (DESIGN.md section 6).
"""

from __future__ import annotations

import re
from typing import Any

import bot_github as github
import issue_categories as categories
import issue_reads as reads
import repository_reads as repositories
from issue_reads import SelectionError

# The splitter's canonical form for an issue: owner/repo#N.
ISSUE_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)"
)


def parse_issues(text: str, org: str) -> list[tuple[str, int]]:
    """Read ``owner/repo#N`` references to ``(repo_name, number)``, in order.

    The workflow's splitter has already accepted the operator's
    spellings and published this one form; this check stands behind
    it. An owner other than the organisation is refused: the run holds
    a token for one owner, and a typo must not reach a stranger's
    issue. Repository names fold to lower case, as GitHub compares
    them, so a reference repeated in another case is one issue.
    """
    targets: list[tuple[str, int]] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        found = ISSUE_RE.fullmatch(token)
        if found is None or found.group("repo") in (".", ".."):
            raise SelectionError(f"invalid issue reference {token!r}")
        if found.group("owner").lower() != org.lower():
            raise SelectionError(f"issue {token!r} is not in {org}")
        target = (found.group("repo").lower(), int(found.group("number")))
        if target not in targets:
            targets.append(target)
    return targets


def repository_obstacle(meta: dict[str, Any] | None) -> str | None:
    """Why a repository cannot take part, or None when it can.

    The author job clones without a credential and the publisher
    forks the target, so these hold whoever names the repository.
    """
    if meta is None:
        return "not found"
    for flag, reason in (
        ("archived", "archived"),
        ("template", "a template"),
        ("fork", "a fork"),
    ):
        if meta[flag]:
            return reason
    if not meta["public"]:
        return "not public"
    if not meta.get("default_branch"):
        return "empty"
    return None


def candidates(
    org: str,
    targets: list[tuple[str, int]],
    *,
    skipped: dict[str, int],
    skipped_issues: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Read each named issue directly, applying the safety filters alone.

    Naming an issue is an operator's decision, so the triage signals
    (Type, labels, assignee, category) do not apply; what remains is
    what the pipeline cannot work around: a repository the author job
    cannot clone or the publisher cannot fork, a closed or absent
    issue, a pull request's number, and the per-issue human opt-out.
    Each skip is named with its reason and the rest proceed. The
    repositories are read one by one, never listed: an owner at the
    listing ceiling can still have an issue named.
    """
    candidates: list[dict[str, Any]] = []
    metadata: dict[str, dict[str, Any] | None] = {}

    def skip(reference: str, counter: str, reason: str) -> None:
        skipped[counter] += 1
        skipped_issues.append({"issue": reference, "reason": reason})

    for repo_name, number in targets:
        reference = f"{org}/{repo_name}#{number}"
        if repo_name not in metadata:
            metadata[repo_name] = repositories.repository_meta(org, repo_name)
        meta = metadata[repo_name]
        obstacle = repository_obstacle(meta)
        if obstacle is not None or meta is None:
            skip(reference, "repository", f"repository is {obstacle}")
            continue
        full = f"{org}/{meta['name']}"
        try:
            details = reads.issue_details(full, number)
        except reads.NotAnIssue:
            skip(reference, "pull_request", "names a pull request")
            continue
        except github.GitHubError as exc:
            # A missing issue is the operator's typo; anything else
            # (permission, rate limit, server) is the run's problem.
            if not github.is_absent(exc):
                raise
            skip(reference, "closed", "issue not found")
            continue
        if details["state"] != "open":
            skip(reference, "closed", "issue is closed")
            continue
        if "no-agent" in details["labels"]:
            skip(reference, "label", "carries the no-agent label")
            continue
        found = categories.categories_of(details["labels"], details["type"])
        candidates.append(
            {
                "repository": full,
                "repo_name": meta["name"],
                "number": number,
                "default_branch": meta["default_branch"],
                **details,
                "categories": sorted(found),
            }
        )
    return candidates
