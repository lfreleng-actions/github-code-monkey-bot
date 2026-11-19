# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fuzz the readers of operator input: named issues and listed authors.

The first byte of an input picks a target and the rest is its text.
Each oracle derives the expected answer from the raw text:

- ``explicit_issues.parse_issues`` reads ``owner/repo#N`` references
  "to ``(repo_name, number)``, in order"; "An owner other than the
  organisation is refused"; "Repository names fold to lower case ...
  so a reference repeated in another case is one issue". Its caller,
  ``select_issues.main``, catches ``SelectionError`` and
  ``ValueError`` by design, so the harness expects either by name.
- ``issue_authors.parse_listed``: "Entries are separated by
  whitespace and/or commas. One entry that is not a GitHub login
  refuses the whole list, and so does a list with no entry at all";
  logins come back "folded to lower case". A login is "at most 39
  alphanumerics and single hyphens, neither starting nor ending with
  a hyphen".
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import Any

from harness_support import Breach, arguments, as_text, on_path

on_path()
explicit_issues = import_module("explicit_issues")
issue_authors = import_module("issue_authors")
SelectionError = import_module("issue_reads").SelectionError

ORG = "lfreleng-actions"
ALNUM = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
DIGITS = frozenset("0123456789")
# Python's own limit on converting a decimal string to an int.
MAX_INT_DIGITS = 4300


def entries(text: str) -> list[str]:
    """The entries of a list separated by whitespace and/or commas."""
    return text.replace(",", " ").split()


def reference(token: str) -> tuple[str, str, str] | None:
    """A token's owner, repository and number when it has the canonical shape."""
    owner, slash, rest = token.partition("/")
    repo, hash_, number = rest.partition("#")
    if not (slash and hash_ and owner and repo and number):
        return None
    if owner[0] == "-" or not set(owner) <= ALNUM | {"-"}:
        return None
    if repo in (".", "..") or not set(repo) <= ALNUM | set("_.-"):
        return None
    if number[0] == "0" or not set(number) <= DIGITS:
        return None
    return owner, repo, number


def oracle_issues(text: str, result: list[tuple[str, int]] | None) -> None:
    """The references in order, folded and once each; refused only when one is bad."""
    tokens = entries(text)
    parsed = [reference(token) for token in tokens]
    valid = all(
        found is not None and found[0].lower() == ORG.lower() for found in parsed
    )
    if result is None:
        if valid and all(
            found is not None and len(found[2]) <= MAX_INT_DIGITS for found in parsed
        ):
            raise Breach(f"parse_issues refused a valid list: {text!r}")
        return
    if not valid:
        raise Breach(f"parse_issues accepted {text!r}")
    expected: list[tuple[str, int]] = []
    for found in parsed:
        if found is not None and (found[1].lower(), int(found[2])) not in expected:
            expected.append((found[1].lower(), int(found[2])))
    if result != expected:
        raise Breach(f"parse_issues read {result!r}, not {expected!r}")


def login(entry: str) -> bool:
    """Whether an entry is a GitHub login."""
    return (
        0 < len(entry) <= 39
        and set(entry) <= ALNUM | {"-"}
        and not entry.startswith("-")
        and not entry.endswith("-")
        and "--" not in entry
    )


def oracle_listed(text: str, result: frozenset[str] | None) -> None:
    """The logins folded, or a refusal when any entry is not one or there is none."""
    found = entries(text)
    valid = bool(found) and all(login(entry) for entry in found)
    if result is None:
        if valid:
            raise Breach(f"parse_listed refused a valid list: {text!r}")
        return
    if not valid or result != frozenset(entry.lower() for entry in found):
        raise Breach(f"parse_listed read {text!r} as {sorted(result)}")


def check_issues(payload: bytes) -> None:
    """``parse_issues`` against the organisation."""
    text = as_text(payload)
    try:
        result: Any = explicit_issues.parse_issues(text, ORG)
    except (SelectionError, ValueError):
        result = None
    oracle_issues(text, result)


def check_listed(payload: bytes) -> None:
    """``parse_listed`` on the variable's text."""
    text = as_text(payload)
    try:
        result: Any = issue_authors.parse_listed(text)
    except issue_authors.AuthorListError:
        result = None
    oracle_listed(text, result)


TARGETS = (check_issues, check_listed)


def check(data: bytes) -> None:
    """The fuzzed function: one input, one target, its contract or ``Breach``."""
    if data:
        TARGETS[data[0] % len(TARGETS)](data[1:])


def seed(target: int, text: str) -> bytes:
    """An input for the target at that index."""
    return bytes([target]) + text.encode("utf-8", "surrogatepass")


SEEDS: tuple[bytes, ...] = (
    seed(
        0, "lfreleng-actions/repo#1, LFReleng-Actions/Repo#1\nlfreleng-actions/x.y#20"
    ),
    seed(0, "lfreleng-actions/.#1 other/repo#2"),
    seed(0, "lfreleng-actions/repo#0"),
    seed(0, "lfreleng-actions/repo#1" + "0" * MAX_INT_DIGITS),
    seed(0, ""),
    seed(1, "alice, Bob\tcarol-d"),
    seed(1, "alice bob-"),
    seed(1, " , "),
    seed(1, "a" * 39 + " " + "b" * 40),
)


def main() -> None:
    """Fuzz ``check`` under Atheris, from the seeds unless files are named."""
    import atheris  # type: ignore[import-not-found]

    fuzzer: Any = atheris
    fuzzer.instrument_all()
    fuzzer.Setup(arguments(sys.argv, SEEDS), check)
    fuzzer.Fuzz()


if __name__ == "__main__":
    main()
