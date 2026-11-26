# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The agent egress mode the select job resolves from CODE_MONKEY_EGRESS_MODE."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
egress = import_module("egress_mode")


class ResolveTest(unittest.TestCase):
    """``resolve`` accepts three spellings exactly and defaults to allow-list."""

    def test_each_mode_selects_itself(self) -> None:
        """The three values resolve to themselves."""
        for mode in ("block-all", "allow-list", "audit-only"):
            with self.subTest(mode=mode):
                self.assertEqual(egress.resolve(mode), mode)

    def test_unset_and_empty_select_allow_list(self) -> None:
        """No value, an empty one or bare whitespace means allow-list."""
        for raw in (None, "", "  ", "\n"):
            with self.subTest(raw=raw):
                self.assertEqual(egress.resolve(raw), "allow-list")

    def test_surrounding_whitespace_is_ignored(self) -> None:
        """Padding around a valid value does not change it."""
        self.assertEqual(egress.resolve("  block-all\n"), "block-all")
        self.assertEqual(egress.resolve("\taudit-only "), "audit-only")

    def test_other_spellings_are_refused(self) -> None:
        """Near misses fail, naming the variable and every allowed value."""
        for raw in (
            "block",
            "Audit",
            "allow_list",
            "audit",
            "Allow-List",
            "allow list",
        ):
            with (
                self.subTest(raw=raw),
                self.assertRaises(egress.EgressModeError) as ctx,
            ):
                egress.resolve(raw)
            message = str(ctx.exception)
            for name in (
                "CODE_MONKEY_EGRESS_MODE",
                "block-all",
                "allow-list",
                "audit-only",
            ):
                self.assertIn(name, message)


class MainTest(unittest.TestCase):
    """The command line writes the step output and the summary line."""

    def run_main(self, mode: str) -> tuple[int, str, str, str]:
        """Run ``main``; return its status, stdout, the output and the summary."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, "output")
            summary = Path(tmp, "summary")
            stdout, stderr = io.StringIO(), io.StringIO()
            status = 0
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    egress.main(
                        [
                            f"--mode={mode}",
                            "--github-output",
                            str(output),
                            "--summary",
                            str(summary),
                        ]
                    )
                except SystemExit as exc:
                    status = int(exc.code or 0)
            written = output.read_text(encoding="utf-8") if output.exists() else ""
            summarised = summary.read_text(encoding="utf-8") if summary.exists() else ""
            return status, stdout.getvalue() + stderr.getvalue(), written, summarised

    def test_default_is_recorded_and_summarised(self) -> None:
        """An unset variable records allow-list in the output and the summary."""
        status, printed, written, summary = self.run_main("")
        self.assertEqual(status, 0)
        self.assertEqual(written, "mode=allow-list\n")
        self.assertIn("`allow-list`", summary)
        self.assertNotIn("::warning::", printed)

    def test_audit_only_warns(self) -> None:
        """Audit-only says agent egress is not enforced."""
        status, printed, written, summary = self.run_main(" audit-only ")
        self.assertEqual(status, 0)
        self.assertEqual(written, "mode=audit-only\n")
        self.assertIn("`audit-only`", summary)
        self.assertIn("::warning::Agent egress is not enforced", printed)

    def test_a_bad_value_fails_and_writes_nothing(self) -> None:
        """A refused value exits non-zero with an error and no output."""
        status, printed, written, summary = self.run_main("block")
        self.assertEqual(status, 1)
        self.assertIn("::error::CODE_MONKEY_EGRESS_MODE", printed)
        self.assertEqual((written, summary), ("", ""))


if __name__ == "__main__":
    unittest.main()
