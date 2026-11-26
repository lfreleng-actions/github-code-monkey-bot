# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The author job's egress mode, resolved once by the select job.

The dispatch caller reads the repository variable
``CODE_MONKEY_EGRESS_MODE`` and passes it as the reusable workflow's
``agent_egress_mode`` input; no dispatch form field reaches it. The
select job resolves it here, before the first App token is minted,
and publishes the result as a job output the author matrix reads.
The author job never reads the variable or the input itself.

Three modes (docs/DESIGN.md section 7.5):

``block-all``   harden-runner ``block`` against the bot list alone
``allow-list``  ``block`` against the organisation list merged with
                the bot list; the default when the value is unset or
                empty
``audit-only``  ``audit``: connections are recorded, not blocked

Surrounding whitespace is ignored. Any other value fails the run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

VARIABLE = "CODE_MONKEY_EGRESS_MODE"
BLOCK_ALL = "block-all"
ALLOW_LIST = "allow-list"
AUDIT_ONLY = "audit-only"
MODES = (BLOCK_ALL, ALLOW_LIST, AUDIT_ONLY)
DEFAULT = ALLOW_LIST
DESCRIPTIONS = {
    BLOCK_ALL: "harden-runner block; the bot allow-list alone",
    ALLOW_LIST: (
        "harden-runner block; the organisation allow-list merged with the bot list"
    ),
    AUDIT_ONLY: "harden-runner audit; agent egress is recorded, not enforced",
}
UNENFORCED = "Agent egress is not enforced: {variable} is {mode}"


class EgressModeError(ValueError):
    """The variable holds a value that names no egress mode."""


def resolve(raw: str | None) -> str:
    """The egress mode a raw variable value selects.

    Unset or blank selects ``DEFAULT``; one of ``MODES``, ignoring
    surrounding whitespace, selects itself; anything else raises.
    """
    value = (raw or "").strip()
    if not value:
        return DEFAULT
    if value in MODES:
        return value
    raise EgressModeError(
        f"{VARIABLE} holds {value!r}; set it to {', '.join(MODES[:-1])} "
        f"or {MODES[-1]}, or leave it unset for {DEFAULT}"
    )


def main(argv: list[str] | None = None) -> None:
    """Resolve the mode, record it as a step output and in the run summary."""
    parser = argparse.ArgumentParser(description="Resolve the agent egress mode.")
    parser.add_argument("--mode", default="")
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        mode = resolve(args.mode)
    except EgressModeError as exc:
        parser.exit(1, f"::error::{exc}\n")
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"mode={mode}\n")
    with args.summary.open("a", encoding="utf-8") as summary:
        summary.write(f"Agent egress mode: `{mode}` ({DESCRIPTIONS[mode]})\n\n")
    if mode == AUDIT_ONLY:
        print(f"::warning::{UNENFORCED.format(variable=VARIABLE, mode=mode)}")
    print(f"agent egress mode: {mode}")


if __name__ == "__main__":
    main()
