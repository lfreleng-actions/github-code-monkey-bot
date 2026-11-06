# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Carry the publisher's last rejection of an issue to the next attempt.

When the bot's latest outcome marker on an issue says ``rejected``,
the selection reads that run's publish result and adds its reasons,
bounded, to the entry as ``previous_rejection`` (DESIGN.md section 6),
so the agent learns why the last attempt failed. Every read here
fails soft: a reason that cannot be read becomes a fixed note, and
the selection goes on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import artifact_fetch
import bot_github as github
import issue_reads as reads
from round_reads import FOOTER, MAX_HISTORY_PAGES
from selection_choose import MAX_SELECTION_BYTES

HEADING = "Your previous attempt was rejected by the publisher for:"
MAX_REASONS = 10
# Both bounds count bytes as selection.json serialises a reason: JSON
# escaping turns one control byte into six, so a bound on the UTF-8
# text alone would not bound the file.
MAX_REASON_BYTES = 2000
MAX_TOTAL_BYTES = 8 * 1024
# Pages of a run's artifact listing read before the search gives up.
MAX_ARTIFACT_PAGES = 10
RESULT_PROFILE: artifact_fetch.Profile = (("result.json", 1024 * 1024, True),)
RUN_URL = re.compile(
    r"https://github\.com/(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/actions/runs/(?P<run>[1-9]\d*)"
)
NOTE_FOREIGN = "the rejecting run belongs to another repository; reasons not read"
NOTE_MISSING = "the rejecting run's publish result was not found"
NOTE_AMBIGUOUS = "the rejecting run holds more than one publish result for this issue"
NOTE_EXPIRED = "the rejecting run's publish result has expired"
NOTE_UNREADABLE = "the rejecting run's publish result could not be read"
NOTE_MISMATCH = "the rejecting run's publish result records no rejection"
NOTE_BUDGET = "the reasons were left out to keep the selection within its size budget"


class Unavailable(Exception):
    """The reasons cannot be read; the message is a fixed note."""


@contextmanager
def results_token() -> Generator[None]:
    """Read the run's artifacts with ``RESULTS_TOKEN`` when it is set.

    The selection's own token reads the target organisation; the
    artifacts live in the repository that runs the workflow.
    """
    token = os.environ.get("RESULTS_TOKEN")
    saved = os.environ.get("GH_TOKEN")
    if token:
        os.environ["GH_TOKEN"] = token
    try:
        yield
    finally:
        if token:
            if saved is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = saved


def latest_outcome(
    repository: str, issue: int, bot_login: str
) -> tuple[str, str] | None:
    """The verdict and run URL of the bot's latest footer, or None.

    A history longer than the round reads span returns None: nothing
    is added rather than a stale outcome taken for the latest.
    """
    latest: tuple[tuple[str, int], str, str] | None = None
    for page in range(1, MAX_HISTORY_PAGES + 1):
        entries = reads.api_page(
            f"repos/{repository}/issues/{issue}/comments"
            f"?per_page={reads.COMMENTS_PER_PAGE}&page={page}"
        )
        for entry in entries:
            user = entry.get("user")
            login = (
                cast("dict[str, Any]", user).get("login")
                if isinstance(user, dict)
                else None
            )
            body, created = entry.get("body"), entry.get("created_at")
            if (
                login != bot_login
                or not isinstance(body, str)
                or not isinstance(created, str)
            ):
                continue
            found = FOOTER.search(body)
            if found is None:
                continue
            ident = entry.get("id")
            order = (created, ident if type(ident) is int else 0)
            if latest is None or order > latest[0]:
                latest = (order, found.group("verdict"), found.group("run"))
        if len(entries) < reads.COMMENTS_PER_PAGE:
            return None if latest is None else (latest[1], latest[2])
    return None


def find_result(repository: str, run_id: str, key: str) -> tuple[int, int]:
    """The id and size of the run's latest publish result for ``key``."""
    pattern = re.compile(
        rf"monkey-result-{run_id}-[1-9]\d*-[0-9a-f]{{32}}-{re.escape(key)}-(?P<attempt>[1-9]\d*)",
        re.IGNORECASE,
    )
    found: list[tuple[int, dict[str, Any]]] = []
    for page in range(1, MAX_ARTIFACT_PAGES + 1):
        listing = github.api_object(
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"
        )
        listed = listing.get("artifacts")
        if not isinstance(listed, list):
            raise Unavailable(NOTE_UNREADABLE)
        raw = cast("list[Any]", listed)
        entries = [cast("dict[str, Any]", e) for e in raw if isinstance(e, dict)]
        for entry in entries:
            name = entry.get("name")
            matched = pattern.fullmatch(name) if isinstance(name, str) else None
            if matched is not None:
                found.append((int(matched.group("attempt")), entry))
        if len(raw) < 100:
            break
    if not found:
        raise Unavailable(NOTE_MISSING)
    top = max(attempt for attempt, _ in found)
    latest = [entry for attempt, entry in found if attempt == top]
    if len(latest) > 1:
        raise Unavailable(NOTE_AMBIGUOUS)
    if latest[0].get("expired"):
        raise Unavailable(NOTE_EXPIRED)
    try:
        return github.require_int(
            latest[0], "id", "artifact"
        ), artifact_fetch.artifact_size(latest[0])
    except github.GitHubError as exc:
        raise Unavailable(NOTE_UNREADABLE) from exc


def read_reasons(repository: str, run_id: str, key: str) -> list[Any]:
    """The reasons the run's publish result records for ``key``."""
    try:
        with results_token():
            artifact_id, size = find_result(repository, run_id, key)
            with tempfile.TemporaryDirectory() as holder:
                artifact_fetch.fetch_artifact(
                    repository, artifact_id, size, Path(holder), RESULT_PROFILE
                )
                result = json.loads((Path(holder) / "result.json").read_bytes())
    except (
        artifact_fetch.Refused,
        artifact_fetch.Missing,
        github.GitHubError,
        OSError,
        RecursionError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        raise Unavailable(NOTE_UNREADABLE) from exc
    if (
        not isinstance(result, dict)
        or cast("dict[str, Any]", result).get("verdict") != "rejected"
    ):
        raise Unavailable(NOTE_MISMATCH)
    reasons = cast("dict[str, Any]", result).get("reasons")
    return cast("list[Any]", reasons) if isinstance(reasons, list) else []


def serialised(value: Any) -> int:
    """The bytes ``value`` takes in selection.json, which escapes to ASCII."""
    return len(json.dumps(value, indent=2))


def cut(reason: str) -> str:
    """The longest prefix of ``reason`` whose serialised form fits a reason's bound."""
    # A lone surrogate, which JSON escapes allow, cannot encode as
    # UTF-8; replace it rather than let one reason stop the selection.
    text = reason.encode("utf-8", "replace").decode("utf-8")
    text = reads.truncate_utf8(text, MAX_REASON_BYTES)
    if serialised(text) <= MAX_REASON_BYTES:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if serialised(text[:middle]) <= MAX_REASON_BYTES:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def bounded(reasons: list[Any]) -> tuple[list[str], int]:
    """At most ``MAX_REASONS`` strings, each and all bounded; the count dropped."""
    kept: list[str] = []
    used = 0
    for reason in reasons:
        if not isinstance(reason, str) or len(kept) == MAX_REASONS:
            continue
        text = cut(reason)
        size = serialised(text)
        if used + size > MAX_TOTAL_BYTES:
            continue
        kept.append(text)
        used += size
    return kept, len(reasons) - len(kept)


def lookup(
    entry: dict[str, Any], results_repository: str, bot_login: str
) -> dict[str, Any] | None:
    """The entry's ``previous_rejection``, or None when the last outcome was not one."""
    try:
        outcome = latest_outcome(
            str(entry["repository"]), int(entry["number"]), bot_login
        )
    except github.GitHubError:
        return None
    if outcome is None or outcome[0] != "rejected":
        return None
    run_url = outcome[1]
    record: dict[str, Any] = {
        "heading": HEADING,
        "run_url": run_url,
        "reasons": [],
        "reasons_dropped": 0,
        "note": None,
    }
    parsed = RUN_URL.fullmatch(run_url)
    try:
        if (
            parsed is None
            or parsed.group("repository").lower() != results_repository.lower()
        ):
            raise Unavailable(NOTE_FOREIGN)
        raw = read_reasons(results_repository, parsed.group("run"), str(entry["key"]))
    except Unavailable as exc:
        record["note"] = str(exc)
        return record
    record["reasons"], record["reasons_dropped"] = bounded(raw)
    return record


def attach(
    chosen: list[dict[str, Any]], *, results_repository: str, bot_login: str
) -> None:
    """Add ``previous_rejection`` to each entry whose last outcome was a rejection.

    The records count against the selection's size budget, as the
    entries did when chosen: a record that would overrun it keeps its
    note and drops its reasons.
    """
    used = sum(serialised(entry) for entry in chosen)
    for entry in chosen:
        record = lookup(entry, results_repository, bot_login)
        if record is None:
            continue
        before = serialised(entry)
        entry["previous_rejection"] = record
        if used + serialised(entry) - before > MAX_SELECTION_BYTES:
            # The note-only record is a few hundred bytes, which the
            # headroom below the verifier's limit absorbs.
            record["reasons_dropped"] += len(record["reasons"])
            record["reasons"] = []
            record["note"] = NOTE_BUDGET
        used += serialised(entry) - before
