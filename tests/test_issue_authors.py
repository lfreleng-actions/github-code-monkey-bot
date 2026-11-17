# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The author rules: the association rule and the listed-author rule."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from importlib import import_module
from pathlib import Path
from typing import Any

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
            {"author": "x[bot]", "author_association": "NONE", "author_is_bot": True},
        )
        self.assertEqual(
            authors.author_fields({"author_association": 5}),
            {"author": None, "author_association": None, "author_is_bot": False},
        )


LISTED = frozenset({"alice", "bob-2"})


def details(**overrides: Any) -> dict[str, Any]:
    """The author fields of an ``issue_details`` reply, never edited."""
    data: dict[str, Any] = {
        "author": "alice",
        "author_is_bot": False,
        "edited": False,
        "editor": None,
        "editor_is_bot": False,
    }
    data.update(overrides)
    return data


class ParseListedTest(unittest.TestCase):
    """``parse_listed`` reads the variable and fails closed."""

    def test_separators_and_case(self) -> None:
        """Commas, spaces and newlines separate; logins fold to lower case."""
        self.assertEqual(
            authors.parse_listed(" Alice,bob-2\n  CAROL ,, "),
            frozenset({"alice", "bob-2", "carol"}),
        )

    def test_empty_list_refused(self) -> None:
        """An unset, empty or separator-only value lists no one and stops the run."""
        for text in (None, "", "  ", ",\n,"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(authors.AuthorListError, authors.VARIABLE),
            ):
                authors.parse_listed(text)

    def test_bad_entry_refuses_the_list(self) -> None:
        """One entry that is no login refuses the whole list, naming it."""
        for bad in (
            "@alice",
            "-alice",
            "alice-",
            "alice--bob",
            "app/helper",
            "x[bot]",
            "a" * 40,
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(authors.AuthorListError, authors.VARIABLE),
            ):
                authors.parse_listed(f"bob,{bad}")

    def test_check_command(self) -> None:
        """``check`` exits one on a bad list and returns on a good one."""
        with (
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as raised,
        ):
            authors.main(["check", "--authors", ""])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn(authors.VARIABLE, err.getvalue())
        with redirect_stderr(io.StringIO()) as err:
            authors.main(["check", "--authors", "alice bob"])
        self.assertIn("2 issue author(s)", err.getvalue())


class UnlistedTest(unittest.TestCase):
    """``unlisted`` checks the author and the last editor."""

    def test_listed_author_passes_in_any_case(self) -> None:
        """A listed author, written in any case, admits an unedited issue."""
        self.assertIsNone(authors.unlisted(details(author="ALICE"), LISTED))

    def test_unlisted_or_missing_author_refused(self) -> None:
        """An unlisted, missing or App author is refused with the counter."""
        for fields in (
            {"author": "mallory"},
            {"author": None},
            {"author": 5},
            {"author": "alice", "author_is_bot": True},
        ):
            with self.subTest(fields=fields):
                refused = authors.unlisted(details(**fields), LISTED)
                assert refused is not None
                self.assertEqual(refused.counter, authors.AUTHOR_NOT_LISTED)
                self.assertIn(authors.VARIABLE, refused.reason)

    def test_editor_must_be_listed(self) -> None:
        """An edited body needs a listed, human last editor."""
        self.assertIsNone(
            authors.unlisted(details(edited=True, editor="Bob-2"), LISTED)
        )
        for fields in (
            {"editor": "mallory"},
            {"editor": None},
            {"editor": "bob-2", "editor_is_bot": True},
        ):
            with self.subTest(fields=fields):
                refused = authors.unlisted(details(edited=True, **fields), LISTED)
                assert refused is not None
                self.assertEqual(refused.counter, authors.EDITOR_NOT_LISTED)

    def test_unknown_edit_state_refused(self) -> None:
        """Details without the edit read are refused, not taken as unedited."""
        data = details()
        del data["edited"]
        refused = authors.unlisted(data, LISTED)
        assert refused is not None
        self.assertEqual(refused.counter, authors.EDITOR_NOT_LISTED)

    def test_empty_list_admits_no_one(self) -> None:
        """With nothing listed, every issue is refused."""
        self.assertIsNotNone(authors.unlisted(details(), frozenset()))

    def test_comment_listed(self) -> None:
        """A comment passes only from a listed login that is no App."""
        self.assertTrue(authors.comment_listed({"user": {"login": "Alice"}}, LISTED))
        for user in (
            {"login": "mallory"},
            {"login": "alice", "type": "Bot"},
            None,
            {"login": 5},
        ):
            with self.subTest(user=user):
                self.assertFalse(authors.comment_listed({"user": user}, LISTED))


if __name__ == "__main__":
    unittest.main()
