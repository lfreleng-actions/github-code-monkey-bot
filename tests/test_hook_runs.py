# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which commands are hook runs, and when the last one marks a proposal."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
hooks = import_module("hook_runs")


def run(command: str, code: Any = 0) -> dict[str, Any]:
    """One kept ``commands`` entry."""
    return {"command": command, "exit_code": code}


class IsHookRunTest(unittest.TestCase):
    """``prek run`` or ``pre-commit run``, as whole words, is a hook run."""

    def test_hook_runs(self) -> None:
        """Exclusions, a runner prefix and a single stage all count."""
        for command in (
            "SKIP=a,b prek run --files x",
            "uvx pre-commit run --all-files",
            "prek run gitlint --hook-stage commit-msg",
        ):
            with self.subTest(command=command):
                self.assertTrue(hooks.is_hook_run(command))

    def test_other_commands(self) -> None:
        """Installing hooks, a test run or a commit that ran them does not."""
        for command in (
            "prek install",
            "uv run pytest",
            "git commit -s (pre-commit and commit-msg hooks)",
            "my-prek run",
            "prek runner",
        ):
            with self.subTest(command=command):
                self.assertFalse(hooks.is_hook_run(command))


class MarkTest(unittest.TestCase):
    """Only a last hook run that did not exit 0 earns a line and a warning."""

    def marked(self, commands: list[dict[str, Any]]) -> tuple[str, list[str]]:
        """The body line and the warnings ``mark`` leaves."""
        warnings: list[str] = []
        return hooks.mark(commands, warnings), warnings

    def test_failing_last_hook_run(self) -> None:
        """The line and the warning both name the command and its exit code."""
        line, warnings = self.marked(
            [run("prek run --files a.py", 1), run("uv run pytest", 0)]
        )
        self.assertEqual(
            warnings, ["last hook run did not exit 0 (exit 1): `prek run --files a.py`"]
        )
        self.assertIn("`prek run --files a.py`", line)
        self.assertIn("(exit 1)", line)
        self.assertTrue(line.endswith("\n\n"))
        self.assertEqual(line.count("\n"), 2)

    def test_failing_run_followed_by_a_passing_one(self) -> None:
        """A later passing hook run is the one the proposal stands on."""
        line, warnings = self.marked(
            [run("prek run --files a.py", 1), run("SKIP=x prek run --files a.py", 0)]
        )
        self.assertEqual((line, warnings), ("", []))

    def test_no_hook_run(self) -> None:
        """No hook run, nothing to mark."""
        self.assertEqual(self.marked([run("uv run pytest", 0)]), ("", []))
        self.assertEqual(self.marked([]), ("", []))

    def test_failing_command_that_is_not_a_hook_run(self) -> None:
        """A non-zero exit elsewhere is left to the provenance table."""
        line, warnings = self.marked(
            [run("prek run --files a.py", 0), run("uv run pytest", 1)]
        )
        self.assertEqual((line, warnings), ("", []))

    def test_last_hook_run_without_an_exit_code(self) -> None:
        """A missing or non-integer exit code is not a pass."""
        for entry in (
            {"command": "prek run --files a.py"},
            run("prek run --files a.py", None),
            run("prek run --files a.py", True),
            run("prek run --files a.py", "0"),
        ):
            with self.subTest(entry=entry):
                line, warnings = self.marked([entry])
                self.assertEqual(len(warnings), 1)
                self.assertIn("no exit code recorded", warnings[0])
                self.assertIn("no exit code recorded", line)


if __name__ == "__main__":
    unittest.main()
