# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The online zizmor audit of a proposal, against a stub zizmor on PATH.

Each test builds a repository with a base commit and a head commit,
puts ``zizmor_stub`` first on PATH with fixed answers for the base and
head trees, and runs ``online_audit.audit`` over the two commits.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
clean_git_env = import_module("git_env").clean_git_env
online = import_module("online_audit")
policy = import_module("proposal_policy")
stub = import_module("zizmor_stub")

TOKEN = "ghs_stand-in-token"
WORKFLOW = ".github/workflows/ci.yaml"
BASE_WORKFLOW = "on: push\njobs: {}\n"
HEAD_WORKFLOW = "on: [push, pull_request]\njobs: {}\n"
STALE = "stale-action-refs"


def run_git(cwd: Path, *args: str) -> str:
    """Run git for fixture construction, failing loudly."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


class AuditCase(unittest.TestCase):
    """A repository with a base commit, isolated from the developer's git."""

    def setUp(self) -> None:
        """Create the repository and its base commit."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        config = self.root / "gitconfig"
        config.write_text(
            "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
            encoding="utf-8",
        )
        env = {
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        environment = patch.dict(os.environ, {**clean_git_env(), **env}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        run_git(self.clone, "init", "-q")
        self.base = self.commit({WORKFLOW: BASE_WORKFLOW, "README.md": "# Repo\n"})

    def commit(self, files: dict[str, str]) -> str:
        """Write and commit files; return the new commit's SHA."""
        for path, content in files.items():
            target = self.clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        run_git(self.clone, "add", "-A")
        run_git(self.clone, "commit", "-q", "-m", "Chore: Change")
        return run_git(self.clone, "rev-parse", "HEAD").strip()

    def fresh_workdir(self) -> None:
        """A new work directory, so a subtest exports into empty paths."""
        self.workdir = Path(tempfile.mkdtemp(dir=self.root))

    def audit(self, zizmor: Any, head: str, token: str = TOKEN) -> str:
        """Run the audit with the stub, if any, first on PATH."""
        path = zizmor.path() if zizmor is not None else os.environ["PATH"]
        with patch.dict(os.environ, {"PATH": path}):
            return online.audit(self.clone, self.base, head, self.workdir, token)


class AuditVerdictTest(AuditCase):
    """The comparison: only findings the head adds block a proposal."""

    def test_added_stale_action_ref_rejects(self) -> None:
        """A finding the base lacks rejects, naming the rule and the file."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root, head=[stub.finding(STALE, WORKFLOW)])
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head)
        text = str(caught.exception)
        self.assertIn(
            f"online audit: new zizmor finding {STALE} in {WORKFLOW}: "
            f"{STALE} description ({STALE} at line 1)",
            text,
        )
        self.assertTrue(text.endswith(online.FOOTER))
        self.assertEqual(len(zizmor.calls()), 2)

    def test_same_findings_on_other_lines_pass(self) -> None:
        """Pre-existing findings that moved are not new."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(
            self.root,
            base=[stub.finding(STALE, WORKFLOW, line=3)],
            head=[stub.finding(STALE, WORKFLOW, line=9)],
        )
        note = self.audit(zizmor, head)
        self.assertEqual(note, "online audit: no new zizmor findings")

    def test_fixed_finding_passes(self) -> None:
        """A head with fewer findings than its base passes."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(
            self.root, base=[stub.finding(STALE, WORKFLOW)], head=[]
        )
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))

    def test_a_second_finding_of_a_rule_in_a_file_is_new(self) -> None:
        """Counts compare per (rule, path), not presence."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(
            self.root,
            base=[stub.finding(STALE, WORKFLOW, line=3)],
            head=[
                stub.finding(STALE, WORKFLOW, line=3),
                stub.finding(STALE, WORKFLOW, line=9),
            ],
        )
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head)
        self.assertEqual(str(caught.exception).count("new zizmor finding"), 1)

    def test_ignored_findings_do_not_count(self) -> None:
        """A finding zizmor reports as ignored never blocks."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(
            self.root, head=[stub.finding(STALE, WORKFLOW, ignored=True)]
        )
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))

    def test_action_definition_change_is_audited(self) -> None:
        """A changed action.yml runs the audit too."""
        head = self.commit({"actions/x/action.yml": "runs: {using: composite}\n"})
        zizmor = stub.StubZizmor(
            self.root, head=[stub.finding("unpinned-uses", "actions/x/action.yml")]
        )
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head)
        self.assertIn("unpinned-uses in actions/x/action.yml", str(caught.exception))

    def test_every_proposal_is_audited(self) -> None:
        """A change to no workflow or action still runs zizmor on both trees."""
        head = self.commit({"README.md": "# Repo\n\nMore.\n"})
        zizmor = stub.StubZizmor(self.root, head=[stub.finding(STALE, WORKFLOW)])
        with self.assertRaises(policy.Rejection):
            self.audit(zizmor, head)
        self.assertEqual(len(zizmor.calls()), 2)

    def test_changed_symlink_target_is_audited(self) -> None:
        """Only the target of an unchanged action.yml symlink changes."""
        target = "actions/setup/definition.yml"
        link = "actions/setup/action.yml"
        (self.clone / "actions/setup").mkdir(parents=True)
        os.symlink("definition.yml", self.clone / link)
        self.base = self.commit({target: "runs: {using: composite}\n"})
        head = self.commit({target: "runs: {using: node20, main: x.js}\n"})
        zizmor = stub.StubZizmor(self.root, head=[stub.finding(STALE, link)])
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head)
        self.assertIn(f"{STALE} in {link}", str(caught.exception))
        exported = self.workdir / "audit-head" / link
        self.assertTrue(exported.is_symlink())
        self.assertIn("node20", exported.read_text(encoding="utf-8"))

    def test_trees_are_exported_side_by_side(self) -> None:
        """zizmor reads the base and head trees, each in its own directory."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root)
        self.audit(zizmor, head)
        base_dir = (self.workdir / "audit-base").resolve()
        head_dir = (self.workdir / "audit-head").resolve()
        self.assertEqual(
            [Path(call["cwd"]).resolve() for call in zizmor.calls()],
            [base_dir, head_dir],
        )
        self.assertEqual((base_dir / WORKFLOW).read_text(), BASE_WORKFLOW)
        self.assertEqual((head_dir / WORKFLOW).read_text(), HEAD_WORKFLOW)


class AuditCollectionTest(AuditCase):
    """The proposal cannot hide an input from the audit or switch it off."""

    def test_gitignored_tracked_workflow_is_audited(self) -> None:
        """A tracked workflow under a new ignore pattern is still collected."""
        hidden = ".github/workflows/hidden.yaml"
        (self.clone / ".gitignore").write_text(f"{hidden}\n", encoding="utf-8")
        (self.clone / hidden).write_text(HEAD_WORKFLOW, encoding="utf-8")
        run_git(self.clone, "add", "-f", ".gitignore", hidden)
        run_git(self.clone, "commit", "-q", "-m", "Chore: Hide")
        head = run_git(self.clone, "rev-parse", "HEAD").strip()
        zizmor = stub.StubZizmor(self.root, head=[stub.finding(STALE, hidden)])
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head)
        self.assertIn(f"{STALE} in {hidden}", str(caught.exception))
        self.assertTrue((self.workdir / "audit-head" / hidden).is_file())
        for call in zizmor.calls():
            self.assertIn("--collect=all", call["argv"])

    def test_malformed_changed_workflow_rejects(self) -> None:
        """Strict collection: an input zizmor cannot parse fails the audit."""
        head = self.commit(
            {WORKFLOW: HEAD_WORKFLOW, ".github/workflows/bad.yaml": "on: [\n"}
        )
        zizmor = stub.StubZizmor(self.root, exits={"head": 1})
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head)
        self.assertIn("exit 1", str(caught.exception))
        for call in zizmor.calls():
            self.assertIn("--strict-collection", call["argv"])

    def test_head_is_audited_with_the_base_configuration(self) -> None:
        """Both trees use the base's zizmor.yml; the head's own is never read."""
        self.base = self.commit({".github/zizmor.yml": "rules: {}\n"})
        head = self.commit(
            {
                WORKFLOW: HEAD_WORKFLOW,
                ".github/zizmor.yml": "rules:\n  unpinned-uses:\n    disable: true\n",
            }
        )
        zizmor = stub.StubZizmor(self.root)
        self.audit(zizmor, head)
        config = str((self.workdir / "audit-base" / ".github/zizmor.yml").resolve())
        calls = zizmor.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--config") + 1], config)
            self.assertNotIn("--no-config", argv)

    def test_head_configuration_without_base_one_is_not_loaded(self) -> None:
        """A configuration only the head adds leaves both trees on none."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW, "zizmor.yaml": "rules: {}\n"})
        zizmor = stub.StubZizmor(self.root)
        self.audit(zizmor, head)
        for call in zizmor.calls():
            self.assertIn("--no-config", call["argv"])
            self.assertNotIn("--config", call["argv"])

    def test_first_workflow_compares_against_an_empty_base(self) -> None:
        """A base with no input: zizmor's no-inputs exit counts as none."""
        run_git(self.clone, "rm", "-q", WORKFLOW)
        run_git(self.clone, "commit", "-q", "-m", "Chore: Drop")
        self.base = run_git(self.clone, "rev-parse", "HEAD").strip()
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root, exits={"base": 3})
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))
        self.fresh_workdir()
        zizmor = stub.StubZizmor(
            self.root, exits={"base": 3}, head=[stub.finding(STALE, WORKFLOW)]
        )
        with self.assertRaises(policy.Rejection):
            self.audit(zizmor, head)

    def test_base_with_only_a_nested_workflow_is_empty(self) -> None:
        """zizmor skips .github/workflows/nested/, so the base has no input."""
        nested = ".github/workflows/nested/example.yml"
        run_git(self.clone, "rm", "-q", WORKFLOW)
        self.base = self.commit({nested: BASE_WORKFLOW})
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root, exits={"base": 3})
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))

    def test_empty_base_and_empty_head_pass(self) -> None:
        """zizmor collects nothing from either tree: no findings, no rejection."""
        run_git(self.clone, "rm", "-q", WORKFLOW)
        run_git(self.clone, "commit", "-q", "-m", "Chore: Drop")
        self.base = run_git(self.clone, "rev-parse", "HEAD").strip()
        head = self.commit({"README.md": "# Repo\n\nMore.\n"})
        zizmor = stub.StubZizmor(self.root, exit_code=3)
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))
        self.assertEqual(len(zizmor.calls()), 2)

    def test_last_workflow_removed_leaves_an_empty_head(self) -> None:
        """Deleting the last input passes when it adds no finding."""
        run_git(self.clone, "rm", "-q", WORKFLOW)
        run_git(self.clone, "commit", "-q", "-m", "Chore: Drop")
        head = run_git(self.clone, "rev-parse", "HEAD").strip()
        zizmor = stub.StubZizmor(
            self.root, base=[stub.finding(STALE, WORKFLOW)], exits={"head": 3}
        )
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))


class AuditOnlineTest(AuditCase):
    """zizmor runs online, or the proposal is rejected."""

    def test_zizmor_runs_online_with_the_token(self) -> None:
        """GH_TOKEN is the token; nothing offline reaches zizmor."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root)
        offline = {
            "ZIZMOR_OFFLINE": "true",
            "ZIZMOR_NO_ONLINE_AUDITS": "true",
            "ZIZMOR_CONFIG": "/elsewhere.yml",
            "GITHUB_TOKEN": "other",
        }
        with patch.dict(os.environ, offline):
            self.audit(zizmor, head)
        calls = zizmor.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            with self.subTest(cwd=call["cwd"]):
                self.assertEqual(call["env"]["GH_TOKEN"], TOKEN)
                for name in (*offline, "ZIZMOR_GITHUB_TOKEN"):
                    self.assertNotIn(name, call["env"])
                self.assertNotIn("--offline", call["argv"])
                self.assertNotIn("--no-online-audits", call["argv"])
                self.assertEqual(
                    call["argv"],
                    [
                        "--persona",
                        "auditor",
                        "--min-severity",
                        "informational",
                        "--no-progress",
                        "--format",
                        "json",
                        "--collect=all",
                        "--strict-collection",
                        "--no-config",
                        ".",
                    ],
                )

    def test_absent_token_rejects(self) -> None:
        """No token: zizmor would quietly run offline, so it never starts."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root)
        with self.assertRaises(policy.Rejection) as caught:
            self.audit(zizmor, head, token="")
        self.assertIn("no GitHub token", str(caught.exception))
        self.assertEqual(zizmor.calls(), [])

    def test_missing_zizmor_rejects(self) -> None:
        """An audit that cannot run is not a pass."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        with patch.object(online.shutil, "which", return_value=None):
            with self.assertRaises(policy.Rejection) as caught:
                self.audit(None, head)
        self.assertIn("zizmor is not installed", str(caught.exception))

    def test_failed_run_rejects(self) -> None:
        """An exit status outside 0 and 11 to 14 rejects."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        for code in (1, 2, 10, 15):
            with self.subTest(exit=code):
                self.fresh_workdir()
                zizmor = stub.StubZizmor(self.root, exit_code=code)
                with self.assertRaises(policy.Rejection) as caught:
                    self.audit(zizmor, head)
                self.assertIn(f"exit {code}", str(caught.exception))

    def test_findings_exit_codes_are_audits(self) -> None:
        """11 to 14 report findings by severity; the JSON still decides."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root, exit_code=14)
        self.assertIn("no new zizmor findings", self.audit(zizmor, head))

    def test_unreadable_output_rejects(self) -> None:
        """Output that is not a JSON list rejects."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        for raw in ("not json", '{"findings": []}', ""):
            with self.subTest(raw=raw):
                self.fresh_workdir()
                zizmor = stub.StubZizmor(self.root, raw=raw)
                with self.assertRaises(policy.Rejection) as caught:
                    self.audit(zizmor, head)
                self.assertIn("unreadable output", str(caught.exception))

    def test_hung_zizmor_rejects_at_the_timeout(self) -> None:
        """A run past the bound is stopped and rejects."""
        head = self.commit({WORKFLOW: HEAD_WORKFLOW})
        zizmor = stub.StubZizmor(self.root, sleep=60)
        started = time.monotonic()
        with patch.object(online, "ZIZMOR_TIMEOUT", 1):
            with self.assertRaises(policy.Rejection) as caught:
                self.audit(zizmor, head)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("did not finish within 1 seconds", str(caught.exception))


class RejectionTextTest(unittest.TestCase):
    """The reason is bounded and safe to show on the issue."""

    def test_lines_are_bounded_and_defused(self) -> None:
        """At most MAX_FINDING_LINES findings, each clipped, mentions defused."""
        added = [
            ("rule", f"path{n}", "@someone " + "x" * 500)
            for n in range(online.MAX_FINDING_LINES + 3)
        ]
        lines = online.rejection_text(added).split(";\n")
        self.assertEqual(len(lines), online.MAX_FINDING_LINES + 2)
        self.assertEqual(lines[-2], "online audit: 3 more new finding(s) not shown")
        self.assertEqual(lines[-1], online.FOOTER)
        for line in lines[:-2]:
            self.assertLessEqual(len(line), online.MAX_LINE)
            self.assertNotIn(" @someone", line)
            self.assertIn("@\u200bsomeone", line)


if __name__ == "__main__":
    unittest.main()
