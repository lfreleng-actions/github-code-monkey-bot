# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Mergeability: the bounded read, the post-review probe and its reports (#104).

A head that conflicts with the default branch gets no ``pull_request``
CI, so its checks look missing rather than failed. These tests pin
that the publish job reads ``mergeable`` after the review gate, that
the comment, report and outputs say so, and that the next selection
counts the conflict as work.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
model = import_module("proposal_model")
comments = import_module("issue_comment")
round_reads = import_module("round_reads")
reporting = import_module("proposal_report")
revision_check = import_module("revision_check")

REPOSITORY = "lfreleng-actions/alpha"
PULL_URL = f"https://github.com/{REPOSITORY}/pull/8"
HEAD = "e" * 40
SELECTED = "b" * 40
MOVED = "f" * 40
ROOT = Path(__file__).resolve().parents[1]


def no_wait(_seconds: float) -> None:
    """A sleep that returns at once."""


def merge_state() -> ModuleType:
    """Import the module inside each test, so its absence fails one test only."""
    return import_module("merge_state")


def outputs() -> ModuleType:
    """The report's output helpers, imported where a test needs them."""
    return import_module("report_outputs")


def pulls_read(*answers: Any, head: str = HEAD) -> list[Any]:
    """The pull request reads ``settle`` sees, in order, each naming ``head``."""
    return [
        answer
        if isinstance(answer, Exception)
        else {
            "mergeable": answer,
            "mergeable_state": "dirty" if answer is False else "unknown",
            "head": {"sha": head},
        }
        for answer in answers
    ]


def result(**overrides: Any) -> dict[str, Any]:
    """A published result, as the publish job holds it before the comment."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "alpha-7",
        "repository": REPOSITORY,
        "issue": 7,
        "verdict": "proposed",
        "reasons": [],
        "warnings": [],
        "dry_run": False,
        "mode": "pull-requests",
        "commits": [HEAD],
        "pull_request_url": PULL_URL,
        "review": {
            "verdict": "clean",
            "assessment": "approval",
            "unresolved_threads": 0,
            "suppressed": 0,
            "previously_missed": 0,
            "reasons": [],
            "review_id": 5,
            "commit_id": HEAD,
            "requested": True,
        },
    }
    data.update(overrides)
    return data


def check(**overrides: Any) -> dict[str, Any]:
    """A check.json for an issue entry: its base is the head it selected."""
    data: dict[str, Any] = {
        "repository": REPOSITORY,
        "task": "issue",
        "base_sha": SELECTED,
        "default_branch": "main",
    }
    data.update(overrides)
    return data


def revision_entry(mergeable: Any) -> dict[str, Any]:
    """A revision's selection entry whose pull request carries ``mergeable``."""
    return {
        "repository": REPOSITORY,
        "number": 7,
        "branch": "code-monkey/issue-7",
        "base_sha": "a" * 40,
        "default_branch": "main",
        "fork_org": "lfreleng-bot-forks",
        "fork_repository": "lfreleng-bot-forks/alpha",
        "task": "revise",
        "rebase_sha": SELECTED,
        "pull_request": {
            "number": 8,
            "url": PULL_URL,
            "head_sha": HEAD,
            "mergeable": mergeable,
        },
        "round": 2,
        "threads": [],
    }


class SettleTest(unittest.TestCase):
    """GitHub computes ``mergeable`` lazily; the read waits, bounded."""

    def test_null_then_false_settles_as_false(self) -> None:
        """Two null answers, then a conflict: three reads and two waits."""
        waits: list[float] = []
        with patch.object(
            github, "api_object", side_effect=pulls_read(None, None, False)
        ) as read:
            settled = merge_state().settle(REPOSITORY, 8, HEAD, sleep=waits.append)
        self.assertIs(settled.mergeable, False)
        self.assertEqual(settled.state, "dirty")
        self.assertIsNone(settled.warning)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(waits, [2, 4])
        self.assertEqual(read.call_args.args[0], f"repos/{REPOSITORY}/pulls/8")

    def test_never_settled_is_unknown_with_a_warning(self) -> None:
        """Null throughout is None after a bounded number of reads."""
        module = merge_state()
        reads_allowed = len(module.BACKOFF_SECONDS) + 1
        waits: list[float] = []
        with patch.object(
            github, "api_object", side_effect=pulls_read(*[None] * reads_allowed)
        ):
            settled = module.settle(REPOSITORY, 8, HEAD, sleep=waits.append)
        self.assertIsNone(settled.mergeable)
        self.assertIn("had not settled", settled.warning or "")
        self.assertEqual(len(waits), reads_allowed - 1)
        self.assertLessEqual(sum(waits), 90)

    def test_a_failed_read_is_unknown_never_mergeable(self) -> None:
        """A GitHub error is None with a warning; it is never read as true."""
        with patch.object(
            github, "api_object", side_effect=github.GitHubError("HTTP 502")
        ):
            settled = merge_state().settle(REPOSITORY, 8, HEAD, sleep=no_wait)
        self.assertIsNone(settled.mergeable)
        self.assertIn("could not read", settled.warning or "")

    def test_unexpected_state_text_is_not_recorded(self) -> None:
        """Only a short lowercase word reaches the result as ``mergeable_state``."""
        with patch.object(
            github,
            "api_object",
            return_value={
                "mergeable": True,
                "mergeable_state": "<b>x</b>",
                "head": {"sha": HEAD},
            },
        ):
            settled = merge_state().settle(REPOSITORY, 8, HEAD, sleep=no_wait)
        self.assertIs(settled.mergeable, True)
        self.assertIsNone(settled.state)

    def test_an_answer_for_another_head_is_read_again(self) -> None:
        """A stale read naming an older head is not this head's answer."""
        waits: list[float] = []
        answers = [*pulls_read(True, head=MOVED), *pulls_read(False)]
        with patch.object(github, "api_object", side_effect=answers) as read:
            settled = merge_state().settle(REPOSITORY, 8, HEAD, sleep=waits.append)
        self.assertIs(settled.mergeable, False)
        self.assertIsNone(settled.warning)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(waits, [2])

    def test_a_head_that_never_appears_is_unknown(self) -> None:
        """Booleans for another head throughout are None with a warning."""
        module = merge_state()
        reads_allowed = len(module.BACKOFF_SECONDS) + 1
        with patch.object(
            github,
            "api_object",
            side_effect=pulls_read(*[True] * reads_allowed, head=MOVED),
        ):
            settled = module.settle(REPOSITORY, 8, HEAD, sleep=no_wait)
        self.assertIsNone(settled.mergeable)
        self.assertIsNone(settled.state)
        self.assertIn(f"did not show head {HEAD[:12]}", settled.warning or "")


class ProbeTest(unittest.TestCase):
    """The publish job's probe records what it read, and nothing it could not."""

    def probe(
        self,
        answers: list[Any],
        data: dict[str, Any],
        entry: dict[str, Any],
        *,
        tip: str = SELECTED,
        behind: Any = 0,
    ) -> list[str]:
        """Run the probe against canned reads and return the API paths it read."""
        paths: list[str] = []
        queue = list(answers)

        def api(path: str) -> Any:
            paths.append(path)
            if "/compare/" in path:
                if isinstance(behind, Exception):
                    raise behind
                return {"behind_by": behind}
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with (
            patch.object(github, "api_object", side_effect=api),
            patch.object(reads, "branch_head", return_value=tip),
        ):
            merge_state().probe(data, entry, sleep=no_wait)
        return paths

    def test_conflict_with_a_moved_base(self) -> None:
        """A conflict, how far behind, and that the base moved during the run."""
        data = result()
        paths = self.probe(pulls_read(None, False), data, check(), tip=MOVED, behind=3)
        self.assertIs(data["mergeable"], False)
        self.assertEqual(data["mergeable_state"], "dirty")
        self.assertEqual(data["base_behind_by"], 3)
        self.assertIs(data["base_moved_during_run"], True)
        self.assertEqual(data["default_branch"], "main")
        self.assertEqual(data["warnings"], [])
        self.assertIn(f"repos/{REPOSITORY}/compare/{MOVED}...{HEAD}", paths)

    def test_revision_compares_with_its_rebase_head(self) -> None:
        """A revision's selected base is ``rebase_sha``, not the old merge base."""
        data = result(round=2)
        entry = check(task="revise", rebase_sha=SELECTED, base_sha="a" * 40)
        self.probe(pulls_read(True), data, entry, tip=SELECTED, behind=0)
        self.assertIs(data["mergeable"], True)
        self.assertIs(data["base_moved_during_run"], False)
        self.assertEqual(data["base_behind_by"], 0)

    def test_failed_reads_leave_warnings_and_unknowns(self) -> None:
        """No read failure stops the job or reads as mergeable."""
        data = result()
        failure = github.GitHubError("HTTP 500")
        self.probe([failure], data, check(), behind=failure)
        self.assertIsNone(data["mergeable"])
        self.assertIsNone(data["base_behind_by"])
        self.assertEqual(len(data["warnings"]), 2)

    def test_no_published_head_reads_no_mergeability(self) -> None:
        """Without a head to match, the probe records unknown, never a guess."""
        data = result(commits=[])
        paths = self.probe([], data, check(), tip=SELECTED)
        self.assertIsNone(data["mergeable"])
        self.assertIsNone(data["base_behind_by"])
        self.assertIs(data["base_moved_during_run"], False)
        self.assertNotIn(f"repos/{REPOSITORY}/pulls/8", paths)
        self.assertIn("no published head", data["warnings"][0])

    def test_nothing_published_is_not_probed(self) -> None:
        """A dry run, a branch alone or a non-proposal reads nothing."""
        for data in (
            result(dry_run=True),
            result(pull_request_url=None),
            result(verdict="abstain"),
        ):
            with self.subTest(data=data):
                self.assertEqual(self.probe([], data, check()), [])
                self.assertNotIn("mergeable", data)

    def test_cli_rewrites_the_result(self) -> None:
        """``merge_state.py`` reads both files and writes the result back."""
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "result.json").write_text(json.dumps(result()), encoding="utf-8")
            (root / "check.json").write_text(json.dumps(check()), encoding="utf-8")
            argv = [
                "--result",
                str(root / "result.json"),
                "--check",
                str(root / "check.json"),
                "--output",
                str(root / "result.json"),
            ]
            with (
                patch.object(
                    github,
                    "api_object",
                    side_effect=[*pulls_read(False), {"behind_by": 2}],
                ),
                patch.object(reads, "branch_head", return_value=SELECTED),
                redirect_stdout(io.StringIO()),
            ):
                merge_state().main(argv)
            written = json.loads((root / "result.json").read_text(encoding="utf-8"))
        self.assertIs(written["mergeable"], False)
        self.assertEqual(written["base_behind_by"], 2)


class CommentTest(unittest.TestCase):
    """The issue comment leads with a conflict and never calls it clean."""

    def test_conflict_leads_and_is_not_clean(self) -> None:
        """A clean review of a conflicting head is not the closing word."""
        data = result(
            mergeable=False, default_branch="main", base_moved_during_run=True
        )
        text = comments.comment_body(data, "") or ""
        self.assertIn(
            "The pull request conflicts with `main`, which moved while this run "
            "worked, so its CI has not run.",
            text,
        )
        self.assertNotIn("clean", text)
        self.assertIn("raised no findings", text)
        self.assertTrue(text.rstrip().endswith("The next round rebases it."))
        self.assertLess(text.index("conflicts"), text.index("raised no findings"))

    def test_unknown_says_unconfirmed(self) -> None:
        """A probe that never settled is said plainly, never as mergeable."""
        text = comments.comment_body(result(mergeable=None), "") or ""
        self.assertIn("GitHub did not confirm whether the pull request can merge", text)
        self.assertNotIn("rebases", text)

    def test_mergeable_or_unprobed_reads_as_before(self) -> None:
        """A head that can merge, or one never probed, keeps the old comment."""
        before = comments.comment_body(result(), "")
        after = comments.comment_body(result(mergeable=True), "")
        self.assertEqual(before, after)
        self.assertIn("The Copilot review of it is clean.", after or "")

    def test_unsafe_branch_name_is_generic(self) -> None:
        """A default branch name that could break the comment is not quoted."""
        data = result(mergeable=False, default_branch="a`b <!-- x")
        text = comments.comment_body(data, "") or ""
        self.assertIn("conflicts with the default branch, so", text)

    def test_conflict_at_the_cap_is_not_finished(self) -> None:
        """A tenth round that leaves a conflict hands over, not 'rebases it'."""
        data = result(round=10, round_summary="s", mergeable=False)
        self.assertTrue(comments.capped(data))
        text = comments.comment_body(data, "") or ""
        self.assertIn("a human must take over", text)
        self.assertNotIn("The next round rebases it.", text)

    def test_marker_records_the_probe_and_still_counts_the_round(self) -> None:
        """The footer gains ``mergeable=`` and the round reader still parses it."""
        posted: dict[str, Any] = {}
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "result.json"
            data = result(
                bot_login="code-monkey[bot]",
                round=3,
                round_summary="s",
                mergeable=False,
            )
            path.write_text(json.dumps(data), encoding="utf-8")

            def write(_method: str, _path: str, payload: dict[str, Any]) -> Any:
                posted.update(payload)
                return {"html_url": "https://x/c/1"}

            with (
                patch.object(reads, "api_page", return_value=[]),
                patch.object(github, "api_write", side_effect=write),
                redirect_stdout(io.StringIO()),
            ):
                comments.run_comment(
                    argparse.Namespace(result=path, run_url="https://run", since="t")
                )
        body = str(posted["body"])
        self.assertTrue(
            body.endswith(
                "<!-- code-monkey https://run proposed review=clean mergeable=false "
                "round=3 -->"
            )
        )
        found = round_reads.FOOTER.search(body)
        assert found is not None
        self.assertEqual(found.group("round"), "3")


class ReportTest(unittest.TestCase):
    """The run report and outputs list a conflicting pull request apart."""

    def setUp(self) -> None:
        """A results tree in a scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def write(self, name: str, data: dict[str, Any]) -> None:
        """One result.json under its own directory."""
        directory = self.root / "results" / name
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(json.dumps(data), encoding="utf-8")

    def test_report_counts_and_lists_conflicts(self) -> None:
        """Totals by merge state, a conflict list, and the output line."""
        conflicting = "https://github.com/lfreleng-actions/beta/pull/9"
        self.write("a", result(key="a-1", mergeable=True))
        self.write(
            "b",
            result(
                key="b-2",
                repository="lfreleng-actions/beta",
                issue=9,
                pull_request_url=conflicting,
                mergeable=False,
                base_behind_by=4,
            ),
        )
        self.write("c", result(key="c-3", mergeable=None))
        self.write("d", result(key="d-4", verdict="abstain", pull_request_url=None))
        github_output = self.root / "github_output"
        args = argparse.Namespace(
            results=self.root / "results",
            output_md=self.root / "report.md",
            output_json=self.root / "report.json",
            org="lfreleng-actions",
            github_output=github_output,
        )
        with redirect_stdout(io.StringIO()):
            reporting.run_report(args)
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(
            report["merge_totals"], {"true": 1, "false": 1, "unknown": 1, "none": 1}
        )
        self.assertEqual(report["unmergeable_pull_requests"], [conflicting])
        markdown = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn(
            "Pull request heads: mergeable 1, conflicting 1, unconfirmed 1, "
            "not probed 1.",
            markdown,
        )
        self.assertIn(
            f"- lfreleng-actions/beta#9: {conflicting}, 4 commits behind", markdown
        )
        lines = github_output.read_text(encoding="utf-8").splitlines()
        self.assertIn(f'unmergeable_pull_requests=["{conflicting}"]', lines)

    def test_a_conflict_at_the_cap_is_handed_to_a_human(self) -> None:
        """No promise of a later round for a pull request at the round cap."""
        lines = reporting.conflict_lines(
            [result(mergeable=False, round=10), result(mergeable=False, round=3)]
        )
        text = "\n".join(lines)
        self.assertNotIn("the next round rebases", text)
        self.assertIn("a human at the round cap", text)
        capped = [line for line in lines if "round cap reached" in line]
        self.assertEqual(len(capped), 1)
        self.assertIn("a human must take over", capped[0])

    def test_only_a_settled_conflict_is_unmergeable(self) -> None:
        """An unknown is not listed, and a link outside the org never is."""
        found = outputs().unmergeable_urls(
            [
                result(mergeable=None),
                result(mergeable=False, pull_request_url="https://evil.example/pull/1"),
                result(mergeable=False),
            ],
            "lfreleng-actions",
        )
        self.assertEqual(found, [PULL_URL])


class RoundNoteTest(unittest.TestCase):
    """The pull request description says the round set out to resolve a conflict."""

    def test_conflicted_round_says_so(self) -> None:
        """Selected conflicting, the note names it; mergeable, it does not."""
        conflicted = model.Check.from_entry("alpha-7", revision_entry(False))
        self.assertIs(conflicted.conflicted, True)
        self.assertIs(conflicted.to_json()["conflicted"], True)
        self.assertIn(
            "It set out to resolve a conflict with the default branch.",
            revision_check.round_note(conflicted),
        )
        for value in (True, None):
            with self.subTest(mergeable=value):
                quiet = model.Check.from_entry("alpha-7", revision_entry(value))
                self.assertIs(quiet.conflicted, False)
                self.assertNotIn("conflict", revision_check.round_note(quiet))

    def test_revise_prompt_rebases_a_conflicting_head(self) -> None:
        """The revise prompt names the reason and asks for the rebase."""
        prompt = (ROOT / "prompt" / "revise.md").read_text(encoding="utf-8")
        flat = " ".join(prompt.split())
        self.assertIn("`conflicts with the default branch`", flat)
        self.assertIn("rebase even when no maintainer asked", flat)


if __name__ == "__main__":
    unittest.main()
