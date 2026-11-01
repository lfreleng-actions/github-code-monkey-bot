# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Git fixtures stay out of the repository a hook runs them from.

A commit in a linked worktree exports that worktree's ``GIT_DIR`` and
``GIT_INDEX_FILE`` to its hooks, and the ``monkey-tests`` hook runs
this suite. Each test here builds a sentinel repository with a linked
worktree, exports those variables as git would, runs a real fixture
under them, and checks the sentinel is untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
clean_git_env = import_module("git_env").clean_git_env
proposal_tests = import_module("test_proposal_check")
bounded_tests = import_module("test_git_bounded")


class InheritedGitDirTest(unittest.TestCase):
    """Fixtures ignore a ``GIT_DIR`` inherited from a worktree hook."""

    def setUp(self) -> None:
        """A sentinel clone with one commit and a linked worktree."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        self.env = {
            **clean_git_env(),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Sentinel",
            "GIT_AUTHOR_EMAIL": "sentinel@example.invalid",
            "GIT_COMMITTER_NAME": "Sentinel",
            "GIT_COMMITTER_EMAIL": "sentinel@example.invalid",
        }
        self.main = root / "main"
        self.worktree = root / "wt"
        self.git(root, "init", "-q", "-b", "main", str(self.main))
        (self.main / "file.txt").write_text("sentinel\n", encoding="utf-8")
        self.git(self.main, "add", "file.txt")
        self.git(self.main, "commit", "-q", "-m", "Chore: Sentinel")
        self.git(self.main, "worktree", "add", "-q", "-b", "wt", str(self.worktree))
        admin = self.git(self.worktree, "rev-parse", "--absolute-git-dir").strip()
        self.exported = {
            "GIT_DIR": admin,
            "GIT_INDEX_FILE": str(Path(admin) / "index"),
            "GIT_PREFIX": "",
        }
        self.before = self.snapshot()

    def git(self, cwd: Path, *args: str) -> str:
        """Run git on the sentinel with a clean environment."""
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout

    def snapshot(self) -> dict[str, str]:
        """What a fixture leaking into the sentinel would change."""
        return {
            "core.bare": self.git(self.main, "config", "--get", "core.bare"),
            "head": self.git(self.worktree, "rev-parse", "HEAD"),
            "refs": self.git(self.main, "for-each-ref"),
            "status": self.git(self.worktree, "status", "--porcelain"),
        }

    def run_case(self, case: unittest.TestCase) -> None:
        """Run ``case`` under the exported variables; the sentinel holds."""
        result = unittest.TestResult()
        with patch.dict(os.environ, self.exported):
            case.run(result)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(result.errors + result.failures, [])

    def test_proposal_check_fixture(self) -> None:
        """A ``GitCase`` fixture builds its repositories where it means to."""
        self.run_case(proposal_tests.RunCheckProposedTest("test_happy_path"))

    def test_git_bounded_fixture(self) -> None:
        """The bounded git fixture writes its blob to its own repository."""
        self.run_case(
            bounded_tests.BoundedGitTest("test_within_limit_returns_everything")
        )


if __name__ == "__main__":
    unittest.main()
