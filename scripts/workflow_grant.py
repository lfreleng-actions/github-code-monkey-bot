# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which gated paths one selection entry may change (DESIGN.md 9.1).

Two grants share this mechanism: one for ``.github/workflows/`` and
one for the root ``.pre-commit-config.yaml``. Each has a run-wide
switch, the default for a new proposal, and a label with which a
maintainer widens it for one issue. The select job reads the labels
from GitHub alongside ``no-agent`` and ``agent-approved``: never from
issue text, and never from the author job. A label also lets that
issue's revision rounds change the grant's paths beyond those its
pull request already changes (DESIGN.md 20.3), which a run's switch
never does. Each grant covers its own paths alone.

The select job records each answer on each entry; the publisher's
check reads it back, falling back to the run's switch for an entry
without it. The run report lists each proposal a label granted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

# GitHub's own label names, as far as the reports will print one.
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}")


@dataclass(frozen=True)
class Kind:
    """One grant: its label, the fields that record it, and what it grants."""

    # The label a maintainer puts on an issue to grant it.
    label: str
    # The run's switch in the selection, and each entry's grant.
    field: str
    # The selection's configured label, and an entry's granting label.
    label_field: str
    # What the grant allows, as the comment and the report say it.
    edits: str


WORKFLOWS = Kind(
    label="workflow-edits-ok",
    field="allow_workflow_edits",
    label_field="workflow_edits_label",
    edits="workflow edits",
)
PRECOMMIT = Kind(
    label="precommit-edits-ok",
    field="allow_precommit_config_edits",
    label_field="precommit_edits_label",
    edits="pre-commit config edits",
)
KINDS = (WORKFLOWS, PRECOMMIT)


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


def attach(chosen: list[dict[str, Any]], kind: Kind, *, run_wide: bool) -> None:
    """Record on each entry its ``kind`` grant and, when the label made it, the label.

    A round takes the label alone: the run's switch governs new
    proposals and never widens a round.
    """
    for entry in chosen:
        carries = labelled(entry, kind.label)
        default = run_wide and entry.get("task", "issue") != "revise"
        entry[kind.field] = carries or default
        entry[kind.label_field] = kind.label if carries and not default else None


@dataclass(frozen=True)
class Grant:
    """One entry's grant of one kind, as the publisher's check reads it."""

    allowed: bool = False
    # The label that granted what the run's switch would not, or blank.
    label: str = ""
    # The run's configured label, named in a rejection; blank if unknown.
    configured: str = ""


def label_of(value: Any) -> str:
    """A label name from the selection, or blank when it is not one."""
    return value if isinstance(value, str) and LABEL_RE.fullmatch(value) else ""


def read(selection: dict[str, Any], entry: dict[str, Any], kind: Kind) -> Grant:
    """The entry's ``kind`` grant; an entry without one takes the run's switch.

    Only a JSON ``true`` turns either on: a present value of any other
    type is off, never the run's switch. A round without the field
    keeps its pull request's gated files alone, as before the label.
    """
    if kind.field in entry:
        allowed = entry[kind.field] is True
    elif entry.get("task") == "revise":
        allowed = False
    else:
        allowed = selection.get(kind.field) is True
    return Grant(
        allowed=allowed,
        label=label_of(entry.get(kind.label_field)) if allowed else "",
        configured=label_of(selection.get(kind.label_field)),
    )


def report_lines(
    results: list[dict[str, Any]],
    cell: Callable[[Any], str],
    kind: Kind = WORKFLOWS,
) -> list[str]:
    """The report's section: the proposals a ``kind`` label granted.

    ``cell`` bounds and escapes each field, which the author job may set.
    """
    lines: list[str] = []
    for item in results:
        label = label_of(item.get(kind.label_field))
        if label:
            issue = cell(f"{item.get('repository')}#{item.get('issue')}")
            url = cell(item.get("pull_request_url") or item.get("branch_url") or "—")
            lines.append(f"- {issue}: {url}, by the `{label}` label")
    if not lines:
        return []
    heading = f"{kind.edits.capitalize()} granted by a label on the issue:"
    return [heading, "", *lines, ""]


def all_report_lines(
    results: list[dict[str, Any]], cell: Callable[[Any], str]
) -> list[str]:
    """Every grant's section of the report, in ``KINDS`` order."""
    return [line for kind in KINDS for line in report_lines(results, cell, kind)]
