# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Whether an issue's author lets the bot work it.

Two rules, both in this module so no site can drift from another.

The association rule: issues triage gives anyone's issue a Type, so a
Type alone is no human decision. A scan works an issue from an owner
or member of the target; any other author, an absent or unreadable
association, or an App needs a maintainer's ``agent-approved`` label
first (DESIGN.md section 6). Every site that applies it calls
``admits``. A named issue or round applies no association rule:
naming it is the approval.

The listed-author rule binds every path, named or not: the issue's
author, and its last editor when the body was edited, must be on the
list the caller reads from ``CODE_MONKEY_ISSUE_AUTHORS``, and an App
or bot never passes. Issue comments reach a packet only from listed
authors. ``parse_listed`` reads the list and refuses an empty or
malformed one; ``unlisted`` and ``comment_listed`` apply it.

``ONLY_KNOWN_AUTHORS`` gates the listed-author rule. Unset, blank or
``true`` keeps it; ``false`` turns it off and the list goes unread;
any other value fails the run. ``resolve_mode`` reads both, and its
None result means the rule is off: the association rule, and the
owner-or-member comment filter, still apply. A selection records the
mode under ``MODE_KEY``, and every later site reads that record
through ``recorded_mode`` instead of resolving the flag again.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, NamedTuple, cast

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})
APPROVAL_LABEL = "agent-approved"
# Whose issue comments reach the agent.
COMMENT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})
# The repository variable the caller reads the list from; errors name it.
VARIABLE = "CODE_MONKEY_ISSUE_AUTHORS"
# The repository variable that gates the rule, and the record key for it.
FLAG = "ONLY_KNOWN_AUTHORS"
MODE_KEY = "only_known_authors"
# How to fix a list the rule refuses, empty or malformed alike.
SETUP_HINT = (
    "populate it with the GitHub usernames whose issues will be accepted, "
    f"for example: gh variable set {VARIABLE} "
    "--repo <owner>/<repo> --body 'alice bob'"
)
# A GitHub login: at most 39 alphanumerics and single hyphens, neither
# starting nor ending with a hyphen.
LOGIN_RE = re.compile(r"(?=[A-Za-z0-9-]{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
AUTHOR_NOT_LISTED = "author_not_listed"
EDITOR_NOT_LISTED = "editor_not_listed"
LIST_COUNTERS = frozenset({AUTHOR_NOT_LISTED, EDITOR_NOT_LISTED})


class AuthorListError(ValueError):
    """The listed-author variable or its flag holds a value the rule refuses."""


class Unlisted(NamedTuple):
    """Why the listed-author rule refuses an issue: a skip counter and a reason."""

    counter: str
    reason: str


class Refused(Exception):
    """A later read of an issue found the listed-author rule refusing it.

    Raised where a skip would otherwise read as a generic one, so the
    recording site counts it under ``unlisted.counter`` instead.
    """

    def __init__(self, unlisted: Unlisted) -> None:
        """Carry the refusal's reason as the message, and the refusal beside it."""
        super().__init__(unlisted.reason)
        self.unlisted = unlisted


def is_bot(user: Any) -> bool:
    """Whether a REST ``user`` or a search ``author`` object is an App or bot.

    The REST API types an App's account ``Bot`` and suffixes its login
    with ``[bot]``; ``gh search`` reports ``is_bot`` and prefixes the
    login with ``app/``. Any one of them is enough.
    """
    if not isinstance(user, dict):
        return False
    data = cast("dict[str, Any]", user)
    login = data.get("login")
    return (
        data.get("type") == "Bot"
        or data.get("is_bot") is True
        or (
            isinstance(login, str)
            and (login.endswith("[bot]") or login.startswith("app/"))
        )
    )


def author_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """The author fields of a REST issue: login, association, whether an App opened it."""
    association = issue.get("author_association")
    user = issue.get("user")
    login = (
        cast("dict[str, Any]", user).get("login") if isinstance(user, dict) else None
    )
    return {
        "author": login if isinstance(login, str) else None,
        "author_association": association if isinstance(association, str) else None,
        "author_is_bot": is_bot(user),
    }


def author_admitted(association: Any, labels: list[str], *, bot: bool = False) -> bool:
    """Whether the author, or failing that the approval label, admits the issue.

    An owner or member who is not an App passes; anyone else passes
    only once a maintainer adds ``APPROVAL_LABEL``. The ``no-agent``
    opt-out is the callers' test and comes first, so an issue carrying
    both labels stays skipped.
    """
    trusted = isinstance(association, str) and association in TRUSTED_ASSOCIATIONS
    return (trusted and not bot) or APPROVAL_LABEL in labels


def admits(entry: dict[str, Any]) -> bool:
    """``author_admitted`` over a candidate or issue details with ``labels``."""
    return author_admitted(
        entry.get("author_association"),
        entry["labels"],
        bot=bool(entry.get("author_is_bot")),
    )


def graphql_actor(actor: Any) -> tuple[str | None, bool]:
    """A GraphQL ``Actor``'s login and whether it is an App, a bot or unknown.

    Only a ``User`` is a person; a missing actor (a deleted account)
    reads as no login and a bot, so it is never listed.
    """
    if not isinstance(actor, dict):
        return None, True
    data = cast("dict[str, Any]", actor)
    raw = data.get("login")
    login = raw if isinstance(raw, str) else None
    return login, data.get("__typename") != "User" or is_bot({"login": login})


def graphql_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """The listed-author fields of a GraphQL issue with author, editor and edit time."""
    author, author_bot = graphql_actor(issue.get("author"))
    if issue.get("lastEditedAt") is None:
        editor, editor_bot, edited = None, False, False
    else:
        editor, editor_bot = graphql_actor(issue.get("editor"))
        edited = True
    return {
        "author": author,
        "author_is_bot": author_bot,
        "edited": edited,
        "editor": editor,
        "editor_is_bot": editor_bot,
    }


def recorded_list(value: Any) -> frozenset[str]:
    """The list a selection or check recorded, or ``AuthorListError``."""
    if not isinstance(value, list) or not all(
        isinstance(login, str) for login in cast("list[Any]", value)
    ):
        raise AuthorListError(f"no {VARIABLE} list was recorded")
    return parse_listed(",".join(cast("list[str]", value)))


def parse_listed(text: str | None) -> frozenset[str]:
    """The listed logins, folded to lower case, or ``AuthorListError``.

    Entries are separated by whitespace and/or commas. One entry that
    is not a GitHub login refuses the whole list, and so does a list
    with no entry at all: the rule fails closed.
    """
    entries = [token for token in re.split(r"[,\s]+", text or "") if token]
    if not entries:
        raise AuthorListError(f"{VARIABLE} is empty or unset; {SETUP_HINT}")
    for entry in entries:
        if not LOGIN_RE.fullmatch(entry):
            raise AuthorListError(
                f"{VARIABLE} holds {entry!r}, which is not a login; {SETUP_HINT}"
            )
    return frozenset(entry.lower() for entry in entries)


def resolve_mode(flag: str | None, text: str | None) -> frozenset[str] | None:
    """The list ``ONLY_KNOWN_AUTHORS`` binds, None when it turns the rule off.

    Unset, blank or ``true`` reads the list through ``parse_listed``;
    ``false`` returns None without reading it. Surrounding whitespace
    is ignored; any other value is an ``AuthorListError``.
    """
    value = (flag or "").strip()
    if value in ("", "true"):
        return parse_listed(text)
    if value == "false":
        return None
    raise AuthorListError(
        f"{FLAG} holds {value!r}; set it to true or false, or leave it unset"
    )


def rule_off(record: dict[str, Any]) -> bool:
    """Whether a selection or check recorded the rule off: a JSON false only."""
    return record.get(MODE_KEY) is False


def recorded_mode(record: dict[str, Any]) -> frozenset[str] | None:
    """The list a record binds, None when it recorded the rule off.

    A record without the mode, or with any value but false, keeps the
    rule on and needs its list, as ``recorded_list`` reads it.
    """
    if rule_off(record):
        return None
    return recorded_list(record.get("issue_authors"))


def mode_fields(listed: frozenset[str] | None) -> dict[str, Any]:
    """What a selection or check records for the mode and its list."""
    return {MODE_KEY: listed is not None, "issue_authors": sorted(listed or ())}


def is_listed(login: Any, listed: frozenset[str], *, bot: bool = False) -> bool:
    """Whether ``login`` is on the list; an App or bot never is."""
    return (
        not bot
        and isinstance(login, str)
        and LOGIN_RE.fullmatch(login) is not None
        and login.lower() in listed
    )


def unlisted(details: dict[str, Any], listed: frozenset[str] | None) -> Unlisted | None:
    """Why the listed-author rule refuses the issue, or None when it admits it.

    ``details`` are an ``issue_reads.issue_details`` reply. The author
    must be listed; when the body was edited, so must its last editor.
    A missing field refuses, as an unlisted login does. With no list
    the rule is off and admits every issue.
    """
    if listed is None:
        return None
    author = details.get("author")
    if not is_listed(author, listed, bot=bool(details.get("author_is_bot"))):
        return Unlisted(
            AUTHOR_NOT_LISTED,
            f"{AUTHOR_NOT_LISTED}: issue author {author or 'unknown'} is not in "
            f"{VARIABLE}",
        )
    if details.get("edited") is not False:
        editor = details.get("editor")
        if not is_listed(editor, listed, bot=bool(details.get("editor_is_bot"))):
            return Unlisted(
                EDITOR_NOT_LISTED,
                f"{EDITOR_NOT_LISTED}: issue body last edited by "
                f"{editor or 'unknown'}, who is not in {VARIABLE}",
            )
    return None


def recorded_refusal(
    selection: dict[str, Any], entry: dict[str, Any]
) -> tuple[frozenset[str] | None, str | None]:
    """The list a selection recorded, and why its entry fails it, or None.

    The list is None when the selection recorded the rule off. A
    selection with the rule on and no usable list refuses every entry
    under an empty list: the publisher re-checks each one and
    publishes nothing on trust.
    """
    try:
        listed = recorded_mode(selection)
    except AuthorListError as exc:
        return frozenset(), f"the selection: {exc}"
    refused = unlisted(entry, listed)
    return listed, refused.reason if refused is not None else None


def comment_listed(entry: dict[str, Any], listed: frozenset[str]) -> bool:
    """Whether a REST issue comment's author is listed and no App or bot."""
    user = entry.get("user")
    login = (
        cast("dict[str, Any]", user).get("login") if isinstance(user, dict) else None
    )
    return is_listed(login, listed, bot=is_bot(user))


def member_comment(
    entry: dict[str, Any], listed: frozenset[str] | None = None
) -> dict[str, Any] | None:
    """An owner's or member's REST issue comment in packet form, or None.

    With ``listed``, its author must also be a listed login and no App.
    """
    association = entry.get("author_association")
    body = entry.get("body")
    user = entry.get("user")
    login = (
        cast("dict[str, Any]", user).get("login") if isinstance(user, dict) else None
    )
    if (
        association not in COMMENT_ASSOCIATIONS
        or not isinstance(body, str)
        or not isinstance(login, str)
        or (listed is not None and not comment_listed(entry, listed))
    ):
        return None
    return {
        "author": login,
        "association": association,
        "created_at": entry.get("created_at"),
        "body": body,
    }


def main(argv: list[str] | None = None) -> None:
    """``check``: refuse a bad flag or list before any token exists."""
    parser = argparse.ArgumentParser(description="Check the listed issue authors.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help=f"validate {VARIABLE}")
    check.add_argument("--authors", required=True)
    check.add_argument("--only-known-authors", default="")
    args = parser.parse_args(argv)
    try:
        listed = resolve_mode(args.only_known_authors, args.authors)
    except AuthorListError as exc:
        parser.exit(1, f"::error::{exc}\n")
    if listed is None:
        print(
            f"::warning::{FLAG} is false: the listed-author rule is disabled; "
            f"{VARIABLE} is not read"
        )
        return
    print(f"{VARIABLE}: {len(listed)} issue author(s) listed", file=sys.stderr)


if __name__ == "__main__":
    main()
