# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A bot pull request's failing checks on its head, as the packet carries them.

The select job records which check runs failed on the head and where
their annotations point; it never reads a job log, which needs
Actions: read and a storage host the select job does not reach. The
session recovers the cause by running the failing job's commands in
its checkout (DESIGN.md section 20). Names, titles, summaries and
annotation messages come from code under review: data, bounded here
as the threads are.

Services that report through the Commit Status API, pre-commit.ci
among them, post no check run. The combined status of the head is
read beside the runs, and each status is carried as a run with no
annotations: its ``context`` as the name, its ``state`` as the
conclusion, its ``description`` as the title and its ``target_url``
as the details link, bounded as a run's are. ``target_url`` is an
arbitrary link from the repository under review: data, never fetched.
A pending status the run's ``stale_statuses.Rule`` covers, left too
long, is stale: set apart, neither unsettled nor failing.

A failing run whose failure-level annotation shows harden-runner's
egress agent never came up failed before the job ran its own steps:
it is ``infrastructure``, which needs a re-run, not a defect of the
change, and it brings no round. A repository that fakes the wording
can at most make its own failure count as one, which spends nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, cast

import bot_github as github
import issue_reads as reads
import stale_statuses as stale

# Conclusions that mean the run failed; success, neutral, skipped,
# cancelled, action_required, stale and runs still going do not. A
# commit status fails with ``failure`` or ``error``.
FAILING = frozenset({"failure", "timed_out", "startup_failure", "error"})
# Annotation levels carried; a notice is not a fault.
LEVELS = frozenset({"failure", "warning"})
# Check names whose failure the head's own round cannot influence, so
# they bring no round on their own. None at present.
EXCLUDED: frozenset[str] = frozenset()
# What harden-runner-block-action writes on a failure-level annotation
# when the egress agent did not start: a stable title, and the three
# wordings its message opens with. Matched exactly, case and all.
AGENT_TITLE = "harden-runner agent did not start"
AGENT_MESSAGES = (
    "harden-runner's agent reverted its changes",
    "harden-runner's agent did not report ready",
    "harden-runner's agent reported an error",
)
DEFECT = "defect"
INFRASTRUCTURE = "infrastructure"
MAX_CHECKS = 30
MAX_ANNOTATIONS = 10
MAX_TEXT = 2 * 1024
MAX_CHECK_BYTES = 32 * 1024
RUNS_PER_PAGE = 100
# A head with more runs than this fails closed rather than guess.
MAX_RUN_PAGES = 10
ANNOTATIONS_PER_PAGE = 100
MAX_ANNOTATION_PAGES = 3
# GitHub returns the latest status per context; one page holds them.
STATUSES_PER_PAGE = 100


class OutOfTime(Exception):
    """The next read would not fit the caller's wait."""


@dataclass(frozen=True)
class CheckPacket:
    """The failing runs the packet carries, and what the whole set said."""

    checks: list[dict[str, Any]]
    # Failing runs, and annotations of kept runs, the bounds left out.
    dropped: int
    # Failing defect runs, kept or dropped, not on the exclusion list.
    outstanding: int
    # Runs not yet completed: queued, in progress or waiting. Whole,
    # whatever the bounds keep; their absence from ``checks`` proves
    # nothing about whether they will pass.
    unsettled: int = 0
    # Failing runs the egress agent failed, kept or dropped.
    infrastructure: int = 0
    # Pending statuses the stale rule set apart (``stale_statuses``):
    # not counted as unsettled, their result unknown.
    stale: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())


def failing_checks(
    repository: str, head_sha: str, rule: stale.Rule = stale.OFF
) -> CheckPacket:
    """The head's failing check runs and statuses, with annotations, bounded.

    Every run and status is read before the bounds apply, so the
    counts of failing checks that bring a round, of infrastructure
    failures and of checks yet to finish are whole whatever the packet
    keeps. Statuses ``rule`` finds stale are set apart first: they
    count as neither unsettled nor failing.
    """
    live, late = rule.split(head_runs(repository, head_sha))
    return replace(failing_of(repository, live), stale=rule.entries(late))


def head_runs(
    repository: str,
    head_sha: str,
    *,
    fits: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """The head's latest check runs, and its commit statuses shaped as runs."""
    runs = check_runs(repository, head_sha, fits=fits)
    return runs + commit_statuses(repository, head_sha, fits=fits)


def failing_of(
    repository: str,
    runs: list[dict[str, Any]],
    *,
    fits: Callable[[], bool] | None = None,
) -> CheckPacket:
    """The failing runs among ``runs`` with their annotations, bounded.

    With ``fits``, an annotation page that would not fit the caller's
    wait is not read: its annotations count as dropped. Every failing
    run is classified on every annotation read, before the bounds cut
    runs or annotations, so a run past the run bound counts by its
    kind too; one whose annotations went unread counts as a defect.
    Defect runs take the bounds first, by name, and infrastructure
    runs fill what is left: the run that brings a round is the one
    the session has to see.
    """
    failing = sorted(
        (run for run in runs if is_failing(run)),
        key=lambda run: (str(run.get("name") or ""), run_id(run) or 0),
    )
    unsettled = sum(1 for run in runs if is_unsettled(run))
    classified: list[tuple[dict[str, Any], list[dict[str, Any]], int, str]] = []
    outstanding = 0
    infrastructure = 0
    for run in failing:
        annotations, unread = run_annotations(repository, run, fits=fits)
        kind = kind_of(annotations)
        if kind == INFRASTRUCTURE:
            infrastructure += 1
        elif run.get("name") not in EXCLUDED:
            outstanding += 1
        classified.append((run, annotations, unread, kind))
    classified.sort(key=lambda item: item[3] == INFRASTRUCTURE)
    kept: list[dict[str, Any]] = []
    dropped = max(0, len(failing) - MAX_CHECKS)
    total = 0
    for run, annotations, unread, kind in classified[:MAX_CHECKS]:
        entry = check_entry(run, annotations[:MAX_ANNOTATIONS], kind)
        while total + entry_size(entry) > MAX_CHECK_BYTES and entry["annotations"]:
            entry["annotations"].pop()
        size = entry_size(entry)
        if total + size > MAX_CHECK_BYTES:
            # A run left out counts once, as a run past the count bound does.
            dropped += 1
            continue
        dropped += unread + len(annotations) - len(entry["annotations"])
        total += size
        kept.append(entry)
    return CheckPacket(kept, dropped, outstanding, unsettled, infrastructure)


def check_runs(
    repository: str,
    head_sha: str,
    *,
    fits: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Every latest check run on the commit, page by page.

    With ``fits``, a page that would not fit the caller's wait raises
    ``OutOfTime`` rather than return a partial list.
    """
    runs: list[dict[str, Any]] = []
    for page in range(1, MAX_RUN_PAGES + 1):
        if fits is not None and not fits():
            raise OutOfTime("could not read every check run within the wait")
        data = github.api_object(
            f"repos/{repository}/commits/{head_sha}/check-runs"
            f"?filter=latest&per_page={RUNS_PER_PAGE}&page={page}"
        )
        reply = data.get("check_runs")
        if not isinstance(reply, list):
            raise github.GitHubError("check run reply carries no check_runs array")
        batch = cast("list[Any]", reply)
        for run in batch:
            # A malformed entry fails closed rather than read as no failure.
            if not isinstance(run, dict):
                raise github.GitHubError("check run reply carries a non-object entry")
            runs.append(cast("dict[str, Any]", run))
        count = data.get("total_count")
        if len(batch) < RUNS_PER_PAGE or (type(count) is int and len(runs) >= count):
            return runs
    raise reads.SelectionError(
        f"{repository}@{head_sha[:12]} carries more check runs than the packet reads"
    )


def commit_statuses(
    repository: str,
    head_sha: str,
    *,
    fits: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """The commit's latest status per context, each shaped as a check run.

    One page; a commit with more contexts than it holds fails closed,
    as a head with too many runs does. With ``fits``, a read that
    would not fit the caller's wait raises ``OutOfTime``.
    """
    if fits is not None and not fits():
        raise OutOfTime("could not read the commit statuses within the wait")
    data = github.api_object(
        f"repos/{repository}/commits/{head_sha}/status?per_page={STATUSES_PER_PAGE}"
    )
    reply = data.get("statuses")
    if not isinstance(reply, list):
        raise github.GitHubError("commit status reply carries no statuses array")
    batch = cast("list[Any]", reply)
    count = data.get("total_count")
    whole = type(count) is int and count <= len(batch)
    if len(batch) >= STATUSES_PER_PAGE and not whole:
        raise reads.SelectionError(
            f"{repository}@{head_sha[:12]} carries more statuses than one page"
        )
    sha = data.get("sha")
    runs: list[dict[str, Any]] = []
    for status in batch:
        if not isinstance(status, dict):
            raise github.GitHubError("commit status reply carries a non-object entry")
        runs.append(status_run(cast("dict[str, Any]", status), sha))
    return runs


def status_run(status: dict[str, Any], sha: Any) -> dict[str, Any]:
    """A commit status in check run form: no id, no app, no annotations.

    ``pending`` has yet to finish; every other state has. ``check_entry``
    bounds the context and description as it bounds a run's name and
    title. ``commit_status`` and ``updated_at`` let the stale rule tell
    a status from a check run of the same name, and its age.
    """
    state = status.get("state")
    return {
        "name": status.get("context"),
        "head_sha": sha,
        "status": "pending" if state == "pending" else "completed",
        "conclusion": state,
        "details_url": status.get("target_url"),
        "output": {"title": status.get("description")},
        "commit_status": True,
        "updated_at": status.get("updated_at"),
    }


def is_failing(run: dict[str, Any]) -> bool:
    """Whether a run finished with a failing conclusion."""
    return run.get("status") == "completed" and run.get("conclusion") in FAILING


def is_unsettled(run: dict[str, Any]) -> bool:
    """Whether a run has yet to finish: queued, in progress or waiting."""
    return run.get("status") != "completed"


def run_id(run: dict[str, Any]) -> int | None:
    """The run's numeric id, or None when the reply lacks one."""
    value = run.get("id")
    return value if type(value) is int else None


def output_of(run: dict[str, Any]) -> dict[str, Any]:
    """The run's output object, empty when absent."""
    output = run.get("output")
    return cast("dict[str, Any]", output) if isinstance(output, dict) else {}


def run_annotations(
    repository: str,
    run: dict[str, Any],
    *,
    fits: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The run's failure and warning annotations, and how many went unread.

    A run whose output counts no annotation costs no read. Past the
    page bound, or once ``fits`` says a page would not fit the
    caller's wait, the rest are unread and counted, whatever their
    level.
    """
    identity = run_id(run)
    count = output_of(run).get("annotations_count")
    if identity is None or type(count) is not int or count <= 0:
        return [], 0
    found: list[dict[str, Any]] = []
    read = 0
    for page in range(1, MAX_ANNOTATION_PAGES + 1):
        if fits is not None and not fits():
            break
        batch = reads.api_page(
            f"repos/{repository}/check-runs/{identity}/annotations"
            f"?per_page={ANNOTATIONS_PER_PAGE}&page={page}"
        )
        read += len(batch)
        found.extend(a for item in batch if (a := annotation(item)) is not None)
        if len(batch) < ANNOTATIONS_PER_PAGE or read >= count:
            return found, 0
    return found, max(0, count - read)


def kind_of(annotations: list[dict[str, Any]]) -> str:
    """``infrastructure`` when a failure-level annotation shows the agent failed.

    A warning carrying the wording is the block action's warning
    path, and the wording part-way through a message is not its own:
    neither classifies.
    """
    for item in annotations:
        if item["level"] != "failure":
            continue
        if item.get("title", "").strip() == AGENT_TITLE:
            return INFRASTRUCTURE
        if item["message"].strip().startswith(AGENT_MESSAGES):
            return INFRASTRUCTURE
    return DEFECT


def annotation(item: dict[str, Any]) -> dict[str, Any] | None:
    """One annotation in packet form, or None for a notice or a malformed one."""
    level = item.get("annotation_level")
    if level not in LEVELS:
        return None
    line = item.get("start_line")
    return {
        "level": level,
        "path": text(item.get("path")),
        "line": line if type(line) is int else None,
        "title": text(item.get("title")),
        "message": text(item.get("message")),
    }


def text(value: Any) -> str:
    """A string field as bounded packet text."""
    return reads.truncate_utf8(value if isinstance(value, str) else "", MAX_TEXT)


def check_entry(
    run: dict[str, Any], annotations: list[dict[str, Any]], kind: str = DEFECT
) -> dict[str, Any]:
    """One failing run in packet form, with its ``kind``."""
    output = output_of(run)
    app = run.get("app")
    slug = cast("dict[str, Any]", app).get("slug") if isinstance(app, dict) else None
    return {
        "name": text(run.get("name")),
        "conclusion": str(run.get("conclusion")),
        "app": slug if isinstance(slug, str) else None,
        "details_url": text(run.get("details_url")),
        "title": text(output.get("title")),
        "summary": text(output.get("summary")),
        "annotations": annotations,
        "kind": kind,
    }


def entry_size(entry: dict[str, Any]) -> int:
    """The bytes a failing run costs the packet: its text and its annotations."""
    fields = [entry["name"], entry["details_url"], entry["title"], entry["summary"]]
    for item in cast("list[dict[str, Any]]", entry["annotations"]):
        fields += [item["path"], item["title"], item["message"]]
    return sum(len(str(field).encode("utf-8")) for field in fields)
