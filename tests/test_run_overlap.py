# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Targets other in-progress runs hold, read from recorded API responses."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

overlap = import_module("run_overlap")
github = import_module("bot_github")
fetcher = import_module("artifact_fetch")

REPOSITORY = "lfreleng-actions/caller"
THIS_RUN = 100
WORKFLOW = 7
UUID = "0123456789abcdef0123456789abcdef"


def selection(*entries: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """A selection.json as another run's select job published it."""
    return {
        "schema": 1,
        "mode": "pull-requests",
        "dry_run": dry_run,
        "issues": list(entries),
    }


def issue(repo: str, number: int) -> dict[str, Any]:
    """An issue entry, trimmed to the fields the overlap check reads."""
    return {"key": f"{repo}-{number}", "repository": f"org/{repo}", "number": number}


def revision(repo: str, issue_number: int, pull: int) -> dict[str, Any]:
    """A revision entry: the issue and the pull request it revises."""
    return {
        **issue(repo, issue_number),
        "task": "revise",
        "pull_request": {"number": pull},
    }


def zipped(document: dict[str, Any]) -> bytes:
    """The evidence zip upload-artifact would have stored."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("selection.json", json.dumps(document))
        bundle.writestr("agents.md", "# Guidance\n")
        bundle.writestr("matrix.json", "{}")
    return buffer.getvalue()


class Recorded:
    """Recorded API replies by endpoint, and the evidence zips by artifact id."""

    def __init__(self) -> None:
        """Start with this run alone in progress and nothing queued."""
        self.runs: dict[str, list[dict[str, Any]]] = {
            "in_progress": [run(THIS_RUN)],
            "queued": [],
        }
        self.artifacts: dict[int, list[dict[str, Any]]] = {}
        self.zips: dict[int, bytes] = {}
        self.failing: set[str] = set()
        self.requested: list[str] = []
        self.downloaded: list[int] = []

    def publish(
        self,
        run_id: int,
        document: dict[str, Any],
        *,
        attempt: int = 1,
        digest: str | None = None,
        producer: int | None = None,
    ) -> int:
        """Record an evidence artifact the run's select job uploaded."""
        artifact_id = 9000 + len(self.zips)
        content = zipped(document)
        self.zips[artifact_id] = content
        recorded = digest or f"sha256:{hashlib.sha256(content).hexdigest()}"
        self.artifacts.setdefault(run_id, []).append(
            {
                "id": artifact_id,
                "name": f"monkey-evidence-{run_id}-{attempt}-{UUID}",
                "size_in_bytes": len(content),
                "expired": False,
                "digest": recorded,
                "workflow_run": {"id": run_id if producer is None else producer},
            }
        )
        return artifact_id

    def reply(self, args: list[str], **_: Any) -> str:
        """Serve ``gh api <endpoint>`` from the recording."""
        endpoint = args[1]
        self.requested.append(endpoint)
        for failing in self.failing:
            if failing in endpoint:
                raise github.GitHubError(f"gh: Server Error (HTTP 502) {failing}", 502)
        if endpoint == f"repos/{REPOSITORY}/actions/runs/{THIS_RUN}":
            return json.dumps({"id": THIS_RUN, "workflow_id": WORKFLOW})
        prefix = f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?status="
        if endpoint.startswith(prefix):
            status = endpoint[len(prefix) :].partition("&")[0]
            runs = self.runs.get(status, [])
            return json.dumps({"total_count": len(runs), "workflow_runs": runs})
        prefix = f"repos/{REPOSITORY}/actions/runs/"
        if endpoint.startswith(prefix) and "/artifacts?" in endpoint:
            run_id = int(endpoint[len(prefix) :].partition("/")[0])
            artifacts = self.artifacts.get(run_id, [])
            return json.dumps({"total_count": len(artifacts), "artifacts": artifacts})
        raise AssertionError(f"unrecorded endpoint {endpoint}")

    def download(
        self, _repository: str, artifact_id: int, target: Path, _limit: int
    ) -> None:
        """Write the recorded zip where the fetch would stream it."""
        self.downloaded.append(artifact_id)
        target.write_bytes(self.zips[artifact_id])


def run(run_id: int, status: str = "in_progress", attempt: int = 1) -> dict[str, Any]:
    """One entry of the workflow's run listing."""
    return {"id": run_id, "status": status, "run_attempt": attempt}


class HeldTargetsTest(unittest.TestCase):
    """``held_targets`` reads other runs' evidence and fails open."""

    def setUp(self) -> None:
        """Serve every gh call and download from a fresh recording."""
        self.api = Recorded()
        patch.object(github, "run_gh", side_effect=self.api.reply).start()
        patch.object(fetcher, "download", side_effect=self.api.download).start()
        self.addCleanup(patch.stopall)

    def held(self) -> tuple[dict[tuple[str, int], int], str]:
        """Run the check; return the held targets and the warnings printed."""
        printed = io.StringIO()
        with redirect_stdout(printed):
            held = overlap.held_targets(REPOSITORY, THIS_RUN)
        return held, printed.getvalue()

    def test_target_in_another_in_progress_run_is_held(self) -> None:
        """Issues and both numbers of a round are held by the run that selected them."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3), revision("beta", 7, 41)))
        held, warnings = self.held()
        self.assertEqual(
            held, {("org/alpha", 3): 200, ("org/beta", 7): 200, ("org/beta", 41): 200}
        )
        self.assertEqual(warnings, "")

    def test_queued_run_with_evidence_is_held(self) -> None:
        """A run queued again after its select job published still holds its targets."""
        self.api.runs["queued"].append(run(300, "queued", attempt=2))
        self.api.publish(300, selection(issue("alpha", 3)), attempt=2)
        held, _ = self.held()
        self.assertEqual(held, {("org/alpha", 3): 300})

    def test_this_run_is_never_read(self) -> None:
        """The run's own selection holds nothing against itself."""
        self.api.publish(THIS_RUN, selection(issue("alpha", 3)))
        held, _ = self.held()
        self.assertEqual(held, {})
        self.assertFalse(any("/artifacts" in e for e in self.api.requested))

    def test_completed_run_is_ignored(self) -> None:
        """Only queued and in-progress runs are listed, and only those are read."""
        self.api.runs["completed"] = [run(400, "completed")]
        self.api.runs["in_progress"].append(run(401, "completed"))
        self.api.publish(400, selection(issue("alpha", 3)))
        self.api.publish(401, selection(issue("beta", 4)))
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertEqual(warnings, "")
        self.assertEqual(self.api.downloaded, [])
        for endpoint in self.api.requested:
            self.assertNotIn("status=completed", endpoint)

    def test_run_without_evidence_yet_is_ignored(self) -> None:
        """A run whose select job has not finished published nothing; no warning."""
        self.api.runs["in_progress"].append(run(200))
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertEqual(warnings, "")

    def test_listing_failure_is_a_warning(self) -> None:
        """A failed run listing holds nothing and warns."""
        self.api.failing.add("status=in_progress")
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("::warning title=Overlapping runs::", warnings)
        self.assertIn("could not list", warnings)

    def test_evidence_failure_is_a_warning_for_that_run_alone(self) -> None:
        """One unreadable run warns; the others still hold their targets."""
        self.api.runs["in_progress"] += [run(200), run(201)]
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.publish(201, selection(issue("beta", 4)))
        self.api.failing.add("runs/200/artifacts")
        held, warnings = self.held()
        self.assertEqual(held, {("org/beta", 4): 201})
        self.assertIn("could not read the selection of run 200", warnings)

    def test_digest_mismatch_is_refused(self) -> None:
        """Bytes that differ from the digest GitHub recorded hold nothing."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)), digest="sha256:" + "0" * 64)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("does not match its digest", warnings)

    def test_missing_digest_is_refused(self) -> None:
        """An artifact with no recorded digest is not read by name alone."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)), digest="none")
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertEqual(self.api.downloaded, [])
        self.assertIn("carries no sha256 digest", warnings)

    def test_artifact_of_another_producer_is_refused(self) -> None:
        """An artifact the listing ties to another run is not this run's evidence."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)), producer=999)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("names no producer run 200", warnings)

    def test_latest_attempt_with_evidence_wins(self) -> None:
        """A full rerun's new selection replaces the earlier attempt's."""
        self.api.runs["in_progress"].append(run(200, attempt=2))
        self.api.publish(200, selection(issue("alpha", 3)), attempt=1)
        self.api.publish(200, selection(issue("beta", 4)), attempt=2)
        held, _ = self.held()
        self.assertEqual(held, {("org/beta", 4): 200})

    def test_every_leg_of_a_run_holds(self) -> None:
        """A caller run with two legs published two selections; both hold."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.publish(200, selection(issue("beta", 4)))
        held, _ = self.held()
        self.assertEqual(held, {("org/alpha", 3): 200, ("org/beta", 4): 200})

    def test_dry_and_select_only_runs_hold_nothing(self) -> None:
        """A run that publishes nothing wastes no session of this one."""
        self.api.runs["in_progress"] += [run(200), run(201)]
        self.api.publish(200, selection(issue("alpha", 3), dry_run=True))
        select_only = selection(issue("beta", 4))
        select_only["mode"] = "select"
        self.api.publish(201, select_only)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertEqual(warnings, "")

    def test_malformed_selection_is_a_warning(self) -> None:
        """A selection that does not parse as expected holds nothing."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, {"dry_run": False, "issues": [{"repository": "x"}]})
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("could not read the selection of run 200", warnings)


class MainTest(unittest.TestCase):
    """The command always writes its output and exits 0 on a failed read."""

    def test_api_failure_writes_an_empty_file(self) -> None:
        """Fail open: a warning, an empty list, and a normal exit."""
        with tempfile.TemporaryDirectory() as holder:
            output = Path(holder) / "held-targets.json"
            printed = io.StringIO()
            with (
                patch.object(
                    github, "run_gh", side_effect=github.GitHubError("gh: boom", 500)
                ),
                redirect_stdout(printed),
            ):
                overlap.main(
                    [
                        "--repository",
                        REPOSITORY,
                        "--run-id",
                        str(THIS_RUN),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(json.loads(output.read_text()), {"held": []})
            self.assertEqual(overlap.load_held(output), {})
        self.assertIn("::warning title=Overlapping runs::", printed.getvalue())

    def test_written_targets_load_back(self) -> None:
        """What the step writes, the selector reads, repository case folded."""
        with tempfile.TemporaryDirectory() as holder:
            output = Path(holder) / "held-targets.json"
            overlap.write_held(output, {("org/alpha", 3): 200})
            self.assertEqual(overlap.load_held(output), {("org/alpha", 3): 200})
            output.write_text(
                json.dumps(
                    {"held": [{"repository": "Org/Beta", "number": 4, "run_id": 5}]}
                )
            )
            self.assertEqual(overlap.load_held(output), {("org/beta", 4): 5})
            output.write_text("[]")
            with self.assertRaises(ValueError):
                overlap.load_held(output)
        self.assertEqual(overlap.load_held(None), {})


class HoldsTest(unittest.TestCase):
    """``Holds`` skips held entries before the selector chooses."""

    def setUp(self) -> None:
        """Hold one issue and one pull request."""
        self.skipped = {"held": 0}
        self.skipped_issues: list[dict[str, str]] = []
        self.holds = overlap.Holds(
            {("org/alpha", 3): 200, ("org/beta", 41): 300},
            self.skipped,
            self.skipped_issues,
        )

    def test_issues_keep_order_and_record_the_run(self) -> None:
        """A held issue drops out with its reason; the rest keep their order."""
        ranked = [issue("gamma", 9), issue("alpha", 3), issue("delta", 1)]
        kept = self.holds.issues(ranked)
        self.assertEqual([c["number"] for c in kept], [9, 1])
        self.assertEqual(self.skipped["held"], 1)
        self.assertEqual(
            self.skipped_issues,
            [{"issue": "org/alpha#3", "reason": "being worked by run 200"}],
        )

    def test_round_held_by_its_pull_request_builds_to_nothing(self) -> None:
        """A wrapped builder returns None for a held round and the entry otherwise."""
        free = revision("beta", 8, 42)
        rounds = self.holds.rounds(
            [
                ("org/beta", lambda: revision("beta", 7, 41)),
                ("org/beta", lambda: free),
                ("org/beta", lambda: None),
            ]
        )
        self.assertEqual([repository for repository, _ in rounds], ["org/beta"] * 3)
        self.assertIsNone(rounds[0][1]())
        self.assertIs(rounds[1][1](), free)
        self.assertIsNone(rounds[2][1]())
        self.assertEqual(
            self.skipped_issues,
            [{"issue": "org/beta#41", "reason": "being worked by run 300"}],
        )


if __name__ == "__main__":
    unittest.main()
