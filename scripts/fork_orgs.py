# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which bot fork organisation holds the forks for a target owner.

The mapping lives in ``config/fork-orgs.json`` (DESIGN.md section
4.3). Every value must be one of the ``lfreleng-bot-forks*``
organisations whatever the file says: the pattern is fixed here so
that an edit to the configuration alone cannot point the publisher's
write token at an organisation that is not a bot fork pool.

``fork_orgs.py check --config FILE`` validates the file;
``fork_orgs.py resolve --config FILE --owner OWNER`` prints
``fork_org=<name>`` for ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

ALLOWED_RE = re.compile(r"^lfreleng-bot-forks(-[a-z0-9]+)?$")
OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_BYTES = 64 * 1024


class ForkOrgError(Exception):
    """The mapping file is malformed, or a target has no acceptable pool."""


def load(path: Path) -> dict[str, Any]:
    """Read and validate the mapping; raise ForkOrgError on any defect."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ForkOrgError(f"cannot read {path}: {exc}") from exc
    if len(raw) > MAX_BYTES:
        raise ForkOrgError(f"{path} exceeds {MAX_BYTES} bytes")
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ForkOrgError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ForkOrgError(f"{path} must hold a JSON object")
    config = cast("dict[str, Any]", parsed)
    default = config.get("default")
    if not isinstance(default, str) or not ALLOWED_RE.fullmatch(default):
        raise ForkOrgError("default is not an allowed fork organisation")
    organisations = config.get("organisations")
    if not isinstance(organisations, dict):
        raise ForkOrgError("organisations must be an object")
    checked: dict[str, str] = {}
    for key, value in cast("dict[Any, Any]", organisations).items():
        if not isinstance(key, str) or not OWNER_RE.fullmatch(key):
            raise ForkOrgError(f"organisation key {key!r} is not a lower-case owner")
        if not isinstance(value, str) or not ALLOWED_RE.fullmatch(value):
            raise ForkOrgError(f"{key} maps to {value!r}, not a fork organisation")
        if key == value:
            raise ForkOrgError(f"{key} cannot be its own fork organisation")
        checked[key] = value
    return {"default": default, "organisations": checked}


def resolve(config: dict[str, Any], target_owner: str) -> str:
    """The fork organisation for ``target_owner``; never the owner itself."""
    organisations = cast("dict[str, str]", config["organisations"])
    fork_org = organisations.get(target_owner.lower(), str(config["default"]))
    if fork_org.lower() == target_owner.lower():
        raise ForkOrgError(f"{target_owner} resolves to itself as fork organisation")
    return fork_org


def run_check(args: argparse.Namespace) -> None:
    """Validate the file and print the mapping it holds."""
    config = load(args.config)
    print(f"default={config['default']}")
    for owner, fork_org in sorted(
        cast("dict[str, str]", config["organisations"]).items()
    ):
        print(f"{owner}={fork_org}")


def run_resolve(args: argparse.Namespace) -> None:
    """Print the fork organisation for one owner in output-file form."""
    print(f"fork_org={resolve(load(args.config), args.owner)}")


def build_parser() -> argparse.ArgumentParser:
    """Describe the two commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate the mapping file")
    check.add_argument("--config", type=Path, required=True)
    check.set_defaults(handler=run_check)
    resolve_cmd = commands.add_parser("resolve", help="print one owner's fork org")
    resolve_cmd.add_argument("--config", type=Path, required=True)
    resolve_cmd.add_argument("--owner", required=True)
    resolve_cmd.set_defaults(handler=run_resolve)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Dispatch a command; a mapping defect exits 1."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except ForkOrgError as exc:
        message = ascii(str(exc)).replace("::", ": :")
        print(f"fork orgs: {message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
