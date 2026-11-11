# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Wait for older runs of this workflow to finish selecting (DESIGN.md section 6).

Two runs that select within the same few minutes both pass the
overlap check of ``run_overlap``, since neither has vouched for its
evidence yet. The run id breaks the tie: before the check applies
holds, a run waits, under a bound of its own, for each older
in-progress run still selecting (``await_older``), so that run's
selection is read and held. A newer run waits for this one instead,
and a queued run holds nothing until it starts.

The wait fails open like the check: a listing or a jobs read that
fails prints a ``::warning::`` and stops waiting. This module also
holds the run listing and the alarm the wait and the check share,
which ``run_overlap`` imports, and reads the waits back as lines for
the selection summary (``load_waits``).
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple, cast

import artifact_fetch as fetch
import bot_github as github
from overlap_evidence import select_jobs, warn

STATUSES = ("in_progress", "queued")
# Runs read at most; a busier workflow has a problem no skip can fix.
MAX_RUNS = 20
# The tie-break's own bound beside the check's: how long, in all, a
# run waits for older runs to finish selecting. Both together sit well
# inside the select job's 20 minutes.
WAIT_SECONDS = 300
POLL_SECONDS = 10

FAILURES = (
    OSError,
    ValueError,
    subprocess.SubprocessError,
    github.GitHubError,
    fetch.Refused,
)


class Deadline(Exception):
    """The check ran past its deadline."""


class Wait(NamedTuple):
    """A wait for an older run's selection: how long, and whether it ended in one."""

    run_id: int
    seconds: int
    vouched: bool


def listed_runs(repository: str, run_id: int) -> dict[int, str]:
    """This workflow's other queued or in-progress runs, each with its status."""
    run = github.api_object(f"repos/{repository}/actions/runs/{run_id}")
    workflow = github.require_int(run, "workflow_id", "this run")
    found: dict[int, str] = {}
    for status in STATUSES:
        listing = github.api_object(
            f"repos/{repository}/actions/workflows/{workflow}/runs"
            f"?status={status}&per_page=100&exclude_pull_requests=true"
        )
        runs = listing.get("workflow_runs")
        if not isinstance(runs, list):
            raise github.GitHubError(f"{status} run listing carried no runs")
        for raw in cast("list[Any]", runs):
            if not isinstance(raw, dict):
                raise github.GitHubError(f"{status} run listing entry is not an object")
            entry = cast("dict[str, Any]", raw)
            other = github.require_int(entry, "id", f"{status} run")
            # The filter is the API's; a run that finished since holds nothing.
            if entry.get("status") not in STATUSES:
                continue
            if other != run_id and other not in found:
                found[other] = status
    return found


def bounded(budget: float, action: Callable[[], None]) -> bool:
    """Run ``action`` under an alarm at ``budget`` seconds; False when it fired.

    The alarm ends the action wherever it waits: gh and a download
    are killed on the way out.
    """

    def expire(_signum: int, _frame: object) -> None:
        raise Deadline

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, budget)
            action()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except Deadline:
        return False
    finally:
        signal.signal(signal.SIGALRM, previous)
    return True


def selecting(repository: str, run_id: int) -> bool:
    """Whether more of the run's selection may still come: a select job is unfinished.

    A select job vouches in its last step. One that succeeded, in an
    earlier attempt or another caller leg, says nothing of a select
    job still running, so the run is selecting while any is
    unfinished. A run with no select job listed yet is still
    selecting; one whose select jobs all finished is not.
    """
    jobs = select_jobs(repository, run_id)
    return not jobs or any(job.get("status") != "completed" for job in jobs)


def older_runs(repository: str, run_id: int) -> list[int]:
    """This workflow's in-progress runs with a lower id, oldest first."""
    try:
        listed = listed_runs(repository, run_id)
    except FAILURES as exc:
        warn(f"could not list this workflow's runs: {exc}")
        return []
    older = sorted(
        other
        for other, status in listed.items()
        if status == "in_progress" and other < run_id
    )
    return older[:MAX_RUNS]


def await_older(
    repository: str, run_id: int, bound: float = WAIT_SECONDS
) -> list[Wait]:
    """Wait for each older in-progress run still selecting; return each wait.

    Two runs selecting at once both pass the check, since neither has
    vouched yet; the lower run id wins. Each older run is polled every
    ``POLL_SECONDS`` until its select job ends, under one ``bound`` for
    every wait; past it, or on a failed read, the check warns and stops
    waiting for that run.
    """
    waits: list[Wait] = []
    current: list[tuple[int, float]] = []

    def wait_all() -> None:
        for other in older_runs(repository, run_id):
            started = time.monotonic()
            current[:] = [(other, started)]
            polled, ended = False, True
            try:
                while selecting(repository, other):
                    polled = True
                    time.sleep(POLL_SECONDS)
            except FAILURES as exc:
                warn(f"could not tell whether run {other} has selected: {exc}")
                ended = False
            current.clear()
            if polled:
                waits.append(Wait(other, round(time.monotonic() - started), ended))

    if not bounded(bound, wait_all):
        for other, started in current:
            waits.append(Wait(other, round(time.monotonic() - started), False))
            warn(
                f"run {other} was still selecting after {bound:g} seconds;"
                " it holds only what it has vouched for when the check reads it"
            )
    return waits


def load_waits(path: Path | None, held: Mapping[tuple[str, int], int]) -> list[str]:
    """The waits ``write_held`` wrote, as lines for the selection summary.

    ``held`` is what ``load_held`` read from the same file: the line
    for a wait cut short says what that run holds.
    """
    if path is None:
        return []
    parsed = json.loads(path.read_text(encoding="utf-8"))
    entries = (
        cast("dict[str, Any]", parsed).get("waits", [])
        if isinstance(parsed, dict)
        else None
    )
    if not isinstance(entries, list):
        raise ValueError(f"{path} lists no waits")
    counts = Counter(held.values())
    lines: list[str] = []
    for raw in cast("list[Any]", entries):
        if not isinstance(raw, dict):
            raise ValueError(f"{path} holds a wait that is not an object")
        entry = cast("dict[str, Any]", raw)
        run = github.require_int(entry, "run_id", "wait")
        seconds = entry.get("seconds")
        # A wait cut short by the shared bound may round to 0 seconds.
        if type(seconds) is not int or seconds < 0:
            raise ValueError(f"{path} holds a wait with no valid 'seconds'")
        line = f"waited {seconds}s for run {run} to publish its selection"
        if entry.get("vouched") is not True:
            # The check reads the evidence after the wait, so a run cut
            # short may still hold what one of its select jobs vouched for.
            count = counts[run]
            holding = f"{count} target(s)" if count else "nothing"
            line += f"; the wait ended before it finished, and it holds {holding}"
        lines.append(line)
    return lines
