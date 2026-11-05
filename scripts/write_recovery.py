# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Publisher writes recovered from a transient failure, and their record.

``bot_github.write_with_recovery`` settles an ambiguous 5xx by reading
the write back, and notes what it did on stderr alone. A write that
needed a second send, or a read-back, is worth a line in the step's
``result.json`` too (issue #100): a maintainer reading the report
then sees the incident the run rode out. The step opens a
``recording`` scope over its result's warnings, and every ``recover``
inside it adds one line there; outside a scope the stderr note is
all there is.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

import bot_github as github

T = TypeVar("T")

_SINK: ContextVar[list[str] | None] = ContextVar("write_recovery_sink", default=None)


@contextmanager
def recording(warnings: list[str]) -> Generator[list[str]]:
    """Send every recovered write's line to ``warnings`` while open."""
    token = _SINK.set(warnings)
    try:
        yield warnings
    finally:
        _SINK.reset(token)


def record(message: str) -> None:
    """Add ``message`` to the open scope's warnings, if a scope is open."""
    sink = _SINK.get()
    if sink is not None:
        sink.append(message)


def recover(
    write: Callable[[], T],
    landed: Callable[[], T | None],
    *,
    repeat_safe: bool,
    label: str,
) -> T:
    """Run ``write`` through ``write_with_recovery``; record any recovery.

    A write that went through on its first send records nothing. One
    that went through on a later send, or that a read-back found
    applied despite its error, records which, under ``label``.
    """
    sends = 0
    found = False

    def counted() -> T:
        nonlocal sends
        sends += 1
        return write()

    def looked() -> T | None:
        nonlocal found
        outcome = landed()
        found = outcome is not None
        return outcome

    outcome = github.write_with_recovery(
        counted, looked, repeat_safe=repeat_safe, label=label
    )
    if found:
        plural = "s" if sends > 1 else ""
        record(
            f"{label} landed despite a transient error, "
            f"found by reading it back after {sends} attempt{plural}"
        )
    elif sends > 1:
        record(f"{label} went through on attempt {sends} after a transient failure")
    return outcome
