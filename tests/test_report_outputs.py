# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The run's pull request links, as the reusable workflow's outputs.

Every ``result.json`` field may come from the author job (docs/DESIGN.md
18.4), so a link reaches the outputs only when it names a pull request
in the run's own organisation, and nothing it carries can add a line to
``$GITHUB_OUTPUT``.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
reporting = import_module("proposal_report")

ORG = "lfreleng-actions"


def outputs() -> ModuleType:
    """Import the helper inside each test, so its absence fails one test only."""
    return import_module("report_outputs")


def pull(repo: str, number: int) -> str:
    """A pull request URL in the run's organisation."""
    return f"https://github.com/{ORG}/{repo}/pull/{number}"


def result(url: Any, **overrides: Any) -> dict[str, Any]:
    """A live result naming ``url``."""
    data: dict[str, Any] = {
        "key": "repo-7",
        "verdict": "proposed",
        "dry_run": False,
        "pull_request_url": url,
    }
    data.update(overrides)
    return data


class PullRequestUrlsTest(unittest.TestCase):
    """Which result links reach the outputs, and in what order."""

    @unittest.expectedFailure
    def test_valid_links_keep_report_order_without_duplicates(self) -> None:
        """Order is the report's; a repeat, in any case, counts once."""
        first, second = pull("b-repo", 3), pull("a.repo_2", 41)
        urls, excluded = outputs().pull_request_urls(
            [
                result(first),
                result(second),
                result(first, key="other"),
                result(
                    second.upper().replace("HTTPS://GITHUB.COM", "https://github.com")
                ),
            ],
            ORG,
        )
        self.assertEqual(urls, [first, second])
        self.assertEqual(excluded, 0)

    @unittest.expectedFailure
    def test_owner_compares_case_insensitively(self) -> None:
        """The organisation's spelling in the URL does not matter."""
        url = "https://github.com/LFReleng-Actions/repo/pull/9"
        urls, excluded = outputs().pull_request_urls([result(url)], ORG)
        self.assertEqual(urls, [url])
        self.assertEqual(excluded, 0)

    @unittest.expectedFailure
    def test_dry_run_and_missing_links_are_left_out_quietly(self) -> None:
        """A dry run or a result without a link is no pull request, not a fault."""
        urls, excluded = outputs().pull_request_urls(
            [
                result(pull("repo", 1), dry_run=True),
                result(pull("repo", 2), dry_run="false"),
                result(None),
                result(""),
                {"key": "repo-9", "dry_run": False},
            ],
            ORG,
        )
        self.assertEqual(urls, [])
        self.assertEqual(excluded, 0)

    @unittest.expectedFailure
    def test_links_failing_the_pattern_are_counted_not_kept(self) -> None:
        """Foreign owners, other pages and smuggled text are all refused."""
        bad = [
            "https://github.com/someone-else/repo/pull/1",
            f"https://github.com/{ORG}-evil/repo/pull/1",
            f"https://github.com/{ORG}/repo/issues/1",
            f"https://github.com/{ORG}/repo/pull/0",
            f"https://github.com/{ORG}/repo/pull/1?x=1",
            f"https://github.com/{ORG}/repo/pull/1#top",
            f"https://github.com/{ORG}/repo/pull/1 and more",
            f"https://github.com/{ORG}/repo/pull/1\n",
            f"https://github.com/{ORG}/repo/pull/1\npull_request_count=99",
            f"http://github.com/{ORG}/repo/pull/1",
            f"https://github.com.evil/{ORG}/repo/pull/1",
            # Unicode case folding would equate these with ASCII letters.
            f"https://g\u0131thub.com/{ORG}/repo/pull/1",
            f"https://github.com/{ORG}/repo/pull/1".replace("s", "\u017f", 1),
            f"https://github.com/{ORG}/\u212arepo/pull/1",
            f"https://github.com/{ORG}/re po/pull/1",
            12,
            ["https://github.com/x"],
        ]
        urls, excluded = outputs().pull_request_urls(
            [result(url) for url in bad] + [result(pull("repo", 5))], ORG
        )
        self.assertEqual(urls, [pull("repo", 5)])
        self.assertEqual(excluded, len(bad))


class OutputLinesTest(unittest.TestCase):
    """The ``$GITHUB_OUTPUT`` text: two fixed keys, one line each."""

    @unittest.expectedFailure
    def test_compact_single_line_values(self) -> None:
        """The array is compact JSON and the count a string of its length."""
        text = outputs().output_lines([pull("a", 1), pull("b", 2)])
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(text.endswith("\n"))
        key, _, value = lines[0].partition("=")
        self.assertEqual(key, "pull_requests")
        self.assertEqual(json.loads(value), [pull("a", 1), pull("b", 2)])
        self.assertNotIn(" ", value)
        self.assertEqual(lines[1], "pull_request_count=2")

    @unittest.expectedFailure
    def test_no_links(self) -> None:
        """An empty run still writes both keys."""
        self.assertEqual(
            outputs().output_lines([]), "pull_requests=[]\npull_request_count=0\n"
        )


class RunReportOutputsTest(unittest.TestCase):
    """``publish.py report --org --github-output`` writes the outputs."""

    def setUp(self) -> None:
        """A results tree and an output file in a scratch directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.results = self.root / "results"
        self.github_output = self.root / "github_output"
        self.github_output.write_text("earlier=1\n", encoding="utf-8")

    def write(self, name: str, data: dict[str, Any]) -> None:
        """Write one result.json under its own directory."""
        directory = self.results / name
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(json.dumps(data), encoding="utf-8")

    def run_report(self, **extra: Any) -> str:
        """Run the report and return what it printed."""
        args = argparse.Namespace(
            results=self.results,
            output_md=self.root / "report.md",
            output_json=self.root / "report.json",
            **extra,
        )
        printed = io.StringIO()
        with redirect_stdout(printed):
            reporting.run_report(args)
        return printed.getvalue()

    @unittest.expectedFailure
    def test_outputs_follow_the_report(self) -> None:
        """Links in report order are appended; a refused one is a warning."""
        self.write("a", result(pull("repo", 8), key="a-1"))
        self.write("b", result("https://evil.example/pull/1", key="b-2"))
        self.write("c", result(pull("other", 3), key="c-3"))
        self.write("d", result(pull("dry", 4), key="d-4", dry_run=True))
        printed = self.run_report(org=ORG, github_output=self.github_output)
        lines = self.github_output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            [
                "earlier=1",
                f'pull_requests=["{pull("repo", 8)}","{pull("other", 3)}"]',
                "pull_request_count=2",
            ],
        )
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["pull_requests"], [pull("repo", 8), pull("other", 3)])
        self.assertEqual(report["excluded_pull_request_urls"], 1)
        self.assertIn("::warning::", printed)
        self.assertNotIn("evil", printed)
        markdown = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn("left 1 pull request link out", markdown)

    def test_without_org_nothing_is_written(self) -> None:
        """The report alone, as before, when no outputs are asked for."""
        self.write("a", result(pull("repo", 8)))
        self.run_report()
        self.assertEqual(self.github_output.read_text(encoding="utf-8"), "earlier=1\n")
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertNotIn("pull_requests", report)


if __name__ == "__main__":
    unittest.main()
