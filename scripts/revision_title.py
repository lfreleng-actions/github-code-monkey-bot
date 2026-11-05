# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A revision's title: set before its branch moves, taken back after a failed push.

The Semantic Pull Request check compares the title with the commit
subject on the push, so a revision whose subject changed retitles
first, with the pull request token (DESIGN.md section 20.3).
"""

from __future__ import annotations

import argparse
import json
from typing import Any, cast

import bot_github as github
import copilot_review as review
import proposal_model as model
from branch_swap import already_moved


def run_retitle(args: argparse.Namespace) -> None:
    """``publish.py retitle``: set a revision's title before its branch moves.

    The Semantic Pull Request check runs on the push and compares the
    title with the commit subject, so the title must lead the push
    (AGENTS.md section 10.1). Only a proposed revision whose title
    differs writes, and only while the pull request heads at the
    selected commit or at this run's own published chain on a retry;
    a head another session moved keeps its title. What the write
    replaced goes to ``--record`` so a push that then fails with the
    branch unchanged can take the title back.
    """
    check = model.load_json(args.check, "check")
    if check.get("task") != "revise" or check.get("verdict") != "proposed":
        print("retitle: nothing to do")
        return
    repository = str(check["repository"])
    number = int(check["pull_number"])
    title = str(check.get("pr_title") or "")
    head = review.pull_head(repository, number)
    if head != str(check["head_sha"]):
        chain = already_moved(check)
        if chain is None or head != chain[-1]:
            print(f"retitle: head moved to {head[:7]}; title left alone")
            return
    current = github.api_object(f"repos/{repository}/pulls/{number}").get("title")
    if not title or title == current:
        print("retitle: title unchanged")
        return
    # The record goes down before the write: GitHub may apply the
    # title and lose the reply, and the restore must still know what
    # to give back.
    record = {
        "repository": repository,
        "pull_number": number,
        "head_sha": head,
        "previous": current if isinstance(current, str) else "",
        "title": title,
    }
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    github.api_write("PATCH", f"repos/{repository}/pulls/{number}", {"title": title})
    print("retitle: title set")


def run_restore_title(args: argparse.Namespace) -> None:
    """``publish.py restore-title``: take back a title the push did not earn.

    Runs after the push whatever its outcome, and after a retitle that
    failed, since GitHub may apply the title and lose the reply. A
    publication keeps the title; a failure that confirmed the branch
    unchanged, or a push that reached no result, gives the old title
    back when the new one is in place, provided the
    head is still the one the retitle saw and the title is still the
    one it set, so another session's work is never touched. An
    unknown swap outcome restores nothing, since the branch may hold
    the new chain.
    """
    if not args.record.is_file():
        print("restore-title: nothing was retitled")
        return
    record = model.load_json(args.record, "retitle record")
    result = model.load_json(args.result, "result") if args.result.is_file() else None
    if result is not None:
        verdict = result.get("verdict")
        reasons = " ".join(
            str(r) for r in cast("list[Any]", result.get("reasons") or [])
        )
        if verdict == "proposed":
            print("restore-title: published; the title stands")
            return
        if verdict == "publish-failed" and "unknown" in reasons:
            print("restore-title: swap outcome unknown; the title stands")
            return
    repository = str(record["repository"])
    number = int(record["pull_number"])
    head = review.pull_head(repository, number)
    if head != str(record["head_sha"]):
        print(f"restore-title: head moved to {head[:7]}; title left alone")
        return
    current = github.api_object(f"repos/{repository}/pulls/{number}").get("title")
    if current != record["title"]:
        print("restore-title: title changed since; left alone")
        return
    github.api_write(
        "PATCH", f"repos/{repository}/pulls/{number}", {"title": record["previous"]}
    )
    print("restore-title: previous title restored")
