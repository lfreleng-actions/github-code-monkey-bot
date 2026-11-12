# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Commit statuses a slow service left pending too long: stale, not waited for.

pre-commit.ci queues jobs for hours when its infrastructure is slow,
often without ever reporting, so a pending status from it could hold
a head unsettled indefinitely. A pending commit status whose context
``config/stale-statuses.txt`` lists, last updated more than the
run's ``stale_status_minutes`` ago, is **stale**: the publish job's
poll stops waiting for it, the scan does not read the head as still
running, and the record names it so a human or a later round can
judge it (DESIGN.md section 13.1). It is not failing. Check runs are
never stale, whatever their name, and ``0`` minutes turns the rule
off.

Context names come from the repository under review: data, bounded
here and escaped by each reader as check names are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import issue_reads as reads
import proposal_policy as policy

DEFAULT_MINUTES = 8
# A context the config may list, and one the record carries, in bytes.
MAX_CONTEXT = 255
# Stale entries a record keeps; one per listed context at most.
MAX_STALE = 30
MINUTES_RE = re.compile(r"[0-9]+", re.ASCII)
# Record states whose stale entries the comment and the report read;
# an unknown record proves nothing.
READ_STATES = ("passing", "infrastructure", "failing", "running", "none")


@dataclass(frozen=True)
class Rule:
    """The listed contexts and the minutes a pending one may stay so."""

    contexts: frozenset[str] = frozenset()
    minutes: int = 0

    def is_stale(self, run: dict[str, Any], now: datetime) -> bool:
        """Whether a run is a listed, pending commit status older than the bound."""
        if not self.minutes or run.get("commit_status") is not True:
            return False
        if run.get("status") != "pending" or run.get("name") not in self.contexts:
            return False
        updated = parse_time(run.get("updated_at"))
        if updated is None:
            return False
        return (now - updated).total_seconds() > self.minutes * 60

    def split(
        self, runs: list[dict[str, Any]], now: datetime | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The runs to wait for and judge, and the stale statuses set apart."""
        moment = now or datetime.now(UTC)
        live: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        for run in runs:
            (stale if self.is_stale(run, moment) else live).append(run)
        return live, stale

    def entries(self, stale: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The stale statuses in record form, bounded and sorted by context."""
        found = [
            {
                "context": reads.truncate_utf8(str(run.get("name")), MAX_CONTEXT),
                "updated_at": str(run.get("updated_at")),
                "minutes": self.minutes,
            }
            for run in stale
        ]
        return sorted(found, key=lambda entry: str(entry["context"]))[:MAX_STALE]


OFF = Rule()


def parse_time(value: Any) -> datetime | None:
    """A GitHub ``...Z`` timestamp, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def parse_minutes(text: str) -> int:
    """Accept a non-negative whole number of minutes; zero turns the rule off."""
    if not MINUTES_RE.fullmatch(text.strip()):
        raise ValueError(
            f"stale_status_minutes must be a non-negative integer, got {text!r}"
        )
    return int(text)


def load_contexts(file: Path | None) -> frozenset[str]:
    """The status contexts the rule covers: one per line, ``#`` starts a comment.

    A context may hold spaces, so a line is taken whole once trimmed. A
    named file that is missing fails the run rather than wait forever
    without saying why.
    """
    if file is None:
        return frozenset()
    if not file.is_file():
        raise ValueError(f"stale status file {file} is not a regular file")
    contexts: set[str] = set()
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if len(stripped.encode("utf-8")) > MAX_CONTEXT:
            raise ValueError(f"status context too long in {file}")
        contexts.add(stripped)
    return frozenset(contexts)


def load_rule(file: Path | None, minutes: str) -> Rule:
    """The run's rule from its context file and its ``stale_status_minutes``."""
    return Rule(load_contexts(file), parse_minutes(minutes))


def recorded(result: dict[str, Any], state: str | None) -> list[dict[str, Any]]:
    """The ``checks`` record's well-formed stale entries; empty when it is unread.

    ``state`` is the record's state as ``review_outcome.checks_state``
    read it: an unknown or absent record carries nothing.
    """
    if state not in READ_STATES:
        return []
    entries = cast("dict[str, Any]", result["checks"]).get("stale")
    if not isinstance(entries, list):
        return []
    kept: list[dict[str, Any]] = []
    for entry in cast("list[Any]", entries)[:MAX_STALE]:
        if not isinstance(entry, dict):
            continue
        data = cast("dict[str, Any]", entry)
        context, minutes = data.get("context"), data.get("minutes")
        if isinstance(context, str) and context.strip() and type(minutes) is int:
            kept.append({"context": context[:MAX_CONTEXT], "minutes": minutes})
    return kept


def contexts(result: dict[str, Any], state: str | None) -> list[str]:
    """The stale contexts the record names, sorted; for the marker's hash."""
    return sorted(entry["context"] for entry in recorded(result, state))


def sentence(result: dict[str, Any], state: str | None) -> str:
    """The comment's one sentence on stale statuses, or empty."""
    entries = recorded(result, state)
    if not entries:
        return ""
    named = policy.failing_checks_text([entry["context"] for entry in entries])
    minutes = max(entry["minutes"] for entry in entries)
    return (
        f" Still pending after {minutes} minutes and not waited for, so its "
        f"result is unknown: {named}."
    )


def warning(entries: list[dict[str, Any]]) -> str | None:
    """The ``Checks`` warning naming the stale statuses, or None."""
    if not entries:
        return None
    names = ", ".join(
        policy.log_safe(str(entry["context"]))[: policy.MAX_CHECK_NAME]
        for entry in entries
    )
    minutes = entries[0]["minutes"]
    text = f"{names} still pending after {minutes} minutes; not waited for"
    data = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title=Checks::{data}"
