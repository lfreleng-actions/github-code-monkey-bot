# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Write the files the later jobs consume from a finished selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


def summary_markdown(selection: dict[str, Any]) -> str:
    """Render the selection for the step summary."""
    lines = [
        "## Issue selection",
        "",
        f"Mode `{selection['mode']}`, dry run `{selection['dry_run']}`, "
        f"model `{selection['model']}`, at most "
        f"{selection['max_pull_requests'] or 'unlimited'} pull request(s).",
        f"Categories: {', '.join(selection['categories'])}.",
        f"Candidates seen: {selection['candidates_seen']}; "
        f"selected: {len(selection['issues'])}.",
        "",
        "| Skipped because | Count |",
        "| --- | --- |",
    ]
    for reason, count in sorted(cast("dict[str, int]", selection["skipped"]).items()):
        lines.append(f"| {reason} | {count} |")
    lines += ["", "| Issue | Priority | Type | Title |", "| --- | --- | --- | --- |"]
    for issue in cast("list[dict[str, Any]]", selection["issues"]):
        # Public input: flatten line breaks before escaping, so a title
        # cannot end the row and inject content into the summary.
        title = " ".join(str(issue["title"]).split()).replace("|", "\\|")
        lines.append(
            f"| [{issue['repo_name']}#{issue['number']}]({issue['url']}) "
            f"| {issue['priority'] or '—'} | {issue['type']} | {title} |"
        )
    if not selection["issues"]:
        lines.append("| — | — | — | nothing to work |")
    return "\n".join(lines) + "\n"


def write_outputs(directory: Path, selection: dict[str, Any], guidance: bytes) -> None:
    """Write every file the later jobs consume."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "selection.json").write_text(
        json.dumps(selection, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    include = [
        {
            "key": issue["key"],
            "repository": issue["repository"],
            "repo_name": issue["repo_name"],
            "number": issue["number"],
            "base_sha": issue["base_sha"],
            "branch": issue["branch"],
        }
        for issue in cast("list[dict[str, Any]]", selection["issues"])
    ]
    (directory / "matrix.json").write_text(
        json.dumps({"include": include}) + "\n", encoding="utf-8"
    )
    (directory / "agents.md").write_bytes(guidance)
    (directory / "excluded-repos.txt").write_text(
        "".join(f"{name}\n" for name in cast("list[str]", selection["exclusions"])),
        encoding="utf-8",
    )
    (directory / "selection-summary.md").write_text(
        summary_markdown(selection), encoding="utf-8"
    )
