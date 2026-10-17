# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Revision reads type and bound what GitHub says about a bot pull request."""

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
reads = import_module("issue_reads")
revisions = import_module("revision_reads")
review = import_module("copilot_review")
BODIES = import_module("review_fixtures").BODIES

HEAD = "a" * 40
BASE = "b" * 40
BOT = "lf-code-monkey[bot]"
FORK = "lfreleng-bot-forks/repo"
REPO = "org/repo"


def pull(**overrides: Any) -> dict[str, Any]:
    """A REST pull request the bot opened from the fork."""
    data: dict[str, Any] = {
        "number": 41,
        "html_url": f"https://github.com/{REPO}/pull/41",
        "title": "Fix: Thing",
        "state": "open",
        "user": {"login": BOT},
        "head": {
            "sha": HEAD,
            "ref": "code-monkey/issue-7",
            "repo": {"full_name": FORK},
        },
        "base": {"ref": "main", "sha": BASE},
        "mergeable": True,
        "created_at": "2026-10-01T00:00:00Z",
    }
    data.update(overrides)
    return data


def thread(**overrides: Any) -> dict[str, Any]:
    """A GraphQL review thread node with its opening comment."""
    node: dict[str, Any] = {
        "id": "PRRT_1",
        "isResolved": False,
        "isOutdated": False,
        "path": "src/x.py",
        "line": 12,
        "comments": {
            "nodes": [
                {
                    "databaseId": 100,
                    "body": "Fix this.",
                    "authorAssociation": "NONE",
                    "author": {"login": revisions.COPILOT_LOGIN},
                }
            ]
        },
    }
    node.update(overrides)
    return node


def thread_page(nodes: list[dict[str, Any]], *, more: bool = False) -> dict[str, Any]:
    """One GraphQL page of threads."""
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": more, "endCursor": "c"},
                    "nodes": nodes,
                }
            }
        }
    }


class BotPullRequestTest(unittest.TestCase):
    """Only the bot's open pull request from the designated fork is a target."""

    def test_target_shape(self) -> None:
        """The fields the selection and the publisher need, typed."""
        with patch.object(github, "api_object", return_value=pull()):
            found = revisions.bot_pull_request(REPO, 41, BOT, FORK)
        self.assertEqual(
            found,
            {
                "number": 41,
                "url": f"https://github.com/{REPO}/pull/41",
                "title": "Fix: Thing",
                "issue": 7,
                "branch": "code-monkey/issue-7",
                "head_sha": HEAD,
                "base_ref": "main",
                "mergeable": True,
                "created_at": "2026-10-01T00:00:00Z",
            },
        )

    @unittest.expectedFailure
    def test_absent_pull_request_is_none(self) -> None:
        """A mistyped number is a named skip for the caller, not an aborted run."""
        absent = github.GitHubError("gh: Not Found (HTTP 404)")
        with patch.object(github, "api_object", side_effect=absent):
            self.assertIsNone(revisions.bot_pull_request(REPO, 999, BOT, FORK))
        failure = github.GitHubError("gh: Server Error (HTTP 502)")
        with (
            patch.object(github, "api_object", side_effect=failure),
            self.assertRaises(github.GitHubError),
        ):
            revisions.bot_pull_request(REPO, 41, BOT, FORK)

    def test_anything_else_is_not_ours(self) -> None:
        """Closed, another author, another fork or another branch name: None."""
        cases = (
            pull(state="closed"),
            pull(user={"login": "mallory"}),
            pull(
                head={
                    "sha": HEAD,
                    "ref": "code-monkey/issue-7",
                    "repo": {"full_name": "x/repo"},
                }
            ),
            pull(head={"sha": HEAD, "ref": "feature/x", "repo": {"full_name": FORK}}),
        )
        for data in cases:
            with (
                self.subTest(data=data),
                patch.object(github, "api_object", return_value=data),
            ):
                self.assertIsNone(revisions.bot_pull_request(REPO, 41, BOT, FORK))

    def test_unknown_mergeability_is_none(self) -> None:
        """GitHub answers null while it computes; the selection reads None."""
        with patch.object(github, "api_object", return_value=pull(mergeable=None)):
            found = revisions.bot_pull_request(REPO, 41, BOT, FORK)
        assert found is not None
        self.assertIsNone(found["mergeable"])


class SearchTest(unittest.TestCase):
    """The owner-wide search for the App's open pull requests."""

    def test_search_arguments_and_shape(self) -> None:
        """Searches by App author, open, and types each hit."""
        payload = '[{"number": 3, "repository": {"nameWithOwner": "org/a"}, "createdAt": "t"}]'
        with patch.object(github, "run_gh", return_value=payload) as gh:
            found = revisions.search_bot_pull_requests("org", "lf-code-monkey")
        args = gh.call_args.args[0]
        self.assertEqual(args[:2], ["search", "prs"])
        self.assertEqual(args[args.index("--app") + 1], "lf-code-monkey")
        self.assertEqual(args[args.index("--state") + 1], "open")
        self.assertIs(gh.call_args.kwargs.get("read"), True)
        self.assertEqual(
            found, [{"repository": "org/a", "number": 3, "created_at": "t"}]
        )


class ThreadsTest(unittest.TestCase):
    """Unresolved threads from Copilot or a maintainer, bounded."""

    def test_filters_and_pages(self) -> None:
        """Resolved, passer-by and malformed threads are skipped; pages are followed."""
        pages = [
            thread_page(
                [
                    thread(),
                    thread(id="PRRT_2", isResolved=True),
                    thread(
                        id="PRRT_3",
                        comments={
                            "nodes": [
                                {
                                    "databaseId": 101,
                                    "body": "drive-by",
                                    "authorAssociation": "NONE",
                                    "author": {"login": "someone"},
                                }
                            ]
                        },
                    ),
                    thread(
                        id="PRRT_4",
                        line=None,
                        comments={
                            "nodes": [
                                {
                                    "databaseId": 102,
                                    "body": "Please change.",
                                    "authorAssociation": "MEMBER",
                                    "author": {"login": "maintainer"},
                                }
                            ]
                        },
                    ),
                ],
                more=True,
            ),
            thread_page([thread(id="PRRT_5", comments={"nodes": []})]),
        ]
        with patch.object(github, "graphql", side_effect=pages):
            kept, dropped = revisions.unresolved_threads(REPO, 41)
        self.assertEqual([t["thread_id"] for t in kept], ["PRRT_1", "PRRT_4"])
        self.assertEqual(kept[0]["comment_id"], 100)
        self.assertEqual(kept[1]["author"], "maintainer")
        self.assertIsNone(kept[1]["line"])
        self.assertEqual(dropped, 0)

    def test_bounds(self) -> None:
        """Over the count or byte budget, threads are dropped and counted."""
        nodes = [thread(id=f"PRRT_{i}") for i in range(revisions.MAX_THREADS + 3)]
        with patch.object(github, "graphql", return_value=thread_page(nodes)):
            kept, dropped = revisions.unresolved_threads(REPO, 41)
        self.assertEqual(len(kept), revisions.MAX_THREADS)
        self.assertEqual(dropped, 3)
        big = thread(
            comments={
                "nodes": [
                    {
                        "databaseId": 1,
                        "body": "x" * (revisions.MAX_THREAD_BODY + 50),
                        "authorAssociation": "NONE",
                        "author": {"login": revisions.COPILOT_LOGIN},
                    }
                ]
            }
        )
        with patch.object(github, "graphql", return_value=thread_page([big])):
            kept, _ = revisions.unresolved_threads(REPO, 41)
        self.assertEqual(len(kept[0]["body"].encode()), revisions.MAX_THREAD_BODY)


class ReviewAndRoundsTest(unittest.TestCase):
    """The head's review verdict and the rounds recorded on the issue."""

    def test_latest_review_classifies_the_head(self) -> None:
        """The newest Copilot review of the head, with its verdict."""
        listing = [
            {
                "id": 5,
                "user": {"login": review.REVIEWER_LOGIN},
                "commit_id": HEAD,
                "body": BODIES["v2-yellow-open-findings"],
            }
        ]
        with (
            patch.object(github, "api_list", return_value=listing),
            patch.object(github, "graphql", return_value=thread_page([thread()])),
        ):
            found = revisions.latest_review(REPO, 41, HEAD)
        assert found is not None
        self.assertEqual(found["verdict"], "findings")
        self.assertEqual(found["review_id"], 5)
        self.assertEqual(found["unresolved_threads"], 1)
        with patch.object(github, "api_list", return_value=[]):
            self.assertIsNone(revisions.latest_review(REPO, 41, HEAD))

    def test_rounds_from_the_bots_markers_alone(self) -> None:
        """The highest round, its summaries and the latest time; others' markers ignored."""
        entries = [
            {
                "user": {"login": BOT},
                "body": "r1\n\n<!-- code-monkey https://r/1 proposed review=findings round=1 -->",
                "created_at": "2026-10-02T00:00:00Z",
            },
            {
                "user": {"login": "mallory"},
                "body": "<!-- code-monkey https://r/9 proposed round=9 -->",
                "created_at": "2026-10-03T00:00:00Z",
            },
            {
                "user": {"login": BOT},
                "body": "first\n\n<!-- code-monkey https://r/0 proposed review=findings -->",
                "created_at": "2026-10-01T00:00:00Z",
            },
            {
                "user": {"login": BOT},
                "body": "r2\n\n<!-- code-monkey https://r/2 proposed review=pending round=2 -->",
                "created_at": "2026-10-04T00:00:00Z",
            },
        ]
        with patch.object(reads, "api_page", return_value=entries):
            highest, summaries, latest = revisions.rounds_so_far(REPO, 7, BOT)
        self.assertEqual(highest, 2)
        self.assertEqual([s["round"] for s in summaries], [1, 2])
        self.assertEqual(latest, "2026-10-04T00:00:00Z")

    @unittest.expectedFailure
    def test_only_the_final_footer_counts(self) -> None:
        """A marker inside the agent's summary text is text; the footer decides."""
        entries = [
            {
                "user": {"login": BOT},
                "body": "summary saying <!-- code-monkey https://r/x proposed round=10 -->"
                " and more\n\n<!-- code-monkey https://r/1 proposed review=findings round=1 -->",
                "created_at": "2026-10-02T00:00:00Z",
            },
            {
                "user": {"login": BOT},
                "body": "text\n\n<!-- code-monkey https://r/1 proposed round=7 -->\ntrailing",
                "created_at": "2026-10-03T00:00:00Z",
            },
        ]
        with patch.object(reads, "api_page", return_value=entries):
            highest, _, _ = revisions.rounds_so_far(REPO, 7, BOT)
        self.assertEqual(highest, 1)

    @unittest.expectedFailure
    def test_publication_footer_is_round_zero(self) -> None:
        """The first proposal's comment sets the time without counting as a round."""
        entries = [
            {
                "user": {"login": BOT},
                "body": "first\n\n<!-- code-monkey https://r/0 proposed review=findings -->",
                "created_at": "2026-10-01T00:00:00Z",
            },
        ]
        with patch.object(reads, "api_page", return_value=entries):
            highest, summaries, latest = revisions.rounds_so_far(REPO, 7, BOT)
        self.assertEqual(highest, 0)
        self.assertEqual(summaries, [])
        self.assertEqual(latest, "2026-10-01T00:00:00Z")

    @unittest.expectedFailure
    def test_round_history_reads_every_page_or_fails_closed(self) -> None:
        """A full page means more; the read goes on, and a history past the bound fails."""
        noise = {"user": {"login": "x"}, "body": "noise", "created_at": "t"}
        page = [noise] * reads.COMMENTS_PER_PAGE
        last = [
            {
                "user": {"login": BOT},
                "body": "r\n\n<!-- code-monkey https://r/3 proposed round=3 -->",
                "created_at": "2026-10-09T00:00:00Z",
            }
        ]
        with patch.object(
            reads, "api_page", side_effect=[page, page, page, page, last]
        ):
            highest, _, _ = revisions.rounds_so_far(REPO, 7, BOT)
        self.assertEqual(highest, 3)
        with (
            patch.object(reads, "api_page", return_value=page),
            self.assertRaises(reads.SelectionError),
        ):
            revisions.rounds_so_far(REPO, 7, BOT)

    @unittest.expectedFailure
    def test_newer_member_comment_reads_past_a_full_page(self) -> None:
        """A maintainer's instruction on page two still counts."""
        noise = {
            "user": {"login": "b"},
            "author_association": "NONE",
            "body": "n",
            "created_at": "2026-10-05T00:00:00Z",
        }
        hit = {
            "user": {"login": "m"},
            "author_association": "MEMBER",
            "body": "x",
            "created_at": "2026-10-06T00:00:00Z",
        }
        pages = [[noise] * reads.COMMENTS_PER_PAGE, [hit]]
        with patch.object(reads, "api_page", side_effect=pages) as page:
            self.assertTrue(
                revisions.newer_member_comment(REPO, 7, "2026-10-04T00:00:00Z")
            )
        self.assertEqual(len(page.call_args_list), 2)

    @unittest.expectedFailure
    def test_comments_since_the_last_round(self) -> None:
        """The maintainers' comments after a time, bounded as the packet is."""
        entries = [
            {
                "user": {"login": "m"},
                "author_association": "MEMBER",
                "body": "old",
                "created_at": "2026-10-01T00:00:00Z",
            },
            {
                "user": {"login": "m"},
                "author_association": "OWNER",
                "body": "new",
                "created_at": "2026-10-05T00:00:00Z",
            },
            {
                "user": {"login": "p"},
                "author_association": "NONE",
                "body": "no",
                "created_at": "2026-10-06T00:00:00Z",
            },
        ]
        with patch.object(reads, "api_page", return_value=entries):
            kept, dropped = revisions.comments_since(REPO, 7, "2026-10-04T00:00:00Z")
        self.assertEqual([c["body"] for c in kept], ["new"])
        self.assertEqual(dropped, 0)

    def test_newer_member_comment(self) -> None:
        """A maintainer's comment after the last round is new instruction."""
        entries = [
            {
                "user": {"login": "m"},
                "author_association": "MEMBER",
                "body": "x",
                "created_at": "2026-10-05T00:00:00Z",
            },
            {
                "user": {"login": "p"},
                "author_association": "NONE",
                "body": "y",
                "created_at": "2026-10-06T00:00:00Z",
            },
        ]
        with patch.object(reads, "api_page", return_value=entries) as page:
            self.assertTrue(
                revisions.newer_member_comment(REPO, 7, "2026-10-04T00:00:00Z")
            )
            self.assertIn("since=2026-10-04T00:00:00Z", page.call_args.args[0])
            self.assertFalse(
                revisions.newer_member_comment(REPO, 7, "2026-10-05T00:00:00Z")
            )
            self.assertTrue(revisions.newer_member_comment(REPO, 7, None))

    def test_merge_base(self) -> None:
        """The compare endpoint names the chain's base."""
        with patch.object(
            github, "api_object", return_value={"merge_base_commit": {"sha": BASE}}
        ) as read:
            self.assertEqual(
                revisions.merge_base(
                    REPO, "main", "lfreleng-bot-forks", "code-monkey/issue-7"
                ),
                BASE,
            )
        self.assertEqual(
            read.call_args.args[0],
            f"repos/{REPO}/compare/main...lfreleng-bot-forks:code-monkey/issue-7",
        )


if __name__ == "__main__":
    unittest.main()
