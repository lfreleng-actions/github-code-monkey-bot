# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Requesting and awaiting the Copilot review, against a fake GitHub."""

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
BODIES = import_module("review_fixtures").BODIES

HEAD = "a" * 40
OTHER = "b" * 40
REPO = "org/repo"


def copilot(review_id: int, commit: str, body: str) -> dict[str, Any]:
    """A review object as the REST listing returns it."""
    return {
        "id": review_id,
        "user": {"login": review.REVIEWER_LOGIN},
        "commit_id": commit,
        "body": body,
        "state": "COMMENTED",
    }


def threads_reply(flags: list[bool], *, more: bool = False) -> dict[str, Any]:
    """A GraphQL page of review threads."""
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": more, "endCursor": "c"},
                    "nodes": [{"isResolved": flag} for flag in flags],
                }
            }
        }
    }


class Clock:
    """A fake monotonic clock the sleep double advances."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """The current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock instead of waiting."""
        self.sleeps.append(seconds)
        self.now += seconds


class PullReferenceTest(unittest.TestCase):
    """The pull request URL names the repository and number."""

    def test_parses_and_refuses(self) -> None:
        """A GitHub pull request URL parses; anything else is refused."""
        self.assertEqual(
            review.pull_reference("https://github.com/org/repo/pull/41"),
            ("org/repo", 41),
        )
        for bad in ("https://github.com/org/repo/issues/41", "org/repo#41", ""):
            with self.subTest(url=bad), self.assertRaises(github.GitHubError):
                review.pull_reference(bad)


class RequestReviewTest(unittest.TestCase):
    """The request uses the form GitHub accepts and never raises."""

    def test_request_form(self) -> None:
        """``gh pr edit --add-reviewer @copilot`` on the repository's pull request."""
        with patch.object(github, "run_gh", return_value="") as gh:
            self.assertIsNone(review.request_review(REPO, 41))
        self.assertEqual(
            gh.call_args.args[0],
            ["pr", "edit", "41", "--repo", REPO, "--add-reviewer", "@copilot"],
        )

    def test_failure_is_a_warning(self) -> None:
        """A refusal is recorded, in log-safe form, and the publish goes on."""
        failure = github.GitHubError(
            "gh: Resource not accessible::by integration (HTTP 403)"
        )
        with patch.object(github, "run_gh", side_effect=failure):
            warning = review.request_review(REPO, 41)
        assert warning is not None
        self.assertIn("could not request a Copilot review", warning)
        self.assertNotIn("::", warning)


class ReadsTest(unittest.TestCase):
    """The review of the head and the unresolved thread count."""

    def test_head_review_picks_the_newest_copilot_review_of_the_head(self) -> None:
        """Other authors, other heads and older reviews are passed over."""
        listing = [
            {"id": 1, "user": {"login": "alice"}, "commit_id": HEAD, "body": "x"},
            copilot(2, OTHER, "old head"),
            copilot(3, HEAD, "first of head"),
            copilot(4, HEAD, "second of head"),
        ]
        with patch.object(github, "api_list", return_value=listing):
            found = review.head_review(REPO, 41, HEAD)
        assert found is not None
        self.assertEqual(found["id"], 4)
        with patch.object(github, "api_list", return_value=listing[:2]):
            self.assertIsNone(review.head_review(REPO, 41, HEAD))

    def test_pull_head(self) -> None:
        """The head SHA comes from the pull request itself."""
        with patch.object(github, "api_object", return_value={"head": {"sha": HEAD}}):
            self.assertEqual(review.pull_head(REPO, 41), HEAD)
        with (
            patch.object(github, "api_object", return_value={"head": {"sha": "short"}}),
            self.assertRaises(github.GitHubError),
        ):
            review.pull_head(REPO, 41)

    def test_unresolved_threads_count_every_page(self) -> None:
        """Pages are followed and only ``isResolved: false`` counts."""
        pages = [
            threads_reply([False, True, False], more=True),
            threads_reply([True, False]),
        ]
        with patch.object(github, "graphql", side_effect=pages) as gql:
            self.assertEqual(review.unresolved_threads(REPO, 41), 3)
        self.assertEqual(len(gql.call_args_list), 2)
        self.assertIs(gql.call_args_list[0].kwargs.get("read"), True)
        self.assertEqual(gql.call_args_list[1].args[1]["after"], "c")

    def test_thread_query_without_data_is_an_error(self) -> None:
        """A reply missing the thread connection is a failure, not zero."""
        with (
            patch.object(github, "graphql", return_value={"repository": None}),
            self.assertRaises(github.GitHubError),
        ):
            review.unresolved_threads(REPO, 41)


class AwaitReviewTest(unittest.TestCase):
    """The bounded wait."""

    def test_review_found_is_classified(self) -> None:
        """A review of the head with its threads yields a verdict and the id."""
        clock = Clock()
        listings = [[], [copilot(7, HEAD, BODIES["v2-green-clean"])]]
        with (
            patch.object(github, "api_object", return_value={"head": {"sha": HEAD}}),
            patch.object(github, "api_list", side_effect=listings),
            patch.object(github, "graphql", return_value=threads_reply([])),
        ):
            verdict, review_id = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(review_id, 7)
        self.assertEqual(clock.sleeps, [review.POLL_SECONDS])

    def test_no_review_within_the_wait_is_pending(self) -> None:
        """Polling stops while a hit's reads still fit; the wait never overruns."""
        clock = Clock()
        with patch.object(github, "api_list", return_value=[]) as listing:
            verdict, review_id = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "pending")
        self.assertIsNone(review_id)
        self.assertIn("within 20 minutes", verdict.reasons[0])
        # Every read began with the reserve still available, and the
        # clock never passed the deadline.
        self.assertLessEqual(clock.now, 20 * 60)
        self.assertGreater(len(listing.call_args_list), 1)
        self.assertGreaterEqual(20 * 60 - sum(clock.sleeps), review.RESERVE_SECONDS)

    def test_a_read_failure_ends_the_wait_as_pending(self) -> None:
        """Failures the wrapper's retries did not cure are pending, not clean."""
        clock = Clock()
        failure = github.GitHubError("gh: Server Error (HTTP 502)")
        with patch.object(github, "api_list", side_effect=failure):
            verdict, _ = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "pending")
        self.assertIn("could not read the review", verdict.reasons[0])

    def test_review_of_another_head_does_not_count(self) -> None:
        """A review requested against the previous push is not this round's."""
        clock = Clock()
        with patch.object(
            github,
            "api_list",
            return_value=[copilot(7, OTHER, BODIES["v2-green-clean"])],
        ):
            verdict, _ = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "pending")

    def test_late_review_with_many_thread_pages_stays_within_the_wait(self) -> None:
        """Each thread page is a read; one that would overrun the wait ends pending."""
        clock = Clock()
        wait = 20

        def listing(_endpoint: str) -> list[dict[str, Any]]:
            # The review lands late: after the listing one thread page
            # fits the wait and a second would not.
            clock.now = wait * 60 - review.READ_BUDGET_SECONDS - 1
            return [copilot(7, HEAD, BODIES["v2-green-clean"])]

        pages = 0

        def threads(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal pages
            pages += 1
            if pages > 3:
                raise AssertionError("paging past the wait")
            clock.now += review.READ_BUDGET_SECONDS
            return threads_reply([False], more=True)

        with (
            patch.object(github, "api_list", side_effect=listing),
            patch.object(github, "graphql", side_effect=threads) as gql,
        ):
            verdict, review_id = review.await_review(
                REPO, 41, HEAD, wait_minutes=wait, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "pending")
        self.assertIsNone(review_id)
        self.assertIn("thread", verdict.reasons[0])
        self.assertEqual(len(gql.call_args_list), 1)
        self.assertLessEqual(clock.now, wait * 60 + review.READ_BUDGET_SECONDS)

    def test_head_that_moved_during_the_wait_is_pending(self) -> None:
        """A clean review of the old head is not a review of the new one."""
        clock = Clock()
        with (
            patch.object(github, "api_object", return_value={"head": {"sha": OTHER}}),
            patch.object(
                github,
                "api_list",
                return_value=[copilot(7, HEAD, BODIES["v2-green-clean"])],
            ),
            patch.object(github, "graphql", return_value=threads_reply([])),
        ):
            verdict, review_id = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "pending")
        self.assertIsNone(review_id)
        self.assertIn("head moved", verdict.reasons[0])

    def test_head_is_confirmed_before_a_verdict_is_accepted(self) -> None:
        """The head is read again once the review lands, and a hit then stands."""
        clock = Clock()
        with (
            patch.object(
                github, "api_object", return_value={"head": {"sha": HEAD}}
            ) as head,
            patch.object(
                github,
                "api_list",
                return_value=[copilot(7, HEAD, BODIES["v2-green-clean"])],
            ),
            patch.object(github, "graphql", return_value=threads_reply([])),
        ):
            verdict, _ = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(head.call_args.args[0], f"repos/{REPO}/pulls/41")

    def test_reserve_covers_a_hit(self) -> None:
        """The reserve is the worst case of the three reads a hit needs."""
        worst = github.READ_ATTEMPTS * github.TIMEOUT_SECONDS + sum(
            github.RETRY_DELAY_SECONDS * n for n in range(1, github.READ_ATTEMPTS)
        )
        self.assertEqual(review.RESERVE_SECONDS, 3 * worst)
        self.assertLess(review.RESERVE_SECONDS, review.DEFAULT_WAIT_MINUTES * 60 / 2)


class HandlersTest(unittest.TestCase):
    """The publish.py commands that wrap the request and the wait."""

    def setUp(self) -> None:
        """A result.json for a published pull request, in a scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.path = self.root / "result.json"

    def write(self, **overrides: Any) -> None:
        """Write a proposed, published result."""
        data: dict[str, Any] = {
            "schema": 1,
            "key": "repo-7",
            "repository": REPO,
            "issue": 7,
            "verdict": "proposed",
            "reasons": [],
            "warnings": [],
            "dry_run": False,
            "mode": "pull-requests",
            "pull_request_url": f"https://github.com/{REPO}/pull/41",
            "commits": [HEAD],
        }
        data.update(overrides)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def stored(self) -> dict[str, Any]:
        """The result as written back."""
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_request_records_success_and_refusal(self) -> None:
        """The request is recorded; a refusal is a warning, never a failure."""
        self.write()
        with patch.object(github, "run_gh", return_value=""):
            review.run_request(argparse.Namespace(result=self.path, output=self.path))
        self.assertIs(self.stored()["review"]["requested"], True)
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=github.GitHubError("no")),
            patch.object(
                github, "api_object", return_value={"body": "Closes #7\n\nbody"}
            ),
            patch.object(github, "api_write", return_value={}) as write,
        ):
            review.run_request(argparse.Namespace(result=self.path, output=self.path))
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], False)
        self.assertTrue(any("Copilot review" in w for w in stored["warnings"]))
        # The refusal is disclosed at the top of the pull request body,
        # as #29 requires, without widening any token.
        method, endpoint, payload = write.call_args.args
        self.assertEqual((method, endpoint), ("PATCH", f"repos/{REPO}/pulls/41"))
        body = payload["body"]
        self.assertTrue(body.startswith(review.REFUSAL_NOTICE_MARKER))
        self.assertIn("could not request a Copilot review", body)
        self.assertTrue(body.endswith("Closes #7\n\nbody"))

    def test_refusal_notice_is_idempotent_and_best_effort(self) -> None:
        """A body already carrying the notice is left alone; a failed edit is a warning."""
        self.write()
        noted = review.REFUSAL_NOTICE_MARKER + "\n> note\n\nCloses #7"
        with (
            patch.object(github, "run_gh", side_effect=github.GitHubError("no")),
            patch.object(github, "api_object", return_value={"body": noted}),
            patch.object(github, "api_write") as write,
        ):
            review.run_request(argparse.Namespace(result=self.path, output=self.path))
        write.assert_not_called()
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=github.GitHubError("no")),
            patch.object(github, "api_object", side_effect=github.GitHubError("boom")),
        ):
            review.run_request(argparse.Namespace(result=self.path, output=self.path))
        warnings = self.stored()["warnings"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("notice" in w for w in warnings))

    def test_nothing_published_means_nothing_to_request_or_await(self) -> None:
        """Without a pull request, or in a dry run, both commands are no-ops."""
        for overrides in ({"pull_request_url": None}, {"dry_run": True}):
            with self.subTest(overrides=overrides):
                self.write(**overrides)
                with patch.object(
                    github, "run_gh", side_effect=AssertionError("no network")
                ):
                    review.run_request(
                        argparse.Namespace(result=self.path, output=self.path)
                    )
                    review.run_await(
                        argparse.Namespace(
                            result=self.path, output=self.path, wait_minutes=1
                        )
                    )
                self.assertNotIn("review", self.stored())

    def test_failed_head_read_records_pending(self) -> None:
        """A head read the wrapper's retries did not cure still writes a record."""
        self.write(review={"requested": True})
        failure = github.GitHubError("gh: Server Error (HTTP 502)")
        with patch.object(github, "api_object", side_effect=failure):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        record = self.stored()["review"]
        self.assertEqual(record["verdict"], "pending")
        self.assertIn("could not read the pull request head", record["reasons"][0])
        # The published head is known from the replay; the read that
        # failed was the confirmation.
        self.assertEqual(record["commit_id"], HEAD)
        self.assertIs(record["requested"], True)

    def test_wait_is_bound_to_the_published_head(self) -> None:
        """The head the publisher pushed is the one judged; a moved one is pending."""
        self.write(review={"requested": True})
        with (
            patch.object(github, "api_object", return_value={"head": {"sha": OTHER}}),
            patch.object(github, "api_list", side_effect=AssertionError("no wait")),
        ):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        record = self.stored()["review"]
        self.assertEqual(record["verdict"], "pending")
        self.assertIn("head moved", record["reasons"][0])
        self.assertEqual(record["commit_id"], HEAD)

    def test_no_recorded_head_is_pending(self) -> None:
        """Without the replayed chain there is nothing to bind the review to."""
        self.write(review={"requested": True}, commits=[])
        with patch.object(github, "api_object", side_effect=AssertionError("no read")):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        record = self.stored()["review"]
        self.assertEqual(record["verdict"], "pending")
        self.assertIn("no published head", record["reasons"][0])

    def test_await_records_the_verdict_against_the_head(self) -> None:
        """The record names the verdict, counts, review id and the head it judged."""
        self.write(review={"requested": True})
        listing = [copilot(9, HEAD, BODIES["v2-blue-with-findings"])]
        with (
            patch.object(github, "api_object", return_value={"head": {"sha": HEAD}}),
            patch.object(github, "api_list", return_value=listing),
            patch.object(github, "graphql", return_value=threads_reply([False])),
        ):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        record = self.stored()["review"]
        self.assertEqual(record["verdict"], "findings")
        self.assertEqual(record["assessment"], "closer-look")
        self.assertEqual(record["unresolved_threads"], 1)
        self.assertEqual(record["review_id"], 9)
        self.assertEqual(record["commit_id"], HEAD)
        self.assertIs(record["requested"], True)


if __name__ == "__main__":
    unittest.main()
