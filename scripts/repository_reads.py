# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Repository reads for selection: the owner's listing, or one repository.

Both produce the same shape, keyed by the flags the safety filters
read: ``name``, ``archived``, ``template``, ``fork``, ``public`` and
``default_branch``. A scan lists the owner once; a run that names
issues reads each repository on its own and never lists, so an owner
at the listing ceiling can still have an issue named.
"""

from __future__ import annotations

from typing import Any, cast

import bot_github as github
from issue_reads import SEARCH_LIMIT, SelectionError


def list_repositories(org: str) -> dict[str, dict[str, Any]]:
    """Map lower-cased repository names to the metadata selection needs."""
    raw = github.run_gh(
        [
            "repo",
            "list",
            org,
            "--limit",
            str(SEARCH_LIMIT),
            "--json",
            "name,isArchived,isTemplate,isFork,isPrivate,visibility,defaultBranchRef",
        ],
        read=True,
    )
    parsed = github.decode_response(raw)
    if not isinstance(parsed, list):
        raise github.GitHubError("expected a repository array")
    repositories: dict[str, dict[str, Any]] = {}
    for entry in cast("list[Any]", parsed):
        if not isinstance(entry, dict):
            raise github.GitHubError("expected repository objects")
        data = cast("dict[str, Any]", entry)
        name = github.require_str(data, "name", "repository")
        default = data.get("defaultBranchRef")
        branch = (
            cast("dict[str, Any]", default).get("name")
            if isinstance(default, dict)
            else None
        )
        visibility = data.get("visibility")
        repositories[name.lower()] = {
            "name": name,
            "archived": bool(data.get("isArchived")),
            "template": bool(data.get("isTemplate")),
            "fork": bool(data.get("isFork")),
            # Private and internal repositories are out of scope (DESIGN
            # 2, 16): the author job clones without a credential.
            "public": not data.get("isPrivate")
            and isinstance(visibility, str)
            and visibility.upper() == "PUBLIC",
            "default_branch": branch if isinstance(branch, str) and branch else None,
        }
    if len(repositories) >= SEARCH_LIMIT:
        raise SelectionError("repository listing reached the search ceiling")
    return repositories


def repository_meta(org: str, name: str) -> dict[str, Any] | None:
    """Read one repository into the shape ``list_repositories`` produces.

    A named run reads its few repositories this way and never lists
    the owner, so an owner at the listing ceiling can still have an
    issue named. An absent repository is None; any other failure
    propagates.
    """
    try:
        data = github.api_object(f"repos/{org}/{name}")
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return None
        raise
    visibility = data.get("visibility")
    branch = data.get("default_branch")
    default_branch = branch if isinstance(branch, str) and branch else None
    # REST names a default branch for a repository with no commits;
    # only the branch itself proves there is something to build on.
    if default_branch is not None:
        try:
            github.api_object(f"repos/{org}/{name}/branches/{default_branch}")
        except github.GitHubError as exc:
            if not github.is_absent(exc):
                raise
            default_branch = None
    return {
        "name": github.require_str(data, "name", "repository"),
        "archived": bool(data.get("archived")),
        "template": bool(data.get("is_template")),
        "fork": bool(data.get("fork")),
        "public": not data.get("private")
        and isinstance(visibility, str)
        and visibility.lower() == "public",
        "default_branch": default_branch,
    }
