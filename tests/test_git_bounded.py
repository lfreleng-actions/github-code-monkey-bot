# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bounded git reads: hostile objects cannot exhaust the publisher."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

bounded = import_module("git_bounded")
policy = import_module("proposal_policy")


class BoundedGitTest(unittest.TestCase):
    """``git`` streams output and stops at its limit."""

    def setUp(self) -> None:
        """A repository holding one large blob."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.repo = Path(holder.name)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, env=env)
        self.big = (
            subprocess.run(
                ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                input=b"x" * 300_000,
                capture_output=True,
                check=True,
                env=env,
            )
            .stdout.decode()
            .strip()
        )

    def test_overflow_is_a_rejection(self) -> None:
        """Reading past the limit stops git and rejects the proposal."""
        with self.assertRaisesRegex(policy.Rejection, "more than 1000 bytes"):
            bounded.git(self.repo, "cat-file", "blob", self.big, limit=1000)

    def test_head_read_returns_the_prefix(self) -> None:
        """A head read keeps what it needs and stops early without error."""
        content = bounded.git(
            self.repo, "cat-file", "blob", self.big, binary=True, limit=8192, head=True
        )
        self.assertEqual(content, b"x" * 8192)

    def test_within_limit_returns_everything(self) -> None:
        """Output under the limit arrives whole."""
        size = bounded.git(self.repo, "cat-file", "-s", self.big)
        self.assertEqual(str(size).strip(), "300000")

    def test_failure_is_operational(self) -> None:
        """A failing git command is a PublishError with its stderr."""
        with self.assertRaisesRegex(policy.PublishError, "failed"):
            bounded.git(self.repo, "cat-file", "blob", "0" * 40)


class ProcessGroupTest(unittest.TestCase):
    """A timeout kills git's helpers along with git itself."""

    def test_helpers_die_with_git(self) -> None:
        """A background child of git does not outlive the timeout."""
        with tempfile.TemporaryDirectory() as holder:
            root = Path(holder)
            pidfile = root / "child.pid"
            alias = f"!sh -c 'echo $$ > {pidfile}; exec sleep 30' & sleep 30"
            with (
                patch.object(bounded, "TIMEOUT_SECONDS", 1),
                self.assertRaises(policy.PublishError),
            ):
                bounded.git(root, "-c", f"alias.slow={alias}", "slow")
            child = int(pidfile.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 5
            alive = True
            while alive and time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    alive = False
            self.assertFalse(alive, "git's helper survived the timeout")


class ResourceLimitTest(unittest.TestCase):
    """Every git process runs under the OS ceilings in ``LIMITS``."""

    def test_limits_apply_in_the_child(self) -> None:
        """The child's CPU ceiling is the one LIMITS names."""
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0])",
            ],
            capture_output=True,
            text=True,
            check=True,
            preexec_fn=bounded.limit_resources,
        )
        self.assertEqual(int(probe.stdout), bounded.LIMITS["RLIMIT_CPU"])

    def test_git_still_runs_under_the_limits(self) -> None:
        """Ordinary git work fits comfortably inside the ceilings."""
        self.assertIn("git version", str(bounded.git(Path("."), "--version")))


if __name__ == "__main__":
    unittest.main()
