# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Ask Copilot to review a bot pull request, and disclose a refusal.

The publish job's request is best effort (DESIGN.md section 8 step
10): a refusal never fails the publish, but becomes a warning and an
``INFO`` notice at the top of the pull request body, which a later
request that succeeds takes down again. GitHub can accept the
request without adding the reviewer, so it counts only once the
reviewer reads back.
"""

from __future__ import annotations

from typing import Any, cast

import bot_github as github

# GraphQL names the bot without the suffix REST adds.
REVIEWER_NAME = "copilot-pull-request-reviewer"

# A refused review request is disclosed at the top of the pull request
# body (#29), once; the marker makes a retry idempotent.
REFUSAL_NOTICE_MARKER = "<!-- code-monkey review-request-refused -->"
# The one line the notice holds after its marker; removal takes the
# marker and this line alone.
REFUSAL_NOTICE_LINE = "> **INFO** The workflow "
MAX_PR_BODY = 65_536

REQUESTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewRequests(first: 100) {
        nodes {
          requestedReviewer {
            __typename
            ... on Bot { login }
            ... on User { login }
          }
        }
      }
    }
  }
}
"""
UNVERIFIED_REQUEST = (
    "Copilot review request was accepted but no Copilot reviewer was added"
)


def is_copilot(login: object) -> bool:
    """Whether a login names the Copilot reviewer bot, in either form."""
    if not isinstance(login, str):
        return False
    return login.lower().removesuffix("[bot]") == REVIEWER_NAME


def request_review(repository: str, number: int) -> str | None:
    """Ask Copilot to review; return a warning instead of raising on failure.

    ``@copilot`` is the form GitHub accepts; the bare login fails. The
    request is best effort: a refusal is recorded, and the wait still
    runs in case a review starts another way.

    Exit status 0 proves nothing: called with an App installation
    token, the mutation ``gh`` sends succeeds without adding the
    reviewer, so the request counts only once ``verify_request``
    reads it back.
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
    return verify_request(repository, number)


def copilot_requested(repository: str, number: int) -> bool:
    """Whether the Copilot reviewer is among the pending review requests.

    GraphQL, because its ``requestedReviewer`` union includes ``Bot``;
    the REST ``requested_reviewers`` listing is not known to.
    """
    owner, _, name = repository.partition("/")
    data = github.graphql(
        REQUESTS_QUERY, {"owner": owner, "name": name, "number": number}, read=True
    )
    reply = data.get("repository")
    pull = (
        cast("dict[str, Any]", reply).get("pullRequest")
        if isinstance(reply, dict)
        else None
    )
    requests = (
        cast("dict[str, Any]", pull).get("reviewRequests")
        if isinstance(pull, dict)
        else None
    )
    nodes = (
        cast("dict[str, Any]", requests).get("nodes")
        if isinstance(requests, dict)
        else None
    )
    if not isinstance(nodes, list):
        raise github.GitHubError("review request query returned no nodes")
    for node in cast("list[Any]", nodes):
        reviewer = (
            cast("dict[str, Any]", node).get("requestedReviewer")
            if isinstance(node, dict)
            else None
        )
        if isinstance(reviewer, dict) and is_copilot(
            cast("dict[str, Any]", reviewer).get("login")
        ):
            return True
    return False


def copilot_reviewed(repository: str, number: int) -> bool:
    """Whether Copilot has reviewed the pull request at all, of any commit."""
    for review in github.api_list(f"repos/{repository}/pulls/{number}/reviews"):
        user = review.get("user")
        if isinstance(user, dict) and is_copilot(
            cast("dict[str, Any]", user).get("login")
        ):
            return True
    return False


def verify_request(repository: str, number: int) -> str | None:
    """Confirm the request landed; return a warning when it did not.

    A review already given counts, since it clears the pending
    request. A failed read is a warning too, never a success.
    """
    try:
        if copilot_requested(repository, number) or copilot_reviewed(
            repository, number
        ):
            return None
    except github.GitHubError as exc:
        return (
            f"could not confirm the Copilot review request: {github.safe_message(exc)}"
        )
    return UNVERIFIED_REQUEST


def add_refusal_notice(repository: str, number: int, warning: str) -> str | None:
    """Put the refusal at the top of the pull request body, once; warn on failure.

    The notice tells a maintainer the review they expect was never
    requested, in the place they read first. Best effort with the
    token the publish already holds: no permission widens for it.
    """
    try:
        data = github.api_object(f"repos/{repository}/pulls/{number}")
        body = data.get("body")
        current = body if isinstance(body, str) else ""
        if current.startswith(REFUSAL_NOTICE_MARKER):
            return None
        notice = (
            f"{REFUSAL_NOTICE_MARKER}\n{REFUSAL_NOTICE_LINE}{warning}; "
            "no Copilot review was requested for this pull request, so a "
            "maintainer should request one.\n\n"
        )
        if len(notice) + len(current) > MAX_PR_BODY:
            return "could not add the review notice: the body is at its size limit"
        github.api_write(
            "PATCH", f"repos/{repository}/pulls/{number}", {"body": notice + current}
        )
    except github.GitHubError as exc:
        return f"could not add the review notice: {github.safe_message(exc)}"
    return None


def remove_refusal_notice(repository: str, number: int) -> str | None:
    """Take an earlier refusal notice off the body once a request succeeds.

    The notice is the publisher's own block: the marker and the INFO
    paragraph, up to the first blank line. Best effort, like placing
    it; a failure is a warning.
    """
    try:
        data = github.api_object(f"repos/{repository}/pulls/{number}")
        body = data.get("body")
        current = body if isinstance(body, str) else ""
        if not current.startswith(REFUSAL_NOTICE_MARKER):
            return None
        end = current.find("\n\n")
        block = current[:end].split("\n") if end >= 0 else []
        if len(block) != 2 or not block[1].startswith(REFUSAL_NOTICE_LINE):
            # A maintainer's edit moved the blank line that bounds the
            # notice: cutting there would take their text, or the
            # whole description, with it. The notice stays.
            return "the review notice was left in place: its boundary is gone"
        rest = current[end + 2 :]
        github.api_write("PATCH", f"repos/{repository}/pulls/{number}", {"body": rest})
    except github.GitHubError as exc:
        return f"could not remove the review notice: {github.safe_message(exc)}"
    return None
