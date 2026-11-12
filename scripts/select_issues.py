# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Choose the issues a run will work and write the trusted selection.

Runs in the trusted select job with a read token. Scans the open
issues of an owner, drops the ones policy excludes, ranks the rest
by Priority then age, keeps one per repository, and caps the list;
or, when the caller names issues, works those alone in the order
given (``explicit_issues``); a revise-only run reads no issue and
keeps the revision rounds alone. For each survivor it records the
default-branch SHA the agent will build on, the filtered comments,
and the branch name the publisher will create. It also fetches the
organisation's ``AGENTS.md`` at a recorded commit so the author job
never reads it live.

Outputs in ``--output-dir``: ``selection.json``, ``matrix.json``,
``agents.md``, ``excluded-repos.txt`` and ``selection-summary.md``.
See DESIGN.md section 18.1 for the selection schema.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import bot_github as github
import explicit_issues as explicit_targets
import fork_orgs as forks
import issue_authors as authors
import issue_categories as categories
import issue_reads as reads
import overlap_wait
import previous_rejection
import repository_reads as repositories
import revision_rounds
import run_overlap
import selection_outputs as outputs
import stale_statuses as stale
from fork_orgs import ForkOrgError
from issue_reads import SelectionError
from selection_choose import choose
from selection_document import Settings, selection_document
from selection_inputs import (
    load_exclusions,
    load_reviewers,
    parse_max_pull_requests,
    parse_max_rounds,
    parse_repositories,
    selection_parser,
)

PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
SKIP_LABELS = frozenset({"question", "breaking-change", "chore", "no-agent"})


def rank_key(candidate: dict[str, Any]) -> tuple[int, str, str, int]:
    """Order by priority, then age, then a stable name for determinism."""
    priority = candidate.get("priority")
    rank = (
        PRIORITY_RANK.get(priority, len(PRIORITY_RANK))
        if priority
        else len(PRIORITY_RANK)
    )
    return (
        rank,
        str(candidate["created_at"]),
        str(candidate["repository"]),
        int(candidate["number"]),
    )


def cheap_filter(
    issues: list[dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    *,
    exclusions: set[str],
    explicit: list[str],
    include_dotgithub: bool,
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    """Apply the filters the search payload alone can answer."""
    kept: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("isPullRequest"):
            skipped["pull_request"] += 1
            continue
        repository = issue.get("repository")
        if not isinstance(repository, dict):
            raise github.GitHubError("search entry without a repository")
        repo_data = cast("dict[str, Any]", repository)
        full = github.require_str(repo_data, "nameWithOwner", "search repository")
        name = github.require_str(repo_data, "name", "search repository")
        lowered = name.lower()
        meta = repositories.get(lowered)
        # Naming repositories overrides the exclusion list and the
        # .github rule, nothing else: an archived, template, forked or
        # non-public repository cannot take part whoever names it.
        overridable = lowered in exclusions or (
            lowered == ".github" and not include_dotgithub
        )
        if (
            (overridable and not explicit)
            or meta is None
            or meta["archived"]
            or meta["template"]
            or meta["fork"]
            or not meta["public"]
            or not meta.get("default_branch")
        ):
            skipped["repository"] += 1
            continue
        labels = reads.label_names(issue.get("labels"))
        if SKIP_LABELS.intersection(labels):
            skipped["label"] += 1
            continue
        association = issue.get("authorAssociation")
        bot = authors.is_bot(issue.get("author"))
        if not authors.author_admitted(association, labels, bot=bot):
            skipped["untrusted_author"] += 1
            continue
        kept.append(
            {
                "repository": full,
                "repo_name": name,
                "number": github.require_int(issue, "number", "search issue"),
                "url": github.require_str(issue, "url", "search issue"),
                "created_at": github.require_str(issue, "createdAt", "search issue"),
                "author_association": association,
                "author_is_bot": bot,
                "default_branch": meta["default_branch"],
            }
        )
    return kept


def enrich(
    candidates: list[dict[str, Any]],
    *,
    include_assigned: bool,
    enabled: frozenset[str],
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    """Add the per-issue reads ranking needs and drop untriaged or claimed issues."""
    enriched: list[dict[str, Any]] = []
    for candidate in candidates:
        details = reads.issue_details(candidate["repository"], candidate["number"])
        if details["state"] != "open":
            # The search result is older than this read; an issue closed
            # in between is not worth an agent session.
            skipped["closed"] += 1
            continue
        if details["type"] is None:
            skipped["no_type"] += 1
            continue
        if details["assignees"] and not include_assigned:
            skipped["assigned"] += 1
            continue
        if SKIP_LABELS.intersection(details["labels"]):
            skipped["label"] += 1
            continue
        # The direct read is newer than the search and overrules it.
        merged = {**candidate, **details}
        if not authors.admits(merged):
            skipped["untrusted_author"] += 1
            continue
        found = categories.categories_of(details["labels"], details["type"])
        if not found & enabled:
            skipped["category"] += 1
            continue
        enriched.append({**merged, "categories": sorted(found)})
    return enriched


SKIP_COUNTERS = (
    "pull_request",
    "repository",
    "label",
    "untrusted_author",
    "no_type",
    "closed",
    "category",
    "assigned",
    "attempted",
    "linked_pr",
    "one_per_repo",
    "cap",
    "revision",
    "duplicate",
    "held",
)


def scan_candidates(
    args: argparse.Namespace,
    repositories: dict[str, dict[str, Any]],
    *,
    explicit: list[str],
    exclusions: set[str],
    enabled: frozenset[str],
    skipped: dict[str, int],
) -> tuple[int, list[dict[str, Any]]]:
    """Scan the owner or the named repositories; return the count seen and the ranking."""
    for name in explicit:
        if name not in repositories:
            raise SelectionError(f"repository {args.org}/{name} not found")
    issues = reads.search_open_issues(args.org, explicit)
    candidates = cheap_filter(
        issues,
        repositories,
        exclusions=exclusions,
        explicit=explicit,
        include_dotgithub=args.include_dotgithub,
        skipped=skipped,
    )
    enriched = enrich(
        candidates,
        include_assigned=args.include_assigned,
        enabled=enabled,
        skipped=skipped,
    )
    return len(issues), sorted(enriched, key=rank_key)


def parse_targets(
    args: argparse.Namespace,
) -> tuple[list[tuple[str, int]], list[str], list[tuple[str, int]]]:
    """Parse the named issues, repositories and pull requests, and check they fit."""
    targets = explicit_targets.parse_issues(args.issues, args.org)
    explicit = parse_repositories(args.repositories)
    pulls = explicit_targets.parse_issues(args.pull_requests, args.org)
    if (targets or pulls) and explicit:
        raise SelectionError(
            "issues or pull requests and repositories cannot both be given"
        )
    if pulls and args.mode != "pull-requests":
        # A round moves a live pull request's head and answers its
        # review; no other mode publishes that.
        raise SelectionError("pull_requests needs pull-requests mode")
    if args.outstanding_only and (targets or not pulls):
        raise SelectionError("outstanding_only needs pull_requests alone")
    if args.continues_run and not args.outstanding_only:
        raise SelectionError("continues_run needs outstanding_only")
    if args.revise_only:
        # Revisions alone: a scan of the owner's bot pull requests and
        # no issue, so nothing may name issues or narrow the scan.
        if targets or explicit:
            raise SelectionError(
                "revise_only cannot be given with issues or repositories"
            )
        if args.mode != "pull-requests":
            raise SelectionError("revise_only needs pull-requests mode")
        if not args.revise:
            raise SelectionError("revise_only needs revise")
    return targets, explicit, pulls


def load_policy(args: argparse.Namespace) -> tuple[frozenset[str], dict[str, Any]]:
    """Read the enabled categories and the fork organisations."""
    try:
        enabled = categories.parse_categories(args.categories)
    except categories.CategoryError as exc:
        raise SelectionError(str(exc)) from exc
    try:
        fork_orgs = forks.load(args.fork_orgs)
    except ForkOrgError as exc:
        raise SelectionError(str(exc)) from exc
    return enabled, fork_orgs


def stamp(chosen: list[dict[str, Any]], session: str, collected_at: str) -> None:
    """Mark every chosen entry with the read time and the session."""
    # The session names this selection: the run and its attempt, so
    # a full rerun that selects anew takes a new one and a publish-only
    # retry, reading this selection, keeps it. A scratch branch or a
    # reply marked with it is this selection's alone where two runs
    # may share a read time.
    name = re.sub(r"[^0-9A-Za-z-]", "", session) or uuid.uuid4().hex
    for entry in chosen:
        entry["collected_at"] = collected_at
        entry["session"] = name


def build_selection(args: argparse.Namespace) -> tuple[dict[str, Any], bytes]:
    """Run the whole selection and return it with the guidance bytes."""
    # Before any read: a comment posted while the run reads is new
    # work for the next round, so every entry carries this time. It
    # sits one second early: the time has second precision and later
    # reads want a comment strictly after it, so a comment posted in
    # this same second would otherwise equal it and never count. The
    # overlap may reread a boundary comment and loses none.
    collected_at = (datetime.now(UTC) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    targets, explicit, pulls = parse_targets(args)
    exclusions = set(load_exclusions(args.exclude_file, args.exclude_repos))
    reviewers = load_reviewers(args.antagonistic_reviewers)
    max_pull_requests = parse_max_pull_requests(args.max_pull_requests)
    max_rounds = parse_max_rounds(args.max_rounds)
    stale_rule = stale.load_rule(args.stale_statuses, args.stale_status_minutes)
    enabled, fork_orgs = load_policy(args)
    skipped: dict[str, int] = dict.fromkeys(SKIP_COUNTERS, 0)
    skipped_issues: list[dict[str, str]] = []
    holds = run_overlap.Holds(
        run_overlap.load_held(args.held_file), skipped, skipped_issues
    )
    bot = reads.bot_identity(args.bot_slug)
    explicit_run = bool(targets or pulls)
    lowered = {name.lower() for name in exclusions}

    def allowed(name: str) -> bool:
        """The run's repository policy for a scan, as cheap_filter applies it."""
        if explicit:
            return name in explicit
        return name not in lowered and (name != ".github" or args.include_dotgithub)

    rounds = revision_rounds.Rounds(
        args.org,
        bot=bot,
        fork_orgs=fork_orgs,
        # A round moves a live pull request's head; branches mode
        # publishes none, so it selects none.
        enabled=bool(args.revise) and args.mode == "pull-requests",
        explicit=explicit_run,
        skips=(skipped, skipped_issues),
        allowed=allowed,
        reviewers=reviewers,
        max_rounds=max_rounds,
        outstanding_only=args.outstanding_only,
        continues=args.continues_run,
        stale=stale_rule,
    )
    if explicit_run:
        # Named issues: no listing, no scan, no ranking, the given
        # order kept.
        seen = len(targets)
        ranked = explicit_targets.candidates(
            args.org, targets, skipped=skipped, skipped_issues=skipped_issues
        )
    elif args.revise_only:
        seen, ranked = 0, []
    else:
        seen, ranked = scan_candidates(
            args,
            repositories.list_repositories(args.org),
            explicit=explicit,
            exclusions=exclusions,
            enabled=enabled,
            skipped=skipped,
        )
    chosen = choose(
        holds.issues(ranked),
        fork_orgs=fork_orgs,
        max_pull_requests=max_pull_requests,
        skipped=skipped,
        one_per_repository=not explicit_run,
        named=explicit_run,
        skipped_issues=skipped_issues if explicit_run else None,
        revisions=holds.rounds(
            rounds.named(holds.pulls(rounds.org, pulls))
            + (rounds.scan(holds.claims) if not explicit_run else [])
        ),
        reviser=rounds.for_issue,
    )
    if args.results_repository:
        previous_rejection.attach(
            chosen,
            results_repository=args.results_repository,
            bot_login=str(bot["login"]),
        )
    stamp(chosen, args.session, collected_at)
    guidance, commit = reads.fetch_guidance(
        args.guidance_repository, args.guidance_ref, args.guidance_path
    )
    selection = selection_document(
        args,
        Settings(bot, enabled, max_pull_requests, guidance, commit),
        lists=(explicit, targets, pulls, exclusions),
        outcome=(seen, skipped, skipped_issues, chosen),
    )
    selection["overlap_waits"] = overlap_wait.load_waits(args.held_file, holds.held)
    return selection, guidance


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line, with this module's own description."""
    return selection_parser(__doc__)


def main(argv: list[str] | None = None) -> None:
    """Run the selection and write its outputs."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        selection, guidance = build_selection(args)
        outputs.write_outputs(args.output_dir, selection, guidance)
    except (OSError, ValueError, SelectionError, github.GitHubError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"select issues: {message}\n")
    print(
        f"Selected {len(selection['issues'])} issue(s) from "
        f"{selection['candidates_seen']} open; skipped {selection['skipped']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
