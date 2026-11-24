# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The run parameters a pull request carries from round to round (DESIGN.md 20.1).

One definition of the inherited set: the cron form inputs the
``continue`` job forwards to the next round, each with the check the
reusable workflow's input step applies. The publisher records the
effective values in every round footer as one compact, versioned
``params=`` token; a hand-back round started with
``inherit_run_parameters`` reads the latest one back. ``settle``
computes the select job's effective values and the budgets that
follow from them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

# The cron form inputs a round inherits, in the order the continue
# job forwards them. Everything else keeps its per-run meaning.
INHERITED = (
    "model",
    "fusion_auto_model_select",
    "fusion_tier",
    "max_runtime_minutes",
    "max_rounds",
    "stale_status_minutes",
    "allow_subagents",
    "request_review",
    "request_antagonistic_review",
    "load_repo_skills",
    "include_dotgithub",
)
BOOLEANS = (
    "allow_subagents",
    "request_review",
    "request_antagonistic_review",
    "load_repo_skills",
    "include_dotgithub",
)
FUSION = "fusion"
FUSION_TIERS = ("standard", "high", "max")
VERSION = "v1"
# Far above what the fields spell with any model id in use. The model
# pattern sets no length, as the input check does not, so a run with
# a longer token records none, and a hand-back from it keeps the form.
MAX_TOKEN = 256
# The token's field names, short to keep the footer one line; the
# Fusion switch is not spelled out, since the model says it. Each
# field is ``name:value``, never ``name=value``: the footer's own
# fields are ``name=value``, and a reader that scans the footer for
# ``antagonistic=`` or ``review=`` must not find one inside the token.
TOKEN_FIELDS = {
    "model": "model",
    "tier": "fusion_tier",
    "runtime": "max_runtime_minutes",
    "rounds": "max_rounds",
    "stale": "stale_status_minutes",
    "subagents": "allow_subagents",
    "skills": "load_repo_skills",
    "dotgithub": "include_dotgithub",
    "review": "request_review",
    "antagonistic": "request_antagonistic_review",
}
# The select step's environment for each form value.
FORM_ENV = {
    "model": "MODEL",
    "fusion_tier": "FUSION_TIER",
    "max_runtime_minutes": "MAX_RUNTIME",
    "max_rounds": "MAX_ROUNDS",
    "stale_status_minutes": "STALE_MINUTES",
    "allow_subagents": "ALLOW_SUBAGENTS",
    "request_review": "REQUEST_REVIEW",
    "request_antagonistic_review": "REQUEST_ANTAGONISTIC_REVIEW",
    "load_repo_skills": "LOAD_REPO_SKILLS",
    "include_dotgithub": "INCLUDE_DOTGITHUB",
}
# The author job's budget beyond the session: toolchain setup and upload.
AUTHOR_MARGIN = 20


class ParameterError(ValueError):
    """A run parameter failed its check; ``field`` names it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


def bounded(low: int, high: int) -> Callable[[str], bool]:
    """A check for a decimal integer from ``low`` to ``high``, no leading zero."""

    def check(value: str) -> bool:
        if not re.fullmatch(r"[1-9][0-9]{0,5}", value):
            return False
        return low <= int(value) <= high

    return check


# The reusable workflow's input check, field by field.
CHECKS: dict[str, tuple[Callable[[str], bool], str]] = {
    "model": (
        lambda value: re.fullmatch(r"[a-z0-9.-]+", value) is not None,
        "must be a lower-case identifier",
    ),
    "fusion_tier": (
        lambda value: value in ("", *FUSION_TIERS),
        "must be standard, high, max or empty",
    ),
    "max_runtime_minutes": (bounded(1, 330), "must be 1-330"),
    "max_rounds": (bounded(1, 50), "must be 1-50"),
    # Digits alone, as the input check and stale_statuses take them:
    # ``08`` and ``00`` stay valid, and the value passes through as given.
    "stale_status_minutes": (
        lambda value: re.fullmatch(r"[0-9]+", value) is not None,
        "must be a non-negative integer",
    ),
    **{
        name: (lambda value: value in ("true", "false"), "must be true or false")
        for name in BOOLEANS
    },
}


def validate(values: Mapping[str, object]) -> dict[str, str]:
    """The inherited set, every value checked, or raise ``ParameterError``.

    ``fusion_auto_model_select`` follows from the model, so a value
    given for it must agree; the tier goes with Fusion alone.
    """
    checked: dict[str, str] = {}
    for name, (check, message) in CHECKS.items():
        value = values.get(name, "")
        if not isinstance(value, str) or not check(value):
            raise ParameterError(name, message)
        checked[name] = value
    fusion = checked["model"] == FUSION
    if fusion and not checked["fusion_tier"]:
        raise ParameterError("fusion_tier", "model fusion needs a tier")
    if not fusion and checked["fusion_tier"]:
        raise ParameterError("fusion_tier", "a tier needs model fusion")
    switch = "true" if fusion else "false"
    given = values.get("fusion_auto_model_select", switch)
    if given != switch:
        raise ParameterError("fusion_auto_model_select", "must match the model")
    checked["fusion_auto_model_select"] = switch
    return {name: checked[name] for name in INHERITED}


def encode(values: Mapping[str, object]) -> str:
    """The footer token's value: ``v1:`` then the checked fields."""
    checked = validate(values)
    fields: list[str] = []
    for key, name in TOKEN_FIELDS.items():
        value = checked[name]
        if name == "fusion_tier" and not value:
            continue
        if name in BOOLEANS:
            value = "1" if value == "true" else "0"
        fields.append(f"{key}:{value}")
    return f"{VERSION}:{','.join(fields)}"


def decode(token: str) -> dict[str, str]:
    """The inherited set a footer token records, or raise ``ParameterError``.

    The token is the App's own text, but read back it is input: a
    version, field or value this module would not write fails, naming
    the field.
    """
    if len(token) > MAX_TOKEN or not token.isascii():
        raise ParameterError("params", "longer than a token or not ASCII")
    version, colon, body = token.partition(":")
    if version != VERSION or not colon:
        raise ParameterError("params", f"version is not {VERSION}")
    values: dict[str, str] = {}
    for field in body.split(","):
        key, colon, value = field.partition(":")
        name = TOKEN_FIELDS.get(key)
        if name is None or not colon:
            raise ParameterError("params", f"unknown field {ascii(key)}")
        if name in values:
            raise ParameterError(name, "given twice")
        if name in BOOLEANS:
            if value not in ("0", "1"):
                raise ParameterError(name, "must be 0 or 1")
            value = "true" if value == "1" else "false"
        values[name] = value
    missing = [name for name in TOKEN_FIELDS.values() if name not in values]
    for name in missing:
        if name != "fusion_tier":
            raise ParameterError(name, "missing")
    values.setdefault("fusion_tier", "")
    return validate(values)


def footer_field(token: str) -> str:
    """The footer's `` params=`` field, or nothing when the run gave none valid.

    Every footer, a failed round's or a handoff's too, records the
    parameters its run used, so a hand-back round can inherit them
    (DESIGN.md 20.1).
    """
    if not token:
        return ""
    try:
        decode(token)
    except ParameterError as exc:
        print(f"::warning::run parameters not recorded: {exc}")
        return ""
    return f" params={token}"


def form_values(environ: Mapping[str, str]) -> dict[str, str]:
    """The form's values from the select step's environment, checked."""
    values = {name: environ.get(variable, "") for name, variable in FORM_ENV.items()}
    return validate(values)


def settle(form: Mapping[str, str], selection: Mapping[str, Any]) -> dict[str, str]:
    """The run's effective values: the form's, or those the selection inherited."""
    inherited = selection.get("inherited_parameters")
    if isinstance(inherited, dict) and inherited:
        return validate(cast("dict[str, object]", inherited))
    return validate(form)


def outputs(effective: Mapping[str, str]) -> dict[str, str]:
    """The select job outputs for the effective values, budgets included."""
    runtime = int(effective["max_runtime_minutes"])
    return {
        **effective,
        "agent_timeout": str(runtime),
        "author_timeout": str(runtime + AUTHOR_MARGIN),
        "params": encode(effective),
        "run_parameters": json.dumps(dict(effective), separators=(",", ":")),
    }


def run_settle(args: argparse.Namespace) -> None:
    """Write the effective values and the budgets to ``GITHUB_OUTPUT``."""
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    effective = settle(form_values(os.environ), selection)
    lines = [f"{key}={value}\n" for key, value in outputs(effective).items()]
    with args.output.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)


def main(argv: list[str] | None = None) -> None:
    """Settle the run's effective parameters."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    settled = commands.add_parser("settle", help="write the effective values")
    settled.add_argument("--selection", type=Path, required=True)
    settled.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("GITHUB_OUTPUT", "/dev/stdout")),
    )
    args = parser.parse_args(argv)
    try:
        run_settle(args)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"run parameters: {ascii(str(exc))}\n")


if __name__ == "__main__":
    main()
