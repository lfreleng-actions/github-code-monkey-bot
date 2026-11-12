# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify an agent's proposal, replay it into the fork, and open the PR.

Eight commands, run in the trusted publish and report jobs:

``check`` works offline (see ``proposal_check``) and writes
``check.json`` with a verdict and the composed messages.

``push`` needs ``GH_TOKEN`` for the fork organisation. For a
``proposed`` verdict outside dry-run it finds or creates the fork,
syncs it, creates the bot branch at the base SHA and replays each
commit through ``createCommitOnBranch``, which GitHub signs.

``open`` needs ``GH_TOKEN`` for the target. When ``push`` left a
branch and the mode asks for one, it opens the pull request from
the fork. Two commands because the two writes need two tokens
(DESIGN.md section 4.3): neither token can do the other's job.

``respond`` answers a revision's review threads and updates the pull
request's description with the target token (``revision_publish``).

``comment`` posts one line on the issue describing the outcome.

``review-request`` and ``review-await`` ask Copilot to review the
pull request with the target token, then wait for its review of the
head with the job's own token and record the verdict
(``copilot_review``).

``report`` merges every ``result.json`` into one table; given
``--org`` and ``--github-output`` it also writes the run's pull
request links as step outputs (``report_outputs``).

A verdict other than ``proposed`` is data, not an error. The script
exits non-zero for operational failures alone.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

import bot_github as github
import issue_recheck as recheck
import proposal_check as checks
import proposal_model as model
import proposal_policy as policy
import publish_cli
import revision_publish
import write_recovery as recovery
from branch_refs import (
    branch_matches,
    create_branch,
    delete_branch,
    ensure_fork,
    existing_pull_request,
    open_pull_request,
    replay_commits,
    sync_fork,
)
from proposal_policy import PublishError, Rejection
from stale_branch import ClosedAttempt, create_fresh_branch, restore_closed_attempt

SCHEMA = 1


def base_result(args: argparse.Namespace, check: dict[str, Any]) -> dict[str, Any]:
    """The result every push ends with, before any write happens."""
    verdict = check.get("verdict")
    if verdict not in policy.VERDICTS:
        raise PublishError(f"check.json has an unknown verdict {verdict!r}")
    return {
        "schema": SCHEMA,
        "key": check.get("key"),
        "bot_login": check.get("bot_login"),
        "run_attempt": args.run_attempt,
        "author_session": args.author_session or None,
        "repository": check.get("repository"),
        "fork_repository": check.get("fork_repository"),
        "issue": check.get("issue"),
        "verdict": verdict,
        "reasons": policy.bounded_reasons(check.get("reasons")),
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
        "task": check.get("task") or "issue",
        "pull_number": check.get("pull_number") or None,
        "round_summary": check.get("round_summary") or None,
        **{k: check.get(k) or 0 for k in ("round", "max_rounds")},
        "failing_checks": list(cast("list[Any]", check.get("failing_checks") or [])),
        "antagonistic_review": check.get("antagonistic_review") or None,
        "collected_at": check.get("collected_at") or None,
    }


def run_push(args: argparse.Namespace) -> dict[str, Any]:
    """Find or create the fork, create the branch there and replay commits.

    A write recovered from a transient failure adds a result warning.
    """
    check = model.load_json(args.check, "check")
    result = base_result(args, check)
    with recovery.recording(cast("list[str]", result["warnings"])):
        if check.get("task") == "revise":
            return revision_publish.run_push(args, check, result, fail=fail)
        if result["verdict"] != "proposed" or args.dry_run or args.mode == "select":
            return result
        if recheck.rejected(args, check, result, fail=fail):
            return result
        target = str(check["repository"])
        fork = str(check["fork_repository"])
        branch = str(check["branch"])
        attempted = False
        created = False
        reset: ClosedAttempt | None = None
        progress: list[str] = []
        owned: list[bool] = []
        try:
            ensure_fork(target, str(check["fork_org"]), target.partition("/")[2])
            sync_fork(fork, str(check["default_branch"]), str(check["base_sha"]))
            # Inside the rollback boundary, but a create without a clear
            # answer, or found only by reading it back, leaves ``created``
            # false: the ref may have existed before, so rollback keeps it.
            attempted = True
            reset = create_fresh_branch(
                check, result, lambda f, b, s: owned.append(create_branch(f, b, s))
            )
            created = reset is not None or owned == [True]
            result["commits"] = replay_commits(args.workdir / "clone", check, progress)
        except Rejection as exc:
            # The branch already existed, not as the closed attempt a named
            # retry may reset, and is never deleted here. It is this run's
            # own work when it carries the proposal's chain: a retry after
            # a later step failed. Otherwise it is a conflict.
            try:
                return reconcile(args, check, result, exc)
            except (PublishError, github.GitHubError) as failure:
                fail(args, result, f"{failure}; existing branch kept", failure)
        except (PublishError, github.GitHubError) as exc:
            if not attempted:
                # The fork was not ready: nothing of ours exists to remove.
                fail(args, result, str(exc), exc)
            if not created:
                # Deleting a branch this run cannot prove it made could take
                # someone else's work; a stray branch costs a human a look.
                fail(
                    args,
                    result,
                    f"{exc}; branch creation did not confirm, so any branch there is kept",
                    exc,
                )
            if reset is not None:
                # Not this run's branch to delete: the closed head goes back.
                restored = restore_closed_attempt(check, reset, progress)
                fail(args, result, f"{exc}; {restored}", exc)
            # Roll the branch back so the issue stays eligible, then fail
            # the step: this is the publisher unable to do its job.
            leftover = delete_branch(fork, branch)
            fail(
                args,
                result,
                str(exc) + (f"; {leftover}" if leftover else "; branch removed"),
                exc,
            )
        result["branch_url"] = model.fork_tree_url(check)
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
    write_result(args.output, result)
    raise PublishError(detail) from cause


def reconcile(
    args: argparse.Namespace,
    check: dict[str, Any],
    result: dict[str, Any],
    conflict: Rejection,
) -> dict[str, Any]:
    """Classify an existing fork branch: an earlier attempt's work, or a conflict.

    The branch is adopted only when its whole chain is what the replay
    would have produced (branch_matches); a pull request counts only
    when the bot opened it from the fork. A matching tree alone proves
    nothing, since anyone with push access could make one from other
    commits. Adopting the branch leaves ``pull_request_url`` null in
    pull-requests mode, so ``open`` finishes what the earlier attempt
    did not.
    """
    fork = str(check["fork_repository"])
    branch = str(check["branch"])
    bot = str(check["bot_login"])
    commits = cast("list[dict[str, Any]]", check["commits"])
    chain = branch_matches(fork, branch, str(check["base_sha"]), commits, bot)
    if chain is None:
        result["verdict"] = "rejected"
        cast("list[str]", result["reasons"]).append(str(conflict))
        return result
    # The adopted chain is the published work; its head is what a
    # review must judge.
    result["commits"] = chain
    earlier = existing_pull_request(str(check["repository"]), branch, bot, fork)
    warnings = cast("list[str]", result["warnings"])
    result["branch_url"] = model.fork_tree_url(check)
    result["pull_request_url"] = earlier
    if earlier is not None or args.mode != "pull-requests":
        warnings.append("an earlier attempt already published this work")
    else:
        warnings.append("resumed an earlier attempt's branch")
    return result


def needs_pull_request(result: dict[str, Any]) -> bool:
    """Whether ``open`` has a pull request to raise for this result."""
    return (
        result.get("verdict") == "proposed"
        and result.get("mode") == "pull-requests"
        and not result.get("dry_run")
        and isinstance(result.get("branch_url"), str)
        and result.get("pull_request_url") is None
    )


def run_open(args: argparse.Namespace) -> dict[str, Any]:
    """Open the pull request on the target for a branch ``push`` left in the fork.

    A failure here never removes the fork branch: the next run's
    selection sees it as a prior attempt, and its publisher adopts
    the branch and opens the pull request then.
    """
    result = model.load_json(args.result, "result")
    check = model.load_json(args.check, "check")
    if result.get("key") != check.get("key"):
        raise PublishError("result.json and check.json describe different proposals")
    if not needs_pull_request(result):
        return result
    target = str(check["repository"])
    branch = str(check["branch"])
    bot = str(check["bot_login"])
    fork = str(check["fork_repository"])
    warnings = cast("list[str]", result.setdefault("warnings", []))
    try:
        url, warning = open_pull_request(check)
    except (PublishError, github.GitHubError) as exc:
        # A PR POST can succeed while gh loses the reply, so look before
        # reporting a failure; the branch stays either way.
        try:
            earlier = existing_pull_request(target, branch, bot, fork)
        except github.GitHubError as lookup:
            fail(
                args,
                result,
                f"{exc}; could not tell whether the pull request exists "
                f"({lookup}); fork branch kept",
                exc,
            )
        if earlier is None:
            if recheck.already_exists(exc, result):
                return result
            fail(args, result, f"{exc}; fork branch kept for the next run", exc)
        url = earlier
        warning = f"the pull request call reported {exc} but the pull request exists"
    result["pull_request_url"] = url
    if warning:
        warnings.append(warning)
    return result


def write_result(path: Path, result: dict[str, Any]) -> None:
    """Write result.json, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


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


def write_push(args: argparse.Namespace) -> None:
    """Run push and write result.json."""
    result = run_push(args)
    write_result(args.output, result)
    print(f"push: {result['verdict']}; branch={result['branch_url']}")


def write_open(args: argparse.Namespace) -> None:
    """Run open and write result.json."""
    result = run_open(args)
    write_result(args.output, result)
    print(f"open: {result['verdict']}; pr={result['pull_request_url']}")


def build_parser() -> argparse.ArgumentParser:
    """Describe the eight commands, each bound to its handler."""
    return publish_cli.build_parser(
        write_check, write_push, write_open, description=__doc__
    )


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
