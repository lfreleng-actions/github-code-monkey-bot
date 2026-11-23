# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tell a pull request its antagonistic review request did not go (issue #228).

The antagonistic job tries each dispatch on up to four passes
(docs/DESIGN.md section 20.5); a nominee that never goes (kind
``failed``), or whose one dispatch ended in an error that does not say
whether GitHub accepted it (kind ``uncertain``), turns the run red,
which nobody reading the pull request sees. The notice job runs this
script once per such nominee. It reads the pull request and posts one
comment on it when the pull request is open, this App opened it, its
head branch is one this run selected (``--branches``, from the trusted
selection, since the nominee comes from an unauthenticated result), its
head is still the published head the request was for (``--head``), and
this App has posted no notice of that kind for that head; the first of
those that fails is the reason it posts nothing. A head that moved on
belongs to a later round with a request of its own, so a notice there
would describe a request nobody made for it.

The comment holds fixed text and values checked here, nothing from the
pull request, the issue, a session or a result. Its marker is not the
round footer (``round_reads.FOOTER``), and an App's comment is never a
maintainer's (``issue_authors.COMMENT_ASSOCIATIONS``), so it neither
starts nor feeds a revision round.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import bot_github as github
import issue_reads as reads
import write_recovery as recovery

SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
RUN_ID_RE = re.compile(r"[1-9][0-9]*")
BRANCH_RE = re.compile(r"code-monkey/issue-[1-9][0-9]*")
RUN_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/actions/runs/(?P<run>[1-9][0-9]*)"
)
# Each kind of notice, as the line saying what the script did names it.
KINDS = {"failed": "a failed notice", "uncertain": "an uncertain notice"}
# The notice's last line, nothing after it; a marker quoted mid-body
# is text, and the kind and head it names are what the notice was about.
MARKER_RE = re.compile(
    r"(?:^|\n)<!-- code-monkey-notice antagonistic-request"
    r" kind=(?P<kind>[a-z]+) head=(?P<head>[0-9a-f]{40}) run=[1-9][0-9]* -->\s*\Z"
)
PER_PAGE = 100
# Pages of pull request comments the search reads before it fails
# rather than post on a partial read.
MAX_PAGES = 30


def marker(kind: str, head: str, run_id: str) -> str:
    """The notice's last line, naming the kind, the head and the run."""
    return (
        "<!-- code-monkey-notice antagonistic-request"
        f" kind={kind} head={head} run={run_id} -->"
    )


def notice_body(
    kind: str, head: str, run_id: str, run_url: str, review_repository: str
) -> str:
    """The whole comment, from validated values alone."""
    opening = "🐒 Notice from the Code Monkey workflow: the request for an antagonistic"
    if kind == "failed":
        what = (
            f"{opening} review of head `{head[:12]}` did not reach the review"
            f" bot. Every dispatch failed in [this run]({run_url}), retries"
            " included.\n\nTo ask again, dispatch"
        )
    else:
        what = (
            f"{opening} review of head `{head[:12]}` may not have reached the"
            f" review bot. Its dispatch in [this run]({run_url}) ended in an"
            " error that does not say whether GitHub accepted it.\n\nIf no"
            " review arrives, dispatch"
        )
    return (
        f"{what} `code-review-antagonistic.yaml` in `{review_repository}`"
        f" naming this pull request.\n\n{marker(kind, head, run_id)}"
    )


def by_app(entry: dict[str, Any], slug: str) -> bool:
    """Whether GitHub says this App wrote ``entry``: a Bot named ``<slug>[bot]``."""
    user = entry.get("user")
    if not isinstance(user, dict):
        return False
    author = cast("dict[str, Any]", user)
    return author.get("type") == "Bot" and author.get("login") == f"{slug}[bot]"


def notice_url(
    repository: str, number: int, kind: str, head: str, slug: str
) -> str | None:
    """The URL of this App's ``kind`` notice for ``head``, or None when it has none.

    Anyone can paste the marker; only the App's own comment counts.

    Raises:
        GitHubError: A read failed, or the comments run past ``MAX_PAGES``.
    """
    for page in range(1, MAX_PAGES + 1):
        entries = reads.api_page(
            f"repos/{repository}/issues/{number}/comments"
            f"?per_page={PER_PAGE}&page={page}"
        )
        for entry in entries:
            found = MARKER_RE.search(str(entry.get("body") or ""))
            if (
                found
                and found.group("kind") == kind
                and found.group("head") == head
                and by_app(entry, slug)
            ):
                url = entry.get("html_url")
                return url if isinstance(url, str) else ""
        if len(entries) < PER_PAGE:
            return None
    raise github.GitHubError(
        f"{repository}#{number} holds more comments than the notice reads"
    )


def pull_head(pull: dict[str, Any]) -> str:
    """The pull request's head commit, checked as a SHA."""
    head = pull.get("head")
    sha = cast("dict[str, Any]", head).get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not github.SHA_RE.fullmatch(sha):
        raise github.GitHubError("the pull request's head is not a commit SHA")
    return sha


def pull_branch(pull: dict[str, Any]) -> str | None:
    """The pull request's head branch name, or None when GitHub gives none."""
    head = pull.get("head")
    ref = cast("dict[str, Any]", head).get("ref") if isinstance(head, dict) else None
    return ref if isinstance(ref, str) else None


def refusal(pull: dict[str, Any], args: argparse.Namespace) -> str | None:
    """The first condition that stops the notice, or None when it posts."""
    if pull.get("state") != "open":
        return "the pull request is not open"
    if not by_app(pull, args.bot_slug):
        return "this App did not open the pull request"
    if pull_branch(pull) not in args.branches:
        return "the pull request's branch is not one this run selected"
    head = pull_head(pull)
    if head != args.head:
        return f"its head moved from {args.head[:12]} to {head[:12]}"
    if (
        notice_url(args.repository, args.number, args.kind, head, args.bot_slug)
        is not None
    ):
        return f"this App already posted {KINDS[args.kind]} for head {head[:12]}"
    return None


def notify(args: argparse.Namespace) -> str:
    """Post the notice when every condition holds; return the line saying which."""
    where = f"{args.repository}#{args.number}"
    pull = github.api_object(f"repos/{args.repository}/pulls/{args.number}")
    reason = refusal(pull, args)
    if reason is not None:
        return f"not posted on {where}: {reason}"
    head = args.head
    body = notice_body(
        args.kind, head, args.run_id, args.run_url, args.review_repository
    )

    def write() -> dict[str, Any]:
        endpoint = f"repos/{args.repository}/issues/{args.number}/comments"
        return github.api_write("POST", endpoint, {"body": body})

    def landed() -> dict[str, Any] | None:
        url = notice_url(args.repository, args.number, args.kind, head, args.bot_slug)
        return None if url is None else {"html_url": url}

    # Found by its marker alone, so a lost POST is never sent again.
    data = recovery.recover(write, landed, repeat_safe=False, label="request notice")
    url = data.get("html_url") or "n/a"
    return f"posted {KINDS[args.kind]} on {where} for head {head[:12]}: {url}"


def checked(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Refuse any argument the notice would carry unchecked."""
    if not github.REPO_RE.fullmatch(args.repository):
        parser.error("--repository is not owner/repo")
    if args.number < 1:
        parser.error("--number is not a positive integer")
    if not github.SHA_RE.fullmatch(args.head):
        parser.error("--head is not a commit SHA")
    if not SLUG_RE.fullmatch(args.bot_slug):
        parser.error("--bot-slug is not an App slug")
    if not RUN_ID_RE.fullmatch(args.run_id):
        parser.error("--run-id is not a run id")
    url = RUN_URL_RE.fullmatch(args.run_url)
    if url is None or url.group("run") != args.run_id:
        parser.error("--run-url is not this run's URL")
    if not github.REPO_RE.fullmatch(args.review_repository):
        parser.error("--review-repository is not owner/repo")
    try:
        branches = json.loads(args.branches)
    except ValueError:
        branches = None
    if (
        not isinstance(branches, list)
        or not branches
        or not all(
            isinstance(b, str) and BRANCH_RE.fullmatch(b)
            for b in cast("list[object]", branches)
        )
    ):
        parser.error("--branches is not a JSON list of code-monkey branches")
    args.branches = branches


def main(argv: list[str] | None = None) -> int:
    """Run the notice; 0 whether or not it posted, 1 on a GitHub failure."""
    parser = argparse.ArgumentParser(
        description="Tell a pull request its antagonistic review request did not go."
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--kind", choices=sorted(KINDS), required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--bot-slug", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--review-repository", required=True)
    parser.add_argument("--branches", required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    checked(args, parser)
    status = 0
    try:
        line = notify(args)
    except github.GitHubError as exc:
        line = f"failed on {args.repository}#{args.number}: {github.safe_message(exc)}"
        print(f"::error::Request notice {line}")
        status = 1
    else:
        print(line)
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(f"Request notice {line}\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
