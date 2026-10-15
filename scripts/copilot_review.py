# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Request a Copilot review of a new pull request, wait for it, classify it.

Two publish-job commands (DESIGN.md section 8 steps 10 and 11):

``review-request`` runs with the pull request token. GitHub starts no
review by itself for a pull request a GitHub App opened, so the
publisher asks for one. The request is best effort, like the label: a
failure is a warning in ``result.json`` and never fails the publish.

``review-await`` runs with the job's own token: the target is public,
so reading reviews and review threads needs no App token. It polls,
for a bounded time, for a review by Copilot of the published head and
records the classified verdict. A review that never arrives is
``pending``, never clean.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from typing import Any, cast

import bot_github as github
import proposal_model as model
import proposal_policy as policy
import review_verdict as verdicts

REVIEWER = "@copilot"
REVIEWER_LOGIN = "copilot-pull-request-reviewer[bot]"
MAX_DETAIL = 500
MAX_THREAD_PAGES = 10
WAIT_SECONDS = 900
INTERVAL_SECONDS = 30

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes { isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def pull_request_number(result: dict[str, Any]) -> int | None:
    """The number of the pull request ``result.json`` names on its own target."""
    url = result.get("pull_request_url")
    repository = result.get("repository")
    if not isinstance(url, str) or not isinstance(repository, str):
        return None
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/pull/([1-9][0-9]{{0,9}})", url
    )
    return int(match.group(1)) if match else None


def detail(exc: BaseException) -> str:
    """GitHub's reason for a failure, flattened and bounded for the record."""
    return policy.log_safe(str(exc))[:MAX_DETAIL]


def request_review(repository: str, number: int) -> str | None:
    """Ask Copilot to review; return a warning instead of raising on failure."""
    try:
        github.run_gh(
            [
                "pr",
                "edit",
                str(number),
                "--repo",
                repository,
                "--add-reviewer",
                REVIEWER,
            ]
        )
    except github.GitHubError as exc:
        return f"could not request a Copilot review: {detail(exc)}"
    return None


def published_head(result: dict[str, Any], repository: str, number: int) -> str:
    """The head commit the publisher pushed, or the pull request's own head.

    An adopted branch replays nothing in this run, so its head comes
    from the pull request.
    """
    commits = cast("list[Any]", result.get("commits") or [])
    if (
        commits
        and isinstance(commits[-1], str)
        and github.SHA_RE.fullmatch(commits[-1])
    ):
        return commits[-1]
    data = github.api_object(f"repos/{repository}/pulls/{number}")
    head = data.get("head")
    if not isinstance(head, dict):
        raise github.GitHubError("pull request: missing head")
    return github.require_sha(cast("dict[str, Any]", head), "sha", "pull request head")


def review_of_head(repository: str, number: int, head: str) -> tuple[int, str] | None:
    """The latest submitted Copilot review of ``head``: its ID and body."""
    found: tuple[int, str] | None = None
    for entry in github.api_list(
        f"repos/{repository}/pulls/{number}/reviews?per_page=100"
    ):
        user = entry.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        review_id = entry.get("id")
        if (
            login != REVIEWER_LOGIN
            or entry.get("commit_id") != head
            or entry.get("state") == "PENDING"
            or type(review_id) is not int
        ):
            continue
        if found is None or review_id > found[0]:
            body = entry.get("body")
            found = (review_id, body if isinstance(body, str) else "")
    return found


def unresolved_threads(repository: str, number: int) -> int:
    """Count the pull request's unresolved review threads through GraphQL."""
    owner, _, name = repository.partition("/")
    count = 0
    after: str | None = None
    for _ in range(MAX_THREAD_PAGES):
        data = github.graphql(
            THREADS_QUERY,
            {"owner": owner, "name": name, "number": number, "after": after},
            read=True,
        )
        try:
            threads = data["repository"]["pullRequest"]["reviewThreads"]
            nodes = cast("list[Any]", threads["nodes"])
            page = threads["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise github.GitHubError("reviewThreads: unexpected reply") from exc
        count += sum(
            1
            for node in nodes
            if isinstance(node, dict)
            and cast("dict[str, Any]", node).get("isResolved") is not True
        )
        if not page.get("hasNextPage"):
            return count
        after = page.get("endCursor")
    raise github.GitHubError("reviewThreads: too many pages")


def await_review(
    repository: str,
    number: int,
    head: str,
    wait_seconds: float,
    interval_seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str | None]:
    """Poll for the head's review until it arrives or the wait runs out.

    Returns the ``review`` record and a warning for a read that kept
    failing; a failed read is retried until the deadline, then counts
    as no review.
    """
    deadline = clock() + wait_seconds
    problem: str | None = None
    while True:
        try:
            found = review_of_head(repository, number, head)
            if found is not None:
                review_id, body = found
                threads = unresolved_threads(repository, number)
                record = verdicts.classify(body, threads).to_json(review_id, head)
                return record, None
            problem = None
        except github.GitHubError as exc:
            problem = f"could not read the Copilot review: {detail(exc)}"
        if clock() >= deadline:
            return verdicts.pending().to_json(commit_id=head), problem
        sleep(max(0.0, min(interval_seconds, deadline - clock())))


def wants_review(result: dict[str, Any]) -> bool:
    """Whether this result opened, or adopted, a pull request to review."""
    return (
        result.get("verdict") == "proposed"
        and not result.get("dry_run")
        and pull_request_number(result) is not None
    )


def run_request(args: argparse.Namespace) -> None:
    """Request the review and record ``pending`` until one is read."""
    result = model.load_json(args.result, "result")
    number = pull_request_number(result)
    if not wants_review(result) or number is None:
        print("review: no pull request to review")
        return
    warning = request_review(str(result["repository"]), number)
    warnings = cast("list[Any]", result.setdefault("warnings", []))
    if warning:
        warnings.append(warning)
    # A wait that is cut short leaves this record: pending, not clean.
    result["review"] = verdicts.pending().to_json()
    write(args, result)
    print("review: requested" if warning is None else "review: request failed")


def run_await(args: argparse.Namespace) -> None:
    """Wait for the review of the head and record its classified verdict."""
    result = model.load_json(args.result, "result")
    number = pull_request_number(result)
    if not wants_review(result) or number is None:
        print("review: no pull request to review")
        return
    repository = str(result["repository"])
    warnings = cast("list[Any]", result.setdefault("warnings", []))
    try:
        head = published_head(result, repository, number)
    except github.GitHubError as exc:
        warnings.append(f"could not read the pull request head: {detail(exc)}")
        result["review"] = verdicts.pending().to_json()
        write(args, result)
        print("review: pending")
        return
    record, problem = await_review(
        repository, number, head, args.wait_seconds, args.interval_seconds
    )
    if problem:
        warnings.append(problem)
    result["review"] = record
    write(args, result)
    print(f"review: {record['verdict']}")


def write(args: argparse.Namespace, result: dict[str, Any]) -> None:
    """Write ``result.json`` back."""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
