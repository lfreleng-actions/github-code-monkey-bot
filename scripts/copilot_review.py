# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Request a Copilot review of a bot pull request, then wait for its verdict.

GitHub starts no review by itself for a pull request a GitHub App
opens (DESIGN.md section 8), so the trusted publish job asks for one
with the pull request token, best effort, then polls with the job's
own token for a review by Copilot whose ``commit_id`` is the head it
published, reads the unresolved review threads, and classifies the
pair (``review_verdict``). A review that never arrives within the
wait is pending, never clean.

Every wait is bounded before it starts: the shared ``gh`` wrapper
retries a read three times with a timeout each, so a poll begun too
close to the deadline could run past the job's own limit; polling
stops while enough budget remains for the two reads a hit needs.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import bot_github as github
import proposal_model as model
import review_verdict as verdicts
from review_verdict import Verdict

REVIEWER_LOGIN = "copilot-pull-request-reviewer[bot]"
DEFAULT_WAIT_MINUTES = 30
POLL_SECONDS = 30
# The worst one read can cost through bot_github.run_gh: every attempt
# timing out, plus the backoff between attempts.
READ_BUDGET_SECONDS = github.READ_ATTEMPTS * github.TIMEOUT_SECONDS + sum(
    github.RETRY_DELAY_SECONDS * attempt for attempt in range(1, github.READ_ATTEMPTS)
)
# A hit costs the review listing, the thread count and the head check.
RESERVE_SECONDS = 3 * READ_BUDGET_SECONDS
PULL_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$")

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
    }
  }
}
"""


def pull_reference(url: str) -> tuple[str, int]:
    """The ``owner/repo`` and number a pull request URL names."""
    found = PULL_URL.fullmatch(url.strip())
    if found is None:
        raise github.GitHubError(f"not a pull request URL: {url!r}")
    return found.group(1), int(found.group(2))


def request_review(repository: str, number: int) -> str | None:
    """Ask Copilot to review; return a warning instead of raising on failure.

    ``@copilot`` is the form GitHub accepts; the bare login fails. The
    request is best effort: a refusal is recorded, and the wait still
    runs in case a review starts another way.
    """
    try:
        github.run_gh(
            [
                "pr",
                "edit",
                str(number),
                "--repo",
                repository,
                "--add-reviewer",
                "@copilot",
            ]
        )
    except github.GitHubError as exc:
        return f"could not request a Copilot review: {github.safe_message(exc)}"
    return None


def pull_head(repository: str, number: int) -> str:
    """The pull request's current head commit."""
    data = github.api_object(f"repos/{repository}/pulls/{number}")
    head = data.get("head")
    if not isinstance(head, dict):
        raise github.GitHubError("pull request reply carried no head")
    return github.require_sha(cast("dict[str, Any]", head), "sha", "pull request head")


def head_review(repository: str, number: int, head_sha: str) -> dict[str, Any] | None:
    """The newest Copilot review of ``head_sha``, or None."""
    newest: dict[str, Any] | None = None
    for review in github.api_list(f"repos/{repository}/pulls/{number}/reviews"):
        user = review.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        if login != REVIEWER_LOGIN or review.get("commit_id") != head_sha:
            continue
        if newest is None or int(review.get("id") or 0) > int(newest.get("id") or 0):
            newest = review
    return newest


class WaitExpired(Exception):
    """The next read would not fit the wait; the review stays pending."""


def unresolved_threads(
    repository: str,
    number: int,
    *,
    budget: Callable[[], float] | None = None,
) -> int:
    """Count the pull request's unresolved review threads, every page.

    Each page is a read with the shared wrapper's own retries, so with
    a ``budget`` (seconds left in the wait) a page that may not fit
    stops the count with ``WaitExpired`` rather than overrun it.
    """
    owner, _, name = repository.partition("/")
    after: str | None = None
    count = 0
    while True:
        if budget is not None and budget() < READ_BUDGET_SECONDS:
            raise WaitExpired("could not read every review thread within the wait")
        data = github.graphql(
            THREADS_QUERY,
            {"owner": owner, "name": name, "number": number, "after": after},
            read=True,
        )
        reply = data.get("repository")
        pull = (
            cast("dict[str, Any]", reply).get("pullRequest")
            if isinstance(reply, dict)
            else None
        )
        threads = (
            cast("dict[str, Any]", pull).get("reviewThreads")
            if isinstance(pull, dict)
            else None
        )
        if not isinstance(threads, dict):
            raise github.GitHubError("review thread query returned no data")
        nodes = cast("dict[str, Any]", threads).get("nodes")
        if not isinstance(nodes, list):
            raise github.GitHubError("review thread query returned no nodes")
        count += sum(
            1
            for node in cast("list[Any]", nodes)
            if isinstance(node, dict)
            and cast("dict[str, Any]", node).get("isResolved") is False
        )
        page = cast("dict[str, Any]", threads).get("pageInfo")
        if not isinstance(page, dict) or not cast("dict[str, Any]", page).get(
            "hasNextPage"
        ):
            return count
        after = str(cast("dict[str, Any]", page).get("endCursor"))


def await_review(
    repository: str,
    number: int,
    head_sha: str,
    *,
    wait_minutes: int = DEFAULT_WAIT_MINUTES,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Verdict, int | None]:
    """Poll for Copilot's review of the head, bounded; classify it when it lands.

    The deadline is checked before every read, with a reserve for the
    reads a hit needs, so the wait can never overrun the budget by a
    read's worst case. Read failures that outlast the wrapper's own
    retries end the wait as pending with the reason, never as clean.
    """
    deadline = clock() + wait_minutes * 60
    while True:
        remaining = deadline - clock()
        if remaining <= RESERVE_SECONDS:
            return (
                verdicts.pending(
                    f"no Copilot review of {head_sha[:7]} within {wait_minutes} minutes"
                ),
                None,
            )
        try:
            threads = 0
            review = head_review(repository, number, head_sha)
            if review is not None:
                threads = unresolved_threads(
                    repository, number, budget=lambda: deadline - clock()
                )
        except WaitExpired as exc:
            return verdicts.pending(str(exc)), None
        except github.GitHubError as exc:
            return (
                verdicts.pending(
                    f"could not read the review: {github.safe_message(exc)}"
                ),
                None,
            )
        if review is not None:
            # A push during the wait makes this a review of the old head;
            # confirm the head still stands before accepting the verdict.
            if deadline - clock() < READ_BUDGET_SECONDS:
                return (
                    verdicts.pending("could not confirm the head within the wait"),
                    None,
                )
            try:
                current = pull_head(repository, number)
            except github.GitHubError as exc:
                return (
                    verdicts.pending(
                        f"could not confirm the head: {github.safe_message(exc)}"
                    ),
                    None,
                )
            if current != head_sha:
                return (
                    verdicts.pending(
                        f"head moved from {head_sha[:7]} to {current[:7]} during the wait"
                    ),
                    None,
                )
            review_id = review.get("id")
            return (
                verdicts.classify(review.get("body"), threads),
                review_id if type(review_id) is int else None,
            )
        # Past this point remaining > RESERVE_SECONDS, so the sleep is
        # positive and the loop always advances.
        sleep(min(POLL_SECONDS, remaining - RESERVE_SECONDS))


def published(result: dict[str, Any]) -> tuple[str, int] | None:
    """The pull request a result published, or None when there is none to review."""
    url = result.get("pull_request_url")
    if result.get("dry_run") or result.get("verdict") != "proposed":
        return None
    if not isinstance(url, str) or not url:
        return None
    return pull_reference(url)


def write_result(path: Path, result: dict[str, Any]) -> None:
    """Write result.json back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def run_request(args: argparse.Namespace) -> None:
    """``publish.py review-request``: ask for the review, record the answer."""
    result = model.load_json(args.result, "result")
    target = published(result)
    if target is None:
        print("no pull request to request a review of")
        return
    warning = request_review(*target)
    record = cast("dict[str, Any]", result.get("review") or {})
    record["requested"] = warning is None
    result["review"] = record
    if warning is not None:
        cast("list[Any]", result.setdefault("warnings", [])).append(warning)
    write_result(args.output, result)
    print(f"review request: {'sent' if warning is None else 'refused'}")


def run_await(args: argparse.Namespace) -> None:
    """``publish.py review-await``: wait for the head's review and record it."""
    result = model.load_json(args.result, "result")
    target = published(result)
    if target is None:
        print("no pull request to await a review of")
        return
    repository, number = target
    record = cast("dict[str, Any]", result.get("review") or {})
    head: str | None = None
    review_id: int | None = None
    try:
        head = pull_head(repository, number)
    except github.GitHubError as exc:
        # The wait never started; the record still says why.
        verdict = verdicts.pending(
            f"could not read the pull request head: {github.safe_message(exc)}"
        )
    else:
        verdict, review_id = await_review(
            repository, number, head, wait_minutes=int(args.wait_minutes)
        )
    record.update(verdict.to_json(review_id=review_id, commit_id=head))
    record.setdefault("requested", False)
    result["review"] = record
    write_result(args.output, result)
    print(f"review: {verdict.verdict}; {'; '.join(verdict.reasons)}")
