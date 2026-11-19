# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fuzz harnesses' seeds replay clean, and their oracles catch a breach.

Atheris is not needed: each harness fuzzes a plain ``check(data)``
function, and this suite feeds it every seed the harness ships. A
regression on an input the fuzzer once found fails here, in the
normal run.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fuzz"))
support = import_module("harness_support")
rules = import_module("fuzz_proposal_rules")
classifier = import_module("fuzz_review_classifier")
operator = import_module("fuzz_operator_input")
verdicts = import_module("review_verdict")
Breach = support.Breach


class SeedReplayTest(unittest.TestCase):
    """Every shipped seed passes its harness's oracles."""

    def test_seeds(self) -> None:
        """No seed of any harness raises."""
        for harness in (rules, classifier, operator):
            for index, seed in enumerate(harness.SEEDS):
                with self.subTest(harness=harness.__name__, seed=index):
                    harness.check(seed)

    def test_empty_input(self) -> None:
        """An empty input checks nothing and raises nothing."""
        for harness in (rules, classifier, operator):
            harness.check(b"")


class OracleTest(unittest.TestCase):
    """Each oracle raises ``Breach`` on a result that breaks its contract."""

    def test_proposal_rules(self) -> None:
        """Wrong path, mode, text and reason results are caught."""
        cases = (
            lambda: rules.oracle_path("../x", True, False),
            lambda: rules.oracle_path("AGENTS.md", True, False),
            lambda: rules.oracle_mode("A", "000000", "100755", True),
            lambda: rules.oracle_comment("@alice", "@alice"),
            lambda: rules.oracle_mentions("@alice", "@alice"),
            lambda: rules.oracle_log("::error::x"),
            lambda: rules.oracle_log("a\ud800b"),
            lambda: rules.oracle_reasons(["a"], ["b"]),
        )
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(Breach):
                case()

    def test_message(self) -> None:
        """A moved subject, a lost trailer or an over-long line is caught."""
        message = "Fix: Add a seed\n\nWhy it is needed."
        pair = f"Co-authored-by: {rules.IDENTITY.coauthor}\n{rules.IDENTITY.sign_off}"
        body = f"Why it is needed.\n\n{pair}"
        rules.oracle_message(message, 50, ("Fix: Add a seed", body))
        cases = (
            ("Fix: Another subject", body),
            ("Fix: Add a seed", "Why it is needed.\n\nSigned-off-by: A <a@b>"),
            ("Fix: Add a seed", f"{'x' * 73}\n\n{pair}"),
            ("Fix: Add a seed", f"Why.\n\nReviewed-by: S <s@x>\n{pair}"),
        )
        for index, result in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(Breach):
                rules.oracle_message(message, 50, result)

    def test_manifest(self) -> None:
        """A returned manifest that differs from the file is caught."""
        check = rules.model.Check.from_entry("key", rules.ENTRY)
        parsed = {
            "schema": 1,
            "outcome": "proposed",
            "repository": rules.ENTRY["repository"],
            "issue": rules.ENTRY["number"],
            "base_sha": rules.BASE,
            "branch": rules.ENTRY["branch"],
        }
        raw = json.dumps(parsed).encode()
        rules.oracle_manifest(raw, check, dict(parsed))
        cases = (
            {**parsed, "repository": "other/untrusted", "issue": 999, "schema": 99},
            {**parsed, "base_sha": "f" * 40},
            {key: value for key, value in parsed.items() if key != "branch"},
        )
        for index, manifest in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(Breach):
                rules.oracle_manifest(raw, check, manifest)

    def test_review_classifier(self) -> None:
        """A wrong clean verdict, a moved span or a missing span reason is caught."""
        clean = verdicts.Verdict(
            verdict="clean",
            assessment="approval",
            reasons=[],
            unresolved_threads=0,
            suppressed=0,
            previously_missed=0,
        )
        review_span = classifier.UNREADABLE_SPAN
        cases = (
            lambda: classifier.oracle_verdict("no heading", 0, clean),
            lambda: classifier.oracle_verdict("### 🟢 Fine", 1, clean),
            lambda: classifier.oracle_verdict(
                "### 🟢 This is not a recognized assessment", 0, clean
            ),
            lambda: classifier.oracle_verdict("### 🔵 Approval recommended", 0, clean),
            lambda: classifier.oracle_verdict(
                "### 🟢 Approval recommendedly", 0, clean
            ),
            lambda: classifier.oracle_verdict(
                "### 🔵 Needs a closer look",
                0,
                dataclasses.replace(clean, assessment="approval"),
            ),
            lambda: classifier.oracle_twin(
                clean, dataclasses.replace(clean, verdict="findings")
            ),
            lambda: classifier.oracle_prefixed("changes", 1, clean),
            lambda: classifier.oracle_prefixed(
                "changes", 1, dataclasses.replace(clean, verdict="findings")
            ),
            lambda: classifier.oracle_prefixed(
                "suppressed", 2, dataclasses.replace(clean, suppressed=1)
            ),
            lambda: classifier.oracle_prefixed(
                "missed", 2, dataclasses.replace(clean, verdict="findings")
            ),
            lambda: classifier.oracle_fenced(clean),
            lambda: classifier.oracle_fenced(
                dataclasses.replace(clean, verdict="findings")
            ),
            lambda: classifier.oracle_fenced(
                dataclasses.replace(
                    clean, verdict="findings", assessment=None, suppressed=1
                )
            ),
            lambda: classifier.oracle_mask("a `b`", "x `b`", False, False),
            lambda: classifier.oracle_mask("`a` Z `b`", "`a` Z `b`", False, False),
            lambda: classifier.oracle_mask("`a` Z `b`", "xxx x xxx", False, False),
            lambda: classifier.oracle_mask("`a:1` `b`", "xxxxx xxx", False, False),
            lambda: classifier.oracle_mask("``a` b``", "``a` b``", True, False),
            lambda: classifier.oracle_mask("`a` `b", "xxx `b", False, False),
            lambda: classifier.oracle_mask(
                "**`src/x.py:12 `**", "**xxxxxxxxxxxxxx**", False, False
            ),
            lambda: classifier.oracle_line("plain", "plain", {"made up"}),
            lambda: classifier.oracle_line(
                "**`src/x.py:12 `**", "**xxxxxxxxxxxxxx**", set()
            ),
            lambda: classifier.oracle_line("`open", "`open", set()),
            lambda: classifier.oracle_line("a \\` b", "a \\` b", set()),
            lambda: classifier.oracle_line("`a`", "`a`", set()),
            lambda: classifier.oracle_line("`a`", "xxx", {review_span}),
            lambda: classifier.oracle_sections(
                "", 3, ((0, False), (0, False), (2, False), (3, False), False)
            ),
        )
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(Breach):
                case()

    def test_operator_input(self) -> None:
        """A refused valid list, or a foreign owner accepted, is caught."""
        cases = (
            lambda: operator.oracle_issues("lfreleng-actions/a#1", None),
            lambda: operator.oracle_issues("other/a#1", [("a", 1)]),
            lambda: operator.oracle_listed("alice", None),
            lambda: operator.oracle_listed("-alice", frozenset({"-alice"})),
        )
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(Breach):
                case()


class ArgumentsTest(unittest.TestCase):
    """``arguments`` adds the seeds only when no input file is named."""

    def test_adds_seed_directory(self) -> None:
        """With no file named, a directory holding each seed is appended."""
        args = support.arguments(["fuzz.py", "-runs=0"], [b"a", b"b"])
        self.assertEqual(args[:2], ["fuzz.py", "-runs=0"])
        seeds = sorted(path.read_bytes() for path in Path(args[2]).iterdir())
        self.assertEqual(seeds, [b"a", b"b"])

    def test_named_file_runs_alone(self) -> None:
        """A named crash input replays without the seeds."""
        with tempfile.NamedTemporaryFile() as crash:
            argv = ["fuzz.py", crash.name]
            self.assertEqual(support.arguments(argv, [b"a"]), argv)


if __name__ == "__main__":
    unittest.main()
