# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The hook run a proposal stands on, and the mark a failing one earns.

A session records each check it ran in the manifest's ``commands``,
and the prompts make the last ``prek run`` or ``pre-commit run``
entry its run of the repository's hooks after its last commit. When
that run did not exit 0, the proposal still publishes, but the
publisher says so outside the collapsed provenance block: one line in
the pull request body and one in the check's ``warnings``. Nothing
here changes a verdict (docs/DESIGN.md sections 8 and 13).
"""

from __future__ import annotations

import re
from typing import Any

# Whole words: ``uvx pre-commit run`` and ``SKIP=a prek run`` count;
# ``prek install`` and ``(pre-commit and commit-msg hooks)`` do not.
HOOK_RUN = re.compile(r"(?<![\w-])(?:prek|pre-commit)\s+run(?![\w-])")
# How the check's warning starts, so the run report can find it.
WARNING = "last hook run did not exit 0"


def is_hook_run(command: str) -> bool:
    """Whether a recorded command runs the repository's hooks."""
    return HOOK_RUN.search(command) is not None


def cell(command: str) -> str:
    """Agent text as one inert line for a code span or a table cell.

    Flattened, so it cannot leave its line; no backtick, so it cannot
    close the span; no ``<``, so it cannot open or close markup.
    """
    text = " ".join(command.split())
    return text.replace("|", "\\|").replace("`", "'").replace("<", "&lt;")


def failing_run(commands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last hook run, when its exit code is anything but the integer 0."""
    runs = [item for item in commands if is_hook_run(str(item.get("command", "")))]
    if not runs:
        return None
    code = runs[-1].get("exit_code")
    return None if type(code) is int and code == 0 else runs[-1]


def mark(commands: list[dict[str, Any]], warnings: list[str]) -> str:
    """Add the warning for a failing last hook run; return the body's line.

    The line, blank-line terminated, goes between the provenance block
    and the rule above the agent's text; it is empty when nothing failed.
    """
    run = failing_run(commands)
    if run is None:
        return ""
    code = run.get("exit_code")
    status = f"exit {code}" if type(code) is int else "no exit code recorded"
    command = cell(str(run.get("command", "")))
    warnings.append(f"{WARNING} ({status}): `{command}`")
    return (
        f"**Hooks did not pass.** The last hook run the session recorded, "
        f"`{command}`, did not exit 0 ({status}).\n\n"
    )
