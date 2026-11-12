# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The one-per-repository rule: issues keep it, revision rounds do not."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
reads = import_module("issue_reads")
choose_module = import_module("selection_choose")
outputs = import_module("selection_outputs")

SHA = "a" * 40
FORK_ORGS: dict[str, Any] = {"default": "lfreleng-bot-forks", "organisations": {}}
COUNTERS = ("duplicate", "one_per_repo", "cap", "attempted", "linked_pr", "revision")
WAITED = "another target in org/alpha was chosen this run"


def issue(repo: str, number: int) -> dict[str, Any]:
    """A ranked issue candidate, as the scan hands it to ``choose``."""
    return {
        "repository": f"org/{repo}",
        "repo_name": repo,
        "number": number,
        "default_branch": "main",
        "title": f"Issue {number}",
        "assignees": [],
    }


def revision(repo: str, number: int) -> dict[str, Any]:
    """A minimal revision entry for issue ``number``."""
    return {
        "key": f"{repo}-{number}",
        "task": "revise",
        "repository": f"org/{repo}",
        "repo_name": repo,
        "number": number,
        "pull_request": {"number": 100 + number, "head_sha": SHA},
    }


def round_of(repo: str, number: int) -> tuple[str, Any]:
    """A revision builder of ``repo`` beside the repository it names."""
    return (f"org/{repo}", lambda: revision(repo, number))


class ChooseTest(unittest.TestCase):
    """Revision rounds pass the rule; issues keep it and record the skip."""

    def setUp(self) -> None:
        """Patch the reads ``choose`` makes for an issue with benign defaults."""
        self.prior = patch.object(reads, "prior_attempt", return_value=None).start()
        patch.object(reads, "has_open_linked_pr", return_value=False).start()
        patch.object(reads, "branch_head", return_value=SHA).start()
        patch.object(reads, "filtered_comments", return_value=([], 0, False)).start()
        self.addCleanup(patch.stopall)
        self.skipped = dict.fromkeys(COUNTERS, 0)
        self.skipped_issues: list[dict[str, str]] = []

    def choose(
        self,
        ranked: list[dict[str, Any]],
        rounds: list[tuple[str, Any]],
        cap: int = 0,
    ) -> list[str]:
        """Run a scan's ``choose`` and return the chosen keys."""
        chosen = choose_module.choose(
            ranked,
            fork_orgs=FORK_ORGS,
            max_pull_requests=cap,
            skipped=self.skipped,
            skipped_issues=self.skipped_issues,
            revisions=rounds,
        )
        return [str(entry["key"]) for entry in chosen]

    def test_two_rounds_of_one_repository_both_build(self) -> None:
        """Every bot pull request of a repository gets its round."""
        keys = self.choose([], [round_of("alpha", 7), round_of("alpha", 8)])
        self.assertEqual(keys, ["alpha-7", "alpha-8"])
        self.assertEqual(self.skipped["one_per_repo"], 0)
        self.assertEqual(self.skipped_issues, [])

    def test_a_revised_repository_gets_no_new_issue(self) -> None:
        """The round is taken and the issue's skip names it, unread."""
        keys = self.choose([issue("alpha", 3)], [round_of("alpha", 7)])
        self.assertEqual(keys, ["alpha-7"])
        self.prior.assert_not_called()
        self.assertEqual(self.skipped["one_per_repo"], 1)
        self.assertEqual(
            self.skipped_issues,
            [{"issue": "org/alpha#3", "reason": WAITED, "chosen": "org/alpha#7"}],
        )

    def test_two_issues_of_one_repository_give_one(self) -> None:
        """The second issue waits, and the record names the first."""
        keys = self.choose([issue("alpha", 3), issue("alpha", 4), issue("beta", 5)], [])
        self.assertEqual(keys, ["alpha-3", "beta-5"])
        self.assertEqual(self.skipped["one_per_repo"], 1)
        self.assertEqual(
            self.skipped_issues,
            [{"issue": "org/alpha#4", "reason": WAITED, "chosen": "org/alpha#3"}],
        )

    def test_the_cap_bounds_rounds_and_issues_together(self) -> None:
        """``max_pull_requests`` counts every entry, whatever its kind."""
        rounds = [round_of("alpha", 7), round_of("alpha", 8), round_of("beta", 9)]
        keys = self.choose([issue("gamma", 3)], rounds, cap=2)
        self.assertEqual(len(keys), 2)
        self.assertEqual(self.skipped["cap"], 2)

    def test_a_scan_records_only_the_rule(self) -> None:
        """A scan's other skips stay counters alone, as before."""
        self.prior.return_value = reads.PriorAttempt("pull_request", "merged", 9)
        self.assertEqual(self.choose([issue("alpha", 3)], []), [])
        self.assertEqual(self.skipped["attempted"], 1)
        self.assertEqual(self.skipped_issues, [])

    def test_summary_names_the_target_chosen(self) -> None:
        """The run summary shows which target took the repository's slot."""
        self.choose([issue("alpha", 3), issue("alpha", 4)], [])
        markdown = outputs.summary_markdown(
            {
                "mode": "pull-requests",
                "dry_run": False,
                "model": "m",
                "max_pull_requests": 0,
                "categories": [],
                "candidates_seen": 2,
                "issues": [],
                "skipped": self.skipped,
                "skipped_issues": self.skipped_issues,
            }
        )
        self.assertIn(f"| org/alpha#4 | {WAITED}: org/alpha#3 |", markdown)


if __name__ == "__main__":
    unittest.main()
