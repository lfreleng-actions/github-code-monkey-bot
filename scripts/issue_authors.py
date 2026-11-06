# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Whether an issue's author lets a scan work it unasked.

Issues triage gives anyone's issue a Type, so a Type alone is no
human decision. A scan works an issue from an owner or member of the
target; any other author, an absent or unreadable association, or an
App needs a maintainer's ``agent-approved`` label first (DESIGN.md
section 6). Every site that applies the rule calls ``admits``, so
they cannot drift. A named issue or round applies no author rule:
naming it is the approval.
"""

from __future__ import annotations

from typing import Any, cast

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})
APPROVAL_LABEL = "agent-approved"


def is_bot(user: Any) -> bool:
    """Whether a REST ``user`` or a search ``author`` object is an App or bot.

    The REST API types an App's account ``Bot`` and suffixes its login
    with ``[bot]``; ``gh search`` reports ``is_bot`` and prefixes the
    login with ``app/``. Any one of them is enough.
    """
    if not isinstance(user, dict):
        return False
    data = cast("dict[str, Any]", user)
    login = data.get("login")
    return (
        data.get("type") == "Bot"
        or data.get("is_bot") is True
        or (
            isinstance(login, str)
            and (login.endswith("[bot]") or login.startswith("app/"))
        )
    )


def author_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """The author fields of a REST issue: its association and whether an App opened it."""
    association = issue.get("author_association")
    return {
        "author_association": association if isinstance(association, str) else None,
        "author_is_bot": is_bot(issue.get("user")),
    }


def author_admitted(association: Any, labels: list[str], *, bot: bool = False) -> bool:
    """Whether the author, or failing that the approval label, admits the issue.

    An owner or member who is not an App passes; anyone else passes
    only once a maintainer adds ``APPROVAL_LABEL``. The ``no-agent``
    opt-out is the callers' test and comes first, so an issue carrying
    both labels stays skipped.
    """
    trusted = isinstance(association, str) and association in TRUSTED_ASSOCIATIONS
    return (trusted and not bot) or APPROVAL_LABEL in labels


def admits(entry: dict[str, Any]) -> bool:
    """``author_admitted`` over a candidate or issue details with ``labels``."""
    return author_admitted(
        entry.get("author_association"),
        entry["labels"],
        bot=bool(entry.get("author_is_bot")),
    )
