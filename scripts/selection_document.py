# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Assemble ``selection.json`` from what the run asked for and found.

See DESIGN.md section 18.1 for the selection schema.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = 1


@dataclass(frozen=True)
class Settings:
    """What the run was asked for, as the selection records it."""

    bot: dict[str, Any]
    enabled: frozenset[str]
    max_pull_requests: int
    guidance: bytes
    commit: str


def selection_document(
    args: argparse.Namespace,
    settings: Settings,
    *,
    lists: tuple[list[str], list[tuple[str, int]], list[tuple[str, int]], set[str]],
    outcome: tuple[int, dict[str, int], list[dict[str, str]], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Assemble selection.json (DESIGN.md section 18.1)."""
    bot, enabled, max_pull_requests = (
        settings.bot,
        settings.enabled,
        settings.max_pull_requests,
    )
    content, commit = settings.guidance, settings.commit
    explicit, targets, pulls, exclusions = lists
    seen, skipped, skipped_issues, chosen = outcome
    return {
        "schema": SCHEMA,
        "org": args.org,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": args.mode,
        "dry_run": bool(args.dry_run),
        "model": args.model,
        "categories": sorted(enabled),
        "max_pull_requests": max_pull_requests,
        "bot": bot,
        "revise": bool(args.revise),
        "revise_only": bool(args.revise_only),
        "guidance": {
            "repository": args.guidance_repository,
            "path": args.guidance_path,
            "ref": args.guidance_ref,
            "commit": commit,
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        "exclusions": sorted(exclusions) if not (explicit or targets or pulls) else [],
        "explicit_repositories": explicit,
        "explicit_issues": [f"{args.org}/{repo}#{number}" for repo, number in targets],
        "explicit_pull_requests": [
            f"{args.org}/{repo}#{number}" for repo, number in pulls
        ],
        "skipped_issues": skipped_issues,
        "candidates_seen": seen,
        "skipped": skipped,
        "issues": chosen,
    }
