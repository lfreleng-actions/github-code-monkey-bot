# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The published head's checks, read inside the review wait (DESIGN.md section 8).

The publish job gates on Copilot's review and reports CI: once the
review's verdict is known, it polls the head's check runs and commit
statuses until none is queued, in progress or pending, within the
deadline and reserve the review wait already had, never past it. A
red check fails nothing here; it is recorded as ``checks`` in
``result.json`` for the comment, the report and the next revision
scan (section 20.1) to read. A head whose every failure is one the
egress agent caused (``check_reads``) is ``infrastructure``: it needs
a re-run, not a fix.

The check-runs listing and the combined status answer the job's own
token on a public repository, so this needs no mint. Names, titles,
summaries and status descriptions come from the code under review:
``check_reads`` bounds them, and every reader escapes them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import bot_github as github
import check_reads as checks
import issue_reads as reads
import proposal_policy as policy

STATES = ("passing", "infrastructure", "failing", "running", "none", "unknown")
FIELDS = frozenset(
    {
        "state",
        "commit_id",
        "failing",
        "dropped",
        "unsettled",
        "infrastructure",
        "defects",
        "read_at",
        "reason",
    }
)
POLL_SECONDS = 30
# Failing check names the annotation lists; the record keeps the rest.
MAX_ANNOTATED_CHECKS = 10


@dataclass(frozen=True)
class Window:
    """What is left of the review wait: its deadline and its clock.

    ``read_budget`` is one read's worst case; a read starts only while
    it and the closing read of the head both fit.
    """

    deadline: float
    read_budget: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]

    @classmethod
    def of(
        cls,
        minutes: int,
        read_budget: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> Window:
        """A window of ``minutes`` from now on ``clock``."""
        return cls(clock() + minutes * 60, read_budget, clock, sleep)

    def fits(self) -> bool:
        """Whether a read and the head's confirmation fit before the deadline."""
        return self.spare() >= 0

    def spare(self) -> float:
        """Seconds to spare beyond those two reads."""
        return self.deadline - self.clock() - 2 * self.read_budget


@dataclass(frozen=True)
class HeadChecks:
    """What the head's check runs said when the wait ended."""

    state: str
    failing: list[dict[str, Any]] = field(
        default_factory=lambda: list[dict[str, Any]]()
    )
    # Failing runs, and annotations of kept runs, the bounds left out.
    dropped: int = 0
    # Runs still queued or in progress when the wait ended.
    unsettled: int = 0
    reason: str | None = None
    # Failing runs the egress agent failed, kept or dropped: whole,
    # so a count past the bounds is not taken from the names kept.
    infrastructure: int = 0
    # Failing runs that are no infrastructure, kept or dropped: whole,
    # so a defect the bounds left out still marks the head failed.
    defects: int = 0

    def to_json(
        self, commit_id: str | None, read_at: str | None = None
    ) -> dict[str, Any]:
        """The ``checks`` record, bound to the head it describes."""
        return {
            "state": self.state,
            "commit_id": commit_id,
            "failing": self.failing,
            "dropped": self.dropped,
            "unsettled": self.unsettled,
            "infrastructure": self.infrastructure,
            "defects": self.defects,
            "read_at": read_at or now(),
            "reason": self.reason,
        }


def now() -> str:
    """The current time as GitHub writes it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def unknown(reason: str) -> HeadChecks:
    """A read that proves nothing about CI."""
    return HeadChecks("unknown", reason=reason)


def await_checks(
    repository: str, head_sha: str, confirm_head: Callable[[], str], window: Window
) -> HeadChecks:
    """Poll the head's check runs and statuses until they settle or time nears.

    Every read starts only while it, and the closing read of the pull
    request's head (``confirm_head``), fit in ``window`` at their
    worst case, so the wait never overruns. A commit status counts as
    a run (``check_reads.commit_statuses``): pending is unsettled,
    failure and error fail. Only runs whose ``head_sha`` is
    ``head_sha`` count. A read that fails, or a head
    that moved, is ``unknown``; runs still going when time runs out
    are ``running``. Settled failures that the egress agent alone
    caused are ``infrastructure``; any other failure makes the head
    ``failing``.
    """
    fits = window.fits
    runs: list[dict[str, Any]] | None = None
    unsettled = 0
    while fits():
        try:
            listed = checks.head_runs(repository, head_sha, fits=fits)
        except checks.OutOfTime:
            break
        except (github.GitHubError, reads.SelectionError) as exc:
            return unknown(
                "could not read the head's check runs and statuses: "
                f"{github.safe_message(exc)}"
            )
        runs = [run for run in listed if run.get("head_sha") == head_sha]
        unsettled = sum(1 for run in runs if checks.is_unsettled(run))
        spare = window.spare()
        if not unsettled or spare <= 0:
            break
        window.sleep(min(POLL_SECONDS, spare))
    if runs is None:
        return unknown("no time left in the wait to read the head's check runs")
    try:
        packet = checks.failing_of(repository, runs, fits=fits)
    except (github.GitHubError, reads.SelectionError) as exc:
        return unknown(
            f"could not read the failing checks' annotations: "
            f"{github.safe_message(exc)}"
        )
    if window.deadline - window.clock() < window.read_budget:
        return unknown("could not confirm the head within the wait")
    try:
        current = confirm_head()
    except github.GitHubError as exc:
        return unknown(f"could not confirm the head: {github.safe_message(exc)}")
    if current != head_sha:
        return unknown(
            f"head moved from {head_sha[:7]} to {current[:7]} during the wait"
        )
    failed = sum(1 for run in runs if checks.is_failing(run))
    if unsettled:
        state = "running"
    elif not runs:
        state = "none"
    elif not failed:
        state = "passing"
    else:
        state = "infrastructure" if failed == packet.infrastructure else "failing"
    return HeadChecks(
        state,
        packet.checks,
        packet.dropped,
        unsettled,
        infrastructure=packet.infrastructure,
        defects=failed - packet.infrastructure,
    )


def is_infrastructure(entry: dict[str, Any]) -> bool:
    """Whether a recorded failing run is one the egress agent caused."""
    return entry.get("kind") == checks.INFRASTRUCTURE


def named_checks(entries: list[dict[str, Any]], total: int = 0) -> str:
    """Check names for the log: flattened, defused, bounded, counted past the cap.

    Check names come from the code under review. ``total`` counts runs
    the record's bounds left out as well, when it exceeds the entries.
    """
    if not entries and total > 0:
        return f"{total} left out of the record by its bounds"
    names = [
        policy.log_safe(str(entry.get("name") or ""))[: policy.MAX_CHECK_NAME]
        for entry in entries[:MAX_ANNOTATED_CHECKS]
    ]
    named = ", ".join(name for name in names if name) or "unnamed"
    more = max(total, len(entries)) - len(names)
    return f"{named} and {more} more" if more > 0 else named


def annotation(found: HeadChecks, head: str) -> str | None:
    """The ``Checks`` warning for a head with failing runs, or None.

    Failures the egress agent caused are named apart, as needing a
    re-run. The escapes GitHub's workflow command syntax defines keep
    the line whole.
    """
    failed = found.failing or found.defects or found.infrastructure
    if found.state not in ("failing", "infrastructure") and not failed:
        return None
    agent = [entry for entry in found.failing if is_infrastructure(entry)]
    defects = [entry for entry in found.failing if not is_infrastructure(entry)]
    count = max(found.infrastructure, len(agent))
    still = "; checks still running at the end of the wait" if found.unsettled else ""
    if found.state == "infrastructure" or (
        found.state == "running" and not defects and not found.defects
    ):
        named = named_checks(agent, found.infrastructure)
        text = (
            f"checks on {head[:12]} failed at the egress agent and need a "
            f"re-run: {named}{still}. This is infrastructure, not the change; "
            "the pull request is published regardless."
        )
    else:
        also = (
            f"; {count} more failed at the egress agent and need a re-run"
            if count
            else ""
        )
        text = (
            f"failing checks on {head[:12]}: "
            f"{named_checks(defects, found.defects)}{also}"
            f"{still}. The pull request is published regardless; a revision "
            "round reads them."
        )
    data = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title=Checks::{data}"


def record(
    found: HeadChecks | None, head: str | None, reasons: list[str]
) -> dict[str, Any]:
    """Log the checks, and warn when one failed; return the ``checks`` record.

    ``found`` is None when the wait never started; ``reasons`` are then
    the review verdict's reasons why.
    """
    if found is None:
        found = unknown("the wait never started: " + "; ".join(reasons))
    for line in log_lines(found, head):
        print(line)
    return found.to_json(head)


def log_lines(found: HeadChecks, head: str | None) -> list[str]:
    """The step log's account of the checks, and the warning when one applies."""
    line = (
        f"checks: {found.state}; failing: {len(found.failing)}; "
        f"unsettled: {found.unsettled}; dropped: {found.dropped}"
    )
    if found.reason:
        line += f"; {found.reason}"
    warning = None if head is None else annotation(found, head)
    return [line] if warning is None else [line, warning]
