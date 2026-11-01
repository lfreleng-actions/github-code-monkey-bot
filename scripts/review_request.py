# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Ask Copilot to review a bot pull request, and disclose a refusal.

The publish job's request is best effort (DESIGN.md section 8 step
10): a refusal never fails the publish, but becomes a warning and an
``INFO`` notice at the top of the pull request body, which a later
request that succeeds takes down again. GitHub can accept the
request without adding the reviewer, so it counts only once the
issue's events show a Copilot ``review_requested`` event newer than
any read before the mutation, a Copilot request was already pending
when the step began, or a review of the head the round published
exists. Neither REST ``requested_reviewers``, which never lists the
Copilot bot, nor GraphQL ``reviewRequests``, which the review token
cannot resolve, can show it.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
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
UNREADABLE_REVIEW = "a Copilot review carried no readable submission time"
# REST events name the requested Copilot reviewer ``Copilot``, of type Bot.
REST_REVIEWER_LOGIN = "copilot"
# The issue events that request or withdraw a reviewer.
REQUEST_EVENTS = frozenset({"review_requested", "review_request_removed"})
# The events lag the request: read again after each delay before
# calling it refused, as revision_publish does for a lagging head.
VERIFY_BACKOFF_SECONDS = (2, 4, 8)
# What a log line may show of a reviewer: GitHub's own name shapes.
TYPENAME_RE = re.compile(r"[A-Za-z]{1,40}")
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")

Reviewer = tuple[str, str | None]


class RequestOutcome(NamedTuple):
    """A request's warning, None when it landed, and the reviewers listed."""

    warning: str | None
    reviewers: tuple[Reviewer, ...] = ()


class RequestEvent(NamedTuple):
    """A Copilot ``review_requested`` or ``review_request_removed`` event."""

    event_id: int
    kind: str
    created_at: str


def is_copilot(reviewer: Reviewer) -> bool:
    """Whether a requested reviewer is the Copilot reviewer bot.

    REST events name it the Bot ``Copilot``; the reviewer's own login,
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
    the teams. GitHub never lists the Copilot reviewer bot here, so the
    list serves the job log alone. A ``requested_reviewers`` that is
    not a list of objects is a malformed reply and raises. Only the
    type and login are kept, and only in the shapes GitHub gives them,
    since they go to the job log.
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


def read_events(repository: str, number: int) -> tuple[int, list[RequestEvent]]:
    """The newest event id on the issue, and its Copilot review request events.

    Every page of ``issues/{number}/events`` is read. The newest id of
    any event bounds what a later read may count as this request's
    own: event ids increase, so the comparison needs no clock.
    """
    newest = 0
    found: list[RequestEvent] = []
    for event in github.api_list(f"repos/{repository}/issues/{number}/events"):
        event_id = event.get("id")
        if not isinstance(event_id, int) or isinstance(event_id, bool):
            continue
        newest = max(newest, event_id)
        kind = event.get("event")
        reviewer = event.get("requested_reviewer")
        if kind not in REQUEST_EVENTS or not isinstance(reviewer, dict):
            continue
        fields = cast("dict[str, Any]", reviewer)
        typename = fields.get("type")
        login = fields.get("login")
        if not is_copilot(
            (
                typename if isinstance(typename, str) else "?",
                login if isinstance(login, str) else None,
            )
        ):
            continue
        created = event.get("created_at")
        found.append(
            RequestEvent(
                event_id, str(kind), created if isinstance(created, str) else ""
            )
        )
    return newest, found


def timestamp(value: Any) -> datetime | None:
    """A GitHub ISO 8601 timestamp, or None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def newest_review_time(repository: str, number: int) -> datetime | None:
    """When Copilot last submitted a review of any commit, or None.

    A Copilot review whose time cannot be read raises, so it never
    reads as "no review yet".
    """
    newest: datetime | None = None
    for review in github.api_list(f"repos/{repository}/pulls/{number}/reviews"):
        user = review.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        if login != REVIEWER_LOGIN or review.get("state") == "PENDING":
            continue
        submitted = timestamp(review.get("submitted_at"))
        if submitted is None:
            raise github.GitHubError(UNREADABLE_REVIEW)
        if newest is None or submitted > newest:
            newest = submitted
    return newest


def already_pending(repository: str, number: int, events: list[RequestEvent]) -> bool:
    """Whether a Copilot request was pending before this step asked again.

    It is when the newest Copilot request event is a request, not a
    removal, and is newer than Copilot's newest review: no review has
    answered it yet.
    """
    if not events:
        return False
    newest = max(events, key=lambda event: event.event_id)
    requested = timestamp(newest.created_at)
    if newest.kind != "review_requested" or requested is None:
        return False
    reviewed = newest_review_time(repository, number)
    return reviewed is None or requested > reviewed


def verify_request(
    repository: str,
    number: int,
    head_sha: str | None,
    baseline: int | None,
    prior: list[RequestEvent] | None = None,
) -> tuple[bool, str | None]:
    """Whether the request landed, and any read failure when it cannot be shown.

    The request counts when the issue's events hold a Copilot
    ``review_requested`` event newer than ``baseline``, the newest
    event read before the mutation, or when Copilot has already
    reviewed ``head_sha``, since a fast review clears the pending
    request. A review of any other commit does not count: on a
    revision round the previous head's review is always there. The
    events lag the request, so both are read again after each delay
    in ``VERIFY_BACKOFF_SECONDS`` before the request is called
    refused. With no ``baseline`` an event cannot be bound to this
    request, and only the head review can prove it. ``prior`` holds
    the events read before the mutation when the check for a request
    already pending could not read the reviews: that check is retried
    on each pass against those same events, since an additive request
    for a pending reviewer may add no event.
    """
    failures: list[str] = []
    unsettled = prior
    for delay in (*VERIFY_BACKOFF_SECONDS, None):
        failures = []
        if unsettled is not None:
            try:
                if already_pending(repository, number, unsettled):
                    return True, None
                unsettled = None
            except github.GitHubError as exc:
                failures.append(github.safe_message(exc))
        if baseline is not None:
            try:
                _, events = read_events(repository, number)
                if any(
                    event.kind == "review_requested" and event.event_id > baseline
                    for event in events
                ):
                    return True, None
            except github.GitHubError as exc:
                failures.append(github.safe_message(exc))
        if head_sha is not None:
            try:
                if head_review(repository, number, head_sha) is not None:
                    return True, None
            except github.GitHubError as exc:
                failures.append(github.safe_message(exc))
        if delay is None or (baseline is None and head_sha is None):
            break
        time.sleep(delay)
    return False, "; ".join(failures) or None


def listed_reviewers(repository: str, number: int) -> tuple[Reviewer, ...]:
    """The reviewers the REST pull request lists, for the log; best effort.

    REST never lists the Copilot reviewer bot as pending, so this read
    decides nothing; a failed or malformed read lists none.
    """
    try:
        return pending_reviewers(
            github.api_object(f"repos/{repository}/pulls/{number}")
        )
    except github.GitHubError:
        return ()


def request_review(
    repository: str, number: int, head_sha: str | None
) -> RequestOutcome:
    """Ask Copilot to review; return a warning instead of raising on failure.

    One ``requestReviewsByLogin`` mutation, additive, run once. ``gh
    pr edit --add-reviewer`` is not used: it replaces the whole
    reviewer set from an earlier read, which can drop a CODEOWNERS
    request. The issue's events are read before the mutation, to bind
    the request to this step and to find one already pending, and
    after it (``verify_request``), whether or not the mutation
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
    baseline: int | None = None
    prior: list[RequestEvent] | None = None
    pending = False
    unread: str | None = None
    try:
        baseline, events = read_events(repository, number)
    except github.GitHubError as exc:
        unread = github.safe_message(exc)
    else:
        try:
            pending = already_pending(repository, number, events)
        except github.GitHubError:
            # Verification retries the check against these events.
            prior = events
    refusal: str | None = None
    try:
        github.graphql(REQUEST_MUTATION, {"id": node_id, "bots": [REVIEWER_LOGIN]})
    except github.GitHubError as exc:
        refusal = github.safe_message(exc)
    landed, failure = (
        (True, None)
        if pending
        else verify_request(repository, number, head_sha, baseline, prior)
    )
    reviewers = listed_reviewers(repository, number)
    if landed:
        return RequestOutcome(None, reviewers)
    if refusal is not None:
        return RequestOutcome(
            f"could not request a Copilot review: {refusal}", reviewers
        )
    failure = "; ".join(text for text in (unread, failure) if text) or None
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
