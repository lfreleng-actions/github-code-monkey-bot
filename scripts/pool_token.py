# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fork pool token check the forks job makes, and its scheduled alert.

``fork_pool.py`` creates and renames forks through a classic token of
a machine account in the bot fork organisations (DESIGN.md section
10.6). ``check_token`` refuses any token but a classic one with the
``public_repo`` scope alone, so an operator's own token, or one with
broader scopes, never reaches a write. It also refuses a token that
never expires or expires further ahead than the lifetime the
``FORK_POOL_TOKEN_MAX_DAYS`` variable sets: a whole number of days
from 1 to 366, or 90 when the variable is empty. It warns when the
expiry is 21 days away or less. The token's expiry header decides
every refusal; the ``FORK_POOL_TOKEN_EXPIRES`` variable, the expiry
date an operator records, only draws a warning when it disagrees
with the header by more than a day.

``pool_token.py expiry-check --output FILE --summary FILE`` is the
scheduled check, which holds no token and so reads that variable
alone. It writes ``days_left``, ``notify`` and the Slack ``message``
to the output file: ``notify`` is true from seven days before the
expiry, and after it. An unset variable warns and notifies nothing;
an unreadable one exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, cast

import bot_github as github

# The one scope the token may carry: enough to fork a public repository
# into, and rename it within, an organisation the account belongs to.
REQUIRED_SCOPES = frozenset({"public_repo"})
SCOPES_HEADER = "x-oauth-scopes"
# GitHub names a classic token's expiry here, as ``2027-01-18 10:39:33
# UTC``, and omits the header for a token that never expires.
EXPIRY_HEADER = "github-authentication-token-expiration"
EXPIRY_FORMAT = "%Y-%m-%d %H:%M:%S %z"
# A leaked token writes until it lapses, so it must lapse within the
# lifetime the variable sets, or this default when it is empty. The
# ceiling keeps a large number from switching the rule off in effect.
MAX_DAYS_VARIABLE = "FORK_POOL_TOKEN_MAX_DAYS"
DEFAULT_LIFETIME_DAYS = 90
LIFETIME_CEILING_DAYS = 366
# The expiry date an operator records, for the scheduled check that
# cannot read the header. Its date may sit a day off the header's,
# which is in UTC.
EXPIRES_VARIABLE = "FORK_POOL_TOKEN_EXPIRES"
EXPIRES_SLACK = timedelta(days=1)
# Every run warns while this much of the token's life remains, and the
# scheduled check posts to Slack from the last days.
ROTATION_WARNING = timedelta(days=21)
ALERT_DAYS = 7
DAYS_PATTERN = re.compile(r"[0-9]{1,3}")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class PoolError(Exception):
    """The matrix, the mapping or the token rules out preparing forks."""


class TokenIdentity(NamedTuple):
    """What ``gh api --include user`` reports about the pool token."""

    scopes: frozenset[str] | None
    expiry: str | None
    login: str | None


def token_identity() -> TokenIdentity:
    """The classic token's scopes and expiry, and its account's login.

    GitHub reports a classic token's scopes in the X-OAuth-Scopes
    header of any API reply; fine-grained and App tokens carry none,
    and the scopes are None for them. The expiry header is None for a
    token that never expires. The login comes from the body.
    """
    lines = github.run_gh(["api", "--include", "user"]).splitlines()
    end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))
    headers: dict[str, str] = {}
    for line in lines[:end]:
        name, sep, value = line.partition(":")
        if sep:
            headers.setdefault(name.strip().lower(), value.strip())
    scopes_text = headers.get(SCOPES_HEADER)
    scopes = (
        None
        if scopes_text is None
        else frozenset(s.strip() for s in scopes_text.split(",") if s.strip())
    )
    try:
        user: Any = json.loads("\n".join(lines[end + 1 :]))
    except (ValueError, RecursionError):
        user = None
    login = (
        cast("dict[str, Any]", user).get("login") if isinstance(user, dict) else None
    )
    return TokenIdentity(
        scopes,
        headers.get(EXPIRY_HEADER),
        login if isinstance(login, str) and login else None,
    )


def token_lifetime(text: str | None) -> timedelta:
    """The longest a token may run, from the FORK_POOL_TOKEN_MAX_DAYS value.

    Empty or unset is the default; anything but ASCII digits naming 1
    to ``LIFETIME_CEILING_DAYS`` days is refused, naming what was read.
    """
    if not text:
        return timedelta(days=DEFAULT_LIFETIME_DAYS)
    if DAYS_PATTERN.fullmatch(text) and 1 <= int(text) <= LIFETIME_CEILING_DAYS:
        return timedelta(days=int(text))
    raise PoolError(
        f"{MAX_DAYS_VARIABLE} is {text!r}; set it to a whole number of days "
        f"from 1 to {LIFETIME_CEILING_DAYS}, or leave it empty for "
        f"{DEFAULT_LIFETIME_DAYS}"
    )


def recorded_expiry(text: str) -> date:
    """The FORK_POOL_TOKEN_EXPIRES date, read as ``YYYY-MM-DD`` alone."""
    problem = f"{EXPIRES_VARIABLE} is {text!r}, not a YYYY-MM-DD date"
    if not DATE_PATTERN.fullmatch(text):
        raise PoolError(problem)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise PoolError(problem) from exc


def compare_recorded(when: datetime, recorded: str | None) -> None:
    """Warn when the recorded expiry has drifted from the header's.

    The scheduled check reads the variable alone, so a stale date
    there silences its alert; the header still decides this job.
    """
    if not recorded:
        return
    try:
        noted = recorded_expiry(recorded)
    except PoolError as exc:
        print(
            "::warning title=Fork pool token expiry unreadable::"
            f"{exc}; the scheduled expiry check fails until it is corrected"
        )
        return
    actual = when.astimezone(UTC).date()
    if abs(noted - actual) > EXPIRES_SLACK:
        print(
            "::warning title=Fork pool token expiry mismatch::the fork pool "
            f"token expires {actual.isoformat()}, but {EXPIRES_VARIABLE} "
            f"says {noted.isoformat()}; set the variable to the token's "
            "expiry date"
        )


def check_expiry(
    expiry: str | None,
    lifetime: timedelta = timedelta(days=DEFAULT_LIFETIME_DAYS),
    recorded: str | None = None,
) -> None:
    """Refuse a token that never expires or outlives ``lifetime``.

    A token within ``ROTATION_WARNING`` of its expiry passes with a
    warning, on every run, so the rotation is due before the job stops.
    ``recorded``, the FORK_POOL_TOKEN_EXPIRES value, can only warn.
    """
    if expiry is None:
        raise PoolError("the fork pool token never expires; create it with an expiry")
    text = expiry.removesuffix(" UTC") + " +0000" if expiry.endswith(" UTC") else expiry
    try:
        when = datetime.strptime(text, EXPIRY_FORMAT)
    except ValueError as exc:
        raise PoolError(
            f"the fork pool token's expiry {expiry!r} cannot be read"
        ) from exc
    left = when - datetime.now(UTC)
    if left > lifetime:
        raise PoolError(
            f"the fork pool token expires {expiry}, more than "
            f"{lifetime.days} days ahead; create it with an expiry "
            f"within {lifetime.days} days, the limit {MAX_DAYS_VARIABLE} sets"
        )
    if left <= ROTATION_WARNING:
        print(
            "::warning title=Fork pool token expires soon::the fork pool "
            f"token expires {expiry}; rotate FORK_POOL_TOKEN before then"
        )
    compare_recorded(when, recorded)


def check_token(
    lifetime: timedelta = timedelta(days=DEFAULT_LIFETIME_DAYS),
    recorded: str | None = None,
) -> str:
    """Refuse any token but a classic, expiring one scoped to public_repo alone.

    ``lifetime`` and ``recorded`` go to ``check_expiry``. Return the
    login of the pool account the token belongs to, which names that
    account in a failure it alone can fix.
    """
    try:
        scopes, expiry, login = token_identity()
    except github.GitHubError as exc:
        raise PoolError(f"could not read the fork pool token's scopes: {exc}") from exc
    if scopes is None:
        raise PoolError(
            "the fork pool token reports no scopes; use a classic token "
            "with the public_repo scope alone"
        )
    if scopes != REQUIRED_SCOPES:
        shown = ", ".join(sorted(scopes)) or "none"
        raise PoolError(
            f"the fork pool token carries scopes {shown}; it must carry "
            "public_repo alone"
        )
    check_expiry(expiry, lifetime, recorded)
    if login is None:
        raise PoolError("the fork pool token's account reports no login")
    return login


class Alert(NamedTuple):
    """What the scheduled check found in FORK_POOL_TOKEN_EXPIRES."""

    expires: date | None
    days_left: int | None
    notify: bool
    annotations: tuple[str, ...]


def expiry_alert(recorded: str | None, today: date) -> Alert:
    """Judge the recorded expiry against ``today``, a UTC date.

    An empty value warns that nothing can be checked; an unreadable
    one raises PoolError.
    """
    if not recorded:
        return Alert(
            None,
            None,
            False,
            (
                "::warning title=Fork pool token expiry not checked::"
                f"{EXPIRES_VARIABLE} is not set; set it to the expiry date of "
                "FORK_POOL_TOKEN, as YYYY-MM-DD, so this check can run",
            ),
        )
    expires = recorded_expiry(recorded)
    left = (expires - today).days
    annotations: tuple[str, ...] = ()
    if left <= ROTATION_WARNING.days:
        annotations = (
            "::warning title=Fork pool token expires soon::"
            f"{status(expires, left)}; rotate FORK_POOL_TOKEN and update "
            f"{EXPIRES_VARIABLE}",
        )
    return Alert(expires, left, left <= ALERT_DAYS, annotations)


def status(expires: date, left: int) -> str:
    """One sentence on when the token lapses, or lapsed."""
    day = expires.isoformat()
    if left < 0:
        gone = "1 day" if left == -1 else f"{-left} days"
        return f"the fork pool token expired on {day}, {gone} ago"
    if left == 0:
        return f"the fork pool token expires today, {day}"
    days = "1 day" if left == 1 else f"{left} days"
    return f"the fork pool token expires on {day}, in {days}"


def alert_message(alert: Alert, repository: str, run_url: str, test: bool) -> str:
    """The one-line Slack text: repository, secret, date, days left, run."""
    if alert.expires is None or alert.days_left is None:
        state = f"{EXPIRES_VARIABLE} is not set, so no expiry date is known"
    else:
        state = status(alert.expires, alert.days_left)
    link = f"<{run_url}|Workflow run>"
    if test:
        return (
            f"[TEST] {repository}: a test of the FORK_POOL_TOKEN expiry alert, "
            f"no action needed; {state}. {link}"
        )
    return (
        f"{repository}: FORK_POOL_TOKEN: {state}. Rotate the secret and "
        f"update {EXPIRES_VARIABLE}. {link}"
    )


def run_expiry_check(args: argparse.Namespace) -> int:
    """Write the scheduled check's outputs and summary; return the status."""
    test = os.environ.get("TEST_NOTIFICATION") == "true"
    try:
        alert = expiry_alert(os.environ.get(EXPIRES_VARIABLE), datetime.now(UTC).date())
    except PoolError as exc:
        print(f"::error title=Fork pool token expiry not checked::{exc}")
        return 1
    for line in alert.annotations:
        print(line)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    notify = alert.notify or test
    left = "" if alert.days_left is None else str(alert.days_left)
    message = alert_message(alert, repository, run_url, test)
    with args.output.open("a", encoding="utf-8") as handle:
        handle.write(f"days_left={left}\nnotify={str(notify).lower()}\n")
        handle.write(f"message={message}\n")
    if args.summary is not None:
        expires = "not set" if alert.expires is None else alert.expires.isoformat()
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(
                "## Fork pool token expiry\n\n"
                f"| Expiry ({EXPIRES_VARIABLE}) | Days left | Slack |\n"
                "| --- | --- | --- |\n"
                f"| {expires} | {left or 'n/a'} | "
                f"{'test post' if test else 'post' if notify else 'none'} |\n"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Describe the one command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("expiry-check", help="the scheduled expiry check")
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--summary", type=Path, default=None)
    check.set_defaults(handler=run_expiry_check)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run a command and exit with its status."""
    args = build_parser().parse_args(argv)
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
