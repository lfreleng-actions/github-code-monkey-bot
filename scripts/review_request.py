# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Ask Copilot to review a bot pull request, and disclose a refusal.

The publish job's request is best effort (DESIGN.md section 8 step
10): a refusal never fails the publish, but becomes a warning and an
``INFO`` notice at the top of the pull request body, which a later
request that succeeds takes down again. GitHub can accept the
request without adding the reviewer, so it counts only once the
mutation's own reply names the reviewer as a pending request, or a
review of the head the round published exists.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, cast

import bot_github as github

# GraphQL replies name the bot without the suffix REST adds; the
# mutation's ``botLogins`` input takes the suffixed form.
REVIEWER_NAME = "copilot-pull-request-reviewer"
REVIEWER_LOGIN = f"{REVIEWER_NAME}[bot]"

# A refused review request is disclosed at the top of the pull request
# body (#29), once; the marker makes a retry idempotent.
REFUSAL_NOTICE_MARKER = "<!-- code-monkey review-request-refused -->"
# The one line the notice holds after its marker; removal takes the
# marker and this line alone.
REFUSAL_NOTICE_LINE = "> **INFO** The workflow "
MAX_PR_BODY = 65_536

# One additive request: ``union: true`` adds the bot to the existing
# reviewers instead of replacing the set, so no user or team is sent.
REQUEST_MUTATION = """
mutation($id: ID!, $bots: [String!]!) {
  requestReviewsByLogin(
    input: {pullRequestId: $id, botLogins: $bots, union: true}
  ) {
    pullRequest {
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
UNEXPECTED_REPLY = (
    "could not confirm the Copilot review request: "
    "the reply did not list review requests in the expected shape"
)
# What a log line may show of a reviewer: GitHub's own name shapes.
TYPENAME_RE = re.compile(r"[A-Za-z]{1,40}")
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")

Reviewer = tuple[str, str | None]


class RequestOutcome(NamedTuple):
    """A request's warning, None when it landed, and the reviewers listed."""

    warning: str | None
    reviewers: tuple[Reviewer, ...] = ()


def is_copilot(login: object) -> bool:
    """Whether a login names the Copilot reviewer bot, in either form."""
    if not isinstance(login, str):
        return False
    return login.lower().removesuffix("[bot]") == REVIEWER_NAME


def listed_reviewers(data: dict[str, Any]) -> tuple[Reviewer, ...] | None:
    """The reviewers the mutation's reply lists, or None for any other shape.

    A null node or reviewer is skipped; a non-null one that is not an
    object, a node without the selected ``requestedReviewer``, or a
    reviewer ``well_formed`` refuses makes the whole reply malformed,
    so a head review cannot turn it into a success.

    Only the typename and login are kept, and only in the shapes
    GitHub gives them, since they go to the job log.
    """
    payload = data.get("requestReviewsByLogin")
    pull = (
        cast("dict[str, Any]", payload).get("pullRequest")
        if isinstance(payload, dict)
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
        return None
    reviewers: list[Reviewer] = []
    for node in cast("list[Any]", nodes):
        # GraphQL may null a node or its reviewer (one the token cannot
        # see); anything else that is not an object is a malformed reply.
        if node is None:
            continue
        if not isinstance(node, dict):
            return None
        entry = cast("dict[str, Any]", node)
        if "requestedReviewer" not in entry:
            return None
        reviewer = entry["requestedReviewer"]
        if reviewer is None:
            continue
        if not isinstance(reviewer, dict):
            return None
        fields = cast("dict[str, Any]", reviewer)
        if not well_formed(fields):
            return None
        typename = fields["__typename"]
        login = fields.get("login")
        reviewers.append(
            (
                typename if TYPENAME_RE.fullmatch(typename) else "?",
                login if isinstance(login, str) and LOGIN_RE.fullmatch(login) else None,
            )
        )
    return tuple(reviewers)


def well_formed(reviewer: dict[str, Any]) -> bool:
    """Whether a reviewer carries the fields the mutation selects.

    Every reviewer names its ``__typename``; a Bot or User names its
    ``login`` too, and no other type selects one. A reviewer missing
    either, or holding one of the wrong type, is a malformed reply,
    checked before the display values are sanitised.
    """
    typename = reviewer.get("__typename")
    if not isinstance(typename, str) or not typename:
        return False
    login = reviewer.get("login")
    if typename in {"Bot", "User"}:
        return isinstance(login, str) and bool(login)
    return "login" not in reviewer or (isinstance(login, str) and bool(login))


def request_review(
    repository: str, number: int, head_sha: str | None
) -> RequestOutcome:
    """Ask Copilot to review; return a warning instead of raising on failure.

    One ``requestReviewsByLogin`` mutation, additive, run once. ``gh
    pr edit --add-reviewer`` is not used: it replaces the whole
    reviewer set from an earlier read, which can drop a CODEOWNERS
    request, and reports nothing of what GitHub did. The mutation's
    reply lists the pending requests, so the request counts when it
    names the Copilot reviewer, or when Copilot has already reviewed
    ``head_sha``, since a fast review clears the pending request. A
    review of any other commit does not count: on a revision round
    the previous head's review is always there. A reply of any other
    shape is a warning, never a success.
    """
    try:
        pull = github.api_object(f"repos/{repository}/pulls/{number}")
        node_id = github.require_str(pull, "node_id", "pull request")
        data = github.graphql(
            REQUEST_MUTATION, {"id": node_id, "bots": [REVIEWER_LOGIN]}
        )
    except github.GitHubError as exc:
        return RequestOutcome(
            f"could not request a Copilot review: {github.safe_message(exc)}"
        )
    reviewers = listed_reviewers(data)
    if reviewers is None:
        return RequestOutcome(UNEXPECTED_REPLY)
    if any(is_copilot(login) for _, login in reviewers):
        return RequestOutcome(None, reviewers)
    try:
        if (
            head_sha is not None
            and head_review(repository, number, head_sha) is not None
        ):
            return RequestOutcome(None, reviewers)
    except github.GitHubError as exc:
        return RequestOutcome(
            f"could not confirm the Copilot review request: {github.safe_message(exc)}",
            reviewers,
        )
    return RequestOutcome(UNVERIFIED_REQUEST, reviewers)


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
