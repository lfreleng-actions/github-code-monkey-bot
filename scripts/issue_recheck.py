# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Re-read an issue's facts before the publisher writes for it.

The selection reads an issue once, and the author session after it
may take an hour; another run may publish for the issue meanwhile, a
human may merge that work, and the issue may close. So the ``push``
command re-makes the selection's checks with the job's own token on
the public target before it touches the fork (DESIGN.md section 8
step 8): the issue's state and labels, every pull request from the
bot branch, and any open pull request linked to close the issue. A
fact that changed is a rejection, as for a revision whose pull
request closed (section 20.3). A read that fails raises, and the
caller fails the step rather than publish on trust.
"""

from __future__ import annotations

import argparse
import urllib.parse
from collections.abc import Callable
from typing import Any, NamedTuple, NoReturn, cast

import bot_github as github
import issue_reads as reads
from branch_refs import branch_matches, is_fork_of, listed_fork
from proposal_policy import PublishError

# The label that keeps the bot off an issue, on a scan and a named run alike.
NO_AGENT = "no-agent"

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      state
      labels(first: 100) { nodes { name } }
      closedByPullRequestsReferences(first: 20, includeClosedPrs: false) {
        nodes { number url }
      }
      timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
        nodes {
          ... on ClosedEvent {
            closer {
              ... on PullRequest { number url }
              ... on Commit { oid }
            }
          }
        }
      }
    }
  }
}
"""


class Refusal(NamedTuple):
    """Why the publisher must not write, and whether the issue is closed."""

    reason: str
    issue_closed: bool = False


class Pull(NamedTuple):
    """A pull request from the bot branch on the target."""

    number: int
    url: str
    state: str
    login: str | None


def _nodes(parent: Any, key: str) -> list[dict[str, Any]]:
    """The object nodes of a GraphQL connection, or none."""
    connection = (
        cast("dict[str, Any]", parent).get(key) if isinstance(parent, dict) else None
    )
    raw = (
        cast("dict[str, Any]", connection).get("nodes")
        if isinstance(connection, dict)
        else None
    )
    if not isinstance(raw, list):
        return []
    return [
        cast("dict[str, Any]", node)
        for node in cast("list[Any]", raw)
        if isinstance(node, dict)
    ]


def read_issue(repository: str, number: int) -> dict[str, Any]:
    """The issue's state, labels, linked pull requests and closer, in one read."""
    owner, _, name = repository.partition("/")
    data = github.graphql(
        ISSUE_QUERY, {"owner": owner, "name": name, "number": number}, read=True
    )
    repo = data.get("repository")
    issue = (
        cast("dict[str, Any]", repo).get("issue") if isinstance(repo, dict) else None
    )
    if not isinstance(issue, dict):
        raise github.GitHubError(f"could not read issue {repository}#{number}")
    return cast("dict[str, Any]", issue)


def closer_of(issue: dict[str, Any]) -> str:
    """What closed the issue, as the timeline says, for a reason."""
    for event in _nodes(issue, "timelineItems"):
        closer = event.get("closer")
        if not isinstance(closer, dict):
            continue
        found = cast("dict[str, Any]", closer)
        if type(found.get("number")) is int:
            return f" by pull request #{found['number']} ({found.get('url')})"
        oid = found.get("oid")
        if isinstance(oid, str) and oid:
            return f" by commit {oid[:7]}"
    return ""


def fork_identity(check: dict[str, Any]) -> str:
    """The target's fork as GitHub names it now, found with reads alone.

    A fork kept from before the target was renamed still carries the
    old name until ``ensure_fork`` renames it, after this check, and
    its pull requests name it so. Found by parent, as ``ensure_fork``
    finds it; with no fork at all, the designated name stands. The
    read by name follows a rename's redirect, so the name kept is the
    one the reply carries, not the one asked for.
    """
    target = str(check["repository"])
    fork = str(check["fork_repository"])
    try:
        data = github.api_object(f"repos/{fork}")
        if is_fork_of(data, target):
            name = data.get("full_name")
            return name if isinstance(name, str) and name else fork
    except github.GitHubError as exc:
        if not github.is_absent(exc):
            raise
    return listed_fork(target, str(check["fork_org"])) or fork


def branch_pulls(repository: str, fork_repository: str, branch: str) -> list[Pull]:
    """Every pull request, in any state, from the bot branch in the fork."""
    fork_owner = fork_repository.partition("/")[0]
    head = urllib.parse.quote(f"{fork_owner}:{branch}", safe="")
    found: list[Pull] = []
    for entry in github.api_list(
        f"repos/{repository}/pulls?state=all&head={head}&per_page=100"
    ):
        head_data = entry.get("head")
        head_repo = (
            cast("dict[str, Any]", head_data).get("repo")
            if isinstance(head_data, dict)
            else None
        )
        name = (
            cast("dict[str, Any]", head_repo).get("full_name")
            if isinstance(head_repo, dict)
            else None
        )
        if not isinstance(name, str) or name.lower() != fork_repository.lower():
            continue
        number = entry.get("number")
        if type(number) is not int:
            continue
        user = entry.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        found.append(
            Pull(
                number,
                str(entry.get("html_url") or f"#{number}"),
                reads.pull_state(entry),
                login if isinstance(login, str) else None,
            )
        )
    return found


def own_rerun(check: dict[str, Any], fork: str, pull: Pull) -> bool:
    """Whether the open pull request is this proposal's own, from an earlier attempt.

    A rerun of a publish job whose first attempt opened the pull
    request meets it here. It counts only when the bot opened it and
    the branch in ``fork``, the fork as it is named now, holds exactly
    the proposal's chain, as ``reconcile`` requires before it adopts
    the branch.
    """
    bot = str(check["bot_login"])
    if pull.state != "open" or pull.login != bot:
        return False
    chain = branch_matches(
        fork,
        str(check["branch"]),
        str(check["base_sha"]),
        cast("list[dict[str, Any]]", check["commits"]),
        bot,
    )
    return chain is not None


def stale_selection(check: dict[str, Any]) -> Refusal | None:
    """Why the selection no longer holds for the issue, or None when it does.

    Refused: a closed issue, an issue labelled ``no-agent``, an open
    or merged pull request from the bot branch, a closed one other
    than the newest when that is the ``retry_of`` a named retry
    carries, and any other open pull request linked to close the
    issue. Pull requests count from the fork under its current name,
    an old one included (``fork_identity``). Reads that fail raise
    ``GitHubError`` or ``PublishError``.
    """
    repository = str(check["repository"])
    number = github.require_int(check, "issue", "check")
    issue = read_issue(repository, number)
    if issue.get("state") != "OPEN":
        return Refusal(
            f"issue #{number} was closed{closer_of(issue)} after this run "
            "selected it; nothing published",
            issue_closed=True,
        )
    labels = [str(node.get("name")) for node in _nodes(issue, "labels")]
    if NO_AGENT in labels:
        return Refusal(
            f"issue #{number} was labelled {NO_AGENT} after this run selected it; "
            "nothing published"
        )
    fork = fork_identity(check)
    pulls = branch_pulls(repository, fork, str(check["branch"]))
    own: str | None = None
    for pull in pulls:
        if pull.state == "merged":
            return Refusal(
                f"pull request #{pull.number} ({pull.url}) from {check['branch']} "
                "was merged; nothing published"
            )
        if pull.state == "open":
            if own is None and own_rerun(check, fork, pull):
                own = pull.url
                continue
            return Refusal(
                f"pull request #{pull.number} ({pull.url}) from {check['branch']} "
                "is open; nothing published"
            )
    closed = [pull.number for pull in pulls if pull.state == "closed"]
    retry_of = check.get("retry_of")
    if (
        closed
        and own is None
        and not (type(retry_of) is int and retry_of == max(closed))
    ):
        newest = max(closed)
        return Refusal(
            f"pull request #{newest} from {check['branch']} was closed and this "
            "run is not a retry of it; nothing published"
        )
    for linked in _nodes(issue, "closedByPullRequestsReferences"):
        url = linked.get("url")
        if own is not None and url == own:
            continue
        return Refusal(
            f"pull request #{linked.get('number')} ({url}) is linked to close "
            f"issue #{number}; nothing published"
        )
    return None


def reject(result: dict[str, Any], reason: str) -> None:
    """Record a rejection, as for a revision whose pull request closed."""
    result["verdict"] = "rejected"
    cast("list[str]", result["reasons"]).append(reason)


def rejected(
    args: argparse.Namespace,
    check: dict[str, Any],
    result: dict[str, Any],
    *,
    fail: Callable[[argparse.Namespace, dict[str, Any], str, Exception], NoReturn],
) -> bool:
    """Record a rejection in ``result`` when the selection no longer holds.

    A closed issue sets ``issue_closed``, so the comment step leaves
    the finished issue alone. A read that fails goes to ``fail``, the
    publisher's own, which records the failure and raises: nothing
    publishes on trust.
    """
    try:
        refusal = stale_selection(check)
    except (PublishError, github.GitHubError) as exc:
        detail = f"could not re-read issue #{check.get('issue')} ({exc})"
        fail(args, result, f"{detail}; nothing written", exc)
    if refusal is None:
        return False
    reject(result, refusal.reason)
    if refusal.issue_closed:
        result["issue_closed"] = True
    return True


def already_exists(exc: Exception, result: dict[str, Any]) -> bool:
    """Record a rejection when a pull request create answered 422: one exists.

    The same fact as an open pull request from the branch, found at
    ``open`` rather than ``push``, when it is not the bot's own: a
    rejection, not a publisher failure. The fork branch stays.
    """
    if not (
        isinstance(exc, github.GitHubError)
        and exc.status == 422
        and "a pull request already exists" in str(exc).lower()
    ):
        return False
    reject(result, f"{exc}; fork branch kept")
    return True
