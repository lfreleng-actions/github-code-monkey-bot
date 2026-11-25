# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Named targets this run did not work because another run held them.

An operator who names an issue or pull request (``issues`` or
``pull_requests``) expects it worked. When another in-progress run
holds it (DESIGN.md section 6 "Overlapping runs"), the selector skips
it, warns, and records this reason, so the selection summary and the
run report list it as not worked: the run itself still ends green.
The report reads these entries from the file the select job hands it
with the unreviewed heads (``unreviewed_heads``), validated there.
"""

from __future__ import annotations

import re

# The selector's reason for a named target another run holds.
NAMED_HELD = re.compile(r"named, but being worked by run [1-9][0-9]*; not worked")


def reason(run: int) -> str:
    """The skip reason for a named target run ``run`` holds."""
    return f"named, but being worked by run {run}; not worked"


def is_named_held(why: object) -> bool:
    """Whether a skip reason is the selector's own for a held named target."""
    return isinstance(why, str) and NAMED_HELD.fullmatch(why) is not None


def entries(skipped_issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """The skipped issues that are held named targets, in order."""
    return [entry for entry in skipped_issues if is_named_held(entry.get("reason"))]


def section(found: list[dict[str, str]]) -> list[str]:
    """The heading and a row per held named target; nothing when there are none."""
    if not found:
        return []
    return [
        "",
        f"### Named targets not worked: {len(found)}",
        "",
        "Another in-progress run holds each; name it again once that run"
        " has finished if its work is still wanted.",
        "",
        "| Target | Reason |",
        "| --- | --- |",
        *(f"| {entry['issue']} | {entry['reason']} |" for entry in found),
    ]
