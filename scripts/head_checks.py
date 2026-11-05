# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The published head's check runs, read inside the review wait (DESIGN.md section 8).

The publish job gates on Copilot's review and reports CI: once the
review's verdict is known, it polls the head's check runs until none
is queued or in progress, within the deadline and reserve the review
wait already had, never past it. A red check fails nothing here; it
is recorded as ``checks`` in ``result.json`` for the comment, the
report and the next revision scan (section 20.1) to read.

The check-runs listing answers the job's own token on a public
repository, so this needs no mint. Names, titles and summaries come
from the workflows of the code under review: ``check_reads`` bounds
them, and every reader escapes them.
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

STATES = ("passing", "failing", "running", "none", "unknown")
FIELDS = frozenset(
    {"state", "commit_id", "failing", "dropped", "unsettled", "read_at", "reason"}
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
    """Poll the head's check runs until they settle or the deadline nears.

    Every read starts only while it, and the closing read of the pull
    request's head (``confirm_head``), fit in ``window`` at their
    worst case, so the wait never overruns. Only runs whose
    ``head_sha`` is ``head_sha`` count. A read that fails, or a head
    that moved, is ``unknown``; runs still going when time runs out
    are ``running``.
    """
    fits = window.fits
    runs: list[dict[str, Any]] | None = None
    unsettled = 0
    while fits():
        try:
            listed = checks.check_runs(repository, head_sha, fits=fits)
        except checks.OutOfTime:
            break
        except (github.GitHubError, reads.SelectionError) as exc:
            return unknown(
                f"could not read the head's check runs: {github.safe_message(exc)}"
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
    else:
        state = "failing" if failed else "passing"
    return HeadChecks(state, packet.checks, packet.dropped, unsettled)


def annotation(found: HeadChecks, head: str) -> str | None:
    """The ``Checks`` warning for a head with failing runs, or None.

    Check names come from the code under review: each is flattened
    and defused for the log, and bounded, before it is named; the
    escapes GitHub's workflow command syntax defines keep the line
    whole.
    """
    if found.state != "failing" and not found.failing:
        return None
    names = [
        policy.log_safe(str(entry.get("name") or ""))[: policy.MAX_CHECK_NAME]
        for entry in found.failing[:MAX_ANNOTATED_CHECKS]
    ]
    named = ", ".join(name for name in names if name) or "unnamed"
    more = len(found.failing) - MAX_ANNOTATED_CHECKS
    if more > 0:
        named += f" and {more} more"
    still = "; checks still running at the end of the wait" if found.unsettled else ""
    text = (
        f"failing checks on {head[:12]}: {named}{still}. The pull request is "
        "published regardless; a revision round reads them."
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
