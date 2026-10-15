# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Requesting, awaiting and recording the Copilot review, offline."""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reviews = import_module("copilot_review")
publish = import_module("publish")

REPOSITORY = "owner/repo"
HEAD = "e" * 40
OLD = "f" * 40
GREEN = "## Copilot review overview\n\n### \U0001f7e2 Approval recommended\n"
YELLOW = "### \U0001f7e1 Changes recommended\n"


def result_json(**overrides: Any) -> dict[str, Any]:
    """A result.json after a pull request opened."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "repo-7",
        "repository": REPOSITORY,
        "issue": 7,
        "verdict": "proposed",
        "reasons": [],
        "warnings": [],
        "dry_run": False,
        "mode": "pull-requests",
        "commits": ["d" * 40, HEAD],
        "pull_request_url": f"https://github.com/{REPOSITORY}/pull/8",
    }
    data.update(overrides)
    return data


def review(
    review_id: int, body: str, commit: str = HEAD, login: str = reviews.REVIEWER_LOGIN
) -> dict[str, Any]:
    """One entry of the pull request reviews list."""
    return {
        "id": review_id,
        "user": {"login": login},
        "commit_id": commit,
        "state": "COMMENTED",
        "body": body,
    }


def threads_page(
    resolved: list[bool], more: bool = False, cursor: str | None = None
) -> dict[str, Any]:
    """One GraphQL reviewThreads page."""
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [{"isResolved": flag} for flag in resolved],
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                }
            }
        }
    }


class FakeClock:
    """A monotonic clock that advances only when slept."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """The current time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock."""
        self.sleeps.append(seconds)
        self.now += seconds


class ReviewCase(unittest.TestCase):
    """Fail any test that reaches a real ``gh``."""

    def setUp(self) -> None:
        """Forbid subprocesses and give each test a directory."""
        guard = patch.object(
            github.subprocess, "run", side_effect=AssertionError("unexpected gh")
        )
        guard.start()
        self.addCleanup(guard.stop)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def args(self, result: dict[str, Any]) -> argparse.Namespace:
        """A namespace for either command, with ``result`` written out."""
        path = self.root / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return argparse.Namespace(
            result=path, output=path, wait_seconds=900, interval_seconds=30
        )

    def written(self) -> dict[str, Any]:
        """The result.json the command left."""
        return json.loads((self.root / "result.json").read_text(encoding="utf-8"))


class PullRequestNumberTest(unittest.TestCase):
    """The number comes from a pull request URL on the result's own target."""

    def test_numbers(self) -> None:
        """Only the target's own pull request URL yields a number."""
        cases = (
            (f"https://github.com/{REPOSITORY}/pull/8", 8),
            ("https://github.com/other/repo/pull/8", None),
            (f"https://github.com/{REPOSITORY}/pull/8/files", None),
            (f"https://github.com/{REPOSITORY}/pull/0", None),
            (None, None),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(
                    reviews.pull_request_number(result_json(pull_request_url=url)),
                    expected,
                )


class RequestTest(ReviewCase):
    """The request is best effort and never fails the publish."""

    def test_request_uses_the_at_form(self) -> None:
        """``@copilot`` is the reviewer; the bare login fails (Appendix B)."""
        with patch.object(github, "run_gh", return_value="") as run:
            self.assertIsNone(reviews.request_review(REPOSITORY, 8))
        run.assert_called_once_with(
            ["pr", "edit", "8", "--repo", REPOSITORY, "--add-reviewer", "@copilot"]
        )

    def test_failure_is_a_warning_with_github_detail(self) -> None:
        """GitHub's reason is kept, flattened, as a warning."""
        failure = github.GitHubError("gh: Forbidden\n::error::x (HTTP 403)")
        with patch.object(github, "run_gh", side_effect=failure):
            warning = reviews.request_review(REPOSITORY, 8)
        self.assertIsNotNone(warning)
        self.assertIn("could not request a Copilot review", warning or "")
        self.assertIn("HTTP 403", warning or "")
        self.assertNotIn("\n", warning or "")
        self.assertNotIn("::error::", warning or "")

    def test_run_request_records_pending(self) -> None:
        """After a request the review is pending until one is read."""
        args = self.args(result_json())
        failure = github.GitHubError("gh: nope (HTTP 422)")
        with (
            patch.object(github, "run_gh", side_effect=failure),
            redirect_stdout(io.StringIO()),
        ):
            reviews.run_request(args)
        result = self.written()
        self.assertEqual(result["review"]["verdict"], "pending")
        self.assertEqual(result["verdict"], "proposed")
        self.assertIn("HTTP 422", result["warnings"][0])

    def test_nothing_to_review(self) -> None:
        """No request without a proposed, live pull request on the target."""
        for overrides in (
            {"dry_run": True},
            {"verdict": "publish-failed"},
            {"pull_request_url": None},
            {"pull_request_url": "https://github.com/x/y/pull/1"},
        ):
            with self.subTest(**overrides):
                args = self.args(result_json(**overrides))
                with (
                    patch.object(github, "run_gh") as run,
                    redirect_stdout(io.StringIO()),
                ):
                    reviews.run_request(args)
                run.assert_not_called()
                self.assertNotIn("review", self.written())


class ReadTest(ReviewCase):
    """Reads find Copilot's review of the head and count open threads."""

    def test_review_of_head_filters_author_and_commit(self) -> None:
        """Another author or an earlier head never counts; the latest wins."""
        entries = [
            review(1, GREEN, commit=OLD),
            review(2, GREEN, login="someone"),
            review(3, YELLOW),
            review(4, GREEN),
            {**review(5, YELLOW), "state": "PENDING"},
        ]
        with patch.object(github, "api_list", return_value=entries) as listed:
            found = reviews.review_of_head(REPOSITORY, 8, HEAD)
        self.assertEqual(found, (4, GREEN))
        self.assertIn(f"repos/{REPOSITORY}/pulls/8/reviews", listed.call_args[0][0])

    def test_no_review_of_head(self) -> None:
        """Reviews of other commits leave nothing."""
        with patch.object(github, "api_list", return_value=[review(1, GREEN, OLD)]):
            self.assertIsNone(reviews.review_of_head(REPOSITORY, 8, HEAD))

    def test_unresolved_threads_follow_pages(self) -> None:
        """Every page counts; resolved threads do not."""
        pages = [
            threads_page([True, False], more=True, cursor="c1"),
            threads_page([False, True, False]),
        ]
        with patch.object(github, "graphql", side_effect=pages) as query:
            self.assertEqual(reviews.unresolved_threads(REPOSITORY, 8), 3)
        self.assertEqual(query.call_args_list[1][0][1]["after"], "c1")
        self.assertTrue(all(c.kwargs["read"] for c in query.call_args_list))

    def test_malformed_thread_reply_is_an_error(self) -> None:
        """A reply without the expected shape is a read failure."""
        with (
            patch.object(github, "graphql", return_value={"repository": None}),
            self.assertRaises(github.GitHubError),
        ):
            reviews.unresolved_threads(REPOSITORY, 8)

    def test_published_head(self) -> None:
        """The replayed head, or the pull request's head for an adopted branch."""
        with patch.object(github, "api_object") as read:
            self.assertEqual(reviews.published_head(result_json(), REPOSITORY, 8), HEAD)
        read.assert_not_called()
        with patch.object(
            github, "api_object", return_value={"head": {"sha": OLD}}
        ) as read:
            head = reviews.published_head(result_json(commits=[]), REPOSITORY, 8)
        self.assertEqual(head, OLD)
        read.assert_called_once_with(f"repos/{REPOSITORY}/pulls/8")


class AwaitTest(ReviewCase):
    """The wait is bounded; no review is pending, never clean."""

    def test_review_arrives_on_a_later_poll(self) -> None:
        """The first review of the head is classified with its threads."""
        clock = FakeClock()
        with (
            patch.object(
                github,
                "api_list",
                side_effect=[[], [review(9, YELLOW, OLD)], [review(11, GREEN)]],
            ),
            patch.object(reviews, "unresolved_threads", return_value=0),
        ):
            record, problem = reviews.await_review(
                REPOSITORY, 8, HEAD, 900, 30, clock=clock, sleep=clock.sleep
            )
        self.assertIsNone(problem)
        self.assertEqual(record["verdict"], "clean")
        self.assertEqual(record["review_id"], 11)
        self.assertEqual(record["commit_id"], HEAD)
        self.assertEqual(clock.sleeps, [30, 30])

    def test_threads_make_findings(self) -> None:
        """Unresolved threads on the pull request outweigh a green body."""
        with (
            patch.object(github, "api_list", return_value=[review(4, GREEN)]),
            patch.object(reviews, "unresolved_threads", return_value=2),
        ):
            record, _ = reviews.await_review(REPOSITORY, 8, HEAD, 900, 30)
        self.assertEqual(record["verdict"], "findings")
        self.assertEqual(record["unresolved_threads"], 2)

    def test_no_review_is_pending_after_the_wait(self) -> None:
        """The deadline holds: the wait never runs past it."""
        clock = FakeClock()
        with patch.object(github, "api_list", return_value=[]):
            record, problem = reviews.await_review(
                REPOSITORY, 8, HEAD, 100, 30, clock=clock, sleep=clock.sleep
            )
        self.assertIsNone(problem)
        self.assertEqual(record["verdict"], "pending")
        self.assertEqual(record["commit_id"], HEAD)
        self.assertIsNone(record["review_id"])
        self.assertEqual(clock.now, 100)

    def test_failing_reads_are_pending_with_a_warning(self) -> None:
        """A read that keeps failing is retried, then recorded, never clean."""
        clock = FakeClock()
        with patch.object(
            github, "api_list", side_effect=github.GitHubError("boom (HTTP 502)")
        ):
            record, problem = reviews.await_review(
                REPOSITORY, 8, HEAD, 60, 30, clock=clock, sleep=clock.sleep
            )
        self.assertEqual(record["verdict"], "pending")
        self.assertIn("HTTP 502", problem or "")

    def test_run_await_records_the_review(self) -> None:
        """``review-await`` writes the classified record into result.json."""
        args = self.args(result_json(review={"verdict": "pending"}))
        with (
            patch.object(github, "api_list", return_value=[review(4, YELLOW)]),
            patch.object(reviews, "unresolved_threads", return_value=1),
            redirect_stdout(io.StringIO()) as out,
        ):
            reviews.run_await(args)
        result = self.written()
        self.assertEqual(
            result["review"],
            {
                "verdict": "findings",
                "assessment": "changes recommended",
                "unresolved_threads": 1,
                "suppressed": 0,
                "reasons": ["changes recommended", "1 unresolved review thread(s)"],
                "review_id": 4,
                "commit_id": HEAD,
            },
        )
        self.assertEqual(out.getvalue(), "review: findings\n")

    def test_run_await_without_a_head_is_pending(self) -> None:
        """An unreadable head leaves the review pending with a warning."""
        args = self.args(result_json(commits=[]))
        with (
            patch.object(
                github, "api_object", side_effect=github.GitHubError("gone (HTTP 404)")
            ),
            redirect_stdout(io.StringIO()),
        ):
            reviews.run_await(args)
        result = self.written()
        self.assertEqual(result["review"]["verdict"], "pending")
        self.assertIn("HTTP 404", result["warnings"][0])


class CliTest(ReviewCase):
    """``publish.py`` dispatches both review commands."""

    def test_commands_are_wired(self) -> None:
        """Each command reaches its handler with its options parsed."""
        parser = publish.build_parser()
        path = str(self.root / "result.json")
        request = parser.parse_args(
            ["review-request", "--result", path, "--output", path]
        )
        self.assertIs(request.handler, reviews.run_request)
        waiter = parser.parse_args(
            ["review-await", "--result", path, "--output", path, "--wait-seconds", "5"]
        )
        self.assertIs(waiter.handler, reviews.run_await)
        self.assertEqual((waiter.wait_seconds, waiter.interval_seconds), (5, 30))


if __name__ == "__main__":
    unittest.main()
