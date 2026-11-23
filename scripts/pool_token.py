# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fork pool token check the forks job makes before any write.

``fork_pool.py`` creates and renames forks through a classic token of
a machine account in the bot fork organisations (DESIGN.md section
10.6). ``check_token`` refuses any token but a classic one with the
``public_repo`` scope alone, so an operator's own token, or one with
broader scopes, never reaches a write. It also refuses a token that
never expires or expires more than 90 days ahead, and warns when the
expiry is 14 days away or less.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
# A leaked token writes until it lapses, so it must lapse within this
# window, and the job warns while this much of it remains.
MAX_TOKEN_LIFETIME = timedelta(days=90)
ROTATION_WARNING = timedelta(days=14)


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


def check_expiry(expiry: str | None) -> None:
    """Refuse a token that never expires or outlives the rotation window.

    A token within ``ROTATION_WARNING`` of its expiry passes with a
    warning, on every run, so the rotation is due before the job stops.
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
    if left > MAX_TOKEN_LIFETIME:
        raise PoolError(
            f"the fork pool token expires {expiry}, more than "
            f"{MAX_TOKEN_LIFETIME.days} days ahead; create it with an expiry "
            f"within {MAX_TOKEN_LIFETIME.days} days"
        )
    if left <= ROTATION_WARNING:
        print(
            "::warning title=Fork pool token expires soon::the fork pool "
            f"token expires {expiry}; rotate FORK_POOL_TOKEN before then"
        )


def check_token() -> str:
    """Refuse any token but a classic, expiring one scoped to public_repo alone.

    Return the login of the pool account the token belongs to, which
    names that account in a failure it alone can fix.
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
    check_expiry(expiry)
    if login is None:
        raise PoolError("the fork pool token's account reports no login")
    return login
