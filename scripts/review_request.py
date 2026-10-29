# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Ask Copilot to review a bot pull request, and disclose a refusal.

The publish job's request is best effort (DESIGN.md section 8 step
10): a refusal never fails the publish, but becomes a warning and an
``INFO`` notice at the top of the pull request body, which a later
request that succeeds takes down again. GitHub can accept the
request without adding the reviewer, so it counts only once the
pull request's REST read lists the reviewer as a pending request, or
a review of the head the round published exists.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, cast

import bot_github as github

# The mutation's ``botLogins`` input takes the suffixed form.
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
# The reply selects nothing the review token may be unable to read:
# reading ``reviewRequests`` back resolves every pending request, and a
# ruleset's team request needs organisation Members: read the token
# lacks, so GitHub applies the mutation and still answers with an error.
REQUEST_MUTATION = """
mutation($id: ID!, $bots: [String!]!) {
  requestReviewsByLogin(
    input: {pullRequestId: $id, botLogins: $bots, union: true}
  ) {
    clientMutationId
  }
}
"""
UNVERIFIED_REQUEST = (
    "Copilot review request was accepted but no Copilot reviewer was added"
)
UNEXPECTED_REPLY = "the pull request did not list requested reviewers"
# REST names the pending Copilot reviewer ``Copilot``, of type Bot.
REST_REVIEWER_LOGIN = "copilot"
# What a log line may show of a reviewer: GitHub's own name shapes.
TYPENAME_RE = re.compile(r"[A-Za-z]{1,40}")
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")

Reviewer = tuple[str, str | None]


class RequestOutcome(NamedTuple):
    """A request's warning, None when it landed, and the reviewers listed."""

    warning: str | None
    reviewers: tuple[Reviewer, ...] = ()


def is_copilot(reviewer: Reviewer) -> bool:
    """Whether a pending reviewer is the Copilot reviewer bot.

    REST lists it as the Bot ``Copilot``; the reviewer's own login,
    with or without the ``[bot]`` suffix, counts too.
    """
    typename, login = reviewer
    if not isinstance(login, str):
        return False
    name = login.lower().removesuffix("[bot]")
    return name == REVIEWER_NAME or (typename == "Bot" and name == REST_REVIEWER_LOGIN)


def pending_reviewers(pull: dict[str, Any]) -> tuple[Reviewer, ...]:
    """The reviewers a REST pull request read lists as requested.

    ``requested_reviewers`` holds users and bots, ``requested_teams``
    the teams. A ``requested_reviewers`` that is not a list of objects
    is a malformed reply and raises, so it never reads as "no Copilot".
    Only the type and login are kept, and only in the shapes GitHub
    gives them, since they go to the job log.
    """
    users = pull.get("requested_reviewers")
    if not isinstance(users, list):
        raise github.GitHubError(UNEXPECTED_REPLY)
    reviewers: list[Reviewer] = []
    for entry in cast("list[Any]", users):
        if not isinstance(entry, dict):
            raise github.GitHubError(UNEXPECTED_REPLY)
        fields = cast("dict[str, Any]", entry)
        typename = fields.get("type")
        login = fields.get("login")
        reviewers.append(
            (
                typename
                if isinstance(typename, str) and TYPENAME_RE.fullmatch(typename)
                else "?",
                login if isinstance(login, str) and LOGIN_RE.fullmatch(login) else None,
            )
        )
    teams = pull.get("requested_teams")
    if isinstance(teams, list):
        reviewers.extend(("Team", None) for _ in cast("list[Any]", teams))
    return tuple(reviewers)


def verify_request(
    repository: str, number: int, head_sha: str | None
) -> tuple[bool, tuple[Reviewer, ...], str | None]:
    """Whether the request landed, the reviewers pending, and any read failure.

    The request counts when the pull request lists Copilot as a
    pending reviewer, or when Copilot has already reviewed
    ``head_sha``, since a fast review clears the pending request. A
    review of any other commit does not count: on a revision round
    the previous head's review is always there.
    """
    failures: list[str] = []
    reviewers: tuple[Reviewer, ...] = ()
    try:
        pull = github.api_object(f"repos/{repository}/pulls/{number}")
        reviewers = pending_reviewers(pull)
        if any(is_copilot(reviewer) for reviewer in reviewers):
            return True, reviewers, None
    except github.GitHubError as exc:
        failures.append(github.safe_message(exc))
    if head_sha is not None:
        try:
            if head_review(repository, number, head_sha) is not None:
                return True, reviewers, None
        except github.GitHubError as exc:
            failures.append(github.safe_message(exc))
    return False, reviewers, "; ".join(failures) or None


def request_review(
    repository: str, number: int, head_sha: str | None
) -> RequestOutcome:
    """Ask Copilot to review; return a warning instead of raising on failure.

    One ``requestReviewsByLogin`` mutation, additive, run once. ``gh
    pr edit --add-reviewer`` is not used: it replaces the whole
    reviewer set from an earlier read, which can drop a CODEOWNERS
    request. The outcome is read back through REST, which the review
    token can read (``verify_request``), whether or not the mutation
    reported an error: GitHub applies it and still errors when the
    token cannot resolve part of the reply. A request that cannot be
    shown to have landed is a warning, never a success; a failed read
    of the pull request before the mutation sends nothing.
    """
    try:
        pull = github.api_object(f"repos/{repository}/pulls/{number}")
        node_id = github.require_str(pull, "node_id", "pull request")
    except github.GitHubError as exc:
        return RequestOutcome(
            f"could not request a Copilot review: {github.safe_message(exc)}"
        )
    refusal: str | None = None
    try:
        github.graphql(REQUEST_MUTATION, {"id": node_id, "bots": [REVIEWER_LOGIN]})
    except github.GitHubError as exc:
        refusal = github.safe_message(exc)
    landed, reviewers, failure = verify_request(repository, number, head_sha)
    if landed:
        return RequestOutcome(None, reviewers)
    if refusal is not None:
        return RequestOutcome(
            f"could not request a Copilot review: {refusal}", reviewers
        )
    if failure is not None:
        return RequestOutcome(
            f"could not confirm the Copilot review request: {failure}", reviewers
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
