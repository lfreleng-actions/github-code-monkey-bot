# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Find, create or rename the bot forks a run's selection needs.

The forks job runs this beside the author matrix (DESIGN.md section
4.3). Creating or renaming a fork needs Administration: write, which
a GitHub App can hold only App-wide, on the target organisation as
much as on the fork pools. A machine account that belongs to the bot
fork organisations alone does it instead, through a classic token
with the ``public_repo`` scope and nothing more; the token check
below refuses any other kind, so an operator's own token, or one
with broader scopes, never reaches a write.

``fork_pool.py prepare --config FILE --org ORG --matrix-env NAME``
reads the select job's matrix from the environment variable ``NAME``
and readies one fork per target repository in it. Every target is
attempted; a failure on one is reported and the rest go ahead, so the
publisher can still publish to the forks that are ready. The exit
status is 1 when any target failed, and 0 with a warning when no
token was provided at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, cast

import bot_github as github
import branch_refs as refs
import fork_orgs as forks
from proposal_policy import PublishError

# The one scope the token may carry: enough to fork a public repository
# into, and rename it within, an organisation the account belongs to.
REQUIRED_SCOPES = frozenset({"public_repo"})
SCOPES_HEADER = "x-oauth-scopes"
FORK_WAIT_SECONDS = 60
FORK_POLL_SECONDS = 5


class PoolError(Exception):
    """The matrix, the mapping or the token rules out preparing forks."""


class Target(NamedTuple):
    """One repository whose fork the run needs, and where that fork lives."""

    repository: str
    fork_org: str
    repo_name: str

    @property
    def fork(self) -> str:
        """The fork's full name in its pool."""
        return f"{self.fork_org}/{self.repo_name}"


def text_field(entry: dict[str, Any], key: str) -> str:
    """A non-empty string field of a matrix entry, or a PoolError."""
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise PoolError(f"matrix entry has no {key}")
    return value


def targets(matrix: str, config: dict[str, Any], org: str) -> list[Target]:
    """The distinct targets of the select job's matrix, each one proven.

    The matrix is trusted output of the select job, but every entry
    must still agree with the committed mapping: a fork organisation
    the mapping does not name for ``org`` is never written to.
    """
    try:
        parsed: Any = json.loads(matrix)
    except (ValueError, RecursionError) as exc:
        raise PoolError(f"the matrix is not valid JSON: {exc}") from exc
    include = (
        cast("dict[str, Any]", parsed).get("include")
        if isinstance(parsed, dict)
        else None
    )
    if not isinstance(include, list):
        raise PoolError("the matrix carries no include list")
    try:
        fork_org = forks.resolve(config, org)
    except forks.ForkOrgError as exc:
        raise PoolError(str(exc)) from exc
    if not forks.ALLOWED_RE.fullmatch(fork_org) or fork_org.lower() == org.lower():
        raise PoolError(f"{fork_org} is not a bot fork organisation for {org}")
    found: dict[str, Target] = {}
    for raw in cast("list[Any]", include):
        if not isinstance(raw, dict):
            raise PoolError("a matrix entry is not an object")
        entry = cast("dict[str, Any]", raw)
        repo_name = text_field(entry, "repo_name")
        target = Target(text_field(entry, "repository"), fork_org, repo_name)
        if target.repository.lower() != f"{org}/{repo_name}".lower():
            raise PoolError(f"{target.repository} is not {repo_name} in {org}")
        if text_field(entry, "fork_org").lower() != fork_org.lower():
            raise PoolError(f"{target.repository} names another fork organisation")
        if text_field(entry, "fork_repository").lower() != target.fork.lower():
            raise PoolError(f"{target.repository} names another fork repository")
        found.setdefault(target.fork.lower(), target)
    return list(found.values())


def token_scopes() -> frozenset[str] | None:
    """The classic token's scopes, or None for a token that reports none.

    GitHub reports a classic token's scopes in the X-OAuth-Scopes
    header of any API reply; fine-grained and App tokens carry none.
    """
    raw = github.run_gh(["api", "--include", "user"])
    for line in raw.splitlines():
        if not line.strip():
            break
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == SCOPES_HEADER:
            return frozenset(s.strip() for s in value.split(",") if s.strip())
    return None


def check_token() -> None:
    """Refuse any token but a classic one scoped to public_repo alone."""
    try:
        scopes = token_scopes()
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


def await_fork(fork: str, deadline: float) -> dict[str, Any]:
    """Poll for a fork GitHub is still creating until ``deadline``.

    Forking is asynchronous. Every fork a run requested shares one
    deadline, so the waits overlap rather than add up.
    """
    while True:
        try:
            return github.api_object(f"repos/{fork}")
        except github.GitHubError as exc:
            if not github.is_absent(exc) or time.monotonic() >= deadline:
                raise PublishError(f"fork {fork} did not appear: {exc}") from exc
        time.sleep(FORK_POLL_SECONDS)


def create_fork(target: str, fork_org: str) -> str:
    """Fork the target into the pool; return the name GitHub replies with.

    GitHub never holds two forks of one parent in one organisation, and
    answers a repeat request with the existing fork, so the reply, not
    the target's name, says where the fork is. Forking is asynchronous:
    the caller polls for a new fork before using it.
    """
    try:
        reply = github.api_write(
            "POST",
            f"repos/{target}/forks",
            {"organization": fork_org, "default_branch_only": True},
        )
    except github.GitHubError as exc:
        raise PublishError(f"could not fork {target} into {fork_org}: {exc}") from exc
    name = reply.get("full_name")
    if not isinstance(name, str) or not name.lower().startswith(f"{fork_org.lower()}/"):
        raise PublishError(f"forking {target} named no fork in {fork_org}: {name!r}")
    return name


class Pending(NamedTuple):
    """A fork requested or renamed, which GitHub may still be making."""

    target: Target
    outcome: str


def start(target: Target) -> str | Pending:
    """Find the target's fork, or request or rename it; never wait.

    A fork kept from before the target was renamed still carries the
    old name; it is found by parent and renamed after the target, so
    the publisher's reads of ``<fork_org>/<repo_name>`` find it. A
    repository of that name which is not a fork of the target is a
    failure, never a push destination: the publisher writes only into
    forks it can prove are the target's. A fork found ready returns
    its outcome; one requested or renamed is Pending until
    :func:`finish` reads it back.
    """
    fork = target.fork
    try:
        data = github.api_object(f"repos/{fork}")
    except github.GitHubError as exc:
        if not github.is_absent(exc):
            raise PublishError(f"could not read fork {fork}: {exc}") from exc
    else:
        if not refs.is_fork_of(data, target.repository):
            raise PublishError(
                f"{fork} exists but is not a fork of {target.repository}"
            )
        return "present"
    listed = refs.listed_fork(target.repository, target.fork_org)
    found = listed or create_fork(target.repository, target.fork_org)
    if found.lower() != fork.lower():
        try:
            github.api_write("PATCH", f"repos/{found}", {"name": target.repo_name})
        except github.GitHubError as exc:
            raise PublishError(
                f"could not rename fork {found} to {target.repo_name}: {exc}"
            ) from exc
    if listed is not None:
        return Pending(target, f"renamed from {listed}")
    if found.lower() != fork.lower():
        return Pending(target, f"created as {found}, then renamed")
    return Pending(target, "created")


def finish(pending: Pending, deadline: float) -> str:
    """Wait for a pending fork and prove it is the target's."""
    target = pending.target
    if not refs.is_fork_of(await_fork(target.fork, deadline), target.repository):
        raise PublishError(
            f"{target.fork} exists but is not a fork of {target.repository}"
        )
    return pending.outcome


class Summary:
    """Report each fork as its outcome is final, so a cut-short job keeps it."""

    def __init__(self, path: Path | None) -> None:
        """Start the step summary's table at ``path``, when there is one."""
        self.path = path
        self.failed = 0
        if path is not None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("## Forks\n\n| Fork | Outcome |\n| ---- | ------- |\n")

    def record(self, fork: str, outcome: str) -> None:
        """Log one fork's outcome and append its row."""
        print(f"{fork}: {outcome}")
        self.write(fork, outcome)

    def fail(self, fork: str, exc: Exception) -> None:
        """Log and append a fork that could not be readied."""
        self.failed += 1
        outcome = f"failed: {github.safe_message(exc)}"
        print(f"::error title=Fork not ready::{fork}: {outcome}")
        self.write(fork, outcome)

    def write(self, fork: str, outcome: str) -> None:
        """Append one row to the step summary."""
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"| `{fork}` | {outcome} |\n")


def run_prepare(args: argparse.Namespace) -> int:
    """Ready every target's fork; return the exit status.

    Every missing fork is requested first and then awaited against one
    deadline: GitHub makes forks in parallel, so a large first run
    against a new organisation waits about as long as a single fork.
    """
    if not os.environ.get("GH_TOKEN"):
        print(
            "::warning title=No fork pool token::forks are neither created "
            "nor renamed; a target without its fork fails to publish"
        )
        return 0
    try:
        config = forks.load(args.config)
    except forks.ForkOrgError as exc:
        raise PoolError(str(exc)) from exc
    chosen = targets(os.environ.get(args.matrix_env, ""), config, args.org)
    if not chosen:
        print("No targets: no fork to prepare")
        return 0
    check_token()
    summary = Summary(args.summary)
    waiting: list[Pending] = []
    for target in chosen:
        try:
            state = start(target)
        except (PublishError, github.GitHubError) as exc:
            summary.fail(target.fork, exc)
            continue
        if isinstance(state, Pending):
            waiting.append(state)
        else:
            summary.record(target.fork, state)
    if waiting:
        deadline = time.monotonic() + FORK_WAIT_SECONDS
        for pending in waiting:
            try:
                summary.record(pending.target.fork, finish(pending, deadline))
            except (PublishError, github.GitHubError) as exc:
                summary.fail(pending.target.fork, exc)
    return 1 if summary.failed else 0


def build_parser() -> argparse.ArgumentParser:
    """Describe the one command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("prepare", help="ready the selection's forks")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--org", required=True)
    run.add_argument("--matrix-env", default="MATRIX")
    run.add_argument("--summary", type=Path, default=None)
    run.set_defaults(handler=run_prepare)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run a command; a PoolError exits 1 with its message."""
    args = build_parser().parse_args(argv)
    try:
        status = args.handler(args)
    except PoolError as exc:
        print(f"::error title=Forks not prepared::{exc}")
        sys.exit(1)
    sys.exit(status)


if __name__ == "__main__":
    main()
