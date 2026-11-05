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
checks = import_module("check_reads")
reads = import_module("issue_reads")
request = import_module("review_request")
BODIES = import_module("review_fixtures").BODIES

HEAD = "a" * 40
OTHER = "b" * 40
REPO = "org/repo"
APP = "App token"


def copilot(
    review_id: int, commit: str, body: str, submitted: str = "2026-11-01T17:20:00Z"
) -> dict[str, Any]:
    """A review object as the REST listing returns it."""
    return {
        "id": review_id,
        "user": {"login": review.REVIEWER_LOGIN},
        "commit_id": commit,
        "body": body,
        "state": "COMMENTED",
        "submitted_at": submitted,
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
# The mutation selects only ``clientMutationId``; its reply is not read.
MUTATED = {"requestReviewsByLogin": {"clientMutationId": None}}
# The Copilot reviewer bot as an issue event names it.
COPILOT_BOT = {"login": "Copilot", "type": "Bot"}
ALICE = {"login": "alice", "type": "User"}
# The token user's PAT error when the reply resolves a team request.
FORBIDDEN = github.GitHubError("gh: Resource not accessible by personal access token")
BAD_GATEWAY = github.GitHubError("HTTP 502")
EVENTS = f"repos/{REPO}/issues/41/events"
REVIEWS = f"repos/{REPO}/pulls/41/reviews"


def pull_reply(
    reviewers: list[Any], body: str = "Closes #7", teams: int = 1
) -> dict[str, Any]:
    """A REST pull request read, with its pending user, bot and team requests.

    Every organisation pull request carries the ruleset's team request,
    so each reply lists one team unless ``teams`` says otherwise.
    GitHub never lists a pending Copilot request here.
    """
    return {
        "node_id": NODE_ID,
        "body": body,
        "requested_reviewers": reviewers,
        "requested_teams": [{"slug": "releng"}] * teams,
    }


# The pull request as REST returns it, Copilot pending or not.
PULL = pull_reply([ALICE])


def requested(
    event_id: int,
    created: str = "2026-11-01T17:15:20Z",
    *,
    reviewer: dict[str, Any] = COPILOT_BOT,
    kind: str = "review_requested",
) -> dict[str, Any]:
    """An issue event that requests, or withdraws, a reviewer."""
    return {
        "id": event_id,
        "event": kind,
        "created_at": created,
        "requested_reviewer": reviewer,
    }


# An event before the step, and the request the step's mutation adds.
EARLIER = {"id": 90, "event": "labeled", "created_at": "2026-11-01T17:00:00Z"}
REQUESTED = requested(100)


class Reads:
    """An ``api_list`` double: the issue's events read by read, and the reviews.

    Each events read takes the next reply; the last one repeats. The
    reviews answer ``reviews`` first and ``later_reviews``, when given,
    on every read after. A reply that is an exception is raised instead.
    """

    def __init__(
        self,
        *events: list[dict[str, Any]] | Exception,
        reviews: Any = (),
        later_reviews: Any = None,
    ) -> None:
        """Queue the events and reviews replies."""
        self.events = list(events) or [[]]
        self.reviews = [reviews] if later_reviews is None else [reviews, later_reviews]
        self.endpoints: list[str] = []

    def __call__(self, endpoint: str) -> list[dict[str, Any]]:
        """Answer one paginated read."""
        self.endpoints.append(endpoint)
        if endpoint == EVENTS:
            reply = self.events.pop(0) if len(self.events) > 1 else self.events[0]
        elif endpoint == REVIEWS:
            reply = self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]
        else:
            raise AssertionError(f"unexpected read of {endpoint}")
        if isinstance(reply, Exception):
            raise reply
        return list(reply)


def sent() -> Reads:
    """Reads in which the mutation's request event appears at once."""
    return Reads([EARLIER], [EARLIER, REQUESTED])


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
    """One additive ``requestReviewsByLogin``, verified by the issue's events."""

    def setUp(self) -> None:
        """Record the verification backoff instead of waiting it out."""
        patcher = patch.object(request.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def outcome(
        self,
        reads: Reads,
        *,
        pull: dict[str, Any] = PULL,
        mutation: Exception | None = None,
        head: str | None = HEAD,
    ) -> Any:
        """Request a review against the fake reads; return the outcome."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=pull),
            patch.object(github, "graphql", side_effect=mutation, return_value=MUTATED),
            patch.object(github, "api_list", side_effect=reads),
        ):
            return request.request_review(REPO, 41, head)

    def slept(self) -> list[float]:
        """The backoff delays the verification waited."""
        return [call.args[0] for call in self.sleep.call_args_list]

    def test_mutation_is_additive_and_names_the_bot(self) -> None:
        """``union: true``, the suffixed bot login, the node id the pull request read."""
        reads = sent()
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL) as pull,
            patch.object(github, "graphql", return_value=MUTATED) as gql,
            patch.object(github, "api_list", side_effect=reads),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        self.assertIsNone(outcome.warning)
        # One read for the node id, one for the log's reviewer list.
        self.assertEqual(
            [call.args for call in pull.call_args_list],
            [(f"repos/{REPO}/pulls/41",), (f"repos/{REPO}/pulls/41",)],
        )
        # The events are read before the mutation and again after it.
        self.assertEqual(reads.endpoints, [EVENTS, EVENTS])
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
        # REST lists the user and the team, never the pending Copilot.
        self.assertEqual(outcome.reviewers, (("User", "alice"), ("Team", None)))
        self.assertEqual(self.slept(), [])

    def test_mutation_selects_no_reviewer(self) -> None:
        """The reply asks for nothing a team request could make unreadable."""
        for field in ("reviewRequests", "requestedReviewer", "pullRequest {"):
            with self.subTest(field=field):
                self.assertNotIn(field, request.REQUEST_MUTATION)
        self.assertIn("clientMutationId", request.REQUEST_MUTATION)

    def test_event_on_a_retried_read_is_sent(self) -> None:
        """The events lag the request; a later read that shows it counts."""
        reads = Reads([EARLIER], [EARLIER], [EARLIER], [EARLIER, REQUESTED])
        self.assertIsNone(self.outcome(reads).warning)
        self.assertEqual(self.slept(), [2, 4])

    def test_unreadable_reply_with_the_event_is_sent(self) -> None:
        """An applied mutation whose reply the PAT cannot read still counts."""
        self.assertIsNone(self.outcome(sent(), mutation=FORBIDDEN).warning)

    def test_unreadable_reply_without_the_event_is_a_refusal(self) -> None:
        """No event and no head review: the mutation's error stands."""
        warning = self.outcome(Reads([EARLIER]), mutation=FORBIDDEN).warning
        assert warning is not None
        self.assertIn("could not request a Copilot review", warning)
        self.assertIn("Resource not accessible by personal access token", warning)

    def test_neither_event_nor_head_review_is_unverified(self) -> None:
        """Every backoff read shows no request and no review of the head."""
        reads = Reads([EARLIER], reviews=[copilot(3, OTHER, "x")])
        self.assertEqual(self.outcome(reads).warning, request.UNVERIFIED_REQUEST)
        self.assertEqual(self.slept(), list(request.VERIFY_BACKOFF_SECONDS))

    def test_an_earlier_rounds_event_alone_is_a_refusal(self) -> None:
        """A request older than the pre-mutation read, already answered, is not this one."""
        earlier = requested(80, "2026-11-01T16:00:00Z")
        reads = Reads(
            [earlier, EARLIER],
            reviews=[copilot(3, OTHER, "x", "2026-11-01T16:05:00Z")],
        )
        self.assertEqual(self.outcome(reads).warning, request.UNVERIFIED_REQUEST)

    def test_a_request_pending_before_the_step_is_sent(self) -> None:
        """A request no review has answered yet is still pending."""
        earlier = requested(80, "2026-11-01T16:10:00Z")
        reads = Reads(
            [earlier, EARLIER],
            reviews=[copilot(3, OTHER, "x", "2026-11-01T16:05:00Z")],
        )
        self.assertIsNone(self.outcome(reads).warning)
        self.assertEqual(reads.endpoints, [EVENTS, REVIEWS])
        # With no Copilot review at all, the request is pending too.
        self.assertIsNone(self.outcome(Reads([earlier])).warning)

    def test_a_pending_check_that_failed_once_is_retried(self) -> None:
        """A failed reviews read before the mutation is retried on the same events.

        The additive mutation adds no event for a reviewer already
        pending, so only the retried check can prove the request.
        """
        earlier = requested(80, "2026-11-01T16:10:00Z")
        reads = Reads(
            [earlier, EARLIER],
            reviews=BAD_GATEWAY,
            later_reviews=[copilot(3, OTHER, "x", "2026-11-01T16:05:00Z")],
        )
        self.assertIsNone(self.outcome(reads).warning)
        self.assertEqual(reads.endpoints, [EVENTS, REVIEWS, REVIEWS])
        self.assertEqual(self.slept(), [])
        # A retried check that still fails names the failure.
        reads = Reads([earlier, EARLIER], reviews=BAD_GATEWAY)
        warning = self.outcome(reads).warning
        assert warning is not None
        self.assertIn("could not confirm", warning)
        self.assertIn("HTTP 502", warning)
        # A retried check that finds the request answered proves nothing.
        reads = Reads(
            [earlier, EARLIER],
            reviews=BAD_GATEWAY,
            later_reviews=[copilot(3, OTHER, "x", "2026-11-01T16:20:00Z")],
        )
        self.assertEqual(self.outcome(reads).warning, request.UNVERIFIED_REQUEST)

    def test_a_withdrawn_request_is_not_pending(self) -> None:
        """A removal newer than the request leaves nothing pending."""
        reads = Reads(
            [
                requested(80, "2026-11-01T16:10:00Z"),
                requested(85, "2026-11-01T16:12:00Z", kind="review_request_removed"),
            ]
        )
        self.assertEqual(self.outcome(reads).warning, request.UNVERIFIED_REQUEST)

    def test_head_review_without_the_event_counts(self) -> None:
        """A fast review of the head clears the pending request."""
        reads = Reads([EARLIER], reviews=[copilot(3, HEAD, "x")])
        self.assertIsNone(self.outcome(reads).warning)

    def test_mutation_error_is_a_log_safe_warning(self) -> None:
        """A GraphQL error that verification does not overturn is log-safe."""
        failure = github.GitHubError("GraphQL errors: Forbidden::by integration")
        warning = self.outcome(Reads([]), mutation=failure).warning
        assert warning is not None
        self.assertIn("could not request a Copilot review", warning)
        self.assertNotIn("::", warning)

    def test_mutation_error_and_failed_reads_are_a_refusal(self) -> None:
        """When nothing can be read back, the mutation's error is the answer."""
        reads = Reads([EARLIER], github.GitHubError("HTTP 502"), reviews=BAD_GATEWAY)
        warning = self.outcome(reads, mutation=FORBIDDEN).warning
        assert warning is not None
        self.assertIn("could not request a Copilot review", warning)
        self.assertIn("personal access token", warning)

    def test_requested_reviewers_decide_nothing(self) -> None:
        """The REST reviewer list serves the log alone, whatever its shape."""
        replies: list[dict[str, Any]] = [
            {"node_id": NODE_ID},
            {"node_id": NODE_ID, "requested_reviewers": None},
            {"node_id": NODE_ID, "requested_reviewers": [None, COPILOT_BOT]},
            {"node_id": NODE_ID, "requested_reviewers": [COPILOT_BOT]},
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertEqual(
                    self.outcome(Reads([]), pull=reply).warning,
                    request.UNVERIFIED_REQUEST,
                )
                self.assertIsNone(self.outcome(sent(), pull=reply).warning)
        listed = self.outcome(sent(), pull=replies[-1]).reviewers
        self.assertEqual(listed, (("Bot", "Copilot"),))

    def test_failed_node_id_read_is_a_refusal(self) -> None:
        """Without the pull request's node id no request is sent."""
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", side_effect=BAD_GATEWAY),
            patch.object(github, "graphql", side_effect=AssertionError("no send")),
        ):
            outcome = request.request_review(REPO, 41, HEAD)
        assert outcome.warning is not None
        self.assertIn("could not request a Copilot review", outcome.warning)

    def test_reviewer_counts_in_each_form(self) -> None:
        """The events' Bot ``Copilot`` and the reviewer's own login all count."""
        for login in (
            "Copilot",
            "copilot-pull-request-reviewer",
            "Copilot-Pull-Request-Reviewer[bot]",
        ):
            with self.subTest(login=login):
                event = requested(100, reviewer={"login": login, "type": "Bot"})
                outcome = self.outcome(Reads([EARLIER], [EARLIER, event]))
                self.assertIsNone(outcome.warning)

    def test_a_user_named_copilot_does_not_count(self) -> None:
        """Only the Bot ``Copilot`` is the reviewer; a User of that name is not."""
        event = requested(100, reviewer={"login": "copilot", "type": "User"})
        outcome = self.outcome(Reads([EARLIER], [EARLIER, event]))
        self.assertEqual(outcome.warning, request.UNVERIFIED_REQUEST)

    def test_no_review_counts_without_a_head(self) -> None:
        """With no published head, only the request event proves the request."""
        reads = Reads(
            [EARLIER], reviews=[copilot(3, HEAD, "done"), copilot(4, OTHER, "done")]
        )
        outcome = self.outcome(reads, head=None)
        self.assertEqual(outcome.warning, request.UNVERIFIED_REQUEST)
        self.assertIsNone(self.outcome(sent(), head=None).warning)

    def test_a_failed_head_review_read_is_a_warning(self) -> None:
        """A request that cannot be confirmed is never recorded as sent."""
        warning = self.outcome(Reads([EARLIER], reviews=BAD_GATEWAY)).warning
        assert warning is not None
        self.assertIn("could not confirm", warning)

    def test_a_failed_pre_mutation_read_binds_no_event(self) -> None:
        """Without the events before the mutation, no event can be this request's."""
        reads = Reads(BAD_GATEWAY, [EARLIER, REQUESTED])
        warning = self.outcome(reads).warning
        assert warning is not None
        self.assertIn("could not confirm", warning)
        self.assertIn("HTTP 502", warning)
        # A review of the head still proves it.
        reads = Reads(BAD_GATEWAY, reviews=[copilot(3, HEAD, "x")])
        self.assertIsNone(self.outcome(reads).warning)

    def test_a_failed_verification_read_still_takes_a_head_review(self) -> None:
        """The events read fails after the mutation, but a review of the head proves it."""
        reads = Reads([EARLIER], BAD_GATEWAY, reviews=[copilot(3, HEAD, "x")])
        self.assertIsNone(self.outcome(reads).warning)

    def test_a_copilot_review_without_a_time_is_never_pending(self) -> None:
        """An unreadable review time proves no request pending."""
        review_reply = copilot(3, OTHER, "x")
        del review_reply["submitted_at"]
        reads = Reads([requested(80, "2026-11-01T16:10:00Z")], reviews=[review_reply])
        warning = self.outcome(reads).warning
        assert warning is not None
        self.assertIn(request.UNREADABLE_REVIEW, warning)

    def test_reviewers_outside_github_shapes_stay_out_of_the_log(self) -> None:
        """Only GitHub's name shapes are kept from the reply."""
        reply = pull_reply(
            [{"login": "a::b", "type": "Bot::x"}, {"login": "bob", "type": "User"}],
            teams=0,
        )
        outcome = self.outcome(sent(), pull=reply)
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


def check_run(
    identity: int,
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    head: str = HEAD,
) -> dict[str, Any]:
    """A check run as the REST listing returns it."""
    return {
        "id": identity,
        "name": name,
        "head_sha": head,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"slug": "github-actions"},
        "details_url": f"https://github.com/{REPO}/actions/runs/1/job/{identity}",
        "output": {"title": f"{name} title", "summary": "s", "annotations_count": 0},
    }


class FakeHead:
    """The pull request, its review and its check runs, on a fake clock.

    ``runs`` answers each check run listing in turn, the last one
    repeating; an exception is raised instead. ``heads`` answers each
    head read the same way. ``cost`` is how long every read takes.
    """

    def __init__(
        self,
        clock: Clock,
        *runs: list[dict[str, Any]] | Exception,
        reviews: list[dict[str, Any]] | None = None,
        heads: tuple[str, ...] = (HEAD,),
        cost: float = 0.0,
    ) -> None:
        """Queue the replies."""
        self.clock = clock
        self.runs = list(runs) or [[]]
        self.reviews = [] if reviews is None else reviews
        self.heads = list(heads)
        self.cost = cost
        self.listings = 0

    def api_object(self, endpoint: str) -> dict[str, Any]:
        """A head read or a check run listing."""
        self.clock.now += self.cost
        if endpoint == f"repos/{REPO}/pulls/41":
            sha = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            return {"head": {"sha": sha}}
        if f"/commits/{HEAD}/check-runs" in endpoint:
            self.listings += 1
            reply = self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
            if isinstance(reply, Exception):
                raise reply
            return {"total_count": len(reply), "check_runs": reply}
        raise AssertionError(f"unexpected read of {endpoint}")

    def api_list(self, endpoint: str) -> list[dict[str, Any]]:
        """The review listing."""
        self.clock.now += self.cost
        if endpoint != REVIEWS:
            raise AssertionError(f"unexpected read of {endpoint}")
        return list(self.reviews)

    def graphql(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """One page of review threads, none unresolved."""
        self.clock.now += self.cost
        return threads_reply([])

    def run(self, wait: int = 20) -> tuple[Any, int | None, Any]:
        """Await the head's review and checks against the fakes."""
        with (
            patch.object(github, "api_object", side_effect=self.api_object),
            patch.object(github, "api_list", side_effect=self.api_list),
            patch.object(github, "graphql", side_effect=self.graphql),
            patch.object(reads, "api_page", side_effect=AssertionError("no notes")),
        ):
            return review.await_head(
                REPO,
                41,
                HEAD,
                wait_minutes=wait,
                clock=self.clock,
                sleep=self.clock.sleep,
            )


CLEAN = [copilot(7, HEAD, BODIES["v2-green-clean"])]


class AwaitChecksTest(unittest.TestCase):
    """The head's checks, read in what is left of the review wait (#110)."""

    def test_checks_that_pass_before_the_deadline(self) -> None:
        """Runs still going are polled until every one has finished."""
        clock = Clock()
        going = [check_run(1, "lint", status="in_progress"), check_run(2, "test")]
        done = [check_run(1, "lint"), check_run(2, "test", conclusion="skipped")]
        fake = FakeHead(clock, going, going, done, reviews=CLEAN)
        verdict, _, found = fake.run()
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(found.state, "passing")
        self.assertEqual((found.failing, found.unsettled), ([], 0))
        self.assertEqual(fake.listings, 3)
        self.assertEqual(clock.sleeps, [review.POLL_SECONDS] * 2)
        self.assertIsNone(found.reason)

    def test_failing_checks_are_named_and_bounded(self) -> None:
        """Failing runs are recorded by name, cut to the packet's bounds."""
        clock = Clock()
        runs = [
            check_run(n, f"job {n:02}", conclusion="failure")
            for n in range(checks.MAX_CHECKS + 5)
        ]
        runs.append(check_run(99, "x" * (checks.MAX_TEXT + 50), conclusion="timed_out"))
        runs.append(check_run(100, "ok"))
        found = FakeHead(clock, runs, reviews=CLEAN).run()[2]
        self.assertEqual(found.state, "failing")
        self.assertEqual(len(found.failing), checks.MAX_CHECKS)
        self.assertEqual(found.failing[0]["name"], "job 00")
        self.assertEqual(found.failing[0]["conclusion"], "failure")
        self.assertEqual(
            set(found.failing[0]),
            {
                "name",
                "conclusion",
                "app",
                "details_url",
                "title",
                "summary",
                "annotations",
            },
        )
        self.assertGreaterEqual(found.dropped, 6)
        for entry in found.failing:
            self.assertLessEqual(len(entry["name"].encode()), checks.MAX_TEXT)

    def test_a_head_without_check_runs_is_none(self) -> None:
        """No run at all on the head is ``none``, not passing."""
        found = FakeHead(Clock(), [], reviews=CLEAN).run()[2]
        self.assertEqual(found.state, "none")

    def test_only_runs_on_the_published_head_count(self) -> None:
        """A run naming another commit is not this head's."""
        stray = check_run(1, "old", conclusion="failure", head=OTHER)
        found = FakeHead(Clock(), [stray, check_run(2, "ok")], reviews=CLEAN).run()[2]
        self.assertEqual(found.state, "passing")
        self.assertEqual(found.failing, [])

    def test_runs_still_going_at_the_deadline_are_running(self) -> None:
        """The deadline ends the poll; the unfinished runs are counted."""
        clock = Clock()
        runs = [
            check_run(1, "slow", status="queued"),
            check_run(2, "slower", status="in_progress"),
            check_run(3, "broke", conclusion="failure"),
        ]
        found = FakeHead(clock, runs, reviews=CLEAN).run(wait=20)[2]
        self.assertEqual(found.state, "running")
        self.assertEqual(found.unsettled, 2)
        self.assertEqual([e["name"] for e in found.failing], ["broke"])
        self.assertLessEqual(clock.now, 20 * 60)

    def test_a_read_error_is_unknown(self) -> None:
        """A listing the wrapper's retries did not cure proves nothing."""
        failure = github.GitHubError("gh: Server Error (HTTP 502)")
        found = FakeHead(Clock(), failure, reviews=CLEAN).run()[2]
        self.assertEqual(found.state, "unknown")
        self.assertIn("could not read the head's check runs", str(found.reason))

    def test_a_head_that_moves_is_unknown(self) -> None:
        """A push during the wait makes the runs another head's."""
        fake = FakeHead(
            Clock(), [check_run(1, "ok")], reviews=CLEAN, heads=(HEAD, OTHER)
        )
        verdict, _, found = fake.run()
        self.assertEqual(verdict.verdict, "clean")
        self.assertEqual(found.state, "unknown")
        self.assertIn("head moved", str(found.reason))

    def test_no_review_leaves_the_reserve_for_the_checks(self) -> None:
        """A review wait that ran to its reserve still reads the checks."""
        clock = Clock()
        fake = FakeHead(clock, [check_run(1, "lint", status="in_progress")])
        verdict, _, found = fake.run(wait=20)
        self.assertEqual(verdict.verdict, "pending")
        self.assertEqual(found.state, "running")
        self.assertGreaterEqual(fake.listings, 1)
        self.assertLessEqual(clock.now, 20 * 60)

    def test_the_wait_never_grows(self) -> None:
        """Worst-case reads included, review and checks share one bound."""
        for reviews in ([], CLEAN):
            for cost in (0.0, 1.0, review.READ_BUDGET_SECONDS):
                with self.subTest(review=bool(reviews), cost=cost):
                    clock = Clock()
                    going = [check_run(1, "slow", status="in_progress")]
                    FakeHead(clock, going, reviews=reviews, cost=cost).run(wait=20)
                    self.assertLessEqual(clock.now, 20 * 60)

    def test_a_read_that_would_not_fit_is_not_started(self) -> None:
        """With no room for a listing and the head's confirmation, nothing is read."""
        clock = Clock()
        window = review.head_checks.Window(
            2 * review.READ_BUDGET_SECONDS - 1,
            review.READ_BUDGET_SECONDS,
            clock,
            clock.sleep,
        )
        found = review.head_checks.await_checks(REPO, HEAD, lambda: HEAD, window)
        self.assertEqual(found.state, "unknown")
        self.assertIn("no time left", str(found.reason))


class HandlersTest(unittest.TestCase):
    """The publish.py commands that wrap the request and the wait."""

    def setUp(self) -> None:
        """A result.json for a published pull request, in a scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.path = self.root / "result.json"
        patcher = patch.object(request.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7"),
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
            patch.object(github, "api_list", return_value=[]),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7\n\nbody"),
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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_list", return_value=[]),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7"),
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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(
                github,
                "api_list",
                side_effect=Reads(reviews=[copilot(3, OTHER, "done")]),
            ),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7"),
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
            patch.object(github, "graphql", return_value=MUTATED) as gql,
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7"),
            ),
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        gql.assert_called_once()
        self.assertIs(self.stored()["review"]["requested"], True)
        self.write(commits=[])
        with (
            patch.object(github, "graphql", return_value=MUTATED) as gql,
            patch.object(
                github,
                "api_list",
                side_effect=Reads(reviews=[copilot(3, HEAD, "done")]),
            ),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7"),
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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], noted),
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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], "Closes #7"),
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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], edited),
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
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(
                github,
                "api_object",
                return_value=pull_reply([ALICE], merged),
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
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_object", return_value=pull_reply([ALICE], noted)),
            patch.object(github, "api_write") as write,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        write.assert_not_called()
        self.write()
        with (
            patch.object(github, "graphql", side_effect=github.GitHubError("no")),
            patch.object(github, "api_list", return_value=[]),
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
            patch.object(github, "graphql", return_value=MUTATED),
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
        """On success the log names the reviewers the pull request lists."""
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(github, "graphql", return_value=MUTATED),
            patch.object(github, "api_write", side_effect=AssertionError("no write")),
            patch("builtins.print") as printed,
        ):
            review.run_request(
                argparse.Namespace(result=self.path, output=self.path, credential=APP)
            )
        lines = [str(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(lines[0], "review request: sent")
        self.assertIn("Team", "\n".join(lines))
        self.assertIn("User alice", "\n".join(lines))
        self.assertFalse(any(line.startswith("::") for line in lines))
        self.assertIs(self.stored()["review"]["requested"], True)

    def test_log_names_the_credential_never_the_token(self) -> None:
        """Either outcome logs which credential asked, so a lapsed PAT shows."""
        for credential, reads, outcome in (
            ("review token", sent(), "sent"),
            ("review token", Reads(), "refused"),
            ("App token", Reads(), "refused"),
        ):
            with self.subTest(credential=credential, outcome=outcome):
                self.write()
                with (
                    patch.object(github, "api_object", return_value=PULL),
                    patch.object(github, "graphql", return_value=MUTATED),
                    patch.object(github, "api_list", side_effect=reads),
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
        real = review.await_head

        def timed(*args: Any, **kwargs: Any) -> Any:
            return real(*args, clock=clock, sleep=clock.sleep, **kwargs)

        with (
            patch.object(review, "await_head", side_effect=timed),
            patch.object(github, "api_object", return_value={"head": {"sha": HEAD}}),
            patch.object(github, "api_list", return_value=[]),
        ):
            review.run_await(
                argparse.Namespace(
                    result=self.path, output=self.path, wait_minutes=wait_minutes
                )
            )
        return clock

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

    def test_grace_leaves_minutes_to_poll(self) -> None:
        """The grace outlasts the read reserve, so polling still runs."""
        polled = review.UNREQUESTED_WAIT_MINUTES * 60 - review.RESERVE_SECONDS
        self.assertGreaterEqual(polled, 3 * 60)
        self.assertLess(review.UNREQUESTED_WAIT_MINUTES, review.DEFAULT_WAIT_MINUTES)

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

    def test_applied_request_with_an_unreadable_reply_is_sent(self) -> None:
        """The PAT's FORBIDDEN on a team request does not hide a landed request.

        The mutation errors, the issue's events show the request:
        the request is recorded, no notice goes up and the await keeps
        its full window.
        """
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "api_list", side_effect=sent()),
            patch.object(github, "graphql", side_effect=FORBIDDEN),
            patch.object(github, "api_write", side_effect=AssertionError("no write")),
            patch("builtins.print") as printed,
        ):
            review.run_request(
                argparse.Namespace(
                    result=self.path, output=self.path, credential="review token"
                )
            )
        lines = [str(call.args[0]) for call in printed.call_args_list]
        self.assertEqual(
            lines[:2], ["review request: sent", "credential: review token"]
        )
        self.assertEqual(lines[2], "requested reviewers: User alice, Team")
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], True)
        self.assertEqual(stored["warnings"], [])
        clock = self.await_on_a_fake_clock(30)
        self.assertEqual(clock.now, 30 * 60 - review.RESERVE_SECONDS)

    def test_unreadable_reply_and_no_pending_request_is_refused(self) -> None:
        """Without the request event or a head review, the mutation's error stands."""
        self.write()
        with (
            patch.object(github, "run_gh", side_effect=AssertionError("no gh call")),
            patch.object(github, "api_object", return_value=PULL),
            patch.object(github, "graphql", side_effect=FORBIDDEN),
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_write", return_value={}) as write,
        ):
            review.run_request(
                argparse.Namespace(
                    result=self.path, output=self.path, credential="review token"
                )
            )
        stored = self.stored()
        self.assertIs(stored["review"]["requested"], False)
        self.assertTrue(any("personal access token" in w for w in stored["warnings"]))
        body = write.call_args.args[2]["body"]
        self.assertTrue(body.startswith(request.REFUSAL_NOTICE_MARKER))

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

    def test_await_records_the_checks_and_warns_on_a_failure(self) -> None:
        """The checks record names the head; a failure is a warning, not a stop."""
        self.write(review={"requested": True})
        hostile = "lint\n::error::spoof %0A @team"
        runs = [check_run(1, hostile, conclusion="failure"), check_run(2, "ok")]
        fake = FakeHead(Clock(), runs, reviews=CLEAN)
        with (
            patch.object(github, "api_object", side_effect=fake.api_object),
            patch.object(github, "api_list", side_effect=fake.api_list),
            patch.object(github, "graphql", side_effect=fake.graphql),
            patch("builtins.print") as printed,
        ):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        record = self.stored()["checks"]
        self.assertEqual(
            set(record),
            {
                "state",
                "commit_id",
                "failing",
                "dropped",
                "unsettled",
                "read_at",
                "reason",
            },
        )
        self.assertEqual(record["state"], "failing")
        self.assertEqual(record["commit_id"], HEAD)
        self.assertEqual(record["failing"][0]["name"], hostile)
        self.assertRegex(record["read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.stored()["review"]["verdict"], "clean")
        lines = [str(call.args[0]) for call in printed.call_args_list]
        warning = [line for line in lines if line.startswith("::warning")]
        self.assertEqual(len(warning), 1)
        self.assertTrue(warning[0].startswith("::warning title=Checks::"))
        data = warning[0].removeprefix("::warning title=Checks::")
        self.assertNotIn("\n", data)
        self.assertNotIn("::", data)
        self.assertIn("%250A", data)

    def test_passing_checks_raise_no_warning(self) -> None:
        """Only failing runs earn the annotation."""
        self.write(review={"requested": True})
        fake = FakeHead(Clock(), [check_run(1, "ok")], reviews=CLEAN)
        with (
            patch.object(github, "api_object", side_effect=fake.api_object),
            patch.object(github, "api_list", side_effect=fake.api_list),
            patch.object(github, "graphql", side_effect=fake.graphql),
            patch("builtins.print") as printed,
        ):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        self.assertEqual(self.stored()["checks"]["state"], "passing")
        lines = [str(call.args[0]) for call in printed.call_args_list]
        self.assertFalse(any(line.startswith("::warning") for line in lines))

    def test_a_wait_that_never_started_records_unknown_checks(self) -> None:
        """A head that moved before the wait reads no checks at all."""
        self.write(review={"requested": True})
        with (
            patch.object(github, "api_object", return_value={"head": {"sha": OTHER}}),
            patch.object(github, "api_list", side_effect=AssertionError("no wait")),
        ):
            review.run_await(
                argparse.Namespace(result=self.path, output=self.path, wait_minutes=20)
            )
        record = self.stored()["checks"]
        self.assertEqual(record["state"], "unknown")
        self.assertEqual(record["commit_id"], HEAD)
        self.assertIn("head moved", record["reason"])


if __name__ == "__main__":
    unittest.main()
