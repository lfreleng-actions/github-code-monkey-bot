# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The selection's command line, and its inputs parsed before any read happens."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import stale_statuses as stale
from issue_reads import SelectionError

REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# An App's slug, the login GraphQL gives it without REST's [bot].
APP_LOGIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def parse_repositories(text: str) -> list[str]:
    """Split a repository list on commas and whitespace, in given order."""
    names: list[str] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        if not REPO_NAME_RE.fullmatch(token) or token in (".", ".."):
            raise SelectionError(f"invalid repository name {token!r}")
        lowered = token.lower()
        if lowered not in names:
            names.append(lowered)
    return names


def load_exclusions(file: Path | None, override: str) -> list[str]:
    """Resolve the exclusion list: an explicit override beats the bundled file."""
    if override.strip():
        return parse_repositories(override)
    if file is None:
        return []
    if not file.is_file():
        raise SelectionError(f"exclusion file {file} is not a regular file")
    names: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            names.extend(parse_repositories(stripped))
    return names


def load_reviewers(file: Path | None) -> frozenset[str]:
    """The antagonistic review Apps whose findings count; empty turns them off.

    One App login per line, without the ``[bot]`` suffix; ``#`` starts
    a comment. A named file that is missing or malformed fails the
    selection rather than drop a reviewer silently.
    """
    if file is None:
        return frozenset()
    if not file.is_file():
        raise SelectionError(f"reviewer file {file} is not a regular file")
    logins: set[str] = set()
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not APP_LOGIN_RE.fullmatch(stripped):
            raise SelectionError(f"invalid App login {stripped!r} in {file}")
        logins.add(stripped)
    return frozenset(logins)


def parse_max_rounds(text: str) -> int:
    """Accept a positive integer: the revision rounds a pull request may get."""
    if not re.fullmatch(r"[1-9]\d*", text.strip()):
        raise SelectionError(f"max_rounds must be a positive integer, got {text!r}")
    return int(text)


def parse_max_pull_requests(text: str) -> int:
    """Accept a non-negative integer; zero lifts the cap."""
    if not re.fullmatch(r"\d+", text.strip()):
        raise SelectionError(
            f"max_pull_requests must be a non-negative integer, got {text!r}"
        )
    return int(text)


def selection_parser(description: str | None) -> argparse.ArgumentParser:
    """The selection's command line, described by ``description``."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--org", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--mode", required=True, choices=("select", "branches", "pull-requests")
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repositories", default="")
    parser.add_argument("--issues", default="")
    parser.add_argument("--pull-requests", default="")
    parser.add_argument("--session", default="")
    parser.add_argument("--revise", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--revise-only", action="store_true")
    parser.add_argument("--outstanding-only", action="store_true")
    parser.add_argument("--continues-run", default="")
    parser.add_argument("--exclude-file", type=Path)
    parser.add_argument("--exclude-repos", default="")
    parser.add_argument("--fork-orgs", type=Path, required=True)
    parser.add_argument("--antagonistic-reviewers", type=Path)
    parser.add_argument("--stale-statuses", type=Path)
    parser.add_argument("--stale-status-minutes", default=str(stale.DEFAULT_MINUTES))
    parser.add_argument("--include-dotgithub", action="store_true")
    parser.add_argument("--include-assigned", action="store_true")
    parser.add_argument("--allow-workflow-edits", action="store_true")
    parser.add_argument("--max-pull-requests", default="10")
    parser.add_argument("--max-rounds", default="10")
    parser.add_argument("--categories", default="all")
    parser.add_argument("--guidance-repository", required=True)
    parser.add_argument("--guidance-ref", default="main")
    parser.add_argument("--guidance-path", default="AGENTS.md")
    parser.add_argument("--bot-slug", default="")
    parser.add_argument("--results-repository", default="")
    parser.add_argument("--held-file", type=Path)
    return parser
