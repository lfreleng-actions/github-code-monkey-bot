# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The publisher's command line: its eight commands and their arguments.

``publish.py`` passes in its own handlers for ``check``, ``push`` and
``open``, so this module never imports it; the other commands' handlers
live in the modules that implement them.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

import copilot_review as review
import issue_comment as comments
import proposal_report as reporting
import revision_publish
import revision_title
import stale_statuses as stale

Handler = Callable[[argparse.Namespace], None]


def build_parser(
    check_handler: Handler,
    push_handler: Handler,
    open_handler: Handler,
    *,
    description: str | None = None,
) -> argparse.ArgumentParser:
    """Describe the eight commands; the first three run the given handlers."""
    parser = argparse.ArgumentParser(description=description)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="offline verification and policy")
    check.add_argument("--selection", type=Path, required=True)
    check.add_argument("--key", required=True)
    check.add_argument("--proposal-dir", type=Path, required=True)
    check.add_argument("--workdir", type=Path, required=True)
    check.add_argument("--coauthors", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--summary", type=Path)
    check.add_argument("--run-url", default="")
    check.set_defaults(handler=check_handler)

    push = commands.add_parser("push", help="fork, branch and commits (fork token)")
    push.add_argument("--check", type=Path, required=True)
    push.add_argument("--workdir", type=Path, required=True)
    push.add_argument(
        "--mode", required=True, choices=("select", "branches", "pull-requests")
    )
    push.add_argument("--dry-run", action="store_true")
    push.add_argument("--run-attempt", type=int, default=1)
    # The proposal's artifact ID; empty when none arrived.
    push.add_argument("--author-session", default="")
    push.add_argument("--output", type=Path, required=True)
    push.set_defaults(handler=push_handler)

    opener = commands.add_parser("open", help="open the pull request (target token)")
    opener.add_argument("--result", type=Path, required=True)
    opener.add_argument("--check", type=Path, required=True)
    opener.add_argument("--output", type=Path, required=True)
    opener.set_defaults(handler=open_handler)

    retitle = commands.add_parser(
        "retitle", help="set a revision's title before its push (target token)"
    )
    retitle.add_argument("--check", type=Path, required=True)
    retitle.add_argument("--record", type=Path, required=True)
    retitle.set_defaults(handler=revision_title.run_retitle)

    restore = commands.add_parser(
        "restore-title", help="take back a title the push did not earn (target token)"
    )
    restore.add_argument("--record", type=Path, required=True)
    restore.add_argument("--result", type=Path, required=True)
    restore.set_defaults(handler=revision_title.run_restore_title)

    respond = commands.add_parser(
        "respond", help="answer the review threads of a revision (target token)"
    )
    respond.add_argument("--result", type=Path, required=True)
    respond.add_argument("--check", type=Path, required=True)
    respond.add_argument("--output", type=Path, required=True)
    respond.set_defaults(handler=revision_publish.run_respond)

    comment = commands.add_parser("comment", help="comment the outcome on the issue")
    comment.add_argument("--result", type=Path, required=True)
    comment.add_argument("--run-url", default="")
    comment.add_argument("--since", default="")
    comment.set_defaults(handler=comments.run_comment)

    request = commands.add_parser(
        "review-request", help="ask Copilot to review the pull request (target token)"
    )
    request.add_argument("--result", type=Path, required=True)
    request.add_argument("--output", type=Path, required=True)
    request.add_argument(
        "--credential", choices=review.CREDENTIALS, default="App token"
    )
    request.set_defaults(handler=review.run_request)

    awaiting = commands.add_parser(
        "review-await", help="wait for Copilot's review of the head (native token)"
    )
    awaiting.add_argument("--result", type=Path, required=True)
    awaiting.add_argument("--output", type=Path, required=True)
    awaiting.add_argument(
        "--wait-minutes", type=int, default=review.DEFAULT_WAIT_MINUTES
    )
    awaiting.add_argument("--stale-statuses", type=Path)
    awaiting.add_argument("--stale-status-minutes", default=str(stale.DEFAULT_MINUTES))
    awaiting.set_defaults(handler=review.run_await)

    report = commands.add_parser("report", help="merge results into a table")
    for flag in ("--results", "--output-md", "--output-json"):
        report.add_argument(flag, type=Path, required=True)
    report.add_argument("--org", help="owner whose pull request links to output")
    report.add_argument(
        "--github-output", type=Path, help="append the links here ($GITHUB_OUTPUT)"
    )
    report.add_argument("--unreviewed-heads", type=Path, help="select's evidence file")
    report.set_defaults(handler=reporting.run_report)
    return parser
