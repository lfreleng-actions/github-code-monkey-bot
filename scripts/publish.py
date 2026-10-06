# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify an agent's proposal, replay it as signed commits, and open the PR.

Four commands, run in the trusted publish and report jobs:

``check`` works offline (see ``proposal_check``) and writes
``check.json`` with a verdict and the composed messages.

``apply`` needs ``GH_TOKEN``. For a ``proposed`` verdict outside
dry-run it creates the bot branch at the base SHA and replays each
commit through ``createCommitOnBranch``, which GitHub signs, then
opens the pull request when the mode asks for one.

``comment`` posts one line on the issue describing the outcome.

``report`` merges every ``result.json`` into one table.

A verdict other than ``proposed`` is data, not an error. The script
exits non-zero for operational failures alone.
"""

from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

import issue_comment as comments
import monkey_github as github
import proposal_check as checks
import proposal_model as model
import proposal_policy as policy
import proposal_report as reporting
from branch_refs import (
    branch_matches,
    create_branch,
    delete_branch,
    existing_pull_request,
)
from proposal_policy import PublishError, Rejection

SCHEMA = 1

COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
"""


def file_changes(clone: Path, commit: dict[str, Any]) -> dict[str, Any]:
    """Build the createCommitOnBranch fileChanges payload for one commit."""
    additions: list[dict[str, str]] = []
    for item in cast("list[dict[str, Any]]", commit["additions"]):
        blob = str(item["blob"])
        if not policy.SHA_RE.fullmatch(blob):
            raise PublishError("check.json carries a malformed blob id")
        content = cast(
            "bytes", checks.git(clone, "cat-file", "blob", blob, binary=True)
        )
        additions.append(
            {"path": str(item["path"]), "contents": base64.b64encode(content).decode()}
        )
    deletions = [{"path": path} for path in cast("list[str]", commit["deletions"])]
    return {"additions": additions, "deletions": deletions}


def replay_commits(clone: Path, check: dict[str, Any]) -> list[str]:
    """Send each commit through createCommitOnBranch, chaining the head."""
    repository = str(check["repository"])
    head = str(check["base_sha"])
    created: list[str] = []
    for commit in cast("list[dict[str, Any]]", check["commits"]):
        payload = {
            "branch": {
                "repositoryNameWithOwner": repository,
                "branchName": str(check["branch"]),
            },
            "expectedHeadOid": head,
            "message": {
                "headline": str(commit["headline"]),
                "body": str(commit["body"]),
            },
            "fileChanges": file_changes(clone, commit),
        }
        try:
            data = github.graphql(COMMIT_MUTATION, {"input": payload})
        except github.GitHubError as exc:
            raise PublishError(f"createCommitOnBranch failed: {exc}") from exc
        result = data.get("createCommitOnBranch")
        node = (
            cast("dict[str, Any]", result).get("commit")
            if isinstance(result, dict)
            else None
        )
        if not isinstance(node, dict):
            raise PublishError("createCommitOnBranch returned no commit")
        oid = github.require_str(cast("dict[str, Any]", node), "oid", "commit")
        created.append(oid)
        head = oid
    return created


def open_pull_request(check: dict[str, Any]) -> tuple[str, str | None]:
    """Open the pull request; label it when the label exists.

    Returns the URL and a warning when labelling did not happen, so
    the result records it instead of the log swallowing it.
    """
    repository = str(check["repository"])
    data = github.api_write(
        "POST",
        f"repos/{repository}/pulls",
        {
            "title": str(check["pr_title"]),
            "head": str(check["branch"]),
            "base": str(check["default_branch"]),
            "body": str(check["pr_body"]),
            "maintainer_can_modify": True,
            "draft": False,
        },
    )
    url = github.require_str(data, "html_url", "pull request")
    number = github.require_int(data, "number", "pull request")
    try:
        github.api_object(f"repos/{repository}/labels/{policy.LABEL}")
        github.api_write(
            "POST",
            f"repos/{repository}/issues/{number}/labels",
            {"labels": [policy.LABEL]},
        )
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return url, None
        return url, f"could not label the pull request: {exc}"
    return url, None


def run_apply(args: argparse.Namespace) -> dict[str, Any]:
    """Create the branch, replay commits and open the pull request."""
    check = model.load_json(args.check, "check")
    verdict = check.get("verdict")
    if verdict not in policy.VERDICTS:
        raise PublishError(f"check.json has an unknown verdict {verdict!r}")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "key": check.get("key"),
        "bot_login": check.get("bot_login"),
        "run_attempt": args.run_attempt,
        "author_session": args.author_session or None,
        "repository": check.get("repository"),
        "issue": check.get("issue"),
        "verdict": verdict,
        "reasons": list(cast("list[Any]", check.get("reasons") or [])),
        "warnings": [],
        "dry_run": bool(args.dry_run),
        "mode": args.mode,
        "branch_url": None,
        "commits": [],
        "pull_request_url": None,
        "comment_url": None,
        "pr_title": check.get("pr_title") or None,
        "premium_requests": check.get("premium_requests"),
        "agent_seconds": check.get("agent_seconds"),
    }
    if verdict != "proposed" or args.dry_run or args.mode == "select":
        return result
    repository = str(check["repository"])
    branch = str(check["branch"])
    pr_attempted = False
    created = False
    try:
        # Inside the rollback boundary, but a create that failed without
        # a clear answer leaves ``created`` false: the ref may be ours or
        # may have existed before, so rollback leaves it alone.
        create_branch(repository, branch, str(check["base_sha"]))
        created = True
        result["commits"] = replay_commits(args.workdir / "clone", check)
        if args.mode == "pull-requests":
            pr_attempted = True
            url, warning = open_pull_request(check)
            result["pull_request_url"] = url
            if warning:
                cast("list[str]", result["warnings"]).append(warning)
    except Rejection as exc:
        # The branch already existed and is never deleted here. It is
        # this run's own work when it carries the proposal's tree: a
        # retry after a later step failed. Otherwise it is a conflict.
        try:
            return reconcile(args, check, result, exc)
        except (PublishError, github.GitHubError) as failure:
            # The branch predates this attempt, so it stays; the result
            # still records what went wrong.
            fail(args, result, f"{failure}; existing branch kept", failure)
    except (PublishError, github.GitHubError) as exc:
        # A PR POST can succeed while gh loses the reply. Deleting the
        # branch then would orphan a live pull request, so look first.
        if pr_attempted:
            try:
                earlier = existing_pull_request(
                    repository, branch, str(check["bot_login"])
                )
            except github.GitHubError as lookup:
                # Unknown is not absent: the pull request may exist, so
                # its branch stays and the result says why.
                fail(
                    args,
                    result,
                    f"{exc}; could not tell whether the pull request exists "
                    f"({lookup}); branch kept",
                    exc,
                )
            if earlier is not None:
                result["pull_request_url"] = earlier
                result["branch_url"] = f"https://github.com/{repository}/tree/{branch}"
                cast("list[str]", result["warnings"]).append(
                    f"the pull request call reported {exc} but the pull request exists"
                )
                return result
        if not created:
            # Deleting a branch this run cannot prove it made could take
            # someone else's work; a stray branch costs a human a look.
            fail(
                args,
                result,
                f"{exc}; branch creation did not confirm, so any branch there is kept",
                exc,
            )
        # Roll the branch back so the issue stays eligible, then fail
        # the step: this is the publisher unable to do its job.
        leftover = delete_branch(repository, branch)
        fail(
            args,
            result,
            str(exc) + (f"; {leftover}" if leftover else "; branch removed"),
            exc,
        )
    result["branch_url"] = f"https://github.com/{repository}/tree/{branch}"
    return result


def fail(
    args: argparse.Namespace, result: dict[str, Any], detail: str, cause: Exception
) -> NoReturn:
    """Record a publish-failed result, then fail the step.

    The result is written first so the comment and report steps still
    see a typed outcome for this issue.
    """
    result["verdict"] = "publish-failed"
    cast("list[str]", result["reasons"]).append(detail)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    raise PublishError(detail) from cause


def reconcile(
    args: argparse.Namespace,
    check: dict[str, Any],
    result: dict[str, Any],
    conflict: Rejection,
) -> dict[str, Any]:
    """Classify an existing bot branch: an earlier attempt's work, or a conflict.

    The branch is adopted only when its whole chain is what the replay
    would have produced (branch_matches); a pull request counts only
    when the bot opened it. A matching tree alone proves nothing, since
    anyone with push access could make one from other commits.
    """
    repository = str(check["repository"])
    branch = str(check["branch"])
    bot = str(check["bot_login"])
    commits = cast("list[dict[str, Any]]", check["commits"])
    if not branch_matches(repository, branch, str(check["base_sha"]), commits, bot):
        result["verdict"] = "rejected"
        cast("list[str]", result["reasons"]).append(str(conflict))
        return result
    earlier = existing_pull_request(repository, branch, bot)
    warnings = cast("list[str]", result["warnings"])
    result["branch_url"] = f"https://github.com/{repository}/tree/{branch}"
    result["pull_request_url"] = earlier
    if earlier is not None or args.mode != "pull-requests":
        warnings.append("an earlier attempt already published this work")
        return result
    # The earlier attempt pushed the branch but its pull request never
    # opened: finish the job instead of rejecting our own work.
    try:
        url, warning = open_pull_request(check)
    except (PublishError, github.GitHubError) as exc:
        # As on a fresh branch: the POST may have landed with its reply
        # lost, so a live pull request is not a failure.
        found = existing_pull_request(repository, branch, bot)
        if found is None:
            raise
        url = found
        warning = f"the pull request call reported {exc} but the pull request exists"
    result["pull_request_url"] = url
    warnings.append("resumed an earlier attempt's branch")
    if warning:
        warnings.append(warning)
    return result


def write_check(args: argparse.Namespace) -> None:
    """Run the offline check and write its outputs."""
    check = checks.run_check(
        selection_path=args.selection,
        key=args.key,
        proposal_dir=args.proposal_dir,
        workdir=args.workdir,
        coauthors_path=args.coauthors,
        run_url=args.run_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(check.to_json(), indent=2) + "\n", encoding="utf-8"
    )
    if args.summary:
        args.summary.write_text(model.check_summary(check), encoding="utf-8")
    reasons = policy.log_safe("; ".join(check.reasons))[:500]
    print(f"verdict: {check.verdict}; {reasons}")


def write_apply(args: argparse.Namespace) -> None:
    """Run apply and write result.json."""
    result = run_apply(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"apply: {result['verdict']}; pr={result['pull_request_url']}")


def build_parser() -> argparse.ArgumentParser:
    """Describe the four commands."""
    parser = argparse.ArgumentParser(description=__doc__)
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
    check.set_defaults(handler=write_check)

    apply = commands.add_parser("apply", help="create branch, commits and PR")
    apply.add_argument("--check", type=Path, required=True)
    apply.add_argument("--workdir", type=Path, required=True)
    apply.add_argument(
        "--mode", required=True, choices=("select", "branches", "pull-requests")
    )
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--run-attempt", type=int, default=1)
    # The proposal's artifact ID; empty when none arrived.
    apply.add_argument("--author-session", default="")
    apply.add_argument("--output", type=Path, required=True)
    apply.set_defaults(handler=write_apply)

    comment = commands.add_parser("comment", help="comment the outcome on the issue")
    comment.add_argument("--result", type=Path, required=True)
    comment.add_argument("--run-url", default="")
    comment.add_argument("--since", default="")
    comment.set_defaults(handler=comments.run_comment)

    report = commands.add_parser("report", help="merge results into a table")
    report.add_argument("--results", type=Path, required=True)
    report.add_argument("--output-md", type=Path, required=True)
    report.add_argument("--output-json", type=Path, required=True)
    report.set_defaults(handler=reporting.run_report)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Dispatch a command, keeping GitHub failures on the error path."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], None]", args.handler)
    try:
        handler(args)
    except (OSError, ValueError, PublishError, github.GitHubError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"publish: {message}\n")


if __name__ == "__main__":
    main()
