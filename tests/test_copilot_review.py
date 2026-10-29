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
request = import_module("review_request")
BODIES = import_module("review_fixtures").BODIES

HEAD = "a" * 40
OTHER = "b" * 40
REPO = "org/repo"
APP = "App token"


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


NODE_ID = "PR_kwDOAAAAAA"
PULL = {"node_id": NODE_ID, "body": "Closes #7"}


def requests_reply(nodes: list[Any]) -> dict[str, Any]:
    """The ``requestReviewsByLogin`` payload around raw ``nodes``."""
    return {
        "requestReviewsByLogin": {"pullRequest": {"reviewRequests": {"nodes": nodes}}}
    }


def mutation_reply(reviewers: list[tuple[str, str | None]]) -> dict[str, Any]:
    """The ``requestReviewsByLogin`` payload, one node per (typename, login)."""
    nodes: list[dict[str, Any]] = []
    for typename, login in reviewers:
        reviewer: dict[str, Any] = {"__typename": typename}
        if login is not None:
            reviewer["login"] = login
        nodes.append({"requestedReviewer": reviewer})
    return requests_reply(nodes)


ADDED = mutation_reply(
    [("Team", None), ("User", "alice"), ("Bot", "copilot-pull-request-reviewer")]
)
NOT_ADDED = mutation_reply([("Team", None), ("User", "alice")])


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


class RequestMutationTest(unittest.TestCase):
    """One additive ``requestReviewsByLogin``, verified from its own reply."""

    def test_mutation_is_additive_and_names_the_bot(self) -> None:
        """``union: true``, the suffixed bot login, the node id the pull request read."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL) as pull,
            patch.object(github, "graphql", return_value=ADDED) as gql,
            patch.object(github, "api_list", side_effect=AssertionError("no read")),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        self.assertIsNone(outcome.warning)
        pull.assert_called_once_with(f"repos/{REPO}/pulls/41")
        # The reply answers the question: no further GraphQL read.
        gql.assert_called_once()
        query, variables = gql.call_args.args
        self.assertIn("requestReviewsByLogin", query)
        self.assertIn("union: true", query)
        self.assertNotIn("userLogins", query)
        self.assertNotIn("teamSlugs", query)
        self.assertEqual(
            variables,
            {"id": NODE_ID, "bots": ["copilot-pull-request-reviewer[bot]"]},
        )
        self.assertNotIn("userLogins", variables)
        self.assertNotIn("teamSlugs", variables)
        # A mutation is never retried as a read.
        self.assertIsNot(gql.call_args.kwargs.get("read"), True)
        self.assertEqual(
            outcome.reviewers,
            (
                ("Team", None),
                ("User", "alice"),
                ("Bot", "copilot-pull-request-reviewer"),
            ),
        )

    def test_reply_without_the_reviewer_and_no_head_review_is_unverified(
        self,
    ) -> None:
        """The reply leaves Copilot out and no review of the head exists."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=NOT_ADDED) as gql,
            patch.object(github, "api_list", return_value=[copilot(3, OTHER, "x")]),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        self.assertEqual(outcome.warning, request.UNVERIFIED_REQUEST)
        gql.assert_called_once()

    def test_reply_without_the_reviewer_but_a_head_review_counts(self) -> None:
        """A fast review of the head clears the request before the reply."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=NOT_ADDED) as gql,
            patch.object(github, "api_list", return_value=[copilot(3, HEAD, "x")]),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        self.assertIsNone(outcome.warning)
        gql.assert_called_once()

    def test_mutation_error_is_a_log_safe_warning(self) -> None:
        """A GraphQL error is a refusal in log-safe form."""
        failure = github.GitHubError("GraphQL errors: Forbidden::by integration")
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", side_effect=failure),
            patch.object(github, "api_list", side_effect=AssertionError("no read")),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        assert outcome.warning is not None
        self.assertIn("could not request a Copilot review", outcome.warning)
        self.assertNotIn("::", outcome.warning)

    def test_reply_of_an_unexpected_shape_is_never_success(self) -> None:
        """A malformed review request list warns, even with a head review."""
        replies: list[dict[str, Any]] = [
            {"requestReviewsByLogin": None},
            {"requestReviewsByLogin": {"pullRequest": {}}},
            {"requestReviewsByLogin": {"pullRequest": {"reviewRequests": {}}}},
            requests_reply([42]),
            requests_reply([{"requestedReviewer": []}]),
            requests_reply([None, {"requestedReviewer": "Bot"}]),
            requests_reply([{}]),
            requests_reply([{"requestedReviewer": {}}]),
            requests_reply(
                [{"requestedReviewer": {"login": "copilot-pull-request-reviewer"}}]
            ),
            requests_reply([{"requestedReviewer": {"__typename": ""}}]),
            requests_reply([{"requestedReviewer": {"__typename": 7, "login": "x"}}]),
            requests_reply([{"requestedReviewer": {"__typename": "Bot"}}]),
            requests_reply(
                [{"requestedReviewer": {"__typename": "User", "login": ""}}]
            ),
            requests_reply([{"requestedReviewer": {"__typename": "Bot", "login": 5}}]),
            requests_reply(
                [{"requestedReviewer": {"__typename": "Team", "login": []}}]
            ),
        ]
        for reply in replies:
            with (
                self.subTest(reply=reply),
                patch.object(
                    github, "run_gh", side_effect=AssertionError("no gh call")
                ),
                patch.object(github, "api_object", return_value=PULL),
                patch.object(github, "graphql", return_value=reply),
                patch.object(github, "api_list", return_value=[copilot(3, HEAD, "x")]),
            ):
                outcome = request.request_review(REPO, 41, HEAD)
            assert outcome.warning is not None
            self.assertIn("could not confirm", outcome.warning)

    def test_null_nodes_and_reviewers_are_skipped(self) -> None:
        """GraphQL's nullable node and reviewer do not make the reply malformed."""
        reply = requests_reply(
            [
                None,
                {"requestedReviewer": None},
                {"requestedReviewer": {"__typename": "Team"}},
            ]
        )
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=reply),
            patch.object(github, "api_list", return_value=[copilot(3, HEAD, "x")]),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        self.assertIsNone(outcome.warning)
        self.assertEqual(outcome.reviewers, (("Team", None),))

    def test_failed_node_id_read_is_a_refusal(self) -> None:
        """Without the pull request's node id no request is sent."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(
                github, "api_object", side_effect=github.GitHubError("HTTP 502")
            ),
            patch.object(github, "graphql", side_effect=AssertionError("no send")),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        assert outcome.warning is not None
        self.assertIn("could not request a Copilot review", outcome.warning)

    def test_reviewer_counts_in_either_form(self) -> None:
        """The Copilot reviewer counts with or without the ``[bot]`` suffix."""
        for login in (
            "copilot-pull-request-reviewer",
            "Copilot-Pull-Request-Reviewer[bot]",
        ):
            with (
                self.subTest(login=login),
                patch.object(
                    github, "run_gh", side_effect=AssertionError("no gh call")
                ),
                patch.object(github, "api_object", return_value=PULL),
                patch.object(
                    github, "graphql", return_value=mutation_reply([("Bot", login)])
                ),
                patch.object(github, "api_list", side_effect=AssertionError("no read")),
            ):
                self.assertIsNone(request.request_review(REPO, 41, HEAD).warning)

    def test_no_review_counts_without_a_head(self) -> None:
        """With no published head, only the reply's request proves the request."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=NOT_ADDED),
            patch.object(
                github,
                "api_list",
                return_value=[copilot(3, HEAD, "done"), copilot(4, OTHER, "done")],
            ),
        ):
            outcome = request.request_review(REPO, 41, None)
        self.assertEqual(outcome.warning, request.UNVERIFIED_REQUEST)

    def test_a_failed_head_review_read_is_a_warning(self) -> None:
        """A request that cannot be confirmed is never recorded as sent."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=NOT_ADDED),
            patch.object(
                github, "api_list", side_effect=github.GitHubError("HTTP 502")
            ),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        assert outcome.warning is not None
        self.assertIn("could not confirm", outcome.warning)

    def test_reviewers_outside_github_shapes_stay_out_of_the_log(self) -> None:
        """Only GitHub's name shapes are kept from the reply."""
        reply = mutation_reply([("Bot::x", "a::b"), ("User", "bob")])
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=reply),
            patch.object(github, "api_list", return_value=[]),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        self.assertEqual(outcome.reviewers, (("?", None), ("User", "bob")))


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
        self.assertIn("20-minute wait", verdict.reasons[0])
        # Every read began with the reserve still available, and the
        # clock never passed the deadline.
        self.assertLessEqual(clock.now, 20 * 60)
        self.assertGreater(len(listing.call_args_list), 1)
        self.assertGreaterEqual(20 * 60 - sum(clock.sleeps), review.RESERVE_SECONDS)

    def test_pending_reason_reports_the_time_polled(self) -> None:
        """The reserve ends polling early; the reason says how long it ran."""
        clock = Clock()
        with patch.object(github, "api_list", return_value=[]):
            verdict, _ = review.await_review(
                REPO, 41, HEAD, wait_minutes=20, clock=clock, sleep=clock.sleep
            )
        polled = (20 * 60 - review.RESERVE_SECONDS) / 60
        self.assertEqual(clock.now, 20 * 60 - review.RESERVE_SECONDS)
        self.assertIn(f"{polled:.1f} minutes", verdict.reasons[0])
        self.assertNotIn("within 20 minutes", verdict.reasons[0])

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
        with (
            patch.object(github, "graphql", return_value=ADDED),
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], True)
        self.assertEqual(stored["warnings"], [])
        self.write()
        with (
            patch.object(github, "graphql", side_effect=github.GitHubError("no")),
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7\n\nbody"},
            ),
            patch.object(github, "api_write", return_value={}) as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], False)
        self.assertTrue(any("Copilot review" in w for w in stored["warnings"]))
        # The refusal is disclosed at the top of the pull request body,
        # as #29 requires, without widening any token.
        method, endpoint, payload = write.call_args.args
        self.assertEqual((method, endpoint), ("PATCH", f"repos/{REPO}/pulls/41"))
        body = payload["body"]
        self.assertTrue(body.startswith(request.REFUSAL_NOTICE_MARKER))
        self.assertIn("could not request a Copilot review", body)
        self.assertTrue(body.endswith("Closes #7\n\nbody"))

    def test_request_that_added_no_reviewer_is_refused(self) -> None:
        """An accepted request with no reviewer takes the refusal path."""
        self.write()
        with (
            patch.object(github, "graphql", return_value=NOT_ADDED),
            patch.object(github, "api_list", return_value=[]),
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
            patch.object(github, "api_write", return_value={}) as write,
            patch("builtins.print") as printed,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], False)
        self.assertTrue(
            any("no Copilot reviewer was added" in w for w in stored["warnings"])
        )
        body = write.call_args.args[2]["body"]
        self.assertTrue(body.startswith(request.REFUSAL_NOTICE_MARKER))
        self.assertTrue(body.endswith("Closes #7"))
        printed.assert_any_call("review request: refused")

    def test_request_is_bound_to_the_published_head(self) -> None:
        """The read-back judges the head the publisher pushed, not any commit."""
        self.write(commits=[OTHER, HEAD])
        with (
            patch.object(
                request, "request_review", return_value=request.RequestOutcome(None)
            ) as sent,
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        self.assertEqual(sent.call_args.args, (REPO, 41, HEAD))
        # A revision round: the only Copilot review is of the previous
        # head and GitHub added no reviewer, so the request was refused.
        self.write(commits=[HEAD])
        with (
            patch.object(github, "graphql", return_value=NOT_ADDED),
            patch.object(github, "api_list", return_value=[copilot(3, OTHER, "done")]),
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
            patch.object(github, "api_write", return_value={}) as write,
            patch("builtins.print") as printed,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], False)
        self.assertIn(request.UNVERIFIED_REQUEST, stored["warnings"])
        body = write.call_args.args[2]["body"]
        self.assertTrue(body.startswith(request.REFUSAL_NOTICE_MARKER))
        self.assertTrue(body.endswith("Closes #7"))
        printed.assert_any_call("review request: refused")

    def test_request_without_commits_verifies_by_the_pending_request(self) -> None:
        """No published head: the request is still sent; no review counts."""
        self.write(commits=[])
        with (
            patch.object(github, "graphql", return_value=ADDED) as gql,
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        gql.assert_called_once()
        self.assertIs(self.stored()["review"]["requested"], True)
        self.write(commits=[])
        with (
            patch.object(github, "graphql", return_value=NOT_ADDED) as gql,
            patch.object(github, "api_list", return_value=[copilot(3, HEAD, "done")]),
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
            patch.object(github, "api_write", return_value={}),
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        gql.assert_called_once()
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], False)
        self.assertIn(request.UNVERIFIED_REQUEST, stored["warnings"])

    def test_successful_retry_removes_the_refusal_notice(self) -> None:
        """A request that succeeds on a rerun takes its earlier notice down."""
        self.write()
        noted = (
            request.REFUSAL_NOTICE_MARKER
            + "\n> **INFO** The workflow x.\n\nCloses #7\n\nbody"
        )
        with (
            patch.object(github, "graphql", return_value=ADDED),
            patch.object(
                github, "api_object", return_value={"node_id": NODE_ID, "body": noted}
            ),
            patch.object(github, "api_write", return_value={}) as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        method, endpoint, payload = write.call_args.args
        self.assertEqual((method, endpoint), ("PATCH", f"repos/{REPO}/pulls/41"))
        self.assertEqual(payload["body"], "Closes #7\n\nbody")
        self.assertIs(self.stored()["review"]["requested"], True)
        # Without a notice there is nothing to remove, and nothing is written.
        self.write()
        with (
            patch.object(github, "graphql", return_value=ADDED),
            patch.object(
                github,
                "api_object",
                return_value={"node_id": NODE_ID, "body": "Closes #7"},
            ),
            patch.object(github, "api_write") as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        write.assert_not_called()

    def test_a_notice_without_its_blank_line_is_left_alone(self) -> None:
        """A maintainer's edit that removed the boundary costs no description.

        The removal cuts at the first blank line after the marker;
        with none, cutting would empty the body, so nothing is written
        and the result says why.
        """
        self.write()
        edited = (
            request.REFUSAL_NOTICE_MARKER + "\n> **INFO** The workflow x.\nCloses #7"
        )
        with (
            patch.object(github, "graphql", return_value=ADDED),
            patch.object(
                github, "api_object", return_value={"node_id": NODE_ID, "body": edited}
            ),
            patch.object(github, "api_write") as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        write.assert_not_called()
        self.assertTrue(
            any("notice" in w and "left" in w for w in self.stored()["warnings"])
        )

    def test_a_notice_whose_block_grew_is_left_alone(self) -> None:
        """The cut takes the notice alone; a body merged into it stays whole.

        With the separator after INFO gone, the first blank line is a
        later one, and cutting there would take the maintainer's text
        with the notice. The removed block must be the marker and the
        INFO line and nothing else.
        """
        self.write()
        merged = (
            request.REFUSAL_NOTICE_MARKER
            + "\n> **INFO** The workflow x.\nCloses #7\n\nbody"
        )
        with (
            patch.object(github, "graphql", return_value=ADDED),
            patch.object(
                github, "api_object", return_value={"node_id": NODE_ID, "body": merged}
            ),
            patch.object(github, "api_write") as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        write.assert_not_called()
        self.assertTrue(
            any("notice" in w and "left" in w for w in self.stored()["warnings"])
        )

    def test_refusal_notice_is_idempotent_and_best_effort(self) -> None:
        """A body already carrying the notice is left alone; a failed edit is a warning."""
        self.write()
        noted = request.REFUSAL_NOTICE_MARKER + "\n> note\n\nCloses #7"
        with (
            patch.object(github, "graphql", side_effect=github.GitHubError("no")),
            patch.object(
                github, "api_object", return_value={"node_id": NODE_ID, "body": noted}
            ),
            patch.object(github, "api_write") as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        write.assert_not_called()
        self.write()
        with (
            patch.object(github, "graphql", side_effect=github.GitHubError("no")),
            patch.object(github, "api_object", side_effect=github.GitHubError("boom")),
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        warnings = self.stored()["warnings"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("notice" in w for w in warnings))

    def test_refusal_is_logged_and_annotated(self) -> None:
        """The reason reaches the job log, and the run as one ``::warning``."""
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=NOT_ADDED),
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_write", return_value={}),
            patch("builtins.print") as printed,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        lines = [str(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(lines[0], "review request: refused")
        self.assertIn(request.UNVERIFIED_REQUEST, lines)
        annotations = [line for line in lines if line.startswith("::")]
        self.assertEqual(
            annotations,
            [f"::warning title=Copilot review request::{request.UNVERIFIED_REQUEST}"],
        )

    def test_sent_request_lists_the_reviewers_without_an_annotation(self) -> None:
        """On success the log names the reviewers the reply listed."""
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", return_value=ADDED),
            patch.object(github, "api_write", side_effect=AssertionError("no write")),
            patch("builtins.print") as printed,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        lines = [str(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(lines[0], "review request: sent")
        self.assertIn("Bot copilot-pull-request-reviewer", "\n".join(lines))
        self.assertIn("User alice", "\n".join(lines))
        self.assertFalse(any(line.startswith("::") for line in lines))
        self.assertIs(self.stored()["review"]["requested"], True)

    def test_log_names_the_credential_never_the_token(self) -> None:
        """Either outcome logs which credential asked, so a lapsed PAT shows."""
        for credential, reply, outcome in (
            ("review token", ADDED, "sent"),
            ("review token", NOT_ADDED, "refused"),
            ("App token", NOT_ADDED, "refused"),
        ):
            with self.subTest(credential=credential, outcome=outcome):
                self.write()
                with (
                    patch.object(github, "api_object", return_value=PULL),
                    patch.object(github, "graphql", return_value=reply),
                    patch.object(github, "api_list", return_value=[]),
                    patch.object(github, "api_write", return_value={}),
                    patch("builtins.print") as printed,
                ):
                    review.run_request(
                        argparse.Namespace(
                            result=self.path, output=self.path, credential=credential
                        )
                    )
                lines = [str(call.args[0]) for call in printed.call_args_list]
                self.assertEqual(lines[0], f"review request: {outcome}")
                self.assertEqual(lines[1], f"credential: {credential}")

    def test_request_command_takes_a_known_credential_name(self) -> None:
        """The CLI accepts the two names and defaults to the App's token."""
        parser = import_module("publish").build_parser()
        base = ["review-request", "--result", "r.json", "--output", "r.json"]
        self.assertEqual(parser.parse_args(base).credential, "App token")
        named = parser.parse_args([*base, "--credential", "review token"])
        self.assertEqual(named.credential, "review token")
        with (
            patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args([*base, "--credential", "github_pat_x"])

    def test_nothing_published_means_nothing_to_request_or_await(self) -> None:
        """Without a pull request, or in a dry run, both commands are no-ops."""
        for overrides in ({"pull_request_url": None}, {"dry_run": True}):
            with self.subTest(overrides=overrides):
                self.write(**overrides)
                with patch.object(
                    github, "run_gh", side_effect=AssertionError("no network")
                ):
                    review.run_request(
                        argparse.Namespace(
                            result=self.path, output=self.path, credential=APP
                        )
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

    def await_on_a_fake_clock(self, wait_minutes: int) -> Clock:
        """Run ``review-await`` with no review arriving; return the clock it used."""
        clock = Clock()
        real = review.await_review

        def timed(*args: Any, **kwargs: Any) -> Any:
            return real(*args, clock=clock, sleep=clock.sleep, **kwargs)

        with (
            patch.object(review, "await_review", side_effect=timed),
            patch.object(github, "api_object", return_value={"head": {"sha": HEAD}}),
            patch.object(github, "api_list", return_value=[]),
        ):
            review.run_await(
                argparse.Namespace(
                    result=self.path, output=self.path, wait_minutes=wait_minutes
                )
            )
        return clock

    @unittest.expectedFailure
    def test_unrequested_review_gets_a_short_grace(self) -> None:
        """A refused request waits minutes for a maintainer's, not the full wait."""
        self.write(review={"requested": False})
        clock = self.await_on_a_fake_clock(30)
        grace = review.UNREQUESTED_WAIT_MINUTES
        self.assertLess(grace, 30)
        self.assertEqual(clock.now, grace * 60 - review.RESERVE_SECONDS)
        record = self.stored()["review"]
        self.assertEqual(record["verdict"], "pending")
        self.assertIs(record["requested"], False)
        self.assertIn(f"{grace}-minute wait", record["reasons"][0])
        self.assertIn("not requested", record["reasons"][-1])

    @unittest.expectedFailure
    def test_grace_leaves_minutes_to_poll(self) -> None:
        """The grace outlasts the read reserve, so polling still runs."""
        polled = review.UNREQUESTED_WAIT_MINUTES * 60 - review.RESERVE_SECONDS
        self.assertGreaterEqual(polled, 3 * 60)
        self.assertLess(review.UNREQUESTED_WAIT_MINUTES, review.DEFAULT_WAIT_MINUTES)

    @unittest.expectedFailure
    def test_grace_never_lengthens_a_shorter_wait(self) -> None:
        """A caller's wait below the grace stands."""
        self.write(review={"requested": False})
        clock = self.await_on_a_fake_clock(review.UNREQUESTED_WAIT_MINUTES - 1)
        wait = review.UNREQUESTED_WAIT_MINUTES - 1
        self.assertEqual(clock.now, wait * 60 - review.RESERVE_SECONDS)

    def test_requested_or_unrecorded_review_gets_the_full_wait(self) -> None:
        """Only a recorded refusal shortens the wait."""
        for record in ({"requested": True}, {}):
            with self.subTest(review=record):
                self.write(review=record)
                clock = self.await_on_a_fake_clock(30)
                self.assertEqual(clock.now, 30 * 60 - review.RESERVE_SECONDS)
                self.assertEqual(len(self.stored()["review"]["reasons"]), 1)

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
