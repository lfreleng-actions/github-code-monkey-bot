# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Continuing a published pull request with its next round (issue #167)."""

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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
continuation = import_module("continuation")
reporting = import_module("proposal_report")
revisions = import_module("revision_reads")

ORG = "lfreleng-actions"
HEAD = "a" * 40
URL = f"https://github.com/{ORG}/sbom-action/pull/41"


def published(**changes: Any) -> dict[str, Any]:
    """A live published proposal whose Copilot review of the head has findings."""
    item: dict[str, Any] = {
        "schema": 1,
        "key": "sbom-action-40",
        "repository": f"{ORG}/sbom-action",
        "issue": 40,
        "verdict": "proposed",
        "reasons": [],
        "dry_run": False,
        "mode": "pull-requests",
        "commits": [HEAD],
        "pull_request_url": URL,
        "comment_url": f"https://github.com/{ORG}/sbom-action/issues/40#issuecomment-1",
        "mergeable": True,
        "round": 2,
        "segment_round": 2,
        "review": {
            "requested": True,
            "verdict": "findings",
            "assessment": "changes",
            "unresolved_threads": 2,
            "suppressed": 0,
            "previously_missed": 0,
            "reasons": [],
            "review_id": 1,
            "commit_id": HEAD,
        },
        "checks": {
            "state": "passing",
            "commit_id": HEAD,
            "failing": [],
            "dropped": 0,
            "unsettled": 0,
            "read_at": "2026-11-11T00:00:00Z",
            "reason": None,
        },
    }
    item.update(changes)
    return item


def clean(**changes: Any) -> dict[str, Any]:
    """A published proposal whose review is clean, with ``changes`` applied."""
    item = published(**changes)
    item["review"] = {
        **item["review"],
        "verdict": "clean",
        "assessment": "approval",
        "unresolved_threads": 0,
    }
    return item


def with_checks(item: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """``item`` with its checks record changed as given."""
    item["checks"] = {**item["checks"], **changes}
    return item


class ContinuationRuleTest(unittest.TestCase):
    """A live proposal with work outstanding continues; nothing else does."""

    def test_findings_on_the_published_head_continue(self) -> None:
        """Copilot's findings on the head it published: the next round answers them."""
        self.assertIsNone(continuation.unmet(published()))

    def test_findings_on_another_head_do_not_continue(self) -> None:
        """A review of an earlier push is not this round's verdict."""
        item = published()
        item["review"] = {**item["review"], "commit_id": "b" * 40}
        self.assertEqual(continuation.unmet(item), "nothing outstanding")

    def test_failing_defect_checks_continue(self) -> None:
        """A failing check on a clean head, settled or still running, continues."""
        lint = [{"name": "lint"}]
        for state in ("failing", "running"):
            with self.subTest(state=state):
                item = with_checks(clean(), state=state, failing=lint)
                self.assertIsNone(continuation.unmet(item))

    def test_infrastructure_failures_do_not_continue(self) -> None:
        """Failures at the egress agent alone need a re-run, not a round (#165)."""
        agent = [{"name": "lint", "kind": "infrastructure"}]
        item = with_checks(clean(), state="infrastructure", failing=agent)
        self.assertEqual(continuation.unmet(item), "nothing outstanding")

    def test_conflict_continues(self) -> None:
        """A head that conflicts with the default branch needs a rebase round."""
        self.assertIsNone(continuation.unmet(clean(mergeable=False)))

    def test_clean_head_with_nothing_else_does_not_continue(self) -> None:
        """Clean, mergeable or unsettled, and green: nothing to do."""
        self.assertEqual(continuation.unmet(clean()), "nothing outstanding")
        self.assertEqual(
            continuation.unmet(clean(mergeable=None)), "nothing outstanding"
        )

    def test_anything_but_a_published_proposal_does_not_continue(self) -> None:
        """A failed or paused round falls back to the schedule."""
        for verdict in (
            "abstain",
            "handoff",
            "rejected",
            "author-failed",
            "publish-failed",
        ):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    continuation.unmet(published(verdict=verdict)),
                    "not a published proposal",
                )

    def test_dry_run_or_no_link_does_not_continue(self) -> None:
        """Nothing was published, so there is no round to continue."""
        self.assertEqual(continuation.unmet(published(dry_run=True)), "a dry run")
        self.assertEqual(
            continuation.unmet(published(pull_request_url=None)),
            "no pull request was opened",
        )

    def test_no_outcome_comment_does_not_continue(self) -> None:
        """Without its footer the round goes uncounted, so no cap could advance."""
        for missing in (None, ""):
            with self.subTest(comment_url=missing):
                self.assertEqual(
                    continuation.unmet(published(comment_url=missing)),
                    "the outcome comment was not posted",
                )
        item = published()
        del item["comment_url"]
        self.assertEqual(continuation.unmet(item), "the outcome comment was not posted")

    def test_pending_review_does_not_continue(self) -> None:
        """Nothing to answer yet, even beside a conflict; the scan picks it up."""
        item = published(mergeable=False)
        item["review"] = {**item["review"], "verdict": "pending"}
        self.assertIn("pending", str(continuation.unmet(item)))
        malformed = published()
        malformed["review"] = {**malformed["review"], "extra": True}
        self.assertIn("pending", str(continuation.unmet(malformed)))

    def test_requested_antagonistic_review_does_not_continue(self) -> None:
        """The review bot's hand-back dispatches the next round; no race with it."""
        item = published(
            mergeable=False, antagonistic={"state": "requested", "reason": None}
        )
        self.assertIn("antagonistic", str(continuation.unmet(item)))
        for state in ("off", "skipped", "dry-run"):
            with self.subTest(state=state):
                other = published(antagonistic={"state": state, "reason": None})
                self.assertIsNone(continuation.unmet(other))

    def test_round_at_a_cap_does_not_continue(self) -> None:
        """The segment's cap, the ceiling and the guard all stop the loop."""
        ceiling = revisions.MAX_TOTAL_ROUNDS
        cases = {
            "segment cap": published(round=10, segment_round=10),
            "run's own cap": published(round=3, segment_round=3, max_rounds=3),
            "ceiling": published(round=ceiling, segment_round=1),
            "ceiling with superseded rounds": published(
                round=ceiling - 2, segment_round=1, stale_rounds=2
            ),
            "superseded guard": published(
                stale_rounds=revisions.MAX_STALE_ROUNDS, superseded=False
            ),
        }
        for name, item in cases.items():
            with self.subTest(case=name):
                self.assertEqual(continuation.unmet(item), continuation.AT_CAP, name)
        below = published(round=ceiling - 1, segment_round=1)
        self.assertIsNone(continuation.unmet(below))


class ContinuingListTest(unittest.TestCase):
    """The list is the report's validated links, as ``owner/repo#N``, once each."""

    def test_only_validated_links_whose_result_continues(self) -> None:
        """A link the report refused, or a result that stops, is never named."""
        stray = "https://github.com/other/x/pull/1"
        results = [
            published(),
            published(
                key="dup",
                pull_request_url="https://github.com/LFReleng-Actions/SBOM-action/pull/41",
            ),
            clean(key="done", pull_request_url=URL[:-2] + "42"),
            published(key="stray", pull_request_url=stray),
            published(key="next", pull_request_url=URL[:-2] + "43"),
        ]
        urls = [URL, URL[:-2] + "42", URL[:-2] + "43"]
        self.assertEqual(
            continuation.continuing(results, urls),
            [f"{ORG}/sbom-action#41", f"{ORG}/sbom-action#43"],
        )
        self.assertEqual(continuation.continuing(results, []), [])

    def test_output_line_is_one_compact_array(self) -> None:
        """Fixed key, one line, no spaces."""
        self.assertEqual(
            continuation.output_lines([f"{ORG}/a#1", f"{ORG}/b#2"]),
            f'continue_pull_requests=["{ORG}/a#1","{ORG}/b#2"]\n',
        )
        self.assertEqual(continuation.output_lines([]), "continue_pull_requests=[]\n")


class ReportContinuationTest(unittest.TestCase):
    """``publish.py report`` writes the continuing pull requests as an output."""

    def test_report_outputs_the_continuing_pull_requests(self) -> None:
        """A result with findings is named; a clean one and a stray link are not."""
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            results = {
                "a": published(key="a"),
                "b": clean(key="b", pull_request_url=URL[:-2] + "42"),
                "c": published(
                    key="c", pull_request_url="https://github.com/other/x/pull/1"
                ),
            }
            for name, item in results.items():
                directory = root / "results" / name
                directory.mkdir(parents=True)
                (directory / "result.json").write_text(
                    json.dumps(item), encoding="utf-8"
                )
            output = root / "github_output"
            output.touch()
            args = argparse.Namespace(
                results=root / "results",
                output_md=root / "report.md",
                output_json=root / "report.json",
                org=ORG,
                github_output=output,
            )
            with redirect_stdout(io.StringIO()):
                reporting.run_report(args)
            lines = output.read_text(encoding="utf-8").splitlines()
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            markdown = (root / "report.md").read_text(encoding="utf-8")
        expected = [f"{ORG}/sbom-action#41"]
        self.assertIn(
            f"continue_pull_requests={json.dumps(expected, separators=(',', ':'))}",
            lines,
        )
        self.assertEqual(report["continue_pull_requests"], expected)
        self.assertIn("continue with their next round", markdown)


if __name__ == "__main__":
    unittest.main()
