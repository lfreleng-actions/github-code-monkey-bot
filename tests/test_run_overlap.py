# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Targets other in-progress runs hold, read from recorded API responses."""

from __future__ import annotations

import hashlib
import io
import json
import signal
import sys
import tempfile
import time
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
SELECT = "Code Monkey / Select"


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
        self.page_size = 100
        self.stalled: set[int] = set()
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.notes: dict[int, list[dict[str, Any]]] = {}

    def job(
        self,
        run_id: int,
        *notes: dict[str, Any],
        name: str = SELECT,
        attempt: int = 1,
        conclusion: str | None = "success",
    ) -> int:
        """Record a job of the run and the annotations on its check run."""
        job_id = 5000 + len(self.notes)
        self.jobs.setdefault(run_id, []).append(
            {
                "id": job_id,
                "check_run_url": f"https://api.github.com/repos/{REPOSITORY}"
                f"/check-runs/{job_id}",
                "name": name,
                "run_attempt": attempt,
                "status": "in_progress" if conclusion is None else "completed",
                "conclusion": conclusion,
            }
        )
        self.notes[job_id] = list(notes)
        return job_id

    def publish(
        self,
        run_id: int,
        document: dict[str, Any],
        *,
        attempt: int = 1,
        digest: str | None = None,
        producer: int | None = None,
        vouch: bool = True,
    ) -> int:
        """Record an evidence artifact the run's select job uploaded and vouched for.

        ``vouch=False`` records one no select job vouches for, as an
        author job holding the runtime token could upload.
        """
        artifact_id = 9000 + len(self.zips)
        content = zipped(document)
        self.zips[artifact_id] = content
        actual = hashlib.sha256(content).hexdigest()
        recorded = digest or f"sha256:{actual}"
        name = f"monkey-evidence-{run_id}-{attempt}-{UUID}"
        self.artifacts.setdefault(run_id, []).append(
            {
                "id": artifact_id,
                "name": name,
                "size_in_bytes": len(content),
                "expired": False,
                "digest": recorded,
                "workflow_run": {"id": run_id if producer is None else producer},
            }
        )
        if vouch:
            claimed = recorded.removeprefix("sha256:")
            if len(claimed) != 64:
                claimed = actual
            self.job(
                run_id,
                note("Uploaded selection evidence", title="Selection"),
                vouch_note(name, artifact_id, claimed),
                attempt=attempt,
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
            size = self.page_size
            pages = [
                {"total_count": len(artifacts), "artifacts": artifacts[i : i + size]}
                for i in range(0, max(len(artifacts), 1), size)
            ]
            # Without --paginate gh returns the first page alone.
            return json.dumps(pages if "--paginate" in args else pages[0])
        if endpoint.startswith(prefix) and "/jobs?" in endpoint:
            if "--paginate" not in args or "filter=all" not in endpoint:
                raise AssertionError(f"jobs of every attempt and page: {args}")
            run_id = int(endpoint[len(prefix) :].partition("/")[0])
            jobs = self.jobs.get(run_id, [])
            return json.dumps([{"total_count": len(jobs), "jobs": jobs}])
        prefix = f"repos/{REPOSITORY}/check-runs/"
        if endpoint.startswith(prefix) and "/annotations?" in endpoint:
            if "--paginate" not in args:
                raise AssertionError(f"annotations of every page: {args}")
            notes = self.notes[int(endpoint[len(prefix) :].partition("/")[0])]
            size = self.page_size
            return json.dumps(
                [notes[i : i + size] for i in range(0, max(len(notes), 1), size)]
            )
        raise AssertionError(f"unrecorded endpoint {endpoint}")

    def download(
        self, _repository: str, artifact_id: int, target: Path, _limit: int
    ) -> None:
        """Write the recorded zip where the fetch would stream it."""
        self.downloaded.append(artifact_id)
        if artifact_id in self.stalled:
            time.sleep(30)
        target.write_bytes(self.zips[artifact_id])


def note(message: str, title: str, level: str = "notice") -> dict[str, Any]:
    """One annotation a workflow command left on a job's check run."""
    return {
        "path": ".github",
        "start_line": 1,
        "annotation_level": level,
        "title": title,
        "message": message,
    }


def vouch_note(name: str, artifact_id: int, digest: str) -> dict[str, Any]:
    """The notice the select job prints once it has uploaded its evidence."""
    return note(f"{name} {artifact_id} sha256:{digest}", "monkey-evidence-vouch")


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

    def held(self, **kwargs: Any) -> tuple[dict[tuple[str, int], int], str]:
        """Run the check; return the held targets and the warnings printed."""
        printed = io.StringIO()
        with redirect_stdout(printed):
            held = overlap.held_targets(REPOSITORY, THIS_RUN, **kwargs)
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
        self.assertIn("no select job of the run vouches for it", warnings)

    def test_artifact_of_another_producer_is_refused(self) -> None:
        """An artifact the listing ties to another run is not this run's evidence."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)), producer=999)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("names no producer run 200", warnings)

    def test_every_attempt_with_evidence_holds(self) -> None:
        """A failed-jobs rerun reselects one leg; the other leg's first selection holds.

        No name ties an attempt's evidence to its leg, so no attempt's
        selection is dropped while the run is active.
        """
        self.api.runs["in_progress"].append(run(200, attempt=2))
        self.api.publish(200, selection(issue("alpha", 3)), attempt=1)
        self.api.publish(200, selection(issue("beta", 4)), attempt=2)
        held, _ = self.held()
        self.assertEqual(held, {("org/alpha", 3): 200, ("org/beta", 4): 200})

    def test_evidence_past_the_first_page_holds(self) -> None:
        """Every page of the artifact listing is read, not the first alone."""
        self.api.page_size = 1
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.publish(200, selection(issue("beta", 4)))
        self.api.publish(200, selection(issue("gamma", 5)))
        held, warnings = self.held()
        self.assertEqual(
            held,
            {("org/alpha", 3): 200, ("org/beta", 4): 200, ("org/gamma", 5): 200},
        )
        self.assertEqual(warnings, "")

    def test_run_with_one_unreadable_leg_holds_nothing(self) -> None:
        """A run holds only once all its evidence is read; a later failure drops it."""
        self.api.runs["in_progress"] += [run(200), run(201)]
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.publish(200, selection(issue("beta", 4)), digest="sha256:" + "0" * 64)
        self.api.publish(201, selection(issue("gamma", 5)))
        held, warnings = self.held()
        self.assertEqual(held, {("org/gamma", 5): 201})
        self.assertIn("could not read the selection of run 200", warnings)

    def test_deadline_ends_the_check_and_keeps_runs_read_in_full(self) -> None:
        """A stalled read past the budget warns; runs read before it still hold."""
        self.api.runs["in_progress"] += [run(200), run(201)]
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.stalled.add(self.api.publish(201, selection(issue("beta", 4))))
        handler = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        held, warnings = self.held(budget=0.5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(held, {("org/alpha", 3): 200})
        self.assertIn("the check ran past 0.5 seconds", warnings)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertIs(signal.getsignal(signal.SIGALRM), handler)

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

    def test_forged_author_job_artifact_holds_nothing(self) -> None:
        """An evidence look-alike no select job vouches for never causes a skip.

        The author job holds the run's runtime token, so it can upload
        an artifact whose name, producer run and digest all check out.
        """
        self.api.runs["in_progress"].append(run(200))
        self.api.job(200, note("Selected 0 issues", title="Selection"))
        self.api.job(200, name="Code Monkey / Author (alpha-3)", conclusion=None)
        self.api.publish(200, selection(issue("alpha", 3)), vouch=False)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertEqual(self.api.downloaded, [])
        self.assertIn("no select job of the run vouches for it", warnings)

    def test_forged_artifact_beside_genuine_evidence_is_disregarded(self) -> None:
        """The select job's own evidence still holds; the look-alike adds nothing."""
        self.api.runs["in_progress"].append(run(200))
        genuine = self.api.publish(200, selection(issue("alpha", 3)))
        self.api.publish(200, selection(issue("beta", 4)), vouch=False)
        held, warnings = self.held()
        self.assertEqual(held, {("org/alpha", 3): 200})
        self.assertEqual(self.api.downloaded, [genuine])
        self.assertIn("disregarded monkey-evidence-200-1-", warnings)

    def test_vouch_outside_a_successful_select_job_is_ignored(self) -> None:
        """Only a select job that finished cleanly vouches; an author's notice does not."""
        for job in ("Code Monkey / Author (alpha-3)", "Code Monkey / Publish"):
            with self.subTest(job=job):
                self.api = Recorded()
                patch.object(github, "run_gh", side_effect=self.api.reply).start()
                self.api.runs["in_progress"].append(run(200))
                forged = self.api.publish(
                    200, selection(issue("alpha", 3)), vouch=False
                )
                (entry,) = self.api.artifacts[200]
                digest = str(entry["digest"]).removeprefix("sha256:")
                self.api.job(
                    200, vouch_note(str(entry["name"]), forged, digest), name=job
                )
                held, warnings = self.held()
                self.assertEqual(held, {})
                self.assertIn("no select job of the run vouches for it", warnings)
        for conclusion in ("failure", None):
            with self.subTest(conclusion=conclusion):
                self.api = Recorded()
                patch.object(github, "run_gh", side_effect=self.api.reply).start()
                self.api.runs["in_progress"].append(run(200))
                self.api.publish(200, selection(issue("alpha", 3)))
                self.api.jobs[200][0]["conclusion"] = conclusion
                held, _ = self.held()
                self.assertEqual(held, {})

    def test_select_job_with_two_vouches_vouches_for_nothing(self) -> None:
        """A check run carrying a second vouch, however it got there, is not trusted."""
        self.api.runs["in_progress"].append(run(200))
        genuine = self.api.publish(200, selection(issue("alpha", 3)))
        (entry,) = self.api.artifacts[200]
        second = vouch_note(
            str(entry["name"]), genuine, str(entry["digest"]).removeprefix("sha256:")
        )
        self.api.notes[self.api.jobs[200][0]["id"]].append(second)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("vouches for 2 evidence artifacts rather than one", warnings)

    def test_vouch_for_another_digest_or_attempt_is_disregarded(self) -> None:
        """The vouch must name the artifact's own digest and the job's own attempt."""
        self.api.runs["in_progress"].append(run(200))
        artifact = self.api.publish(200, selection(issue("alpha", 3)), vouch=False)
        (entry,) = self.api.artifacts[200]
        name = str(entry["name"])
        self.api.job(200, vouch_note(name, artifact, "f" * 64))
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("no select job of the run vouches for it", warnings)
        self.api.jobs[200] = []
        digest = str(entry["digest"]).removeprefix("sha256:")
        self.api.job(200, vouch_note(name, artifact, digest), attempt=2)
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("names another run or attempt", warnings)

    def test_vouch_needs_its_title_and_notice_level(self) -> None:
        """A vouch's text under another title or level vouches for nothing."""
        self.api.runs["in_progress"].append(run(200))
        artifact = self.api.publish(200, selection(issue("alpha", 3)), vouch=False)
        (entry,) = self.api.artifacts[200]
        digest = str(entry["digest"]).removeprefix("sha256:")
        vouch = vouch_note(str(entry["name"]), artifact, digest)
        self.api.job(
            200,
            {**vouch, "annotation_level": "warning"},
            {**vouch, "title": "Selection"},
        )
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("vouches for 0 evidence artifacts rather than one", warnings)

    def test_malformed_vouch_vouches_for_nothing(self) -> None:
        """A vouch notice whose message does not parse is no vouch."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)), vouch=False)
        self.api.job(200, note("$NAME $ID sha256:$DIGEST", "monkey-evidence-vouch"))
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("vouches in a malformed notice", warnings)

    def test_vouch_past_the_first_annotation_page_counts(self) -> None:
        """Every page of a busy select job's annotations is read."""
        self.api.page_size = 2
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)))
        notes = self.api.notes[self.api.jobs[200][0]["id"]]
        notes[:0] = [note(f"warning {i}", "Selection", "warning") for i in range(3)]
        held, warnings = self.held()
        self.assertEqual(held, {("org/alpha", 3): 200})
        self.assertEqual(warnings, "")

    def test_select_job_without_a_check_run_holds_nothing(self) -> None:
        """A job listing that names no check run leaves nothing to vouch."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)))
        del self.api.jobs[200][0]["check_run_url"]
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("names no check run", warnings)

    def test_unreadable_select_job_annotations_hold_nothing(self) -> None:
        """Evidence whose provenance cannot be read is not held; the check warns."""
        self.api.runs["in_progress"] += [run(200), run(201)]
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.publish(201, selection(issue("beta", 4)))
        self.api.failing.add(f"check-runs/{self.api.jobs[200][0]['id']}/annotations")
        held, warnings = self.held()
        self.assertEqual(held, {("org/beta", 4): 201})
        self.assertIn("could not read the selection of run 200", warnings)
        self.assertIn("could not read the annotations of select job", warnings)
        self.assertNotIn("Server Error", warnings)

    def test_vouched_evidence_missing_from_the_listing_holds_nothing(self) -> None:
        """A vouch the listing cannot back is unverifiable; the run holds nothing."""
        self.api.runs["in_progress"].append(run(200))
        self.api.publish(200, selection(issue("alpha", 3)))
        self.api.job(200, vouch_note(f"monkey-evidence-200-1-{UUID}", 1, "a" * 64))
        self.api.jobs[200][0]["conclusion"] = "cancelled"
        held, warnings = self.held()
        self.assertEqual(held, {})
        self.assertIn("vouched evidence 1 is not in the listing", warnings)

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

    def test_named_pulls_keep_order_and_record_the_run(self) -> None:
        """A held named pull request drops out, unread, under its own reference."""
        kept = self.holds.pulls("org", [("beta", 40), ("beta", 41), ("alpha", 4)])
        self.assertEqual(kept, [("beta", 40), ("alpha", 4)])
        self.assertEqual(self.skipped["held"], 1)
        self.assertEqual(
            self.skipped_issues,
            [{"issue": "org/beta#41", "reason": "being worked by run 300"}],
        )

    def test_round_is_recorded_under_its_pull_request(self) -> None:
        """A round held by its issue or its pull request names the pull request."""
        holds = overlap.Holds(
            {("org/alpha", 7): 200, ("org/alpha", 41): 200},
            self.skipped,
            self.skipped_issues,
        )
        self.assertTrue(holds.holds(revision("alpha", 7, 41)))
        self.assertTrue(holds.holds(revision("alpha", 7, 99)))
        self.assertEqual(
            [skip["issue"] for skip in self.skipped_issues],
            ["org/alpha#41", "org/alpha#99"],
        )

    def test_claims_record_the_reference_given(self) -> None:
        """``claims`` checks every number and records the caller's reference."""
        self.assertFalse(self.holds.claims("org/beta", (8, 42), "org/beta#42"))
        self.assertTrue(self.holds.claims("Org/Beta", (40, 41), "Org/Beta#40"))
        self.assertEqual(self.skipped["held"], 1)
        self.assertEqual(
            self.skipped_issues,
            [{"issue": "Org/Beta#40", "reason": "being worked by run 300"}],
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
