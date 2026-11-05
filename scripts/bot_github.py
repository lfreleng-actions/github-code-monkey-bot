# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads and writes shared by the select, apply and report scripts.

Kept apart from the selection and review logic so that the rules
stay readable without API plumbing interleaved through them.
Nothing here decides whether an action is permitted; callers do
that first.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

API_VERSION = "2026-03-10"
# gh ends its message with the status in one of two forms: after the
# message of a JSON reply, "gh: Not Found (HTTP 404)", or alone for any
# other reply, such as an XML error from artifact storage, "gh: HTTP
# 403". Each alternative is a whole form that ends a line, and the last
# match wins, so neither a hybrid nor a status quoted earlier counts.
STATUS_RE = re.compile(r"\(HTTP (\d{3})\)$|^gh: HTTP (\d{3})$", re.MULTILINE)
ABSENT = frozenset({404, 410})
TIMEOUT_SECONDS = 60
TRANSIENT = frozenset({500, 502, 503, 504})
READ_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
# A recoverable write waits longer between tries than a read does: a
# read retries through a blip, a write is retried through an incident,
# where GitHub answers 5xx for minutes rather than seconds.
WRITE_RETRY_DELAYS_SECONDS = (2, 6, 18)
WRITE_ATTEMPTS = len(WRITE_RETRY_DELAYS_SECONDS) + 1
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
# gh prints a failed call's JSON reply on stdout and keeps only its
# summary for stderr; the reply's ``errors`` entries say what failed.
MAX_ERROR_DETAIL = 500
MAX_ERROR_ENTRIES = 5
# Keys a REST ``errors`` entry uses; the ones that name the failure.
ERROR_ENTRY_KEYS = ("resource", "field", "code", "message")


def parse_status(text: str) -> int | None:
    """The HTTP status gh appends to ``text``, in either of its forms."""
    matches = STATUS_RE.findall(text)
    if not matches:
        return None
    json_form, bare_form = matches[-1]
    return int(json_form or bare_form)


class GitHubError(Exception):
    """A call to GitHub failed.

    Carries the HTTP status where gh reported one, so callers can
    tell an endpoint that is absent from one that is briefly
    unreachable.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Record the message and the status given, or else gh's."""
        super().__init__(message)
        self.status: int | None = (
            status if status is not None else parse_status(message)
        )


T = TypeVar("T")


def is_transient(exc: GitHubError) -> bool:
    """Whether a failure may pass if the same call is made again.

    An HTTP status decides on its own: GitHub's reply text reaches the
    message, so a 422 whose detail mentions a timeout is still a 422.
    The "timed out" wording counts only for a failure with no status,
    such as gh's own timeout.
    """
    if exc.status is not None:
        return exc.status in TRANSIENT
    return "timed out" in str(exc)


def is_read(args: list[str]) -> bool:
    """Whether a gh invocation only reads, and so is safe to repeat.

    GraphQL goes through one endpoint for queries and mutations alike,
    so no GraphQL call counts as a read here unless the caller says so.
    """
    if args[:1] == ["api"]:
        return args[1:2] != ["graphql"] and "--method" not in args
    return args[:2] in (["pr", "list"], ["pr", "view"], ["search", "prs"])


def entry_text(entry: Any) -> str:
    """One ``errors`` entry as text: ``Resource.field code: message``."""
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return ""
    data = cast("dict[str, Any]", entry)
    parts = {key: data.get(key) for key in ERROR_ENTRY_KEYS}
    where = ".".join(
        str(parts[key]) for key in ("resource", "field") if isinstance(parts[key], str)
    )
    code = parts["code"] if isinstance(parts["code"], str) else ""
    message = parts["message"] if isinstance(parts["message"], str) else ""
    head = " ".join(text for text in (where, code) if text)
    return f"{head}: {message}" if head and message else head or message


def error_detail(body: str) -> str:
    """The ``errors`` entries of a failed call's reply, as one bounded line.

    Empty when the reply is not JSON or carries no entries. Newlines
    are flattened, so nothing in the reply can add a line that
    ``parse_status`` would read.
    """
    try:
        reply = json.loads(body)
    except (ValueError, RecursionError):
        return ""
    errors = (
        cast("dict[str, Any]", reply).get("errors") if isinstance(reply, dict) else None
    )
    if not isinstance(errors, list):
        return ""
    entries = cast("list[Any]", errors)
    texts = [text for text in map(entry_text, entries[:MAX_ERROR_ENTRIES]) if text]
    if len(entries) > MAX_ERROR_ENTRIES:
        texts.append(f"and {len(entries) - MAX_ERROR_ENTRIES} more")
    detail = " ".join("; ".join(texts).split())
    return detail[:MAX_ERROR_DETAIL]


def run_once(args: list[str], input: str | None) -> str:
    """One gh invocation, raising GitHubError on any failure."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=input,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh timed out after {TIMEOUT_SECONDS} seconds") from exc
    except OSError as exc:
        raise GitHubError(f"could not run gh: {exc}") from exc
    if proc.returncode != 0:
        summary = proc.stderr.strip() or f"gh {' '.join(args)} failed"
        status = parse_status(summary)
        detail = error_detail(proc.stdout) if status is not None else ""
        # The status is gh's, passed explicitly: the detail is reply
        # text and never gets to supply one.
        raise GitHubError(f"{summary}: {detail}" if detail else summary, status)
    return proc.stdout


def run_gh(
    args: list[str], *, input: str | None = None, read: bool | None = None
) -> str:
    """Run gh with a pinned REST API version, returning stdout or raising.

    Reads retry a transient 5xx or timeout with a short backoff: a
    selection makes many reads, and one 502 should not cost the run.
    Writes run once; a repeated review submission is not harmless. A
    caller that can tell whether a write took effect makes it
    recoverable with :func:`write_with_recovery` instead. ``read``
    overrides the guess from the arguments, for a GraphQL query the
    caller knows to be read-only.
    """
    if args[:1] == ["api"] and args[1:2] != ["graphql"]:
        args = [*args, "--header", f"X-GitHub-Api-Version: {API_VERSION}"]
    repeatable = is_read(args) if read is None else read
    attempts = READ_ATTEMPTS if repeatable else 1
    for attempt in range(1, attempts + 1):
        try:
            return run_once(args, input)
        except GitHubError as exc:
            if attempt == attempts or not is_transient(exc):
                raise
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def write_with_recovery(
    write: Callable[[], T],
    landed: Callable[[], T | None],
    *,
    repeat_safe: bool,
    attempts: int | None = None,
    label: str = "write",
) -> T:
    """Run a write once, and recover it from a transient failure.

    A 5xx or a timeout on a write is ambiguous: GitHub may have applied
    it and lost the reply, or may still be applying it. Repeating it
    blindly could apply it twice, which is why ``run_gh`` never retries
    a write.

    ``write`` performs the write and returns its result. ``landed``
    reads the resource the write would have changed and returns the
    result the write would have returned when it took effect, or
    ``None`` when it has not. Only ``None`` means "not landed". After a
    transient failure, ``landed`` is consulted, and a write that landed
    returns that result.

    ``repeat_safe`` is the caller's statement, required at every call,
    of whether the write may be sent again when ``landed`` finds no
    effect. "Not landed yet" is not "will not land": a timed-out request
    can still be processed after the look. A write is repeat-safe when
    a late first copy cannot combine with a second, as with a
    compare-and-swap (``createCommitOnBranch``'s ``expectedHeadOid``:
    the second commit fails if the first arrives) or an idempotent
    call (syncing a fork). Such a write is tried again after
    ``WRITE_RETRY_DELAYS_SECONDS``, up to ``attempts`` in all, default
    ``WRITE_ATTEMPTS``. A write found only by a marker, such as a
    review or comment carrying the run's identifier, is not
    repeat-safe: the marker proves a write landed when present, but
    its absence does not prove the request is finished. With
    ``repeat_safe=False`` the helper looks once and never writes again.

    ``landed`` is consulted after the last failure too, since that
    write may also have landed. A failure ``landed`` cannot settle is
    never read as "not landed": when it raises, the write fails with
    both errors and keeps the write's own status. A failure that is not
    transient raises at once, without a look, as it always has.
    ``landed`` must recognise this write's own effect, not merely some
    change.

    Raises:
        GitHubError: The write failed and did not land, failed with a
            non-transient error, or could not be checked.
        ValueError: ``attempts`` is outside 1 to ``WRITE_ATTEMPTS``, or
            above 1 for a write that is not repeat-safe.
    """
    if attempts is None:
        attempts = WRITE_ATTEMPTS if repeat_safe else 1
    if not 1 <= attempts <= WRITE_ATTEMPTS:
        raise ValueError(f"attempts must be 1 to {WRITE_ATTEMPTS}, not {attempts}")
    if attempts > 1 and not repeat_safe:
        raise ValueError("a write that is not repeat-safe is attempted once")
    for attempt in range(1, attempts + 1):
        try:
            return write()
        except GitHubError as exc:
            if not is_transient(exc):
                raise
            try:
                outcome = landed()
            except GitHubError as lookup:
                unknown = GitHubError(
                    f"{exc}; could not tell whether the {label} landed: {lookup}"
                )
                # The write's own status, even None for a timeout: left to
                # the constructor, None means "parse the message", which
                # would take the look's status, and a 404 from the look
                # would then read as the write's target being absent.
                unknown.status = exc.status
                raise unknown from lookup
            if outcome is not None:
                note(f"{label}: landed despite {safe_message(exc)}")
                return outcome
            if attempt == attempts:
                raise
            delay = WRITE_RETRY_DELAYS_SECONDS[attempt - 1]
            note(
                f"{label}: attempt {attempt} of {attempts} did not land "
                f"({safe_message(exc)}); retrying in {delay}s"
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def note(message: str) -> None:
    """Report a recovered or retried write on stderr, never stdout."""
    print(message, file=sys.stderr)


def decode_response(raw: str) -> Any:
    """Keep malformed API responses on the operational-failure path."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise GitHubError(f"invalid JSON from GitHub: {exc}") from exc


def api_object(endpoint: str) -> dict[str, Any]:
    """Read one endpoint that returns a JSON object."""
    parsed = decode_response(run_gh(["api", endpoint]))
    if not isinstance(parsed, dict):
        raise GitHubError(f"expected an object from {endpoint}")
    return cast("dict[str, Any]", parsed)


def api_list(endpoint: str) -> list[dict[str, Any]]:
    """Read every page, refusing incomplete or non-list responses."""
    raw = run_gh(["api", endpoint, "--paginate", "--slurp"])
    pages = decode_response(raw)
    if not isinstance(pages, list):
        raise GitHubError(f"expected paginated arrays from {endpoint}")
    entries: list[dict[str, Any]] = []
    for page in cast("list[Any]", pages):
        if not isinstance(page, list):
            raise GitHubError(f"expected an array page from {endpoint}")
        for entry in cast("list[Any]", page):
            if not isinstance(entry, dict):
                raise GitHubError(f"expected an object entry from {endpoint}")
            entries.append(cast("dict[str, Any]", entry))
    return entries


def api_write(method: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a JSON body with the given method and return the object reply."""
    raw = run_gh(
        ["api", "--method", method, endpoint, "--input", "-"],
        input=json.dumps(payload),
    )
    parsed: Any = decode_response(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise GitHubError(f"expected an object from {method} {endpoint}")
    return cast("dict[str, Any]", parsed)


def graphql(
    query: str, variables: dict[str, Any], *, read: bool = False
) -> dict[str, Any]:
    """Run one GraphQL operation, surfacing any error entry as a failure.

    gh exits non-zero when the response carries ``errors``, and the
    message lands on stderr; a partial ``data`` payload is never
    returned to the caller as if it were complete. Operations run once
    unless the caller passes ``read=True`` for a query with no side
    effects, which then retries transient failures like a REST read.
    """
    raw = run_gh(
        ["api", "graphql", "--input", "-"],
        input=json.dumps({"query": query, "variables": variables}),
        read=read,
    )
    parsed = decode_response(raw)
    if not isinstance(parsed, dict):
        raise GitHubError("expected an object from GraphQL")
    reply = cast("dict[str, Any]", parsed)
    if reply.get("errors"):
        raise GitHubError(f"GraphQL errors: {json.dumps(reply['errors'])[:2000]}")
    data = reply.get("data")
    if not isinstance(data, dict):
        raise GitHubError("GraphQL reply carried no data object")
    return cast("dict[str, Any]", data)


def is_absent(exc: GitHubError) -> bool:
    """Whether a failure means the resource does not exist."""
    return exc.status in ABSENT


def require_str(data: dict[str, Any], key: str, context: str) -> str:
    """Return a non-empty string field or fail the operation."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubError(f"{context}: missing or invalid {key!r}")
    return value


def require_int(data: dict[str, Any], key: str, context: str) -> int:
    """Return a positive integer field or fail the operation."""
    value = data.get(key)
    if type(value) is not int or value <= 0:
        raise GitHubError(f"{context}: missing or invalid {key!r}")
    return value


def require_sha(data: dict[str, Any], key: str, context: str) -> str:
    """Return a 40-hex commit SHA field or fail the operation."""
    value = require_str(data, key, context)
    if not SHA_RE.fullmatch(value):
        raise GitHubError(f"{context}: {key!r} is not a commit SHA")
    return value


def safe_message(exc: BaseException) -> str:
    """Render an error for a log that workflow commands are parsed from."""
    return ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
