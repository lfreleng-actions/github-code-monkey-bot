# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Transient read failures inside the publish job's bounded waits (issue #186).

The shared ``gh`` wrapper retries a read for a few seconds; a 5xx
that lasts longer surfaces from the read. Inside a wait with a
deadline that should cost one poll, not the round: the review wait
(``copilot_review``) and the checks wait (``head_checks``) ask
``Retries.again`` whether to read once more. A failure
``bot_github.is_transient`` accepts, with time left for another poll,
is counted and slept past; anything else ends the wait as before.
The window is the bound, so no retry adds a timer of its own, and the
count reaches ``result.json`` ``warnings`` so the run report shows the
incident even when the round succeeded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import bot_github as github

POLL_SECONDS = 30
# The last error a warning quotes, cut to this many characters.
MAX_ERROR_TEXT = 300


def error_text(exc: BaseException) -> str:
    """An error for a log or a warning: escaped and bounded."""
    text = github.safe_message(exc)
    return text if len(text) <= MAX_ERROR_TEXT else text[:MAX_ERROR_TEXT] + "..."


@dataclass
class Retries:
    """The transient read failures one wait slept past, and the last of them."""

    wait: str
    count: int = 0
    last: str | None = None

    def again(
        self, exc: BaseException, spare: float, sleep: Callable[[float], None]
    ) -> bool:
        """Whether the wait reads again after ``exc``; sleeps until then when so.

        Only a ``GitHubError`` that ``is_transient`` accepts, met with
        ``spare`` seconds left beyond what the wait keeps in reserve,
        is read again, after ``POLL_SECONDS`` or the spare time, the
        shorter. Anything else is final.
        """
        if not isinstance(exc, github.GitHubError) or not github.is_transient(exc):
            return False
        if spare <= 0:
            return False
        self.count += 1
        self.last = error_text(exc)
        sleep(min(POLL_SECONDS, spare))
        return True

    def warning(self) -> str | None:
        """The ``result.json`` warning for the failures retried, or None."""
        if not self.count:
            return None
        plural = "" if self.count == 1 else "s"
        return (
            f"{self.wait} retried {self.count} transient read failure{plural}; "
            f"last: {self.last}"
        )


@dataclass
class Gate:
    """A wait's ``fits`` that remembers whether it ever cut a read short.

    A read retried after a transient failure that the window then cuts
    short is not a whole read: the wait ends on the saved failure.
    """

    fits: Callable[[], bool]
    refused: bool = False

    def __call__(self) -> bool:
        """Whether the next read fits; a no is remembered."""
        if self.fits():
            return True
        self.refused = True
        return False


def head_before_wait(
    read_head: Callable[[], str],
    head_sha: str,
    retries: Retries,
    *,
    spare: Callable[[], float],
    sleep: Callable[[float], None],
) -> str | None:
    """Why the wait cannot start on ``head_sha``, or None when the head stands.

    ``read_head`` reads the pull request's head; ``spare`` is the time
    left beyond the wait's reserve, and every read, the first one too,
    starts only while some is left, so the reserve covers the read's
    worst case. A transient failure reads again at the next poll while
    time is spare; a head that moved, any other failure, or a window
    spent on transient ones is the reason.
    """
    failure = "no time left in the wait to read the pull request head"
    while True:
        if spare() <= 0:
            return failure
        try:
            current = read_head()
        except github.GitHubError as exc:
            failure = f"could not read the pull request head: {error_text(exc)}"
            if retries.again(exc, spare(), sleep):
                continue
            return failure
        if current != head_sha:
            return f"head moved from {head_sha[:7]} to {current[:7]} before the wait"
        return None
