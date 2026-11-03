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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import bot_github as github
import issue_reads as reads

# Conclusions that mean the run failed; success, neutral, skipped,
# cancelled, action_required, stale and runs still going do not.
FAILING = frozenset({"failure", "timed_out", "startup_failure"})
# Annotation levels carried; a notice is not a fault.
LEVELS = frozenset({"failure", "warning"})
# Check names whose failure the head's own round cannot influence, so
# they bring no round on their own. None at present.
EXCLUDED: frozenset[str] = frozenset()
MAX_CHECKS = 30
MAX_ANNOTATIONS = 10
MAX_TEXT = 2 * 1024
MAX_CHECK_BYTES = 32 * 1024
RUNS_PER_PAGE = 100
# A head with more runs than this fails closed rather than guess.
MAX_RUN_PAGES = 10
ANNOTATIONS_PER_PAGE = 100
MAX_ANNOTATION_PAGES = 3


@dataclass(frozen=True)
class CheckPacket:
    """The failing runs the packet carries, and what the whole set said."""

    checks: list[dict[str, Any]]
    # Failing runs, and annotations of kept runs, the bounds left out.
    dropped: int
    # Failing runs, kept or dropped, that are not on the exclusion list.
    outstanding: int


def failing_checks(repository: str, head_sha: str) -> CheckPacket:
    """The head's failing check runs with their annotations, bounded.

    Every run is read before the bounds apply, so the count of
    failing runs that bring a round is whole whatever the packet
    keeps.
    """
    failing = sorted(
        (run for run in check_runs(repository, head_sha) if is_failing(run)),
        key=lambda run: (str(run.get("name") or ""), run_id(run) or 0),
    )
    outstanding = sum(1 for run in failing if run.get("name") not in EXCLUDED)
    kept: list[dict[str, Any]] = []
    dropped = max(0, len(failing) - MAX_CHECKS)
    total = 0
    for run in failing[:MAX_CHECKS]:
        annotations, unread = run_annotations(repository, run)
        entry = check_entry(run, annotations[:MAX_ANNOTATIONS])
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
    return CheckPacket(kept, dropped, outstanding)


def check_runs(repository: str, head_sha: str) -> list[dict[str, Any]]:
    """Every latest check run on the commit, page by page."""
    runs: list[dict[str, Any]] = []
    for page in range(1, MAX_RUN_PAGES + 1):
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


def is_failing(run: dict[str, Any]) -> bool:
    """Whether a run finished with a failing conclusion."""
    return run.get("status") == "completed" and run.get("conclusion") in FAILING


def run_id(run: dict[str, Any]) -> int | None:
    """The run's numeric id, or None when the reply lacks one."""
    value = run.get("id")
    return value if type(value) is int else None


def output_of(run: dict[str, Any]) -> dict[str, Any]:
    """The run's output object, empty when absent."""
    output = run.get("output")
    return cast("dict[str, Any]", output) if isinstance(output, dict) else {}


def run_annotations(
    repository: str, run: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """The run's failure and warning annotations, and how many went unread.

    A run whose output counts no annotation costs no read. Past the
    page bound the rest are unread and counted, whatever their level.
    """
    identity = run_id(run)
    count = output_of(run).get("annotations_count")
    if identity is None or type(count) is not int or count <= 0:
        return [], 0
    found: list[dict[str, Any]] = []
    read = 0
    for page in range(1, MAX_ANNOTATION_PAGES + 1):
        batch = reads.api_page(
            f"repos/{repository}/check-runs/{identity}/annotations"
            f"?per_page={ANNOTATIONS_PER_PAGE}&page={page}"
        )
        read += len(batch)
        found.extend(a for item in batch if (a := annotation(item)) is not None)
        if len(batch) < ANNOTATIONS_PER_PAGE or read >= count:
            return found, 0
    return found, max(0, count - read)


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
        "message": text(item.get("message")),
    }


def text(value: Any) -> str:
    """A string field as bounded packet text."""
    return reads.truncate_utf8(value if isinstance(value, str) else "", MAX_TEXT)


def check_entry(
    run: dict[str, Any], annotations: list[dict[str, Any]]
) -> dict[str, Any]:
    """One failing run in packet form."""
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
    }


def entry_size(entry: dict[str, Any]) -> int:
    """The bytes a failing run costs the packet: its text and its annotations."""
    fields = [entry["name"], entry["details_url"], entry["title"], entry["summary"]]
    for item in cast("list[dict[str, Any]]", entry["annotations"]):
        fields += [item["path"], item["message"]]
    return sum(len(str(field).encode("utf-8")) for field in fields)
