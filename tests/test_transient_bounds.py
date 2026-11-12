# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Retried reads stay inside the wait and never report a partial state (#186)."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
github = import_module("bot_github")
checks = import_module("check_reads")
head_checks = import_module("head_checks")
reads = import_module("issue_reads")
transient = import_module("transient_reads")
fixtures = import_module("test_copilot_review")

HEAD = fixtures.HEAD
REPO = fixtures.REPO
Clock = fixtures.Clock
check_run = fixtures.check_run
UNAVAILABLE = github.GitHubError("gh: HTTP 503")


class LateClock:
    """A fake clock whose sleep overshoots, as a real one may."""

    def __init__(self, overshoot: float = 1.0) -> None:
        """Start at zero; every sleep runs ``overshoot`` seconds long."""
        self.now = 0.0
        self.overshoot = overshoot

    def __call__(self) -> float:
        """The current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance past the time asked for."""
        self.now += seconds + self.overshoot


def failed(identity: int, name: str) -> dict[str, Any]:
    """A failed run with one annotation to read."""
    run = check_run(identity, name, conclusion="failure")
    run["output"]["annotations_count"] = 1
    return run


class HeadBeforeWaitTest(unittest.TestCase):
    """The head read before the wait starts only while time is spare."""

    def test_no_read_starts_without_time_spare(self) -> None:
        """A window with nothing spare reads nothing and says so."""
        reads_made: list[int] = []

        def read() -> str:
            reads_made.append(1)
            return HEAD

        reason = transient.head_before_wait(
            read,
            HEAD,
            transient.Retries("w"),
            spare=lambda: 0.0,
            sleep=Clock().sleep,
        )
        self.assertEqual(reads_made, [])
        self.assertEqual(
            reason, "no time left in the wait to read the pull request head"
        )

    def test_a_retry_that_spends_the_spare_time_reads_no_more(self) -> None:
        """After the sleep takes what was spare, the last error is the reason."""
        clock = Clock()
        reads_made: list[int] = []

        def read() -> str:
            reads_made.append(1)
            raise UNAVAILABLE

        reason = transient.head_before_wait(
            read,
            HEAD,
            transient.Retries("w"),
            spare=lambda: 10.0 - clock(),
            sleep=clock.sleep,
        )
        self.assertEqual(reads_made, [1])
        self.assertEqual(clock.sleeps, [10.0])
        self.assertIn("could not read the pull request head", str(reason))
        self.assertIn("HTTP 503", str(reason))


class ChecksRetryBoundsTest(unittest.TestCase):
    """A retried checks read the window cuts short is unknown, not partial."""

    def await_checks(
        self, clock: Any, deadline: float, confirm: Any = None
    ) -> tuple[Any, Any]:
        """Run the checks wait on ``clock`` with a ten-second read budget."""
        window = head_checks.Window(deadline, 10.0, clock, clock.sleep)
        retries = transient.Retries("the checks wait")
        found = head_checks.await_checks(
            REPO, HEAD, confirm or (lambda: HEAD), window, retries=retries
        )
        return found, retries

    def test_a_failed_listing_drops_the_earlier_snapshot(self) -> None:
        """A window spent retrying the listing is unknown, never running."""
        clock = LateClock()
        listings: list[Any] = [[check_run(1, "ci", status="in_progress")]]

        def head_runs(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            if listings:
                return listings.pop(0)
            raise UNAVAILABLE

        with patch.object(checks, "head_runs", side_effect=head_runs):
            found, retries = self.await_checks(clock, 200.0)
        self.assertEqual(found.state, "unknown")
        self.assertIn("could not read the head's check runs", found.reason)
        self.assertIn("HTTP 503", found.reason)
        self.assertGreater(retries.count, 0)

    def annotations(self, clock: Any, fail_first: bool) -> Any:
        """Two failed runs; each annotation read costs the read budget."""
        pages: list[str] = []

        def api_page(endpoint: str) -> list[dict[str, Any]]:
            clock.now += 10
            pages.append(endpoint)
            if fail_first and len(pages) == 1:
                raise UNAVAILABLE
            return []

        runs = [failed(1, "a"), failed(2, "b")]
        with (
            patch.object(checks, "head_runs", return_value=runs),
            patch.object(reads, "api_page", side_effect=api_page),
        ):
            found, _ = self.await_checks(clock, 50.0)
        return found, pages

    def test_a_retried_annotation_read_cut_short_is_unknown(self) -> None:
        """The retry reaches one run of two; the saved error is the reason."""
        found, pages = self.annotations(Clock(), fail_first=True)
        self.assertEqual(len(pages), 2)
        self.assertEqual(found.state, "unknown")
        self.assertIn("could not read the failing checks' annotations", found.reason)
        self.assertIn("HTTP 503", found.reason)

    def test_a_first_read_cut_short_keeps_its_bounded_record(self) -> None:
        """With no failure to retry, a cut-short read counts as before."""
        clock = Clock()
        clock.now = 25.0
        found, pages = self.annotations(clock, fail_first=False)
        self.assertEqual(len(pages), 1)
        self.assertEqual(found.state, "failing")
        self.assertEqual(found.dropped, 1)
        self.assertIsNone(found.reason)

    def test_a_failed_head_confirmation_retries_alone(self) -> None:
        """The annotations are read once; only the head read repeats."""
        clock = Clock()
        heads: list[Any] = [UNAVAILABLE, HEAD]
        pages: list[str] = []

        def confirm() -> str:
            reply = heads.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return str(reply)

        def api_page(endpoint: str) -> list[dict[str, Any]]:
            pages.append(endpoint)
            return []

        with (
            patch.object(checks, "head_runs", return_value=[failed(1, "a")]),
            patch.object(reads, "api_page", side_effect=api_page),
        ):
            found, retries = self.await_checks(clock, 600.0, confirm=confirm)
        self.assertEqual(found.state, "failing")
        self.assertEqual(len(pages), 1)
        self.assertEqual(heads, [])
        self.assertEqual(retries.count, 1)

    def test_a_confirmation_out_of_time_names_the_last_error(self) -> None:
        """A head read the window cannot repeat ends on the saved failure."""
        # The sleep overshoots past the read budget, so the retry finds
        # no time for the head read rather than none spare to sleep.
        clock = LateClock(overshoot=15.0)
        calls: list[int] = []

        def confirm() -> str:
            calls.append(1)
            raise UNAVAILABLE

        with patch.object(checks, "head_runs", return_value=[]):
            found, _ = self.await_checks(clock, 40.0, confirm=confirm)
        self.assertEqual(calls, [1])
        self.assertEqual(found.state, "unknown")
        self.assertIn("could not confirm the head", found.reason)
        self.assertIn("HTTP 503", found.reason)


if __name__ == "__main__":
    unittest.main()
