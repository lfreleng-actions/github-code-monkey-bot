# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Transient read failures inside the review and checks waits (#186)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
github = import_module("bot_github")
review = import_module("copilot_review")
reads = import_module("issue_reads")
transient = import_module("transient_reads")
fixtures = import_module("test_copilot_review")

HEAD = fixtures.HEAD
OTHER = fixtures.OTHER
REPO = fixtures.REPO
CLEAN = fixtures.CLEAN
Clock = fixtures.Clock
check_run = fixtures.check_run
threads_reply = fixtures.threads_reply

UNAVAILABLE = github.GitHubError("gh: HTTP 503")
NOT_FOUND = github.GitHubError("gh: Not Found (HTTP 404)")
INVALID = github.GitHubError("gh: Validation Failed (HTTP 422)")
PULL = f"repos/{REPO}/pulls/41"
WAIT = 20


class Script:
    """A fake GitHub whose every read answers from its own queue, on a clock.

    Each queue answers in turn, its last reply repeating; a reply that
    is an exception is raised instead. ``cost`` is each read's time.
    """

    def __init__(
        self,
        clock: Any,
        *,
        heads: list[Any] | None = None,
        reviews: list[Any] | None = None,
        threads: list[Any] | None = None,
        runs: list[Any] | None = None,
        cost: float = 0.0,
    ) -> None:
        """Queue the replies."""
        self.clock = clock
        self.queues: dict[str, list[Any]] = {
            "heads": heads or [HEAD],
            "reviews": reviews or [CLEAN],
            "threads": threads or [threads_reply([])],
            "runs": runs or [[check_run(1, "ok")]],
        }
        self.cost = cost
        self.counts = dict.fromkeys(self.queues, 0)

    def answer(self, name: str) -> Any:
        """The next reply of one queue, raised when it is an exception."""
        self.clock.now += self.cost
        self.counts[name] += 1
        queue = self.queues[name]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def api_object(self, endpoint: str) -> dict[str, Any]:
        """A head read, a check run listing or the combined status."""
        if endpoint == PULL:
            return {"head": {"sha": self.answer("heads")}}
        if f"/commits/{HEAD}/check-runs" in endpoint:
            listed = self.answer("runs")
            return {"total_count": len(listed), "check_runs": listed}
        if f"/commits/{HEAD}/status?" in endpoint:
            return {"sha": HEAD, "total_count": 0, "statuses": []}
        raise AssertionError(f"unexpected read of {endpoint}")

    def api_list(self, endpoint: str) -> list[dict[str, Any]]:
        """The review listing."""
        if endpoint != fixtures.REVIEWS:
            raise AssertionError(f"unexpected read of {endpoint}")
        return list(self.answer("reviews"))

    def graphql(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """One page of review threads."""
        return self.answer("threads")

    def patched(self) -> Any:
        """Every read the waits make, answered by this fake."""
        return patch.multiple(
            github,
            api_object=self.api_object,
            api_list=self.api_list,
            graphql=self.graphql,
        )

    def run(self, warnings: list[str] | None = None) -> tuple[Any, int | None, Any]:
        """Confirm the head and await its review and checks against the fake."""
        with (
            self.patched(),
            patch.object(reads, "api_page", side_effect=AssertionError("no notes")),
        ):
            return review.await_head(
                REPO,
                41,
                HEAD,
                wait_minutes=WAIT,
                clock=self.clock,
                sleep=self.clock.sleep,
                warnings=warnings,
            )


class RetriesTest(unittest.TestCase):
    """The one decision: read again, or end the wait."""

    def test_only_a_transient_failure_with_time_spare_is_read_again(self) -> None:
        """A 5xx with time spare sleeps a poll; anything else is final."""
        cases = [
            (UNAVAILABLE, 100.0, True),
            (github.GitHubError("gh: unexpected EOF"), 100.0, True),
            (UNAVAILABLE, 0.0, False),
            (NOT_FOUND, 100.0, False),
            (INVALID, 100.0, False),
            (github.GitHubError("pull request reply carried no head"), 100.0, False),
            (ValueError("HTTP 503"), 100.0, False),
        ]
        for exc, spare, expected in cases:
            with self.subTest(exc=str(exc), spare=spare):
                clock = Clock()
                retries = transient.Retries("the review wait")
                self.assertIs(retries.again(exc, spare, clock.sleep), expected)
                self.assertEqual(retries.count, int(expected))
                self.assertEqual(
                    clock.sleeps, [transient.POLL_SECONDS] if expected else []
                )

    def test_the_sleep_never_passes_the_spare_time(self) -> None:
        """Near the deadline the poll shortens to what is left."""
        clock = Clock()
        transient.Retries("w").again(UNAVAILABLE, 4.5, clock.sleep)
        self.assertEqual(clock.sleeps, [4.5])

    def test_the_warning_counts_and_quotes_the_last_error_bounded(self) -> None:
        """One line, the count and the last error, escaped and cut short."""
        retries = transient.Retries("the review wait")
        self.assertIsNone(retries.warning())
        clock = Clock()
        for _ in range(3):
            retries.again(UNAVAILABLE, 100.0, clock.sleep)
        self.assertEqual(
            retries.warning(),
            "the review wait retried 3 transient read failures; last: 'gh: HTTP 503'",
        )
        hostile = github.GitHubError("::error::x\n" + "y" * 1000 + " (HTTP 502)")
        retries.again(hostile, 100.0, clock.sleep)
        warning = str(retries.warning())
        self.assertNotIn("\n", warning)
        self.assertNotIn("::", warning)
        self.assertLess(len(warning), transient.MAX_ERROR_TEXT + 100)


class TransientReviewWaitTest(unittest.TestCase):
    """A transient failure costs the review wait one poll, not the round."""

    def test_a_503_on_the_review_listing_then_a_clean_review(self) -> None:
        """The next poll reads again and the verdict is the review's."""
        clock = Clock()
        warnings: list[str] = []
        fake = Script(clock, reviews=[UNAVAILABLE, CLEAN])
        verdict, review_id, found = fake.run(warnings)
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(review_id, 7)
        self.assertEqual(found.state, "passing")
        self.assertEqual(fake.counts["reviews"], 2)
        self.assertEqual(clock.sleeps, [transient.POLL_SECONDS])
        self.assertEqual(
            warnings,
            ["the review wait retried 1 transient read failure; last: 'gh: HTTP 503'"],
        )

    def test_a_503_on_the_thread_count_then_success(self) -> None:
        """A failed thread page re-reads the poll; the verdict stands."""
        clock = Clock()
        fake = Script(clock, threads=[UNAVAILABLE, threads_reply([])])
        verdict, _, _ = fake.run()
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(fake.counts["threads"], 2)

    def test_a_503_on_the_confirming_head_read_then_success(self) -> None:
        """The head read that confirms a review is read again; the verdict stands."""
        clock = Clock()
        warnings: list[str] = []
        # Before the wait, the confirmation that fails, then the one
        # that succeeds, then the checks' closing read.
        fake = Script(clock, heads=[HEAD, UNAVAILABLE, HEAD])
        verdict, review_id, found = fake.run(warnings)
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(review_id, 7)
        self.assertEqual(found.state, "passing")
        self.assertEqual(fake.counts["heads"], 4)
        self.assertEqual(len(warnings), 1)

    def test_503_on_every_read_is_pending_and_unknown_within_the_budget(self) -> None:
        """A wait spent on transient failures proves nothing and never overruns."""
        for cost in (0.0, 1.0, review.READ_BUDGET_SECONDS):
            with self.subTest(cost=cost):
                clock = Clock()
                warnings: list[str] = []
                fake = Script(
                    clock,
                    heads=[HEAD, UNAVAILABLE],
                    reviews=[UNAVAILABLE],
                    runs=[UNAVAILABLE],
                    cost=cost,
                )
                verdict, review_id, found = fake.run(warnings)
                self.assertEqual(verdict.verdict, "pending")
                self.assertIsNone(review_id)
                self.assertIn("could not read the review", verdict.reasons[0])
                self.assertIn("HTTP 503", verdict.reasons[0])
                self.assertEqual(found.state, "unknown")
                self.assertIn("could not read the head's check runs", found.reason)
                self.assertIn("HTTP 503", found.reason)
                self.assertLessEqual(clock.now, WAIT * 60)
                self.assertGreater(fake.counts["reviews"], 1)
                self.assertTrue(warnings[0].startswith("the review wait retried"))
                # At a read's worst case the checks have time for one read
                # and none to repeat it.
                waits = ["the review wait"]
                if cost < review.READ_BUDGET_SECONDS:
                    waits.append("the checks wait")
                self.assertEqual([w.split(" retried")[0] for w in warnings], waits)

    def test_503_on_every_head_read_before_the_wait_is_pending(self) -> None:
        """A head never read leaves the review pending and the checks unread."""
        clock = Clock()
        fake = Script(clock, heads=[UNAVAILABLE])
        verdict, _, found = fake.run()
        self.assertEqual(verdict.verdict, "pending")
        self.assertIn("could not read the pull request head", verdict.reasons[0])
        self.assertIn("HTTP 503", verdict.reasons[0])
        self.assertIsNone(found)
        self.assertEqual(fake.counts["reviews"], 0)
        self.assertLessEqual(clock.now, WAIT * 60 - review.RESERVE_SECONDS)

    def test_a_final_failure_on_any_review_read_ends_the_wait_at_once(self) -> None:
        """A 404 or 422 is no blip: one read, no poll, pending."""
        for failure in (NOT_FOUND, INVALID):
            cases: dict[str, dict[str, Any]] = {
                "the head before the wait": {"heads": [failure]},
                "the review listing": {"reviews": [failure]},
                "the thread count": {"threads": [failure]},
                "the confirming head": {"heads": [HEAD, failure]},
            }
            for name, queues in cases.items():
                with self.subTest(failure=str(failure), read=name):
                    clock = Clock()
                    warnings: list[str] = []
                    fake = Script(clock, **queues)
                    verdict, _, _ = fake.run(warnings)
                    self.assertEqual(verdict.verdict, "pending")
                    self.assertIn(str(failure.status), verdict.reasons[0])
                    self.assertEqual(clock.sleeps, [])
                    self.assertEqual(warnings, [])


class TransientChecksWaitTest(unittest.TestCase):
    """A transient failure costs the checks wait one poll, not the record."""

    def test_a_503_on_the_check_run_listing_then_settled_runs(self) -> None:
        """The next poll reads again; the state is the runs', not unknown."""
        clock = Clock()
        warnings: list[str] = []
        fake = Script(clock, runs=[UNAVAILABLE, [check_run(1, "ok")]])
        verdict, _, found = fake.run(warnings)
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(found.state, "passing")
        self.assertIsNone(found.reason)
        self.assertEqual(fake.counts["runs"], 2)
        self.assertEqual(
            warnings,
            ["the checks wait retried 1 transient read failure; last: 'gh: HTTP 503'"],
        )

    def test_a_503_on_the_closing_head_read_then_success(self) -> None:
        """The checks' closing head read is read again; the state stands."""
        clock = Clock()
        fake = Script(clock, heads=[HEAD, HEAD, UNAVAILABLE, HEAD])
        _, _, found = fake.run()
        self.assertEqual(found.state, "passing")
        self.assertEqual(fake.counts["heads"], 4)

    def test_a_final_failure_on_a_checks_read_is_unknown_at_once(self) -> None:
        """A 404 or 422 on the listing or the closing head read ends the wait."""
        for failure in (NOT_FOUND, INVALID):
            cases: dict[str, dict[str, Any]] = {
                "the check run listing": {"runs": [failure]},
                "the closing head": {"heads": [HEAD, HEAD, failure]},
            }
            for name, queues in cases.items():
                with self.subTest(failure=str(failure), read=name):
                    clock = Clock()
                    fake = Script(clock, **queues)
                    verdict, _, found = fake.run()
                    self.assertEqual(verdict.verdict, "clean")
                    self.assertEqual(found.state, "unknown")
                    self.assertIn(str(failure.status), found.reason)
                    self.assertEqual(clock.sleeps, [])


class TransientAwaitHandlerTest(unittest.TestCase):
    """``review-await`` records what the waits rode out (#186)."""

    def setUp(self) -> None:
        """A result.json for a published pull request, in a scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.path = Path(holder.name) / "result.json"
        self.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "repository": REPO,
                    "issue": 7,
                    "verdict": "proposed",
                    "warnings": [],
                    "dry_run": False,
                    "pull_request_url": f"https://github.com/{REPO}/pull/41",
                    "commits": [HEAD],
                    "review": {"requested": True},
                }
            ),
            encoding="utf-8",
        )

    def test_a_503_on_the_head_read_before_the_wait_then_success(self) -> None:
        """The wait still runs, returns the verdict and records one retry."""
        clock = Clock()
        fake = Script(clock, heads=[UNAVAILABLE, HEAD])
        real = review.await_head

        def timed(*args: Any, **kwargs: Any) -> Any:
            return real(*args, clock=clock, sleep=clock.sleep, **kwargs)

        with (
            fake.patched(),
            patch.object(review, "await_head", side_effect=timed),
            patch("builtins.print"),
        ):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["review"]["verdict"], "clean")
        self.assertEqual(stored["checks"]["state"], "passing")
        self.assertEqual(
            stored["warnings"],
            ["the review wait retried 1 transient read failure; last: 'gh: HTTP 503'"],
        )
        self.assertEqual(clock.sleeps, [transient.POLL_SECONDS])


if __name__ == "__main__":
    unittest.main()
