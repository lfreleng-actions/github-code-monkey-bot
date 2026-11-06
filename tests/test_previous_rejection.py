# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The publisher's last rejection, carried to the next attempt's packet."""

from __future__ import annotations

import json
import os
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
artifact_fetch = import_module("artifact_fetch")
previous = import_module("previous_rejection")

BOT = "monkey[bot]"
RESULTS = "org/caller"
RUN_URL = f"https://github.com/{RESULTS}/actions/runs/42"
NAMESPACE = "42-1-" + "a" * 32
ENTRY: dict[str, Any] = {"key": "alpha-3", "repository": "org/alpha", "number": 3}


def comment(
    verdict: str,
    created: str,
    *,
    login: str = BOT,
    run_url: str = RUN_URL,
    ident: int = 1,
) -> dict[str, Any]:
    """An issue comment carrying the bot's outcome footer."""
    return {
        "id": ident,
        "user": {"login": login},
        "created_at": created,
        "body": f"Summary\n\n<!-- code-monkey {run_url} {verdict} -->",
    }


def artifact(name: str, *, expired: bool = False, ident: int = 7) -> dict[str, Any]:
    """An artifact listing entry."""
    return {"id": ident, "name": name, "size_in_bytes": 512, "expired": expired}


class PreviousRejectionCase(unittest.TestCase):
    """Patch every read; no test reaches a real ``gh`` subprocess."""

    def setUp(self) -> None:
        """Serve fixture comments, artifacts and a publish result."""
        guard = patch.object(
            github.subprocess,
            "run",
            side_effect=AssertionError("unexpected subprocess"),
        )
        guard.start()
        self.comments: list[dict[str, Any]] = [
            comment("rejected", "2026-01-02T00:00:00Z")
        ]
        self.artifacts: list[dict[str, Any]] = [
            artifact(f"monkey-result-{NAMESPACE}-alpha-3-1"),
            artifact(f"monkey-result-{NAMESPACE}-beta-9-1", ident=8),
        ]
        self.result: Any = {"verdict": "rejected", "reasons": ["subject too long"]}
        self.raw: str | None = None
        self.fetched: list[tuple[str, int]] = []
        self.tokens: list[str | None] = []
        patch.object(reads, "api_page", side_effect=self.page).start()
        patch.object(github, "api_object", side_effect=self.listing).start()
        patch.object(artifact_fetch, "fetch_artifact", side_effect=self.fetch).start()
        self.addCleanup(patch.stopall)

    def page(self, endpoint: str) -> list[dict[str, Any]]:
        """Serve the issue comments as one page."""
        self.assertIn("repos/org/alpha/issues/3/comments", endpoint)
        return self.comments if endpoint.endswith("page=1") else []

    def listing(self, endpoint: str) -> dict[str, Any]:
        """Serve the rejecting run's artifact listing."""
        self.assertTrue(
            endpoint.startswith(f"repos/{RESULTS}/actions/runs/42/artifacts")
        )
        self.tokens.append(os.environ.get("GH_TOKEN"))
        return {"artifacts": self.artifacts}

    def fetch(
        self, repository: str, artifact_id: int, _size: int, output: Path, _profile: Any
    ) -> list[str]:
        """Write the fixture result.json where the fetch would."""
        self.fetched.append((repository, artifact_id))
        text = self.raw if self.raw is not None else json.dumps(self.result)
        (output / "result.json").write_text(text)
        return ["result.json"]

    def attach(self) -> dict[str, Any]:
        """Run the attach step over one fixture entry and return it."""
        entry = dict(ENTRY)
        previous.attach([entry], results_repository=RESULTS, bot_login=BOT)
        return entry


class AttachTest(PreviousRejectionCase):
    """Reasons reach the packet when the last outcome was a rejection."""

    def test_rejection_reasons_reach_the_packet(self) -> None:
        """The latest rejection's reasons, under the fixed heading."""
        record = self.attach()["previous_rejection"]
        self.assertEqual(record["heading"], previous.HEADING)
        self.assertEqual(record["run_url"], RUN_URL)
        self.assertEqual(record["reasons"], ["subject too long"])
        self.assertEqual(record["reasons_dropped"], 0)
        self.assertIsNone(record["note"])
        self.assertEqual(self.fetched, [(RESULTS, 7)])

    def test_reasons_are_bounded(self) -> None:
        """Count, each reason and the total are bounded; the rest are counted."""
        self.result["reasons"] = ["x" * 5000] * 3 + [17] + ["short"] * 20
        record = self.attach()["previous_rejection"]
        sizes = [len(json.dumps(r)) for r in record["reasons"]]
        self.assertLessEqual(len(record["reasons"]), previous.MAX_REASONS)
        self.assertTrue(all(size <= previous.MAX_REASON_BYTES for size in sizes))
        self.assertLessEqual(sum(sizes), previous.MAX_TOTAL_BYTES)
        self.assertEqual(record["reasons_dropped"], 24 - len(record["reasons"]))

    def test_escaping_counts_against_the_bounds(self) -> None:
        """Control characters are bounded as serialised, not as UTF-8."""
        self.result["reasons"] = ["\x01" * 2000, "\U0001f600" * 1000] * 4
        record = self.attach()["previous_rejection"]
        self.assertTrue(record["reasons"])
        sizes = [len(json.dumps(r)) for r in record["reasons"]]
        self.assertTrue(all(size <= previous.MAX_REASON_BYTES for size in sizes))
        self.assertLessEqual(sum(sizes), previous.MAX_TOTAL_BYTES)
        self.assertLessEqual(
            len(json.dumps(record, indent=2)), previous.MAX_TOTAL_BYTES + 1024
        )

    def test_lone_surrogate_does_not_stop_selection(self) -> None:
        """A reason JSON can carry but UTF-8 cannot is kept, with a replacement."""
        self.raw = '{"verdict": "rejected", "reasons": ["bad \\ud800 text", "ok"]}'
        record = self.attach()["previous_rejection"]
        self.assertEqual(record["reasons"], ["bad ? text", "ok"])
        self.assertIsNone(record["note"])

    def test_records_count_against_the_selection_budget(self) -> None:
        """A record that would overrun the budget keeps a note, not its reasons."""
        self.result["reasons"] = ["x" * 1500] * 4
        with patch.object(previous, "MAX_SELECTION_BYTES", 1024):
            record = self.attach()["previous_rejection"]
        self.assertEqual(record["reasons"], [])
        self.assertEqual(record["reasons_dropped"], 4)
        self.assertEqual(record["note"], previous.NOTE_BUDGET)

    def test_budget_spans_entries(self) -> None:
        """The budget counts every chosen entry, not each alone."""
        self.result["reasons"] = ["x" * 1500]
        first, second = dict(ENTRY), dict(ENTRY)
        limit = len(json.dumps(first, indent=2)) * 2 + 1800
        with patch.object(previous, "MAX_SELECTION_BYTES", limit):
            previous.attach([first, second], results_repository=RESULTS, bot_login=BOT)
        self.assertEqual(first["previous_rejection"]["reasons"], ["x" * 1500])
        self.assertEqual(second["previous_rejection"]["note"], previous.NOTE_BUDGET)

    def test_results_token_reads_and_is_restored(self) -> None:
        """The artifact read uses RESULTS_TOKEN; GH_TOKEN comes back after."""
        with patch.dict(os.environ, {"GH_TOKEN": "app", "RESULTS_TOKEN": "run"}):
            self.attach()
            self.assertEqual(os.environ["GH_TOKEN"], "app")
        self.assertEqual(self.tokens, ["run"])

    def test_latest_attempt_wins(self) -> None:
        """A rerun's result replaces the first attempt's."""
        self.artifacts.append(artifact(f"monkey-result-{NAMESPACE}-alpha-3-2", ident=9))
        self.attach()
        self.assertEqual(self.fetched, [(RESULTS, 9)])


class NoRejectionTest(PreviousRejectionCase):
    """No rejection, or none the bot posted, leaves the packet unchanged."""

    def test_later_outcome_supersedes_rejection(self) -> None:
        """A proposal after the rejection means no field."""
        self.comments.append(comment("proposed", "2026-01-03T00:00:00Z", ident=2))
        self.assertNotIn("previous_rejection", self.attach())
        self.assertEqual(self.fetched, [])

    def test_no_footer(self) -> None:
        """An issue the bot never commented on gets no field."""
        self.comments.clear()
        self.assertNotIn("previous_rejection", self.attach())

    def test_other_authors_do_not_count(self) -> None:
        """A footer someone else posted is text."""
        self.comments[:] = [
            comment("rejected", "2026-01-02T00:00:00Z", login="someone")
        ]
        self.assertNotIn("previous_rejection", self.attach())

    def test_comment_read_failure_adds_nothing(self) -> None:
        """A failed history read fails soft with no field."""
        with patch.object(reads, "api_page", side_effect=github.GitHubError("boom")):
            self.assertNotIn("previous_rejection", self.attach())

    def test_overlong_history_adds_nothing(self) -> None:
        """A history past the page cap is not trusted for the latest outcome."""
        full = [comment("rejected", "2026-01-02T00:00:00Z")] * reads.COMMENTS_PER_PAGE
        with patch.object(reads, "api_page", return_value=full):
            self.assertNotIn("previous_rejection", self.attach())


class FailSoftTest(PreviousRejectionCase):
    """An unreadable result becomes a fixed note; the selection goes on."""

    def note(self) -> str | None:
        """The note the attach step records."""
        record = self.attach()["previous_rejection"]
        self.assertEqual(record["reasons"], [])
        return record["note"]

    def test_expired_result(self) -> None:
        """An expired artifact is named as such and not downloaded."""
        self.artifacts[0]["expired"] = True
        self.assertEqual(self.note(), previous.NOTE_EXPIRED)
        self.assertEqual(self.fetched, [])

    def test_missing_result(self) -> None:
        """No artifact for this key."""
        self.artifacts.pop(0)
        self.assertEqual(self.note(), previous.NOTE_MISSING)

    def test_ambiguous_result(self) -> None:
        """Two results of one attempt are not guessed between."""
        self.artifacts.append(
            artifact(f"monkey-result-42-2-{'b' * 32}-alpha-3-1", ident=9)
        )
        self.assertEqual(self.note(), previous.NOTE_AMBIGUOUS)

    def test_foreign_run_is_not_read(self) -> None:
        """A run URL outside the results repository is never read."""
        url = "https://github.com/other/repo/actions/runs/42"
        self.comments[:] = [comment("rejected", "2026-01-02T00:00:00Z", run_url=url)]
        self.assertEqual(self.note(), previous.NOTE_FOREIGN)
        self.assertEqual(self.fetched, [])

    def test_refused_download(self) -> None:
        """A download the fetch refuses is unreadable."""
        refused = artifact_fetch.Refused("too big")
        with patch.object(artifact_fetch, "fetch_artifact", side_effect=refused):
            self.assertEqual(self.note(), previous.NOTE_UNREADABLE)

    def test_listing_failure(self) -> None:
        """A failed artifact listing is unreadable."""
        with patch.object(github, "api_object", side_effect=github.GitHubError("x")):
            self.assertEqual(self.note(), previous.NOTE_UNREADABLE)

    def test_unparseable_result(self) -> None:
        """A result.json that is not JSON is unreadable."""
        self.result = None
        with patch.object(previous.json, "loads", side_effect=ValueError("bad")):
            self.assertEqual(self.note(), previous.NOTE_UNREADABLE)

    def test_deeply_nested_result(self) -> None:
        """Nesting that exhausts the parser's recursion is unreadable."""
        self.raw = "[" * 200_000 + "]" * 200_000
        self.assertEqual(self.note(), previous.NOTE_UNREADABLE)

    def test_result_without_rejection(self) -> None:
        """A result whose verdict is not a rejection carries no reasons."""
        self.result = {"verdict": "proposed", "reasons": ["x"]}
        self.assertEqual(self.note(), previous.NOTE_MISMATCH)


if __name__ == "__main__":
    unittest.main()
