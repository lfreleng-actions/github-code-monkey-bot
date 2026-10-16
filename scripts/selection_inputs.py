# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Parse the selection's scalar and list inputs before any read happens."""

from __future__ import annotations

import re
from pathlib import Path

from issue_reads import SelectionError

REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def parse_repositories(text: str) -> list[str]:
    """Split a repository list on commas and whitespace, in given order."""
    names: list[str] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        if not REPO_NAME_RE.fullmatch(token) or token in (".", ".."):
            raise SelectionError(f"invalid repository name {token!r}")
        lowered = token.lower()
        if lowered not in names:
            names.append(lowered)
    return names


def load_exclusions(file: Path | None, override: str) -> list[str]:
    """Resolve the exclusion list: an explicit override beats the bundled file."""
    if override.strip():
        return parse_repositories(override)
    if file is None:
        return []
    if not file.is_file():
        raise SelectionError(f"exclusion file {file} is not a regular file")
    names: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            names.extend(parse_repositories(stripped))
    return names


def parse_max_pull_requests(text: str) -> int:
    """Accept a non-negative integer; zero lifts the cap."""
    if not re.fullmatch(r"\d+", text.strip()):
        raise SelectionError(
            f"max_pull_requests must be a non-negative integer, got {text!r}"
        )
    return int(text)
