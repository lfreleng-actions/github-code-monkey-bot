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


def unpack(packet: Any) -> tuple[list[dict[str, Any]], int]:
    """The packet's threads and dropped count, for the older assertions."""
    return packet.threads, packet.dropped


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
            kept, dropped = unpack(threads_of.unresolved_threads(REPO, 41))
        # With no watermark the maintainer's thread goes first.
        self.assertEqual([t["thread_id"] for t in kept], ["PRRT_4", "PRRT_1"])
        self.assertEqual(kept[1]["comment_id"], 100)
        self.assertEqual(kept[0]["author"], "maintainer")
        self.assertIsNone(kept[0]["line"])
        self.assertEqual(dropped, 0)

    def test_bounds(self) -> None:
        """Over the count or byte budget, threads are dropped and counted."""
        nodes = [thread(id=f"PRRT_{i}") for i in range(threads_of.MAX_THREADS + 3)]
        with patch.object(github, "graphql", return_value=thread_page(nodes)):
            kept, dropped = unpack(threads_of.unresolved_threads(REPO, 41))
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
            kept, _ = unpack(threads_of.unresolved_threads(REPO, 41))
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
            kept, _ = unpack(threads_of.unresolved_threads(REPO, 41))
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
            kept, _ = unpack(threads_of.unresolved_threads(REPO, 41))
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
            kept, _ = unpack(threads_of.unresolved_threads(REPO, 41))
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
            kept, dropped = unpack(threads_of.unresolved_threads(REPO, 41))
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
            kept, _ = unpack(threads_of.unresolved_threads(REPO, 41))
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

    def test_a_maintainers_word_survives_a_run_of_later_replies(self) -> None:
        """Ten of Copilot's replies after a maintainer's neither hide nor drop it."""
        since = "2026-10-03T00:00:00Z"
        opener = {
            "databaseId": 100,
            "body": "Fix this.",
            "authorAssociation": "NONE",
            "createdAt": "2026-10-01T00:00:00Z",
            "author": {"login": threads_of.COPILOT_LOGIN},
        }
        decision = {
            "databaseId": 101,
            "body": "No, do it.",
            "authorAssociation": "MEMBER",
            "createdAt": "2026-10-04T00:00:00Z",
            "author": {"login": "maintainer"},
        }
        later = [
            {
                "databaseId": 200 + i,
                "body": f"still {i}",
                "authorAssociation": "NONE",
                "createdAt": f"2026-10-05T00:00:{i:02d}Z",
                "author": {"login": threads_of.COPILOT_LOGIN},
            }
            for i in range(threads_of.MAX_THREAD_REPLIES)
        ]
        node = thread(comments={"nodes": [opener, decision, *later]})
        with patch.object(github, "graphql", return_value=thread_page([node])):
            packet = threads_of.unresolved_threads(REPO, 41, since)
        self.assertTrue(packet.maintainer_since)
        kept = packet.threads[0]
        self.assertTrue(kept["replies_dropped"])
        self.assertEqual(len(kept["replies"]), threads_of.MAX_THREAD_REPLIES)
        # The maintainer's reply is kept in place of the oldest of
        # Copilot's, and the order stays the thread's.
        self.assertEqual(kept["replies"][0]["author"], "maintainer")
        self.assertEqual(kept["replies"][1]["body"], "still 1")
        self.assertEqual(kept["replies"][-1]["body"], "still 9")
        # Before the decision, the watermark hears nothing.
        with patch.object(github, "graphql", return_value=thread_page([node])):
            packet = threads_of.unresolved_threads(REPO, 41, "2026-10-04T01:00:00Z")
        self.assertFalse(packet.maintainer_since)


def comment(
    database_id: int, created: str, login: str, association: str = "NONE", **extra: Any
) -> dict[str, Any]:
    """One GraphQL review thread comment."""
    return {
        "databaseId": database_id,
        "body": f"comment {database_id}",
        "authorAssociation": association,
        "createdAt": created,
        "author": {"login": login, **extra},
    }


BOT = "lf-code-monkey[bot]"


class ResolvedThreadsTest(unittest.TestCase):
    """A maintainer's word on a resolved thread is heard (#234)."""

    since = "2026-10-03T00:00:00Z"
    opener = comment(100, "2026-10-01T00:00:00Z", threads_of.COPILOT_LOGIN)

    def read(self, *nodes: dict[str, Any], since: str | None = None) -> Any:
        """The packet the reads build from one page of threads."""
        with patch.object(github, "graphql", return_value=thread_page(list(nodes))):
            return threads_of.unresolved_threads(
                REPO, 41, since or self.since, frozenset({"review-app"}), BOT
            )

    @unittest.expectedFailure
    def test_a_maintainer_reply_after_the_watermark_is_carried(self) -> None:
        """Carried, marked resolved and heard; an older reply is neither."""
        reply = comment(101, "2026-10-04T00:00:00Z", "maintainer", "MEMBER")
        packet = self.read(
            thread(
                id="PRRT_r", isResolved=True, comments={"nodes": [self.opener, reply]}
            )
        )
        self.assertTrue(packet.maintainer_since)
        self.assertEqual([t["thread_id"] for t in packet.threads], ["PRRT_r"])
        self.assertIs(packet.threads[0]["resolved"], True)
        self.assertEqual(packet.threads[0]["replies"][0]["author"], "maintainer")
        self.assertEqual((packet.dropped, packet.resolved), (0, 1))
        older = {**reply, "createdAt": "2026-10-02T00:00:00Z"}
        packet = self.read(
            thread(
                id="PRRT_r", isResolved=True, comments={"nodes": [self.opener, older]}
            )
        )
        self.assertFalse(packet.maintainer_since)
        self.assertEqual((packet.threads, packet.dropped), ([], 0))

    @unittest.expectedFailure
    def test_reviewer_replies_on_a_resolved_thread_stay_out(self) -> None:
        """Copilot's and a review App's replies are not a maintainer's word."""
        replies = [
            comment(101, "2026-10-04T00:00:00Z", threads_of.COPILOT_LOGIN),
            comment(102, "2026-10-04T00:00:01Z", "review-app", __typename="Bot"),
        ]
        packet = self.read(
            thread(),
            thread(
                id="PRRT_r",
                isResolved=True,
                comments={"nodes": [self.opener, *replies]},
            ),
        )
        self.assertFalse(packet.maintainer_since)
        self.assertEqual([t["thread_id"] for t in packet.threads], ["PRRT_1"])
        self.assertIs(packet.threads[0]["resolved"], False)
        self.assertEqual((packet.dropped, packet.resolved), (0, 0))

    @unittest.expectedFailure
    def test_resolved_threads_share_the_bounds_and_go_first(self) -> None:
        """A heard resolved thread leads; a quiet one costs the bounds nothing."""
        reply = comment(101, "2026-10-04T00:00:00Z", "maintainer", "MEMBER")
        quiet = [thread(id=f"PRRT_{i}") for i in range(threads_of.MAX_THREADS)]
        packet = self.read(
            *quiet,
            thread(id="PRRT_quiet", isResolved=True),
            thread(
                id="PRRT_r", isResolved=True, comments={"nodes": [self.opener, reply]}
            ),
        )
        self.assertEqual(packet.threads[0]["thread_id"], "PRRT_r")
        self.assertEqual(len(packet.threads), threads_of.MAX_THREADS)
        self.assertNotIn("PRRT_quiet", [t["thread_id"] for t in packet.threads])
        self.assertEqual((packet.dropped, packet.resolved), (1, 1))

    @unittest.expectedFailure
    def test_the_368_sequence_is_carried(self) -> None:
        """Bot reply, resolve, then a maintainer's reply forty seconds later."""
        since = "2026-11-22T23:20:00Z"
        nodes = [
            comment(100, "2026-11-22T22:00:00Z", threads_of.COPILOT_LOGIN),
            comment(101, "2026-11-22T23:27:48Z", "lf-code-monkey", __typename="Bot"),
            comment(102, "2026-11-22T23:28:28Z", "maintainer", "MEMBER"),
        ]
        with patch.object(
            github,
            "graphql",
            return_value=thread_page(
                [thread(id="PRRT_368", isResolved=True, comments={"nodes": nodes})]
            ),
        ):
            packet = threads_of.unresolved_threads(REPO, 368, since)
        self.assertTrue(packet.maintainer_since)
        carried = packet.threads[0]
        self.assertEqual(
            (carried["thread_id"], carried["resolved"]), ("PRRT_368", True)
        )
        self.assertEqual([r["author"] for r in carried["replies"]], ["maintainer"])

    @unittest.expectedFailure
    def test_a_reply_the_bounds_left_out_returns_after_publication(self) -> None:
        """Overflow, a published round, a later scan: the left-out reply is heard."""
        answer = comment(
            101, "2026-10-03T00:00:10Z", "lf-code-monkey", __typename="Bot"
        )
        word = comment(102, "2026-10-04T00:00:00Z", "maintainer", "MEMBER")
        answered = [
            thread(
                id=f"PRRT_{i}",
                isResolved=True,
                comments={"nodes": [self.opener, answer, word]},
            )
            for i in range(threads_of.MAX_THREADS + 1)
        ]
        # The bot never replied here, so only the watermark brings it back.
        unanswered = thread(
            id="PRRT_human", isResolved=True, comments={"nodes": [self.opener, word]}
        )
        packet = self.read(*answered, unanswered)
        carried = [t["thread_id"] for t in packet.threads]
        self.assertEqual(carried[0], "PRRT_human")
        self.assertEqual((len(carried), packet.dropped), (threads_of.MAX_THREADS, 2))
        self.assertTrue(packet.maintainer_since)
        self.assertEqual(packet.unanswered, 0)
        # The round answers what it carried; the watermark moves past it all.
        reply = comment(103, "2026-10-05T00:00:00Z", "lf-code-monkey", __typename="Bot")
        later = [
            thread(
                id=t["id"],
                isResolved=True,
                comments={
                    "nodes": [
                        *t["comments"]["nodes"],
                        *([reply] * (t["id"] in carried)),
                    ]
                },
            )
            for t in [*answered, unanswered]
        ]
        packet = self.read(*later, since="2026-10-05T01:00:00Z")
        self.assertEqual(
            [t["thread_id"] for t in packet.threads],
            [t["id"] for t in answered if t["id"] not in carried],
        )
        self.assertFalse(packet.maintainer_since)
        self.assertEqual(
            (packet.dropped, packet.resolved, packet.unanswered), (0, 2, 2)
        )

    @unittest.expectedFailure
    def test_unanswered_threads_past_the_bounds_fail_closed(self) -> None:
        """Watermark-only threads the bounds cannot hold raise, not vanish."""
        word = comment(101, "2026-10-04T00:00:00Z", "maintainer", "MEMBER")
        nodes = [
            thread(
                id=f"PRRT_{i}", isResolved=True, comments={"nodes": [self.opener, word]}
            )
            for i in range(threads_of.MAX_THREADS + 1)
        ]
        with self.assertRaises(threads_of.reads.SelectionError):
            self.read(*nodes)

    @unittest.expectedFailure
    def test_a_user_with_the_apps_login_is_not_the_bot(self) -> None:
        """Only the App, typed Bot, answers a maintainer's word."""
        nodes = [
            self.opener,
            comment(101, "2026-10-01T01:00:00Z", "lf-code-monkey", __typename="Bot"),
            comment(102, "2026-10-02T00:00:00Z", "maintainer", "MEMBER"),
            comment(103, "2026-10-02T01:00:00Z", "lf-code-monkey", __typename="User"),
        ]
        packet = self.read(
            thread(id="PRRT_r", isResolved=True, comments={"nodes": nodes})
        )
        self.assertEqual([t["thread_id"] for t in packet.threads], ["PRRT_r"])
        self.assertEqual(packet.unanswered, 1)

    @unittest.expectedFailure
    def test_an_unanswered_word_returns_after_publication(self) -> None:
        """Omitted or failed reply, a published round, a later scan: heard again."""
        pending_of = import_module("thread_pending")
        word = comment(101, "2026-10-04T00:00:00Z", "maintainer", "MEMBER")
        node = thread(
            id="PRRT_human", isResolved=True, comments={"nodes": [self.opener, word]}
        )
        packet = self.read(node)
        carried = [
            {"thread_id": t["thread_id"], "held": pending_of.held_word(t)}
            for t in packet.threads
        ]
        self.assertEqual(carried[0]["held"], "2026-10-04T00:00:00Z")
        # A reply posted clears it; one left out or refused keeps it.
        self.assertIsNone(pending_of.pending_since(carried, {"PRRT_human"}))
        pending = pending_of.pending_since(carried, set())
        self.assertEqual(pending, "2026-10-04T00:00:00Z")
        later = "2026-10-05T00:00:00Z"
        with patch.object(github, "graphql", return_value=thread_page([node])):
            packet = threads_of.unresolved_threads(
                REPO, 41, later, frozenset(), BOT, pending
            )
        self.assertEqual([t["thread_id"] for t in packet.threads], ["PRRT_human"])
        self.assertFalse(packet.maintainer_since)
        self.assertEqual((packet.resolved, packet.unanswered), (1, 1))
        # Without the footer's pending, or with a newer one, it stays out.
        for held in (None, "2026-10-04T00:00:01Z"):
            with patch.object(github, "graphql", return_value=thread_page([node])):
                packet = threads_of.unresolved_threads(
                    REPO, 41, later, frozenset(), BOT, held
                )
            self.assertEqual((packet.threads, packet.unanswered), ([], 0))

    @unittest.expectedFailure
    def test_answered_threads_are_not_replayed_or_counted(self) -> None:
        """Only the pending thread and the new word come back; nothing overflows.

        A round answered all but one of a full packet of watermark-only
        threads, then a maintainer spoke on another the bot never replied on.
        """
        word = comment(101, "2026-10-02T00:00:00Z", "maintainer", "MEMBER")
        reply = comment(102, "2026-10-02T01:00:00Z", "lf-code-monkey", __typename="Bot")
        answered = [
            thread(
                id=f"PRRT_{i}",
                isResolved=True,
                comments={"nodes": [self.opener, word, reply]},
            )
            for i in range(threads_of.MAX_THREADS - 1)
        ]
        owed = thread(
            id="PRRT_owed", isResolved=True, comments={"nodes": [self.opener, word]}
        )
        fresh = comment(103, "2026-10-04T00:00:00Z", "maintainer", "MEMBER")
        new = thread(
            id="PRRT_new", isResolved=True, comments={"nodes": [self.opener, fresh]}
        )
        page = thread_page([*answered, owed, new])
        with patch.object(github, "graphql", return_value=page):
            packet = threads_of.unresolved_threads(
                REPO, 41, self.since, frozenset(), BOT, "2026-10-02T00:00:00Z"
            )
        self.assertEqual(
            sorted(t["thread_id"] for t in packet.threads), ["PRRT_new", "PRRT_owed"]
        )
        self.assertEqual(
            (packet.dropped, packet.resolved, packet.unanswered), (0, 2, 1)
        )

    @unittest.expectedFailure
    def test_a_thread_the_bot_answers_is_not_held(self) -> None:
        """A word after the bot's reply is owed on its own; it needs no pending."""
        pending_of = import_module("thread_pending")
        nodes = [
            self.opener,
            comment(101, "2026-10-01T01:00:00Z", "lf-code-monkey", __typename="Bot"),
            comment(102, "2026-10-04T00:00:00Z", "maintainer", "MEMBER"),
        ]
        packet = self.read(
            thread(id="PRRT_r", isResolved=True, comments={"nodes": nodes})
        )
        self.assertIsNone(pending_of.held_word(packet.threads[0]))

    @unittest.expectedFailure
    def test_a_word_in_the_bots_second_is_ordered_by_the_thread(self) -> None:
        """Same-second comments: thread order says which answered which."""
        bot = comment(101, "2026-10-02T00:00:00Z", "lf-code-monkey", __typename="Bot")
        word = comment(102, "2026-10-02T00:00:00Z", "maintainer", "MEMBER")
        packet = self.read(
            thread(
                id="PRRT_after",
                isResolved=True,
                comments={"nodes": [self.opener, bot, word]},
            ),
            thread(
                id="PRRT_before",
                isResolved=True,
                comments={"nodes": [self.opener, word, bot]},
            ),
        )
        self.assertEqual([t["thread_id"] for t in packet.threads], ["PRRT_after"])
        self.assertEqual((packet.resolved, packet.unanswered), (1, 1))

    @unittest.expectedFailure
    def test_a_word_posted_while_the_round_ran_is_unanswered(self) -> None:
        """The bot's reply answers what its round read, not what came after.

        A maintainer spoke after the selection read the thread and before
        the reply went up: the agent never saw that word, so it stays
        unanswered through a later round that moves the watermark past
        it, leaves the thread out of the bounds or posts no reply.
        """
        marker = (
            "<!-- code-monkey reply round=2 session=1 seen=2026-10-03T00:00:00Z -->"
        )
        reply = comment(102, "2026-10-03T00:30:00Z", "lf-code-monkey", __typename="Bot")
        reply["body"] = f"Fixed.\n\n{marker}"
        unseen = comment(101, "2026-10-03T00:10:00Z", "maintainer", "MEMBER")
        nodes = [
            thread(
                id=f"PRRT_{i}",
                isResolved=True,
                comments={"nodes": [self.opener, unseen, reply]},
            )
            for i in range(threads_of.MAX_THREADS + 1)
        ]
        packet = self.read(*nodes)
        self.assertIs(packet.threads[0]["maintainer_unanswered"], True)
        self.assertIs(packet.threads[0]["maintainer_answered"], False)
        self.assertEqual(packet.dropped, 1)
        # The round answers what it carried, then the watermark moves on;
        # its reply read after every word, so those threads owe nothing.
        carried = {t["thread_id"] for t in packet.threads}
        later_reply = {
            **reply,
            "databaseId": 103,
            "createdAt": "2026-10-04T00:30:00Z",
            "body": "Done.\n\n<!-- code-monkey reply round=3 session=2"
            " seen=2026-10-04T00:00:00Z -->",
        }
        later = [
            thread(
                id=t["id"],
                isResolved=True,
                comments={
                    "nodes": [
                        *t["comments"]["nodes"],
                        *([later_reply] * (t["id"] in carried)),
                    ]
                },
            )
            for t in nodes
        ]
        packet = self.read(*later, since="2026-10-04T00:00:00Z")
        self.assertEqual(
            [t["thread_id"] for t in packet.threads],
            [t["id"] for t in nodes if t["id"] not in carried],
        )
        self.assertEqual((packet.dropped, packet.unanswered), (0, 1))
        # A word the round read before its reply is answered by it.
        seen = {**unseen, "createdAt": "2026-10-02T23:59:59Z"}
        packet = self.read(
            thread(
                id="PRRT_seen",
                isResolved=True,
                comments={"nodes": [self.opener, seen, reply]},
            ),
            since="2026-10-03T00:00:00Z",
        )
        self.assertEqual((packet.threads, packet.unanswered), ([], 0))
