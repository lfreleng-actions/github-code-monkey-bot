# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Whether one selection entry may change ``.github/workflows/`` (DESIGN.md 9.1).

The run's ``allow_workflow_edits`` switch is the default for a new
proposal. A maintainer widens it for one issue with a label, which
the select job reads from GitHub alongside ``no-agent`` and
``agent-approved``: never from issue text, and never from the author
job. The label also lets that issue's revision rounds change workflow
files beyond those its pull request already changes (DESIGN.md 20.3),
which the run's switch never does.

The select job records the answer on each entry; the publisher's
check reads it back, falling back to the run's switch for an entry
without it. The run report lists each proposal the label granted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

# A maintainer's grant of workflow edits to one issue and its rounds.
LABEL = "workflow-edits-ok"
FIELD = "allow_workflow_edits"
LABEL_FIELD = "workflow_edits_label"
# GitHub's own label names, as far as the reports will print one.
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}")


def labelled(entry: dict[str, Any], label: str) -> bool:
    """Whether the entry's issue carries ``label``; GitHub ignores case."""
    labels = entry.get("labels")
    if not isinstance(labels, list):
        return False
    wanted = label.casefold()
    return any(
        isinstance(name, str) and name.casefold() == wanted
        for name in cast("list[Any]", labels)
    )


def attach(chosen: list[dict[str, Any]], *, run_wide: bool, label: str = LABEL) -> None:
    """Record on each entry its grant and, when the label made it, the label.

    A round takes the label alone: the run's switch governs new
    proposals and never widens a round.
    """
    for entry in chosen:
        carries = labelled(entry, label)
        default = run_wide and entry.get("task", "issue") != "revise"
        entry[FIELD] = carries or default
        entry[LABEL_FIELD] = label if carries and not default else None


@dataclass(frozen=True)
class Grant:
    """One entry's workflow-edit grant, as the publisher's check reads it."""

    allowed: bool = False
    # The label that granted what the run's switch would not, or blank.
    label: str = ""
    # The run's configured label, named in a rejection; blank if unknown.
    configured: str = ""


def label_of(value: Any) -> str:
    """A label name from the selection, or blank when it is not one."""
    return value if isinstance(value, str) and LABEL_RE.fullmatch(value) else ""


def read(selection: dict[str, Any], entry: dict[str, Any]) -> Grant:
    """The entry's grant; an entry without one takes the run's switch.

    Only a JSON ``true`` turns either on: a present value of any other
    kind is off, never the run's switch. A round without the field
    keeps its pull request's workflow files alone, as before the label.
    """
    if FIELD in entry:
        allowed = entry[FIELD] is True
    elif entry.get("task") == "revise":
        allowed = False
    else:
        allowed = selection.get(FIELD) is True
    return Grant(
        allowed=allowed,
        label=label_of(entry.get(LABEL_FIELD)) if allowed else "",
        configured=label_of(selection.get(LABEL_FIELD)),
    )


def report_lines(
    results: list[dict[str, Any]], cell: Callable[[Any], str]
) -> list[str]:
    """The report's section: the proposals a label let change workflow files.

    ``cell`` bounds and escapes each field, which the author job may set.
    """
    lines: list[str] = []
    for item in results:
        label = label_of(item.get(LABEL_FIELD))
        if label:
            issue = cell(f"{item.get('repository')}#{item.get('issue')}")
            url = cell(item.get("pull_request_url") or item.get("branch_url") or "—")
            lines.append(f"- {issue}: {url}, by the `{label}` label")
    if not lines:
        return []
    return ["Workflow edits granted by a label on the issue:", "", *lines, ""]
