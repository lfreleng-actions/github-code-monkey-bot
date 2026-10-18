# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Thread reads carry a bot pull request's review threads, typed and bounded."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
threads_of = import_module("thread_reads")

REPO = "org/repo"


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
                    "author": {"login": threads_of.COPILOT_LOGIN},
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
            kept, dropped = threads_of.unresolved_threads(REPO, 41)
        self.assertEqual([t["thread_id"] for t in kept], ["PRRT_1", "PRRT_4"])
        self.assertEqual(kept[0]["comment_id"], 100)
        self.assertEqual(kept[1]["author"], "maintainer")
        self.assertIsNone(kept[1]["line"])
        self.assertEqual(dropped, 0)

    def test_bounds(self) -> None:
        """Over the count or byte budget, threads are dropped and counted."""
        nodes = [thread(id=f"PRRT_{i}") for i in range(threads_of.MAX_THREADS + 3)]
        with patch.object(github, "graphql", return_value=thread_page(nodes)):
            kept, dropped = threads_of.unresolved_threads(REPO, 41)
        self.assertEqual(len(kept), threads_of.MAX_THREADS)
        self.assertEqual(dropped, 3)
        big = thread(
            comments={
                "nodes": [
                    {
                        "databaseId": 1,
                        "body": "x" * (threads_of.MAX_THREAD_BODY + 50),
                        "authorAssociation": "NONE",
                        "author": {"login": threads_of.COPILOT_LOGIN},
                    }
                ]
            }
        )
        with patch.object(github, "graphql", return_value=thread_page([big])):
            kept, _ = threads_of.unresolved_threads(REPO, 41)
        self.assertEqual(len(kept[0]["body"].encode()), threads_of.MAX_THREAD_BODY)

    def test_replies_reach_the_packet(self) -> None:
        """Later comments from Copilot or a maintainer follow the opener, bounded."""
        opener = {
            "databaseId": 100,
            "body": "Fix this.",
            "authorAssociation": "NONE",
            "author": {"login": threads_of.COPILOT_LOGIN},
        }
        replies = [
            {
                "databaseId": 101,
                "body": "Disputed: the lock is held.",
                "authorAssociation": "CONTRIBUTOR",
                "author": {"login": "lf-code-monkey"},
            },
            {
                "databaseId": 102,
                "body": "No, do it.",
                "authorAssociation": "MEMBER",
                "author": {"login": "maintainer"},
            },
            {
                "databaseId": 103,
                "body": "me too",
                "authorAssociation": "NONE",
                "author": {"login": "passer-by"},
            },
            {
                "databaseId": 104,
                "body": "x" * (threads_of.MAX_THREAD_BODY + 50),
                "authorAssociation": "NONE",
                "author": {"login": threads_of.COPILOT_LOGIN},
            },
            {"databaseId": None, "body": 7, "author": None},
        ]
        node = thread(comments={"nodes": [opener, *replies]})
        with patch.object(github, "graphql", return_value=thread_page([node])):
            kept, _ = threads_of.unresolved_threads(REPO, 41)
        self.assertEqual(kept[0]["comment_id"], 100)
        self.assertEqual(kept[0]["body"], "Fix this.")
        self.assertEqual(
            [(r["author"], r["body"][:10]) for r in kept[0]["replies"]],
            [("maintainer", "No, do it."), (threads_of.COPILOT_LOGIN, "x" * 10)],
        )
        self.assertEqual(
            len(kept[0]["replies"][1]["body"].encode()), threads_of.MAX_THREAD_BODY
        )
        self.assertFalse(kept[0]["replies_dropped"])
        self.assertIn("comments(first: 100)", threads_of.THREADS_QUERY)
        self.assertNotIn("replies:", threads_of.THREADS_QUERY)

    def test_threads_carry_who_and_when(self) -> None:
        """The opener and each reply say whether a maintainer wrote it, and when."""
        opener = {
            "databaseId": 100,
            "body": "Fix this.",
            "authorAssociation": "NONE",
            "createdAt": "2026-10-02T00:00:00Z",
            "author": {"login": threads_of.COPILOT_LOGIN},
        }
        answer = {
            "databaseId": 102,
            "body": "No, do it.",
            "authorAssociation": "OWNER",
            "createdAt": "2026-10-04T01:00:00Z",
            "author": {"login": "maintainer"},
        }
        undated = {**answer, "databaseId": 103, "createdAt": 5}
        node = thread(comments={"nodes": [opener, answer, undated]})
        with patch.object(github, "graphql", return_value=thread_page([node])):
            kept, _ = threads_of.unresolved_threads(REPO, 41)
        self.assertIs(kept[0]["maintainer"], False)
        self.assertEqual(kept[0]["created_at"], "2026-10-02T00:00:00Z")
        self.assertEqual(
            [(r["maintainer"], r["created_at"]) for r in kept[0]["replies"]],
            [(True, "2026-10-04T01:00:00Z"), (True, None)],
        )
        self.assertIn("createdAt", threads_of.THREADS_QUERY)

    def test_replies_are_bounded_and_count_toward_the_thread_budget(self) -> None:
        """Over the per-thread cap, the newest replies are kept; bytes count whole."""
        opener = {
            "databaseId": 100,
            "body": "Fix this.",
            "authorAssociation": "NONE",
            "author": {"login": threads_of.COPILOT_LOGIN},
        }
        many = [
            {
                "databaseId": 200 + i,
                "body": f"reply {i}",
                "authorAssociation": "MEMBER",
                "author": {"login": "maintainer"},
            }
            for i in range(threads_of.MAX_THREAD_REPLIES + 2)
        ]
        node = thread(comments={"nodes": [opener, *many]})
        with patch.object(github, "graphql", return_value=thread_page([node])):
            kept, _ = threads_of.unresolved_threads(REPO, 41)
        self.assertEqual(len(kept[0]["replies"]), threads_of.MAX_THREAD_REPLIES)
        self.assertEqual(kept[0]["replies"][-1]["body"], f"reply {len(many) - 1}")
        self.assertTrue(kept[0]["replies_dropped"])
        # The thread budget counts replies: full threads fill it sooner.
        full = [
            {
                "databaseId": 300 + i,
                "body": "y" * threads_of.MAX_THREAD_BODY,
                "authorAssociation": "MEMBER",
                "author": {"login": "maintainer"},
            }
            for i in range(threads_of.MAX_THREAD_REPLIES)
        ]
        per_thread = threads_of.MAX_THREAD_BODY * (threads_of.MAX_THREAD_REPLIES + 1)
        count = threads_of.MAX_THREAD_BYTES // per_thread + 1
        nodes = [
            thread(
                id=f"PRRT_{i}",
                comments={
                    "nodes": [
                        {**opener, "body": "z" * threads_of.MAX_THREAD_BODY},
                        *full,
                    ]
                },
            )
            for i in range(count)
        ]
        with patch.object(github, "graphql", return_value=thread_page(nodes)):
            kept, dropped = threads_of.unresolved_threads(REPO, 41)
        self.assertLess(len(kept), count)
        self.assertEqual(len(kept) + dropped, count)

    def test_a_long_thread_is_read_whole(self) -> None:
        """A maintainer's answer behind a page of passers-by still reaches the packet."""
        opener = {
            "databaseId": 100,
            "body": "Fix this.",
            "authorAssociation": "NONE",
            "createdAt": "2026-10-02T00:00:00Z",
            "author": {"login": threads_of.COPILOT_LOGIN},
        }
        crowd = [
            {
                "databaseId": 200 + i,
                "body": "me too",
                "authorAssociation": "NONE",
                "author": {"login": f"passer-by-{i}"},
            }
            for i in range(99)
        ]
        decision = {
            "databaseId": 400,
            "body": "No, do it.",
            "authorAssociation": "MEMBER",
            "createdAt": "2026-10-04T01:00:00Z",
            "author": {"login": "maintainer"},
        }
        first = thread(
            comments={
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [opener, *crowd],
            }
        )
        later = {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "c2"},
                    "nodes": [*crowd[:5], decision],
                }
            }
        }
        with patch.object(
            github, "graphql", side_effect=[thread_page([first]), later]
        ) as gql:
            kept, _ = threads_of.unresolved_threads(REPO, 41)
        self.assertEqual(
            [(r["author"], r["created_at"]) for r in kept[0]["replies"]],
            [("maintainer", "2026-10-04T01:00:00Z")],
        )
        self.assertFalse(kept[0]["replies_dropped"])
        second = gql.call_args_list[1]
        self.assertEqual(second.args[0], threads_of.THREAD_COMMENTS_QUERY)
        self.assertEqual(second.args[1], {"id": "PRRT_1", "after": "c1"})
        self.assertIs(second.kwargs.get("read"), True)

    def test_a_thread_too_long_to_read_fails_closed(self) -> None:
        """Past the page bound the read raises rather than guess at the replies."""
        page: dict[str, Any] = {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                    "nodes": [],
                }
            }
        }
        first = thread(
            comments={
                "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                "nodes": [
                    {
                        "databaseId": 100,
                        "body": "Fix this.",
                        "authorAssociation": "NONE",
                        "author": {"login": threads_of.COPILOT_LOGIN},
                    }
                ],
            }
        )
        pages = [thread_page([first])] + [page] * (threads_of.MAX_THREAD_PAGES + 1)
        with (
            patch.object(github, "graphql", side_effect=pages),
            self.assertRaises(threads_of.reads.SelectionError),
        ):
            threads_of.unresolved_threads(REPO, 41)

    @unittest.expectedFailure
    def test_a_maintainers_word_is_heard_before_the_budget_cuts(self) -> None:
        """A reply on a thread the budget would drop still counts, and goes first."""
        since = "2026-10-03T00:00:00Z"
        quiet = [thread(id=f"PRRT_{i}") for i in range(threads_of.MAX_THREADS + 2)]
        answered = thread(
            id="PRRT_last",
            comments={
                "nodes": [
                    {
                        "databaseId": 900,
                        "body": "Fix this.",
                        "authorAssociation": "NONE",
                        "createdAt": "2026-10-01T00:00:00Z",
                        "author": {"login": threads_of.COPILOT_LOGIN},
                    },
                    {
                        "databaseId": 901,
                        "body": "No, do it.",
                        "authorAssociation": "MEMBER",
                        "createdAt": "2026-10-04T00:00:00Z",
                        "author": {"login": "maintainer"},
                    },
                ]
            },
        )
        with patch.object(
            github, "graphql", return_value=thread_page([*quiet, answered])
        ):
            packet = threads_of.unresolved_threads(REPO, 41, since)
        self.assertTrue(packet.maintainer_since)
        self.assertEqual(packet.threads[0]["thread_id"], "PRRT_last")
        self.assertEqual(len(packet.threads), threads_of.MAX_THREADS)
        self.assertEqual(packet.dropped, 3)
        # An older reply, or Copilot's own, is no word since the round.
        older = thread(
            comments={
                "nodes": [
                    answered["comments"]["nodes"][0],
                    {
                        **answered["comments"]["nodes"][1],
                        "createdAt": "2026-10-02T00:00:00Z",
                    },
                    {
                        **answered["comments"]["nodes"][1],
                        "databaseId": 902,
                        "authorAssociation": "NONE",
                        "author": {"login": threads_of.COPILOT_LOGIN},
                    },
                ]
            }
        )
        with patch.object(github, "graphql", return_value=thread_page([older])):
            packet = threads_of.unresolved_threads(REPO, 41, since)
        self.assertFalse(packet.maintainer_since)
        # With no watermark, any maintainer word counts; an undated one too.
        with patch.object(github, "graphql", return_value=thread_page([older])):
            self.assertTrue(
                threads_of.unresolved_threads(REPO, 41, None).maintainer_since
            )
