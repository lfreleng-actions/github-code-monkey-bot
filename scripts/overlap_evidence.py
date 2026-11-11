# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read another run's selection evidence, trusting what its select job vouches for.

Each evidence artifact is found by its producer run, through every
page of the run's own artifact listing and the run id its name
carries. A run id and an upload digest prove which run uploaded an
artifact, not which job: the untrusted author job holds the run's
Actions runtime token and can upload a look-alike. So an artifact
counts only when a successful select job of that run vouches for it:
the job's own check run, whose annotations only that job's workflow
commands write since no job holds ``checks: write``, carries exactly
one ``monkey-evidence-vouch`` notice naming the artifact, its id and
its digest. The annotations come from the API host itself; a job log
would redirect to a rotating storage host that block-mode egress
refuses. Evidence no select job vouches for is disregarded with a
warning and holds nothing, so a forged artifact can never cause a
skip. A vouched zip is accepted only when its bytes match that
digest. A run that has not published and vouched for evidence yet
holds nothing; the publish job's refusal covers that window.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, cast

import artifact_fetch as fetch
import bot_github as github
from bot_evidence import MAX_EVIDENCE_BYTES

EVIDENCE_RE = re.compile(r"monkey-evidence-([0-9]+)-([0-9]+)-[0-9a-f]{32}")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
# The select job's display name; a caller's job prefixes it with its own.
SELECT_JOB = "Select"
# The notice the select job prints once it has uploaded its evidence.
VOUCH_TITLE = "monkey-evidence-vouch"
VOUCH_RE = re.compile(
    r"(monkey-evidence-([0-9]+)-([0-9]+)-[0-9a-f]{32}) ([0-9]+) sha256:([0-9a-f]{64})"
)
CHECK_RUN_RE = re.compile(r"https://[^/]+/repos/[^/]+/[^/]+/check-runs/([0-9]+)")
SELECTION: fetch.Profile = (("selection.json", MAX_EVIDENCE_BYTES, True),)


def warn(message: str) -> None:
    """Print a workflow warning; the check carries on."""
    data = ascii(message)[1:-1].replace("::", ": :").replace("##[", "# #[")
    print(f"::warning title=Overlapping runs::{data}")


def pages_of(endpoint: str, key: str) -> list[Any]:
    """Every page of a listing that wraps its entries under ``key``, joined."""
    pages = github.decode_response(
        github.run_gh(["api", endpoint, "--paginate", "--slurp"])
    )
    if not isinstance(pages, list):
        raise github.GitHubError(f"{key} listing carried no pages")
    entries: list[Any] = []
    for page in cast("list[Any]", pages):
        found = (
            cast("dict[str, Any]", page).get(key) if isinstance(page, dict) else None
        )
        if not isinstance(found, list):
            raise github.GitHubError(f"{key} listing carried no {key}")
        entries.extend(cast("list[Any]", found))
    return entries


def artifacts_of(repository: str, run_id: int) -> list[Any]:
    """Every page of the run's artifact listing, joined.

    Proposal, session and result uploads share the listing with the
    evidence, so a busy run's evidence can sit past the first page.
    """
    return pages_of(
        f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
        "artifacts",
    )


def is_select_name(job: dict[str, Any]) -> bool:
    """Whether the job is a select job by name, whatever its state.

    Job names come from the trusted workflows, never from the author
    job, and an author job's name always starts ``Author (``.
    """
    name = job.get("name")
    return isinstance(name, str) and (
        name == SELECT_JOB or name.endswith(f" / {SELECT_JOB}")
    )


def is_select_job(job: dict[str, Any]) -> bool:
    """Whether the job is a select job that finished cleanly."""
    return (
        is_select_name(job)
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
    )


def select_jobs(repository: str, run_id: int) -> list[dict[str, Any]]:
    """Every select job of the run, in every attempt and any state."""
    jobs = pages_of(
        f"repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
        "jobs",
    )
    return [
        cast("dict[str, Any]", raw)
        for raw in jobs
        if isinstance(raw, dict) and is_select_name(cast("dict[str, Any]", raw))
    ]


def annotations_of(repository: str, job: dict[str, Any]) -> list[dict[str, Any]]:
    """Every annotation on the job's check run, from every page."""
    match = CHECK_RUN_RE.fullmatch(str(job.get("check_run_url") or ""))
    if match is None:
        raise github.GitHubError(f"select job {job.get('id')} names no check run")
    endpoint = f"repos/{repository}/check-runs/{match.group(1)}/annotations"
    try:
        reply = github.run_gh(
            ["api", f"{endpoint}?per_page=100", "--paginate", "--slurp"]
        )
    except github.GitHubError as exc:
        status = "with no HTTP status" if exc.status is None else f"(HTTP {exc.status})"
        raise github.GitHubError(
            f"could not read the annotations of select job {job.get('id')} {status}",
            exc.status,
        ) from None
    pages = github.decode_response(reply)
    if not isinstance(pages, list):
        raise github.GitHubError("annotation listing carried no pages")
    found: list[dict[str, Any]] = []
    for page in cast("list[Any]", pages):
        if not isinstance(page, list):
            raise github.GitHubError("annotation listing page is not a list")
        found.extend(
            cast("dict[str, Any]", entry)
            for entry in cast("list[Any]", page)
            if isinstance(entry, dict)
        )
    return found


def vouched(repository: str, run_id: int) -> dict[int, tuple[str, str]]:
    """The evidence the run's select jobs vouch for: artifact id to name and digest.

    Every attempt's select jobs count. A job whose check run carries
    no vouch, or more than one, vouches for nothing, and a vouch naming
    another run or attempt than the job's own is disregarded.
    """
    found: dict[int, tuple[str, str]] = {}
    for job in select_jobs(repository, run_id):
        if not is_select_job(job):
            continue
        job_id = github.require_int(job, "id", "select job")
        vouches = [
            VOUCH_RE.fullmatch(str(note.get("message", "")))
            for note in annotations_of(repository, job)
            if note.get("title") == VOUCH_TITLE
            and note.get("annotation_level") == "notice"
        ]
        if len(vouches) != 1:
            warn(
                f"select job {job_id} of run {run_id} vouches for {len(vouches)}"
                " evidence artifacts rather than one; none of it counts"
            )
            continue
        if vouches[0] is None:
            warn(f"select job {job_id} of run {run_id} vouches in a malformed notice")
            continue
        name, run, attempt, artifact, digest = vouches[0].groups()
        if int(run) != run_id or int(attempt) != job.get("run_attempt"):
            warn(
                f"select job {job_id} of run {run_id} vouches for {name},"
                " which names another run or attempt; it does not count"
            )
            continue
        found[int(artifact)] = (name, digest)
    return found


def evidence_of(repository: str, run_id: int) -> list[dict[str, Any]]:
    """The run's evidence artifacts that a select job of the run vouches for.

    Each leg of a caller run selects under its own random namespace,
    and a rerun of failed jobs reruns one leg's select job while
    another leg's author jobs retry on its first selection. No name
    ties an attempt's evidence to its leg, so every attempt's vouched
    evidence holds while the run is active: a full rerun's superseded
    selection holds its targets a little longer, and no leg's work
    goes unheld. Evidence no select job vouches for, such as an
    artifact the author job uploaded, is disregarded with a warning.
    """
    named: list[dict[str, Any]] = []
    for raw in artifacts_of(repository, run_id):
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
        named.append(entry)
    if not named:
        return []
    vouches = vouched(repository, run_id)
    found: list[dict[str, Any]] = []
    for entry in named:
        artifact_id = github.require_int(entry, "id", "artifact")
        digest = DIGEST_RE.fullmatch(str(entry.get("digest") or ""))
        claim = vouches.pop(artifact_id, None)
        if claim is None or digest is None or claim != (entry["name"], digest.group(1)):
            warn(
                f"disregarded {entry['name']} (artifact {artifact_id}) of run"
                f" {run_id}: no select job of the run vouches for it"
            )
            continue
        found.append(entry)
    if vouches:
        missing = ", ".join(str(artifact) for artifact in sorted(vouches))
        raise github.GitHubError(f"vouched evidence {missing} is not in the listing")
    return found


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
