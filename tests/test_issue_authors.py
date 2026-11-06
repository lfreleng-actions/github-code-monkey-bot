# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The author rule: owners and members pass, anyone else needs approval."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
authors = import_module("issue_authors")

UNTRUSTED = (
    "COLLABORATOR",
    "CONTRIBUTOR",
    "FIRST_TIMER",
    "FIRST_TIME_CONTRIBUTOR",
    "MANNEQUIN",
    "NONE",
    None,
    "",
    ["MEMBER"],
)


class AuthorAdmittedTest(unittest.TestCase):
    """``author_admitted`` and ``admits`` apply one rule."""

    def test_owners_and_members_pass(self) -> None:
        """The two trusted associations need no label."""
        self.assertEqual(authors.TRUSTED_ASSOCIATIONS, frozenset({"OWNER", "MEMBER"}))
        self.assertTrue(authors.author_admitted("OWNER", []))
        self.assertTrue(authors.author_admitted("MEMBER", ["bug"]))

    def test_anyone_else_needs_the_approval_label(self) -> None:
        """Every other, absent or malformed association needs agent-approved."""
        for association in UNTRUSTED:
            with self.subTest(association=association):
                self.assertFalse(authors.author_admitted(association, ["bug"]))
                self.assertTrue(
                    authors.author_admitted(association, ["agent-approved"])
                )

    def test_a_bot_needs_the_approval_label(self) -> None:
        """An App's issue is untrusted whatever association it carries."""
        self.assertFalse(authors.author_admitted("MEMBER", [], bot=True))
        self.assertTrue(authors.author_admitted("MEMBER", ["agent-approved"], bot=True))

    def test_admits_reads_an_entry(self) -> None:
        """``admits`` takes the association, labels and bot flag from one dict."""
        self.assertTrue(authors.admits({"author_association": "OWNER", "labels": []}))
        self.assertFalse(authors.admits({"labels": []}))
        self.assertFalse(
            authors.admits(
                {"author_association": "MEMBER", "author_is_bot": True, "labels": []}
            )
        )


class IsBotTest(unittest.TestCase):
    """``is_bot`` reads the REST user and the search author alike."""

    def test_shapes(self) -> None:
        """Type, the search flag and either login form each mark a bot."""
        for user, expected in (
            ({"login": "helper[bot]", "type": "Bot"}, True),
            ({"login": "helper[bot]"}, True),
            ({"login": "app/helper", "is_bot": True, "type": ""}, True),
            ({"login": "app/helper"}, True),
            ({"login": "alice", "type": "User", "is_bot": False}, False),
            ({}, False),
            (None, False),
            ("helper[bot]", False),
        ):
            with self.subTest(user=user):
                self.assertIs(authors.is_bot(user), expected)

    def test_author_fields(self) -> None:
        """A REST issue yields a typed association and the bot flag."""
        self.assertEqual(
            authors.author_fields(
                {"author_association": "NONE", "user": {"login": "x[bot]"}}
            ),
            {"author_association": "NONE", "author_is_bot": True},
        )
        self.assertEqual(
            authors.author_fields({"author_association": 5}),
            {"author_association": None, "author_is_bot": False},
        )


if __name__ == "__main__":
    unittest.main()
