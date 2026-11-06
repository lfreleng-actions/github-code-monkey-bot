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

Each evidence artifact is found by its producer run, through the
run's own artifact listing and the run id and attempt its name
carries, and its zip is accepted only when the bytes match the digest
GitHub recorded at upload: never by name alone. A run that has not
published evidence yet holds nothing; the publish job's refusal
covers that window.

The check only saves wasted work; the publisher's compare-and-swap is
the safety net. So it fails open: a listing or a read that fails
prints a ``::warning::`` and holds nothing for that run, or for every
run when the listing itself fails, and the output is written all the
same. Exit status is 0 unless the output cannot be written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import artifact_fetch as fetch
import bot_github as github
from bot_evidence import MAX_EVIDENCE_BYTES

STATUSES = ("in_progress", "queued")
# Runs read at most; a busier workflow has a problem no skip can fix.
MAX_RUNS = 20
EVIDENCE_RE = re.compile(r"monkey-evidence-([0-9]+)-([0-9]+)-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
SELECTION: fetch.Profile = (("selection.json", MAX_EVIDENCE_BYTES, True),)
Held = dict[tuple[str, int], int]


def warn(message: str) -> None:
    """Print a workflow warning; the check carries on."""
    data = ascii(message)[1:-1].replace("::", ": :").replace("##[", "# #[")
    print(f"::warning title=Overlapping runs::{data}")


def other_runs(repository: str, run_id: int) -> list[int]:
    """The ids of this workflow's other queued or in-progress runs."""
    run = github.api_object(f"repos/{repository}/actions/runs/{run_id}")
    workflow = github.require_int(run, "workflow_id", "this run")
    found: list[int] = []
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
                found.append(other)
    return found


def evidence_of(repository: str, run_id: int) -> list[dict[str, Any]]:
    """The run's evidence artifacts from its latest attempt that selected.

    A full rerun selects anew and publishes new evidence under its own
    attempt; a retry of later jobs alone keeps the earlier selection.
    The highest attempt that published evidence is so the one the run
    works, one artifact per leg of the caller run.
    """
    listing = github.api_object(
        f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
    )
    entries = listing.get("artifacts")
    if not isinstance(entries, list):
        raise github.GitHubError("artifact listing carried no artifacts")
    by_attempt: dict[int, list[dict[str, Any]]] = {}
    for raw in cast("list[Any]", entries):
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, Any]", raw)
        match = EVIDENCE_RE.fullmatch(str(entry.get("name", "")))
        if match is None or int(match.group(1)) != run_id or entry.get("expired"):
            continue
        producer = entry.get("workflow_run")
        if (
            not isinstance(producer, dict)
            or cast("dict[str, Any]", producer).get("id") != run_id
        ):
            raise github.GitHubError(f"{entry['name']} names no producer run {run_id}")
        by_attempt.setdefault(int(match.group(2)), []).append(entry)
    return by_attempt[max(by_attempt)] if by_attempt else []


def read_selection(repository: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Download one evidence artifact, check its digest, and parse its selection."""
    artifact_id = github.require_int(entry, "id", "artifact")
    digest = DIGEST_RE.fullmatch(str(entry.get("digest") or ""))
    if digest is None:
        raise fetch.Refused(f"artifact {artifact_id} carries no sha256 digest")
    limit = fetch.zip_limit(SELECTION)
    if fetch.artifact_size(entry) > limit:
        raise fetch.Refused(f"artifact {artifact_id} is over {limit} bytes")
    with tempfile.TemporaryDirectory() as holder:
        archive = Path(holder) / "evidence.zip"
        fetch.download(repository, artifact_id, archive, limit)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest.group(1):
            raise fetch.Refused(f"artifact {artifact_id} does not match its digest")
        fetch.extract(archive, Path(holder) / "out", SELECTION)
        text = (Path(holder) / "out" / "selection.json").read_text(encoding="utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("selection.json is not an object")
    return cast("dict[str, Any]", parsed)


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


def held_targets(repository: str, run_id: int) -> Held:
    """Map each target another run selected to that run; fail open throughout."""
    failures = (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        github.GitHubError,
        fetch.Refused,
    )
    try:
        runs = other_runs(repository, run_id)
    except failures as exc:
        warn(f"could not list this workflow's runs: {exc}")
        return {}
    if len(runs) > MAX_RUNS:
        warn(f"{len(runs)} other runs are active; reading the first {MAX_RUNS}")
    held: Held = {}
    for other in runs[:MAX_RUNS]:
        try:
            for entry in evidence_of(repository, other):
                for target in targets_of(read_selection(repository, entry)):
                    held.setdefault(target, other)
        except failures as exc:
            warn(f"could not read the selection of run {other}: {exc}")
    return held


def write_held(path: Path, held: Held) -> None:
    """Write the held targets for the selector."""
    document = {
        "held": [
            {"repository": repository, "number": number, "run_id": run}
            for (repository, number), run in sorted(held.items())
        ]
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
    targets alike, naming the issue or pull request that matched.
    """

    def __init__(
        self, held: Held, skipped: dict[str, int], skipped_issues: list[dict[str, str]]
    ) -> None:
        """Remember the held targets and where skips are counted and recorded."""
        self.held = held
        self.skipped = skipped
        self.skipped_issues = skipped_issues

    def holds(self, entry: dict[str, Any]) -> bool:
        """Whether another run holds the entry's issue or pull request; record it."""
        repository = str(entry["repository"])
        numbers = [int(entry["number"])]
        pull = entry.get("pull_request")
        if isinstance(pull, dict):
            numbers.append(int(cast("dict[str, Any]", pull)["number"]))
        for number in numbers:
            run = self.held.get((repository.lower(), number))
            if run is not None:
                self.skipped["held"] += 1
                self.skipped_issues.append(
                    {
                        "issue": f"{repository}#{number}",
                        "reason": f"being worked by run {run}",
                    }
                )
                return True
        return False

    def issues(self, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The issue candidates no other run holds, in their order."""
        return [candidate for candidate in ranked if not self.holds(candidate)]

    def rounds(self, revisions: list[tuple[str, Builder]]) -> list[tuple[str, Builder]]:
        """The revision builders, each yielding nothing for a held pull request.

        A round's pull request is known once it is built, which the
        selector does only when its budget has room.
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
    held = held_targets(args.repository, args.run_id)
    write_held(args.output, held)
    print(f"Other runs hold {len(held)} target(s)")


if __name__ == "__main__":
    main()
