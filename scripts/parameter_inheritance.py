# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A hand-back round's run parameters, read from its pull request (DESIGN.md 20.1).

With ``inherit_run_parameters`` the select job takes the run
parameters from the latest round footer the App posted for the one
named pull request, in place of the form's. Any doubt keeps the
form's values: a history with no ``params=`` token, one the selector
cannot read, and a token with any field that fails its check.
"""

from __future__ import annotations

import argparse
from typing import Any

import bot_github as github
import fork_orgs as forks
import issue_reads as reads
import repository_reads as repositories
import revision_reads as revisions
import round_reads as rounds_of
import run_parameters
from fork_orgs import ForkOrgError


def last_token(
    org: str, pull: tuple[str, int], bot_login: str, fork_orgs: dict[str, Any]
) -> tuple[str | None, str | None, str]:
    """The latest footer's token and run URL for the pull request, or why there is none."""
    repo_name, number = pull
    meta = repositories.repository_meta(org, repo_name)
    if meta is None:
        return None, None, "the repository cannot be read"
    repository = f"{org}/{meta['name']}"
    try:
        fork_org = forks.resolve(fork_orgs, org)
    except ForkOrgError as exc:
        return None, None, str(exc)
    found = revisions.bot_pull_request(
        repository, number, bot_login, f"{fork_org}/{meta['name']}"
    )
    if found is None:
        return None, None, "not an open pull request the bot opened from its fork"
    history = rounds_of.round_history(
        repository, int(found["issue"]), bot_login, str(found["created_at"]), number
    )
    return history.last_params, history.last_run, ""


def apply(args: argparse.Namespace, values: dict[str, str]) -> None:
    """Put the inherited values the selector itself reads in place of the form's."""
    args.model = values["model"]
    args.fusion_tier = values["fusion_tier"]
    args.max_rounds = values["max_rounds"]
    args.stale_status_minutes = values["stale_status_minutes"]
    args.include_dotgithub = values["include_dotgithub"] == "true"


def inherit(
    args: argparse.Namespace,
    bot: dict[str, Any],
    fork_orgs: dict[str, Any],
    pulls: list[tuple[str, int]],
) -> dict[str, Any]:
    """Inherit the named pull request's run parameters into ``args``.

    Returns the selection's record of it: the values inherited, the
    run whose footer carried them, and a note for the summary. With
    nothing inherited, ``args`` keeps the form's values; with the
    switch off, the record is empty and the selection unchanged.
    """
    if not getattr(args, "inherit_run_parameters", False):
        return {}
    record: dict[str, Any] = {
        "inherit_run_parameters": True,
        "inherited_parameters": {},
        "inherited_from": None,
        "inheritance_note": "",
    }

    def keep(note: str, *, warn: bool) -> dict[str, Any]:
        record["inheritance_note"] = f"{note}; the form's values stand"
        if warn:
            print(
                f"::warning::run parameters not inherited: {record['inheritance_note']}"
            )
        return record

    if bot.get("placeholder"):
        return keep("no App to read the round history", warn=False)
    try:
        token, run, why = last_token(args.org, pulls[0], str(bot["login"]), fork_orgs)
    except (github.GitHubError, reads.SelectionError) as exc:
        return keep(f"the round history could not be read: {exc}", warn=True)
    if why:
        return keep(why, warn=True)
    if token is None:
        return keep("no round footer records the parameters", warn=False)
    try:
        values = run_parameters.decode(token)
    except run_parameters.ParameterError as exc:
        return keep(f"field {exc.field} failed its check ({exc})", warn=True)
    apply(args, values)
    record["inherited_parameters"] = values
    record["inherited_from"] = run
    return record
