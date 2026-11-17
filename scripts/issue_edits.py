# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""An issue's body with its last edit, which the issue author list checks."""

from __future__ import annotations

from typing import Any, cast

import bot_github as github
import issue_authors as authors

LAST_EDIT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      body
      lastEditedAt
      editor { login __typename }
    }
  }
}
"""


def last_edit(repo: str, number: int) -> dict[str, Any]:
    """The issue body, whether it was edited, by whom, and whether that was an App.

    REST carries no editor, so this is one GraphQL read, and the body
    comes from the same reply: the editor the list checks is then the
    last editor of the very body handed on, never of a version a
    separate read saw. A failed or malformed reply raises rather than
    reading as never edited. GitHub records body edits here, not title
    edits.
    """
    owner, _, name = repo.partition("/")
    data = github.graphql(
        LAST_EDIT_QUERY, {"owner": owner, "name": name, "number": number}, read=True
    )
    repository = data.get("repository")
    issue = (
        cast("dict[str, Any]", repository).get("issue")
        if isinstance(repository, dict)
        else None
    )
    if not isinstance(issue, dict):
        raise github.GitHubError(f"{repo}#{number}: no issue in the edit reply")
    data = cast("dict[str, Any]", issue)
    body = data.get("body")
    if body is not None and not isinstance(body, str):
        raise github.GitHubError(f"{repo}#{number}: invalid body in the edit reply")
    fields = authors.graphql_fields(data)
    return {
        "body": body or "",
        **{key: fields[key] for key in ("edited", "editor", "editor_is_bot")},
    }
