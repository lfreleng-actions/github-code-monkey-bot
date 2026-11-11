# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Find the targets other in-progress runs of this workflow already selected.

A scheduled scan and a named dispatch hold different concurrency
groups, so both can select one issue or pull request and author it,
and one session is wasted (DESIGN.md sections 6 and 20.1). Before the
selector runs, this lists the caller workflow's other runs that are
queued or in progress, reads the selection evidence each one's select
job published, and writes every issue and pull request those
selections hold, with the run that holds it, for the selector to skip.

Each artifact counts only when a select job of its run vouches for
it in a notice on its own check run, and its bytes match the vouched
digest
(``overlap_evidence``): a run id and an upload digest alone prove the
run, not the job, and the untrusted author job can upload a
look-alike. Evidence no select job vouches for holds nothing. A run
that has not published and vouched for evidence yet holds nothing;
the publish job's re-read covers that window. So does a tie-break on
the run id: a run first waits, under a bound, for each older
in-progress run still selecting (``overlap_wait``).

The check only saves wasted work; the publisher's compare-and-swap is
the safety net. So it fails open: a listing or a read that fails
prints a ``::warning::`` and holds nothing for that run, or for every
run when the listing itself fails, and the output is written all the
same. A run holds its targets only once all of its evidence has been
read. The whole check runs under one deadline well inside the select
job's timeout; past it, the runs read in full still hold and the rest
hold nothing. Exit status is 0 unless the output cannot be written.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import bot_github as github
from overlap_evidence import evidence_of, read_selection, warn
from overlap_wait import FAILURES, MAX_RUNS, Wait, await_older, bounded, listed_runs

# The whole check's deadline, well inside the select job's 20 minutes;
# one stalled download alone may otherwise take 300 seconds.
BUDGET_SECONDS = 300
Held = dict[tuple[str, int], int]


def other_runs(repository: str, run_id: int) -> list[int]:
    """The ids of this workflow's other queued or in-progress runs."""
    return list(listed_runs(repository, run_id))


def targets_of(selection: dict[str, Any]) -> list[tuple[str, int]]:
    """Every issue and pull request a selection will work.

    A dry run or a select-only run publishes nothing, so it holds
    nothing either.
    """
    if selection.get("dry_run") is not False or selection.get("mode") == "select":
        return []
    entries = selection.get("issues")
    if not isinstance(entries, list):
        raise ValueError("selection.json lists no issues")
    found: list[tuple[str, int]] = []
    for raw in cast("list[Any]", entries):
        if not isinstance(raw, dict):
            raise ValueError("selection entry is not an object")
        entry = cast("dict[str, Any]", raw)
        repository = github.require_str(entry, "repository", "selection entry")
        found.append((repository.lower(), github.require_int(entry, "number", "entry")))
        pull = entry.get("pull_request")
        if isinstance(pull, dict):
            number = github.require_int(
                cast("dict[str, Any]", pull), "number", "entry pull request"
            )
            found.append((repository.lower(), number))
    return found


def run_targets(repository: str, run_id: int) -> list[tuple[str, int]]:
    """Every target the run's evidence holds; raises if any of it is unreadable."""
    found: list[tuple[str, int]] = []
    for entry in evidence_of(repository, run_id):
        found.extend(targets_of(read_selection(repository, entry)))
    return found


def collect(
    repository: str, run_id: int, complete: list[tuple[int, list[tuple[str, int]]]]
) -> None:
    """Append each other run's targets once all its evidence has been read."""
    try:
        runs = other_runs(repository, run_id)
    except FAILURES as exc:
        warn(f"could not list this workflow's runs: {exc}")
        return
    if len(runs) > MAX_RUNS:
        warn(f"{len(runs)} other runs are active; reading the first {MAX_RUNS}")
    for other in runs[:MAX_RUNS]:
        try:
            complete.append((other, run_targets(repository, other)))
        except FAILURES as exc:
            warn(f"could not read the selection of run {other}: {exc}")


def held_targets(repository: str, run_id: int, budget: float = BUDGET_SECONDS) -> Held:
    """Map each target another run selected to that run; fail open throughout.

    An alarm ends the check at ``budget`` seconds wherever it waits.
    The runs read in full by then still hold their targets.
    """
    complete: list[tuple[int, list[tuple[str, int]]]] = []
    if not bounded(budget, lambda: collect(repository, run_id, complete)):
        warn(
            f"the check ran past {budget:g} seconds; only the {len(complete)}"
            " run(s) read in full hold targets"
        )
    held: Held = {}
    for other, targets in complete:
        for target in targets:
            held.setdefault(target, other)
    return held


def write_held(path: Path, held: Held, waits: Iterable[Wait] = ()) -> None:
    """Write the held targets, and the waits for older runs, for the selector."""
    document = {
        "held": [
            {"repository": repository, "number": number, "run_id": run}
            for (repository, number), run in sorted(held.items())
        ],
        "waits": [wait._asdict() for wait in waits],
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def load_held(path: Path | None) -> Held:
    """Read what ``write_held`` wrote; no path means no other run holds anything."""
    if path is None:
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    entries = (
        cast("dict[str, Any]", parsed).get("held") if isinstance(parsed, dict) else None
    )
    if not isinstance(entries, list):
        raise ValueError(f"{path} lists no held targets")
    held: Held = {}
    for raw in cast("list[Any]", entries):
        if not isinstance(raw, dict):
            raise ValueError(f"{path} holds an entry that is not an object")
        entry = cast("dict[str, Any]", raw)
        repository = github.require_str(entry, "repository", "held target")
        number = github.require_int(entry, "number", "held target")
        held[(repository.lower(), number)] = github.require_int(
            entry, "run_id", "held target"
        )
    return held


Builder = Callable[[], dict[str, Any] | None]


class Holds:
    """Skip, before the selector chooses, what another run already selected.

    Each skip counts under ``held`` and is recorded in
    ``skipped_issues`` with the holding run, for named and scanned
    targets alike, naming the issue, or the pull request a round
    revises.
    """

    def __init__(
        self, held: Held, skipped: dict[str, int], skipped_issues: list[dict[str, str]]
    ) -> None:
        """Remember the held targets and where skips are counted and recorded."""
        self.held = held
        self.skipped = skipped
        self.skipped_issues = skipped_issues

    def claims(self, repository: str, numbers: tuple[int, ...], reference: str) -> bool:
        """Whether another run holds any of the numbers; record the skip as ``reference``."""
        for number in numbers:
            run = self.held.get((repository.lower(), number))
            if run is not None:
                self.skipped["held"] += 1
                self.skipped_issues.append(
                    {"issue": reference, "reason": f"being worked by run {run}"}
                )
                return True
        return False

    def holds(self, entry: dict[str, Any]) -> bool:
        """Whether another run holds the entry's issue or pull request; record it.

        A round is recorded under its pull request, the target an
        operator names, whichever of its numbers is held.
        """
        repository = str(entry["repository"])
        numbers: tuple[int, ...] = (int(entry["number"]),)
        pull = entry.get("pull_request")
        if isinstance(pull, dict):
            numbers = (int(cast("dict[str, Any]", pull)["number"]), *numbers)
        return self.claims(repository, numbers, f"{repository}#{numbers[0]}")

    def pulls(self, org: str, pulls: list[tuple[str, int]]) -> list[tuple[str, int]]:
        """The named pull requests no other run holds, passed over before any read."""
        return [
            (name, number)
            for name, number in pulls
            if not self.claims(f"{org}/{name}", (number,), f"{org}/{name}#{number}")
        ]

    def issues(self, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The issue candidates no other run holds, in their order."""
        return [candidate for candidate in ranked if not self.holds(candidate)]

    def rounds(self, revisions: list[tuple[str, Builder]]) -> list[tuple[str, Builder]]:
        """The revision builders, each yielding nothing for a held round.

        ``pulls`` and the scan's ``claims`` have already passed over a
        round whose pull request, or a scanned round's issue, is held,
        before any read; this
        catches a named round whose issue alone is held, which is known
        only once it is built.
        """
        return [(repository, self.guard(build)) for repository, build in revisions]

    def guard(self, build: Builder) -> Builder:
        """Wrap one builder so a held entry is skipped rather than chosen."""

        def guarded() -> dict[str, Any] | None:
            entry = build()
            return None if entry is None or self.holds(entry) else entry

        return guarded


def main(argv: list[str] | None = None) -> None:
    """Write the targets other runs hold; never fail on a read."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    waits = await_older(args.repository, args.run_id)
    held = held_targets(args.repository, args.run_id)
    write_held(args.output, held, waits)
    for wait in waits:
        print(f"Waited {wait.seconds}s for run {wait.run_id} to publish its selection")
    print(f"Other runs hold {len(held)} target(s)")


if __name__ == "__main__":
    main()
