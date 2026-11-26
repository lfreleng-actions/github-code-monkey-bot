# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline proposal verification against real git repositories and bundles.

Each test builds an upstream repository, an agent clone with a bot
branch, a bundle of the agent's commits and the trusted selection, then
runs ``proposal_check.run_check`` with ``prepare_clone`` patched to
fetch from the local upstream instead of GitHub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
clean_git_env = import_module("git_env").clean_git_env
checks = import_module("proposal_check")
model = import_module("proposal_model")
github = import_module("bot_github")
policy = import_module("proposal_policy")
revision_check = import_module("revision_check")
online_audit = import_module("online_audit")
stub = import_module("zizmor_stub")

BOT_LOGIN = "code-monkey[bot]"
BOT_EMAIL = "1+code-monkey[bot]@users.noreply.github.com"
SIGN_OFF = f"Signed-off-by: {BOT_LOGIN} <{BOT_EMAIL}>"
COAUTHOR = "Co-authored-by: Claude <noreply@anthropic.com>"
REPOSITORY = "owner/repo"
ISSUE = 7
BRANCH = f"code-monkey/issue-{ISSUE}"
KEY = f"repo-{ISSUE}"
ISSUE_URL = f"https://github.com/{REPOSITORY}/issues/{ISSUE}"
RUN_URL = "https://example/run"
COAUTHORS: dict[str, str] = {
    "claude-": "Claude <noreply@anthropic.com>",
    "gpt-": "ChatGPT <chatgpt@openai.com>",
}
GITLINT = "[title-max-length]\nline-length=60\n"
REAL_COAUTHORS = Path(__file__).resolve().parents[1] / "config" / "coauthors.json"
COPILOT_TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
# The usage file's shape after a Copilot Fusion session (issue #240).
FUSION_USAGE: dict[str, Any] = {
    "totalPremiumRequestCost": 3,
    "modelMetrics": {"gpt-6-luna": {"requests": 2}, "claude-opus-5.5": {}},
    "agentMetrics": {"main": {"modelMetrics": {"gemini-4-neon": {}}}},
    "currentModel": "gpt-6-luna",
}
README = "# Repo\n\nA readme with a typo.\n"


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


def local_prepare_clone(upstream: Path) -> Any:
    """A ``prepare_clone`` double that fetches from a local path, not GitHub."""

    def prepare(workdir: Path, repository: str, base_sha: str) -> Path:
        """Mirror the real steps with the remote pointed at ``upstream``."""
        if repository != REPOSITORY:
            raise AssertionError(f"unexpected repository {repository!r}")
        clone = workdir / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        checks.git(clone, "init", "-q")
        checks.git(clone, "remote", "add", "origin", str(upstream))
        checks.git(
            clone,
            "-c",
            "protocol.version=2",
            "fetch",
            "-q",
            "--depth=1",
            "origin",
            base_sha,
        )
        resolved = str(
            checks.git(clone, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        ).strip()
        if resolved != base_sha:
            raise policy.PublishError("fetched commit does not match the recorded base")
        return clone

    return prepare


class Fixture:
    """An upstream repository, an agent clone and the proposal directory."""

    def __init__(self, root: Path) -> None:
        """Create the upstream with one commit and clone it for the agent."""
        self.root = root
        self.upstream = root / "upstream"
        self.agent = root / "agent"
        self.proposal = root / "proposal"
        self.workdir = root / "work"
        self.proposal.mkdir()
        self.upstream.mkdir()
        run_git(self.upstream, "init", "-q", "-b", "main")
        (self.upstream / "README.md").write_text(README, encoding="utf-8")
        (self.upstream / ".gitlint").write_text(GITLINT, encoding="utf-8")
        (self.upstream / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (self.upstream / ".github").mkdir()
        (self.upstream / ".github" / "copilot-instructions.md").write_text(
            "# Instructions\n", encoding="utf-8"
        )
        (self.upstream / "old.txt").write_text("old\n", encoding="utf-8")
        run_git(self.upstream, "add", "-A")
        run_git(self.upstream, "commit", "-q", "-m", "Chore: Initial commit")
        self.base_sha = run_git(self.upstream, "rev-parse", "HEAD").strip()
        run_git(root, "clone", "-q", str(self.upstream), str(self.agent))
        self.branch = BRANCH
        run_git(self.agent, "checkout", "-q", "-b", self.branch)
        # The run's switch as the selection records it; None leaves it
        # out, as a selection from before the switch did.
        self.allow_workflow_edits: bool | None = None
        self.allow_precommit_config_edits: bool | None = None

    def commit(
        self,
        message: str,
        files: dict[str, str | None] | None = None,
        *,
        executable: tuple[str, ...] = (),
        symlinks: dict[str, str] | None = None,
        sign_off: bool = True,
    ) -> str:
        """Write, stage and commit files in the agent clone; return the SHA."""
        for path, content in (files or {}).items():
            target = self.agent / path
            if content is None:
                run_git(self.agent, "rm", "-q", path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            if path in executable:
                target.chmod(0o755)
            run_git(self.agent, "add", path)
        for path, destination in (symlinks or {}).items():
            os.symlink(destination, self.agent / path)
            run_git(self.agent, "add", path)
        args = ["commit", "-q", "--allow-empty", "-m", message]
        if sign_off:
            args.insert(1, "-s")
        run_git(self.agent, *args)
        return run_git(self.agent, "rev-parse", "HEAD").strip()

    def bundle(self, base: str | None = None) -> Path:
        """Create changes.bundle from the base to the agent branch."""
        target = self.proposal / "changes.bundle"
        run_git(
            self.agent,
            "bundle",
            "create",
            "-q",
            str(target),
            f"{base or self.base_sha}..refs/heads/{self.branch}",
        )
        return target

    def link_base(
        self, symlinks: dict[str, str], files: dict[str, str] | None = None
    ) -> None:
        """Give the base tree ``symlinks`` and ``files``; the agent starts there."""
        for path in symlinks:
            if (self.upstream / path).exists():
                run_git(self.upstream, "rm", "-q", path)
        for path, content in (files or {}).items():
            target = self.upstream / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for path, destination in symlinks.items():
            (self.upstream / path).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(destination, self.upstream / path)
        run_git(self.upstream, "add", "-A")
        run_git(self.upstream, "commit", "-q", "-m", "Chore: Link files")
        self.base_sha = run_git(self.upstream, "rev-parse", "HEAD").strip()
        run_git(self.agent, "fetch", "-q", "origin")
        run_git(self.agent, "reset", "-q", "--hard", "origin/main")

    def advance_upstream(self) -> str:
        """Add a commit to the upstream default branch; return its SHA."""
        (self.upstream / "CHANGES.md").write_text("new\n", encoding="utf-8")
        run_git(self.upstream, "add", "-A")
        run_git(self.upstream, "commit", "-q", "-m", "Chore: Advance main")
        return run_git(self.upstream, "rev-parse", "HEAD").strip()

    def revision_entry(self, rebase_sha: str, head_sha: str) -> dict[str, Any]:
        """The selection entry fields a revision adds."""
        return {
            "task": "revise",
            "rebase_sha": rebase_sha,
            "checkout_sha": rebase_sha,
            "collected_at": "2026-09-18T00:00:00Z",
            "session": "39115306869",
            "round": 2,
            "pull_request": {
                "number": 41,
                "url": f"https://github.com/{REPOSITORY}/pull/41",
                "title": "Fix(readme): Correct typo",
                "head_sha": head_sha,
                "mergeable": True,
                "created_at": "2026-09-02T00:00:00Z",
            },
            "threads": [
                {
                    "thread_id": "PRRT_1",
                    "comment_id": 100,
                    "path": "README.md",
                    "line": 3,
                    "outdated": False,
                    "author": "copilot-pull-request-reviewer",
                    "body": "Typo here too.",
                }
            ],
        }

    def revision_manifest(self, **overrides: Any) -> None:
        """A proposed revision manifest."""
        fields: dict[str, Any] = {
            "round_summary": "Fixed the typo the reviewer found.",
            "responses": [
                {
                    "thread_id": "PRRT_1",
                    "comment_id": 100,
                    "reply": "Fixed in the amended commit, @someone.",
                    "resolve": True,
                }
            ],
        }
        fields.update(overrides)
        self.manifest(**fields)

    def manifest(self, **overrides: Any) -> None:
        """Write a proposed manifest, applying overrides."""
        data: dict[str, Any] = {
            "schema": 1,
            "outcome": "proposed",
            "repository": REPOSITORY,
            "issue": ISSUE,
            "base_sha": self.base_sha,
            "branch": BRANCH,
            "reason": None,
            "pr_title": "Fix(readme): Correct typo",
            "pr_body": f"Fix the typo in the readme.\n\nCloses #{ISSUE}\n",
            "commands": [{"command": "uv run pytest", "exit_code": 0}],
        }
        data.update(overrides)
        (self.proposal / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    def selection(self, **overrides: Any) -> dict[str, Any]:
        """The trusted selection document."""
        entry: dict[str, Any] = {
            "key": KEY,
            "repository": REPOSITORY,
            "repo_name": "repo",
            "number": ISSUE,
            "url": ISSUE_URL,
            "title": "Typo in readme",
            "body": "There is a typo.",
            "labels": ["bug"],
            "priority": "Low",
            "type": "Bug",
            "created_at": "2026-09-01T00:00:00Z",
            "author_association": "MEMBER",
            "author": "alice",
            "author_is_bot": False,
            "edited": False,
            "editor": None,
            "editor_is_bot": False,
            "default_branch": "main",
            "base_sha": self.base_sha,
            "branch": BRANCH,
            "fork_org": "lfreleng-bot-forks",
            "fork_repository": "lfreleng-bot-forks/repo",
            "comments": [],
            "comments_dropped": 0,
        }
        entry.update(overrides)
        document: dict[str, Any] = {
            "schema": 1,
            "org": "owner",
            "generated_at": "2026-09-18T00:00:00Z",
            "mode": "pull-requests",
            "dry_run": False,
            "model": "claude-opus-5.5",
            "bot": {"login": BOT_LOGIN, "email": BOT_EMAIL, "placeholder": False},
            "guidance": {},
            "candidates_seen": 1,
            "skipped": {},
            "issue_authors": ["alice"],
            "issues": [entry],
        }
        if self.allow_workflow_edits is not None:
            document["allow_workflow_edits"] = self.allow_workflow_edits
        if self.allow_precommit_config_edits is not None:
            document["allow_precommit_config_edits"] = self.allow_precommit_config_edits
        return document

    def write_selection(self, selection: dict[str, Any] | None = None) -> Path:
        """Write selection.json and return its path."""
        path = self.root / "selection.json"
        path.write_text(json.dumps(selection or self.selection()), encoding="utf-8")
        return path

    def write_coauthors(self) -> Path:
        """Write coauthors.json and return its path."""
        path = self.root / "coauthors.json"
        path.write_text(json.dumps(COAUTHORS), encoding="utf-8")
        return path

    def run(
        self,
        *,
        key: str = KEY,
        zizmor: Any = None,
        zizmor_token: str | None = "t0ken",
        selection: dict[str, Any] | None = None,
    ) -> Any:
        """Run the check with the clone pointed at the local upstream.

        ``zizmor`` is a ``StubZizmor`` to put first on PATH for the
        online audit, every proposal's last stage; by default one that
        finds nothing. ``zizmor_token`` is the token the audit gets;
        None leaves it to the environment, as the publish job does.
        """
        if zizmor is None:
            zizmor = stub.StubZizmor(self.root)
        path = zizmor.path()
        with (
            patch.object(checks, "prepare_clone", local_prepare_clone(self.upstream)),
            patch.dict(os.environ, {"PATH": path}),
        ):
            return checks.run_check(
                selection_path=self.write_selection(selection),
                key=key,
                proposal_dir=self.proposal,
                workdir=self.workdir,
                coauthors_path=self.write_coauthors(),
                run_url=RUN_URL,
                zizmor_token=zizmor_token,
            )


class GitCase(unittest.TestCase):
    """Base class isolating git from the developer's own configuration."""

    def setUp(self) -> None:
        """Give git a private config, a fixed identity and no inherited repo."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        config = self.root / "gitconfig"
        config.write_text(
            "[init]\n\tdefaultBranch = main\n"
            "[commit]\n\tgpgsign = false\n"
            "[tag]\n\tgpgsign = false\n"
            f"[core]\n\thooksPath = {self.root / 'nohooks'}\n"
            "[uploadpack]\n\tallowAnySHA1InWant = true\n",
            encoding="utf-8",
        )
        env = {
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": BOT_LOGIN,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_LOGIN,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            "GIT_AUTHOR_DATE": "2026-09-18T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-09-18T10:00:00Z",
        }
        environment = patch.dict(os.environ, {**clean_git_env(), **env}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = Fixture(self.root)


class RunCheckProposedTest(GitCase):
    """Proposals that pass every rule."""

    def test_happy_path(self) -> None:
        """A single clean commit yields ``proposed`` with composed messages."""
        fixed = README.replace("with a typo", "without a typo")
        sha = self.fixture.commit(
            "Fix(readme): Correct typo\n\nThe word was misspelt.\n",
            {"README.md": fixed},
        )
        self.fixture.bundle()
        self.fixture.manifest()
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(check.reasons, [])
        self.assertFalse(check.needs_workflows)
        self.assertEqual(len(check.commits), 1)
        commit = check.commits[0]
        self.assertEqual(commit["sha"], sha)
        self.assertEqual(commit["headline"], "Fix(readme): Correct typo")
        self.assertTrue(commit["body"].endswith(f"\n\n{COAUTHOR}\n{SIGN_OFF}"))
        self.assertEqual(commit["body"].count(SIGN_OFF), 1)
        self.assertIn("The word was misspelt.", commit["body"])
        self.assertEqual(commit["deletions"], [])
        (addition,) = commit["additions"]
        self.assertEqual(addition["path"], "README.md")
        self.assertTrue(policy.SHA_RE.fullmatch(addition["blob"]))
        self.assertEqual(addition["size"], len(fixed.encode("utf-8")))
        self.assertEqual(check.pr_title, "Fix(readme): Correct typo")
        provenance = model.provenance_block(
            model="claude-opus-5.5",
            run_url=RUN_URL,
            issue_url=ISSUE_URL,
            base_sha=self.fixture.base_sha,
            commands=[{"command": "uv run pytest", "exit_code": 0}],
        )
        self.assertEqual(
            check.pr_body,
            f"Closes #{ISSUE}\n\n{provenance}\n---\n\n"
            f"Fix the typo in the readme.\n\nCloses #{ISSUE}\n",
        )
        self.assertEqual(check.files_changed, 1)
        self.assertEqual(check.added_bytes, len(fixed.encode("utf-8")))
        self.assertEqual(check.commands, [{"command": "uv run pytest", "exit_code": 0}])
        self.assertIsNone(check.premium_requests)
        serialised = check.to_json()
        self.assertEqual(serialised["schema"], model.SCHEMA)
        self.assertEqual(
            serialised["stats"], {"files_changed": 1, "added_bytes": len(fixed)}
        )

    def test_relative_proposal_directory(self) -> None:
        """The workflow passes ``--proposal-dir accepted``, relative to its cwd.

        Every git call runs in the clone, so a relative bundle path
        must not reach git as given: run 37587554533 rejected a valid
        proposal because git resolved ``accepted/changes.bundle``
        inside the clone.
        """
        fixed = README.replace("with a typo", "without a typo")
        self.fixture.commit("Fix(readme): Correct typo\n", {"README.md": fixed})
        self.fixture.bundle()
        self.fixture.manifest()
        previous = os.getcwd()
        os.chdir(self.fixture.proposal.parent)
        self.addCleanup(os.chdir, previous)
        self.fixture.proposal = Path(self.fixture.proposal.name)
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)

    def test_publisher_owns_the_closing_line(self) -> None:
        """The body opens with this issue's Closes line whatever the agent wrote.

        Nothing the agent writes can precede it or turn it into code,
        so the merged pull request closes the selected issue.
        """
        for index, agent_body in enumerate(
            (
                "Fix the typo in the readme.\n",
                "<pre>\nCloses #7\n</pre>\n",
                "- ```text\n  unclosed fence\n",
            )
        ):
            root = self.root / f"case-{index}"
            root.mkdir()
            fixture = Fixture(root)
            fixture.commit("Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"})
            fixture.bundle()
            fixture.manifest(pr_body=agent_body)
            check = fixture.run()
            self.assertEqual(check.verdict, "proposed", check.reasons)
            self.assertTrue(
                check.pr_body.startswith(f"Closes #{ISSUE}\n\n"), check.pr_body
            )

    def test_provenance_precedes_the_agent_body(self) -> None:
        """An unclosed fence or HTML block cannot hide the AI disclosure."""
        for index, agent_body in enumerate(
            ("- ```text\n  unclosed fence\n", "<pre>\nunclosed block\n")
        ):
            root = self.root / f"case-{index}"
            root.mkdir()
            fixture = Fixture(root)
            fixture.commit("Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"})
            fixture.bundle()
            fixture.manifest(pr_body=agent_body)
            body = fixture.run().pr_body
            disclosure = body.index("Authored by an AI agent")
            self.assertLess(body.index("Closes #"), disclosure)
            self.assertLess(body.index("</details>"), body.index(agent_body.strip()))

    def test_workflow_change_flags_permission(self) -> None:
        """Touching a workflow file sets ``needs_workflows``."""
        self.fixture.allow_workflow_edits = True
        self.fixture.commit(
            "CI: Add a workflow\n\nBody.\n",
            {".github/workflows/ci.yaml": "on: push\njobs: {}\n"},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="CI: Add a workflow")
        zizmor = stub.StubZizmor(self.root)
        check = self.fixture.run(zizmor=zizmor, zizmor_token="t0ken")
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.needs_workflows)

    def test_non_utf8_path_rejected(self) -> None:
        """A path that is not valid UTF-8 cannot be replayed faithfully.

        Built through git plumbing: APFS refuses such names on disk, and
        a hostile bundle would carry the bytes without a checkout anyway.
        """
        agent = self.fixture.agent
        blob = (
            subprocess.run(
                ["git", "-C", str(agent), "hash-object", "-w", "--stdin"],
                input=b"x\n",
                capture_output=True,
                check=True,
                timeout=60,
            )
            .stdout.decode()
            .strip()
        )
        # Add the bad name to the current index so the rest of the tree,
        # protected files included, stays as it was.
        subprocess.run(
            ["git", "-C", str(agent), "update-index", "--add", "--index-info"],
            input=b"100644 " + blob.encode() + b"\tdocs/note\xe9.md\n",
            capture_output=True,
            check=True,
            timeout=60,
        )
        tree = run_git(agent, "write-tree").strip()
        commit = run_git(
            agent, "commit-tree", tree, "-p", "HEAD", "-m", "Docs: Add note\n\nBody.\n"
        ).strip()
        run_git(agent, "update-ref", f"refs/heads/{self.fixture.branch}", commit)
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Docs: Add note")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(
            any("not valid UTF-8" in r for r in check.reasons), check.reasons
        )

    def test_non_utf8_message_rejected(self) -> None:
        """A message that is not UTF-8 would publish differently; refuse it.

        ``git commit`` repairs such a message itself, so the commit object
        is forged with ``hash-object``, as a hostile bundle could carry it.
        """
        agent = self.fixture.agent
        (agent / "README.md").write_text("changed\n", encoding="utf-8")
        run_git(agent, "add", "README.md")
        tree = run_git(agent, "write-tree").strip()
        parent = run_git(agent, "rev-parse", "HEAD").strip()
        ident = (
            b"code-monkey[bot] <1+code-monkey[bot]@users.noreply.github.com> 0 +0000"
        )
        body = (
            b"tree "
            + tree.encode()
            + b"\nparent "
            + parent.encode()
            + b"\nauthor "
            + ident
            + b"\ncommitter "
            + ident
            + b"\n\nFix: Na\xefve message\n\nBody.\n"
        )
        forged = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(agent),
                    "hash-object",
                    "-t",
                    "commit",
                    "-w",
                    "--stdin",
                ],
                input=body,
                capture_output=True,
                check=True,
                timeout=60,
            )
            .stdout.decode()
            .strip()
        )
        run_git(agent, "update-ref", f"refs/heads/{self.fixture.branch}", forged)
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: Nave message")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(
            any("not valid UTF-8" in r for r in check.reasons), check.reasons
        )

    def test_commit_trees_are_recorded(self) -> None:
        """Each checked commit carries its tree, for reconciliation later."""
        sha = self.fixture.commit("Fix: Record tree\n\nBody.\n", {"README.md": "t\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: Record tree")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        expected = run_git(self.fixture.agent, "rev-parse", f"{sha}^{{tree}}").strip()
        self.assertEqual(check.commits[0]["tree"], expected)

    def test_workflow_deletion_flags_permission(self) -> None:
        """Deleting a workflow file needs the grant as much as writing one."""
        self.fixture.allow_workflow_edits = True
        self.fixture.commit(
            "CI: Add a workflow\n\nBody.\n",
            {".github/workflows/old.yaml": "on: push\njobs: {}\n"},
        )
        self.fixture.commit(
            "CI: Remove the workflow\n\nBody.\n",
            {".github/workflows/old.yaml": None},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Two commits")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.needs_workflows)
        self.assertEqual(check.commits[1]["deletions"], [".github/workflows/old.yaml"])

    def test_multiple_commits_and_deletion(self) -> None:
        """Two commits keep their order; deletions are recorded; title is free."""
        first = self.fixture.commit(
            "Feat(core): Add module\n\nBody.\n", {"src/core.py": "x = 1\n"}
        )
        second = self.fixture.commit(
            "Chore: Remove old file\n\nBody.\n", {"old.txt": None}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Any title for two commits")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual([c["sha"] for c in check.commits], [first, second])
        self.assertEqual(check.commits[1]["additions"], [])
        self.assertEqual(check.commits[1]["deletions"], ["old.txt"])
        self.assertEqual(check.files_changed, 2)
        self.assertEqual(check.pr_title, "Any title for two commits")

    def test_usage_figures(self) -> None:
        """usage.json supplies premium requests and agent seconds."""
        (self.fixture.proposal / "usage.json").write_text(
            json.dumps({"totalPremiumRequestCost": 12, "totalApiDurationMs": 65000}),
            encoding="utf-8",
        )
        self.fixture.manifest(outcome="abstain", reason="nothing to do")
        check = self.fixture.run()
        self.assertEqual(check.premium_requests, 12.0)
        self.assertEqual(check.agent_seconds, 65)

    def test_fusion_run_names_its_tier_and_models(self) -> None:
        """A Fusion run's body names the tier and the models usage.json lists."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nThe word was misspelt.\n",
            {"README.md": README.replace("with a typo", "without a typo")},
        )
        self.fixture.bundle()
        self.fixture.manifest()
        (self.fixture.proposal / "usage.json").write_text(
            json.dumps(FUSION_USAGE), encoding="utf-8"
        )
        selection = self.fixture.selection()
        selection.update(model="fusion", fusion_tier="high")
        with patch.object(self.fixture, "write_coauthors", return_value=REAL_COAUTHORS):
            check = self.fixture.run(selection=selection)
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertIn(
            "- Model: Copilot Fusion (`high` tier), routed to `claude-opus-5.5`,"
            " `gpt-6-luna` via GitHub Copilot CLI",
            check.pr_body,
        )
        self.assertIn(f"\n\n{COPILOT_TRAILER}\n", check.commits[0]["body"])
        serialised = check.to_json()
        self.assertEqual(serialised["models_used"], ["claude-opus-5.5", "gpt-6-luna"])
        self.assertEqual(serialised["models_dropped"], 0)
        self.assertIn(
            "Models used: `claude-opus-5.5`, `gpt-6-luna`", model.check_summary(check)
        )

    def test_unreadable_usage_is_a_note(self) -> None:
        """A corrupt usage.json is recorded, not fatal."""
        (self.fixture.proposal / "usage.json").write_text("{nope", encoding="utf-8")
        self.fixture.manifest(outcome="abstain", reason="nothing to do")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "abstain")
        self.assertTrue(any("usage.json unreadable" in r for r in check.reasons))


ROUND_6 = [
    {"command": "uv run --extra test pytest -q", "exit_code": 0},
    {
        "command": "uvx tox --root . -c ./tox.ini -e build -- /tmp (expected rejection)",
        "exit_code": 1,
    },
    {
        "command": "prek run --files scripts/build_pinned.py "
        "tests/test_build_pinned.py (basedpyright hook could not spawn its binary)",
        "exit_code": 1,
    },
    {"command": "uvx gitlint --commits HEAD~1..HEAD", "exit_code": 0},
]
ROUND_7 = [
    {
        "command": "SKIP=no-commit-to-branch prek run --files <changed files> "
        "(before fix; reproduced mypy no-any-return)",
        "exit_code": 1,
    },
    {
        "command": "SKIP=no-commit-to-branch,basedpyright prek run --files "
        "<changed files>",
        "exit_code": 0,
    },
    {"command": "prek run mypy --all-files", "exit_code": 0},
    {"command": "uv run pytest -q", "exit_code": 0},
]
HOOKS_FAILED = "**Hooks did not pass.**"


class RunCheckHookRunTest(GitCase):
    """A failing last hook run is marked outside the provenance block."""

    def propose(self, commands: list[dict[str, Any]]) -> Any:
        """Run the check on one clean commit recording ``commands``."""
        self.fixture.commit("Fix(readme): Correct typo\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(commands=commands)
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        return check

    def test_round_6_is_marked(self) -> None:
        """One warning, and one line between ``</details>`` and the rule."""
        check = self.propose(ROUND_6)
        self.assertEqual(len(check.warnings), 1)
        self.assertIn("exit 1", check.warnings[0])
        self.assertIn("prek run --files scripts/build_pinned.py", check.warnings[0])
        body = check.pr_body
        self.assertEqual(body.count(HOOKS_FAILED), 1)
        line = body.index(HOOKS_FAILED)
        self.assertLess(body.index("</details>"), line)
        self.assertLess(line, body.index("\n---\n"))
        self.assertIn("could not spawn its binary)`, did not exit 0 (exit 1)", body)

    def test_round_7_is_not_marked(self) -> None:
        """A later passing hook run leaves the body as it was."""
        check = self.propose(ROUND_7)
        self.assertEqual(check.warnings, [])
        provenance = model.provenance_block(
            model="claude-opus-5.5",
            run_url=RUN_URL,
            issue_url=ISSUE_URL,
            base_sha=self.fixture.base_sha,
            commands=check.commands,
        )
        self.assertEqual(
            check.pr_body,
            f"Closes #{ISSUE}\n\n{provenance}\n---\n\n"
            f"Fix the typo in the readme.\n\nCloses #{ISSUE}\n",
        )

    def test_an_exit_code_that_is_not_the_integer_0_is_marked(self) -> None:
        """Omitted, ``true`` or ``"0"``: none is a pass."""
        extras: tuple[dict[str, Any], ...] = (
            {},
            {"exit_code": True},
            {"exit_code": "0"},
        )
        for index, extra in enumerate(extras):
            with self.subTest(extra=extra):
                root = self.root / f"case-{index}"
                root.mkdir()
                self.fixture = Fixture(root)
                check = self.propose([{"command": "prek run --files x", **extra}])
                self.assertEqual(len(check.warnings), 1)
                self.assertEqual(check.pr_body.count(HOOKS_FAILED), 1)

    def test_hostile_command_stays_on_one_inert_line(self) -> None:
        """A newline, backtick, ``</details>``, pipe or mention cannot escape."""
        command = "prek run\n`x` </details> a | b @alice\n\n---\n# H"
        check = self.propose([{"command": command, "exit_code": 2}])
        (line,) = [x for x in check.pr_body.splitlines() if HOOKS_FAILED in x]
        self.assertIn("`prek run 'x' &lt;/details> a \\| b @alice --- # H`", line)
        self.assertEqual(check.pr_body.count("</details>"), 1)
        self.assertEqual(line.count("`"), 2)
        (warning,) = check.warnings
        self.assertNotIn("\n", warning)
        self.assertEqual(warning.count("`"), 2)


class RunCheckOnlineAuditTest(GitCase):
    """The check audits every proposal online with zizmor."""

    WORKFLOW = ".github/workflows/ci.yaml"

    def propose_workflow(self) -> None:
        """One commit adding a workflow, bundled with its manifest."""
        self.fixture.allow_workflow_edits = True
        self.fixture.commit(
            "CI: Add a workflow\n\nBody.\n",
            {self.WORKFLOW: "on: push\njobs: {}\n"},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="CI: Add a workflow")

    def test_added_finding_rejects(self) -> None:
        """A finding the base lacks rejects, naming the rule and the file."""
        self.propose_workflow()
        zizmor = stub.StubZizmor(
            self.root, head=[stub.finding("stale-action-refs", self.WORKFLOW)]
        )
        check = self.fixture.run(zizmor=zizmor, zizmor_token="t0ken")
        self.assertEqual(check.verdict, "rejected")
        self.assertEqual(check.commits, [])
        self.assertIn("stale-action-refs", check.reasons[0])
        self.assertIn(self.WORKFLOW, check.reasons[0])
        self.assertEqual(len(zizmor.calls()), 2)

    def test_clean_workflow_change_is_proposed(self) -> None:
        """No new finding: proposed, with the audit's note on the check."""
        self.propose_workflow()
        zizmor = stub.StubZizmor(self.root)
        check = self.fixture.run(zizmor=zizmor, zizmor_token="t0ken")
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertIn("no new zizmor findings", check.online_audit)
        self.assertEqual(check.to_json()["online_audit"], check.online_audit)
        self.assertIn(check.online_audit, model.check_summary(check))
        self.assertEqual(
            [call["env"]["GH_TOKEN"] for call in zizmor.calls()], ["t0ken"] * 2
        )

    def test_token_defaults_to_the_environment(self) -> None:
        """Without an explicit token, the check reads the job's variable."""
        self.propose_workflow()
        zizmor = stub.StubZizmor(self.root)
        with patch.dict(os.environ, {online_audit.TOKEN_VARIABLE: "env-t0ken"}):
            check = self.fixture.run(zizmor=zizmor, zizmor_token=None)
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(
            [call["env"]["GH_TOKEN"] for call in zizmor.calls()], ["env-t0ken"] * 2
        )
        self.assertNotIn(online_audit.TOKEN_VARIABLE, zizmor.calls()[0]["env"])

    def test_missing_token_rejects(self) -> None:
        """A proposal with no token to audit it fails closed."""
        self.propose_workflow()
        zizmor = stub.StubZizmor(self.root)
        check = self.fixture.run(zizmor=zizmor, zizmor_token="")
        self.assertEqual(check.verdict, "rejected")
        self.assertIn("token", check.reasons[0])
        self.assertEqual(zizmor.calls(), [])

    def test_change_without_workflows_is_audited(self) -> None:
        """A proposal that touches no workflow is audited all the same."""
        fixed = README.replace("with a typo", "without a typo")
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": fixed}
        )
        self.fixture.bundle()
        self.fixture.manifest()
        zizmor = stub.StubZizmor(self.root, exit_code=3)
        check = self.fixture.run(zizmor=zizmor, zizmor_token="t0ken")
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(check.online_audit, "online audit: no new zizmor findings")
        self.assertEqual(len(zizmor.calls()), 2)


class RunCheckOutcomeTest(GitCase):
    """Manifest outcomes other than ``proposed`` are recorded, not verified."""

    def test_abstain(self) -> None:
        """Abstain carries the reason and needs no bundle."""
        self.fixture.manifest(outcome="abstain", reason="issue needs a design")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "abstain")
        self.assertEqual(check.reasons, ["issue needs a design"])
        self.assertEqual(check.commits, [])

    def test_an_issue_abstention_is_never_superseded(self) -> None:
        """Only a revision round can lose a race to another run's push."""
        self.fixture.manifest(outcome="abstain", reason="r", superseded=True)
        self.assertIs(self.fixture.run().superseded, False)

    def test_listed_author_is_rechecked(self) -> None:
        """An entry the recorded list does not admit, or no list, is rejected."""
        self.fixture.manifest(outcome="abstain", reason="r")
        cases: tuple[tuple[dict[str, Any], str], ...] = (
            ({"author": "mallory"}, "author_not_listed"),
            ({"edited": True, "editor": "mallory"}, "editor_not_listed"),
            ({"issue_authors": None}, "no CODE_MONKEY_ISSUE_AUTHORS list was recorded"),
            ({"issue_authors": []}, "CODE_MONKEY_ISSUE_AUTHORS is empty"),
        )
        for change, reason in cases:
            selection = self.fixture.selection()
            if "issue_authors" in change:
                selection["issue_authors"] = change["issue_authors"]
            else:
                selection["issues"][0].update(change)
            with self.subTest(change=change):
                check = self.fixture.run(selection=selection)
                self.assertEqual(check.verdict, "rejected")
                self.assertIn(reason, check.reasons[0])
                self.assertIs(check.to_json()["author_unlisted"], True)

    def test_rule_off_skips_the_listed_author_recheck(self) -> None:
        """A selection that recorded the rule off admits any author, and says so."""
        self.fixture.manifest(outcome="abstain", reason="r")
        selection = self.fixture.selection()
        selection["only_known_authors"] = False
        selection["issue_authors"] = []
        selection["issues"][0].update(author="mallory", edited=True, editor="eve")
        check = self.fixture.run(selection=selection)
        self.assertEqual(check.verdict, "abstain")
        record = check.to_json()
        self.assertIs(record["only_known_authors"], False)
        self.assertEqual(record["issue_authors"], [])
        self.assertIs(record["author_unlisted"], False)
        # Only a JSON false turns it off: anything else needs the list.
        selection["only_known_authors"] = "false"
        check = self.fixture.run(selection=selection)
        self.assertEqual(check.verdict, "rejected")
        self.assertIs(check.to_json()["only_known_authors"], True)

    def test_author_failed(self) -> None:
        """author-failed without a reason gets a placeholder."""
        self.fixture.manifest(outcome="author-failed", reason=None)
        check = self.fixture.run()
        self.assertEqual(check.verdict, "author-failed")
        self.assertEqual(check.reasons, ["no reason given"])

    def test_unknown_outcome_rejected(self) -> None:
        """An outcome outside the contract is a rejection."""
        self.fixture.manifest(outcome="merged")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn("not recognised", check.reasons[0])

    def test_unsupported_schema_rejected(self) -> None:
        """A manifest in any schema but the supported one is a rejection."""
        for schema in (None, 2, "1", True, 1.0):
            self.fixture.manifest(outcome="abstain", reason="no", schema=schema)
            check = self.fixture.run()
            self.assertEqual(check.verdict, "rejected", schema)
            self.assertIn("schema", check.reasons[0])
        self.fixture.manifest(outcome="abstain", reason="no")
        self.assertEqual(self.fixture.run().verdict, "abstain")

    def test_missing_or_malformed_manifest_is_a_rejection(self) -> None:
        """Manifest problems are agent output, so they become a typed verdict."""
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn("manifest", check.reasons[0])
        for content in ("not json", "[1, 2]", '"a string"'):
            with self.subTest(content=content):
                (self.fixture.proposal / "manifest.json").write_text(
                    content, encoding="utf-8"
                )
                check = self.fixture.run()
                self.assertEqual(check.verdict, "rejected")

    def test_missing_bundle_rejected(self) -> None:
        """A proposed outcome without a bundle is rejected."""
        self.fixture.manifest()
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn("lacks changes.bundle", check.reasons[0])


class OutcomeIdentityTest(GitCase):
    """Every manifest outcome must name the selected issue."""

    def test_mismatched_abstention_is_rejected(self) -> None:
        """An abstention for another issue does not land on this one."""
        self.fixture.manifest(outcome="abstain", reason="no", issue=99)
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(any("issue" in r for r in check.reasons), check.reasons)


class ReasonBoundTest(GitCase):
    """Reasons from the untrusted manifest are cut to a bounded size."""

    def test_abstain_reason_truncated(self) -> None:
        """A 70,000-character reason is recorded at MAX_REASON characters."""
        self.fixture.manifest(outcome="abstain", reason="z" * 70_000)
        check = self.fixture.run()
        self.assertEqual(check.verdict, "abstain")
        self.assertEqual(len(check.reasons[0]), policy.MAX_REASON)


class RunCheckRejectionTest(GitCase):
    """Proposals that break a rule are rejected with a reason."""

    def rejected(self) -> str:
        """Run the check, assert rejection, return the joined reasons."""
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected", check.reasons)
        self.assertEqual(check.commits, [])
        return "; ".join(check.reasons)

    def test_executable_file(self) -> None:
        """An executable addition is rejected."""
        self.fixture.commit(
            "Feat: Add tool\n\nBody.\n",
            {"bin/tool.sh": "#!/bin/sh\n"},
            executable=("bin/tool.sh",),
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Feat: Add tool")
        reasons = self.rejected()
        self.assertIn("executable", reasons)
        self.assertIn("bin/tool.sh", reasons)

    def test_symlink(self) -> None:
        """A symlink addition is rejected."""
        self.fixture.commit(
            "Feat: Add link\n\nBody.\n", symlinks={"link.md": "README.md"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Feat: Add link")
        self.assertIn("symlink", self.rejected())

    def test_protected_file(self) -> None:
        """A change to AGENTS.md is rejected as protected."""
        self.fixture.commit(
            "Docs: Update agents\n\nBody.\n", {"AGENTS.md": "# Changed\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Docs: Update agents")
        self.assertIn("AGENTS.md is protected", self.rejected())

    def test_protected_deletion(self) -> None:
        """Deleting an agent instruction file is refused like editing one."""
        self.fixture.commit(
            "Chore: Drop instructions\n\nBody.\n",
            {".github/copilot-instructions.md": None},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Chore: Drop instructions")
        self.assertIn(".github/copilot-instructions.md is protected", self.rejected())

    def test_sudo_marker_addition(self) -> None:
        """A proposal cannot turn on sudo and containers for its target (#189)."""
        self.fixture.commit(
            "Chore: Enable sudo\n\nBody.\n", {".enable-sudo-and-containers": ""}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Chore: Enable sudo")
        self.assertIn(".enable-sudo-and-containers is protected", self.rejected())

    def test_zizmor_configuration_addition(self) -> None:
        """A proposal cannot switch off its target's zizmor audits (#207)."""
        self.fixture.commit(
            "Chore: Add zizmor config\n\nBody.\n",
            {".github/zizmor.yml": "rules:\n  unpinned-uses:\n    disable: true\n"},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Chore: Add zizmor config")
        self.assertIn(".github/zizmor.yml is protected", self.rejected())

    def test_subject_without_type(self) -> None:
        """A subject lacking a Conventional Commit type is rejected."""
        self.fixture.commit("fix readme\n\nBody.\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title="fix readme")
        self.assertIn("capitalised Conventional Commit type", self.rejected())

    def test_subject_over_gitlint_limit(self) -> None:
        """The target's .gitlint limit of 60 applies to the subject."""
        subject = "Fix(readme): " + "x" * 48
        self.assertEqual(len(subject), 61)
        self.fixture.commit(f"{subject}\n\nBody.\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title=subject)
        self.assertIn("exceeds 60 characters", self.rejected())

    def test_subject_within_gitlint_limit(self) -> None:
        """A 60-character subject, over the default 50, passes."""
        subject = "Fix(readme): X" + "x" * 46
        self.assertEqual(len(subject), 60)
        self.fixture.commit(f"{subject}\n\nBody.\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title=subject)
        self.assertEqual(self.fixture.run().verdict, "proposed")

    def test_pr_title_mismatch(self) -> None:
        """A single-commit title that differs from the subject is rejected."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix(readme): Something else")
        self.assertIn("must equal the subject", self.rejected())

    def test_manifest_repository_mismatch(self) -> None:
        """A manifest naming another repository disagrees with the selection."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(repository="owner/other", issue=ISSUE + 1)
        reasons = self.rejected()
        self.assertIn("disagrees", reasons)
        self.assertIn("repository", reasons)
        self.assertIn("issue", reasons)

    def test_oversized_blob_refused_on_size_alone(self) -> None:
        """A blob past the total cap is rejected from its header, unread."""
        self.fixture.commit(
            "Feat: Add blob\n\nBody.\n",
            {"big.txt": "y" * (policy.MAX_ADDED_BYTES + 1)},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Feat: Add blob")
        with patch.object(checks, "is_binary") as sniff:
            check = self.fixture.run()
        sniff.assert_not_called()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(any("big.txt" in r for r in check.reasons), check.reasons)

    def test_empty_commit_rejected(self) -> None:
        """A commit with no file changes cannot go through the API."""
        self.fixture.commit("Chore: Nothing\n\nBody.\n")
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Chore: Nothing")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(
            any("changes no files" in r for r in check.reasons), check.reasons
        )

    def test_too_many_files_in_one_commit_rejected(self) -> None:
        """The API replays at most 100 file changes per commit."""
        files: dict[str, str | None] = {
            f"many/f{i:03d}.txt": f"{i}\n" for i in range(101)
        }
        self.fixture.commit("Feat: Add many files\n\nBody.\n", files)
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Feat: Add many files")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(any("101 files" in r for r in check.reasons), check.reasons)

    def test_too_many_commits(self) -> None:
        """Six commits exceed the limit of five."""
        for index in range(6):
            self.fixture.commit(
                f"Fix: Step {index}\n\nBody.\n", {f"f{index}.txt": f"{index}\n"}
            )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: Steps")
        self.assertIn("6 commits exceed the limit of 5", self.rejected())

    def test_bundle_branch_mismatch(self) -> None:
        """A bundle carrying a different branch name is rejected."""
        self.fixture.branch = "code-monkey/issue-8"
        run_git(self.fixture.agent, "checkout", "-q", "-b", self.fixture.branch)
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest()
        self.assertIn(f"does not carry refs/heads/{BRANCH}", self.rejected())

    def test_merge_commit(self) -> None:
        """A merge in the bundled history is rejected as non-linear."""
        agent = self.fixture.agent
        run_git(agent, "checkout", "-q", "-b", "side", self.fixture.base_sha)
        self.fixture.commit("Fix: Side\n\nBody.\n", {"side.txt": "s\n"})
        run_git(agent, "checkout", "-q", BRANCH)
        self.fixture.commit("Fix: Main\n\nBody.\n", {"main.txt": "m\n"})
        run_git(agent, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: Merge")
        self.assertIn("linear", self.rejected())

    def test_missing_blank_line_after_subject(self) -> None:
        """Body text glued to the subject is rejected."""
        self.fixture.commit("Fix: X\nbody here\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: X")
        self.assertIn("blank line after the subject", self.rejected())

    def test_unsigned_commit_still_gets_sign_off(self) -> None:
        """Without the agent's -s the publisher still appends the bot sign-off."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}, sign_off=False
        )
        self.fixture.bundle()
        self.fixture.manifest()
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.commits[0]["body"].endswith(f"{COAUTHOR}\n{SIGN_OFF}"))

    def test_corrupt_bundle(self) -> None:
        """A file that is not a bundle fails verification."""
        (self.fixture.proposal / "changes.bundle").write_bytes(b"not a bundle")
        self.fixture.manifest()
        self.assertIn("bundle failed verification", self.rejected())

    def test_bundle_from_wrong_base(self) -> None:
        """A bundle whose prerequisite the clone lacks fails verification."""
        agent = self.fixture.agent
        run_git(agent, "checkout", "-q", "main")
        (agent / "extra.txt").write_text("e\n", encoding="utf-8")
        run_git(agent, "add", "extra.txt")
        run_git(agent, "commit", "-q", "-m", "Chore: Upstream moved on")
        moved = run_git(agent, "rev-parse", "HEAD").strip()
        run_git(agent, "checkout", "-q", BRANCH)
        run_git(agent, "rebase", "-q", moved)
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        target = self.fixture.proposal / "changes.bundle"
        run_git(agent, "bundle", "create", "-q", str(target), f"{moved}..{BRANCH}")
        self.fixture.manifest()
        self.assertIn("bundle failed verification", self.rejected())


class SymlinkTargetTest(GitCase):
    """A protected link in the base tree protects what it points at (#216)."""

    EXAMPLES: tuple[tuple[str, str, str], ...] = (
        (".github/zizmor.yml", "../docs/zizmor.yml", "docs/zizmor.yml"),
        ("AGENTS.md", "docs/guide.md", "docs/guide.md"),
        (".pre-commit-config.yaml", "config/pre-commit.yaml", "config/pre-commit.yaml"),
        (".claude", "tooling/claude", "tooling/claude/settings.json"),
    )

    def propose(
        self,
        fixture: Fixture,
        files: dict[str, str | None],
        symlinks: dict[str, str] | None = None,
        base_files: dict[str, str] | None = None,
    ) -> Any:
        """Commit ``files`` on a base holding ``symlinks``; run the check."""
        if symlinks or base_files:
            fixture.link_base(symlinks or {}, base_files)
        fixture.commit("Chore: Edit files\n\nBody.\n", files)
        fixture.bundle()
        fixture.manifest(pr_title="Chore: Edit files")
        return fixture.run()

    def fixture_for(self, name: str) -> Fixture:
        """A separate fixture, for a test that needs more than one base."""
        root = self.root / name
        root.mkdir()
        return Fixture(root)

    def assert_rejected(self, check: Any, path: str, cause: str) -> None:
        """The check refused ``path`` and named the link that protects it."""
        self.assertEqual(check.verdict, "rejected", check.reasons)
        self.assertIn(
            f"{path} is protected and cannot change ({cause}", "; ".join(check.reasons)
        )

    def test_examples_are_rejected(self) -> None:
        """Each example in the issue is refused, naming its link."""
        for index, (link, target, edited) in enumerate(self.EXAMPLES):
            with self.subTest(link=link):
                fixture = self.fixture_for(f"example-{index}")
                check = self.propose(
                    fixture,
                    {edited: "changed\n"},
                    {link: target},
                    {edited: "original\n"},
                )
                self.assert_rejected(check, edited, f"target of {link})")

    def test_examples_without_links_are_accepted(self) -> None:
        """The same edits on a base with no links pass as before."""
        for index, (_link, _target, edited) in enumerate(self.EXAMPLES):
            with self.subTest(path=edited):
                fixture = self.fixture_for(f"plain-{index}")
                check = self.propose(
                    fixture, {edited: "changed\n"}, base_files={edited: "original\n"}
                )
                self.assertEqual(check.verdict, "proposed", check.reasons)

    def test_chain_of_two_links_is_followed(self) -> None:
        """The file at the end of a chain is the protected one."""
        check = self.propose(
            self.fixture,
            {"text/b.md": "changed\n"},
            {"AGENTS.md": "docs/a.md", "docs/a.md": "../text/b.md"},
            {"text/b.md": "original\n"},
        )
        self.assert_rejected(check, "text/b.md", "target of AGENTS.md)")

    def test_dangling_target_cannot_be_created(self) -> None:
        """A target missing from the base cannot be added."""
        check = self.propose(
            self.fixture, {"docs/guide.md": "new\n"}, {"AGENTS.md": "docs/guide.md"}
        )
        self.assert_rejected(check, "docs/guide.md", "target of AGENTS.md)")

    def test_link_out_of_the_repository_is_refused(self) -> None:
        """Touching a protected link that leaves the repository is refused."""
        check = self.propose(self.fixture, {".claude": None}, {".claude": "../claude"})
        self.assert_rejected(check, ".claude", ".claude -> ../claude")

    def test_loop_is_refused(self) -> None:
        """Touching the first path of a looping link is refused, naming both."""
        check = self.propose(
            self.fixture, {"a.md": None}, {"AGENTS.md": "a.md", "a.md": "AGENTS.md"}
        )
        self.assert_rejected(check, "a.md", "AGENTS.md -> a.md")

    def test_loop_hops_cannot_be_replaced(self) -> None:
        """No link on a loop can become a file or directory the name means."""
        cases: tuple[
            tuple[dict[str, str], dict[str, str | None], dict[str, str | None], str],
            ...,
        ] = (
            (
                {"AGENTS.md": "a.md", "a.md": "b.md", "b.md": "a.md"},
                {"b.md": None},
                {"b.md": "new instructions\n"},
                "b.md",
            ),
            (
                {".claude": "x", "x": "y", "y": "x"},
                {"y": None},
                {"y/settings.json": "{}\n"},
                "y",
            ),
        )
        for index, (symlinks, removed, added, path) in enumerate(cases):
            link = next(iter(symlinks))
            with self.subTest(link=link):
                fixture = self.fixture_for(f"loop-{index}")
                fixture.link_base(symlinks)
                fixture.commit("Chore: Remove link\n\nBody.\n", removed)
                check = self.propose(fixture, added)
                self.assert_rejected(check, path, f"{link} -> ")

    def test_byte_distinct_links_stay_distinct(self) -> None:
        """Links whose names differ only in invalid UTF-8 each protect."""
        symlinks = {
            os.fsdecode(b"docs/\xfe/AGENTS.md"): "../../a.md",
            os.fsdecode(b"docs/\xff/AGENTS.md"): "../../b.md",
        }
        base = {"a.md": "a\n", "b.md": "b\n"}
        for edited in base:
            with self.subTest(path=edited):
                fixture = self.fixture_for(f"bytes-{edited}")
                check = self.propose(fixture, {edited: "changed\n"}, symlinks, base)
                self.assert_rejected(check, edited, "target of docs/")

    def test_unprotected_link_adds_no_protection(self) -> None:
        """A link whose own path is not protected leaves its target editable."""
        check = self.propose(
            self.fixture,
            {"README.md": "changed\n"},
            {"docs/link.md": "../README.md"},
        )
        self.assertEqual(check.verdict, "proposed", check.reasons)

    def test_link_past_the_bound_cannot_be_replaced(self) -> None:
        """The link that passes the chain bound cannot become a file."""
        bound = 8  # protected_links.MAX_LINK_CHAIN
        last = f"a{bound}"
        symlinks = {"AGENTS.md": "a1"}
        symlinks |= {f"a{i}": f"a{i + 1}" for i in range(1, bound)}
        symlinks[last] = "target.md"
        self.fixture.link_base(symlinks, {"target.md": "original\n"})
        self.fixture.commit("Chore: Remove link\n\nBody.\n", {last: None})
        check = self.propose(self.fixture, {last: "new instructions\n"})
        self.assert_rejected(check, last, "AGENTS.md -> a1")

    def test_child_of_a_target_past_the_chain_bound(self) -> None:
        """A file under the target of an over-long .claude chain is refused."""
        bound = 8  # protected_links.MAX_LINK_CHAIN
        symlinks = {".claude": "a1"}
        symlinks |= {f"a{i}": f"a{i + 1}" for i in range(1, bound)}
        symlinks[f"a{bound}"] = "settings"
        check = self.propose(
            self.fixture,
            {"settings/options.json": "changed\n"},
            symlinks,
            {"settings/options.json": "original\n"},
        )
        self.assert_rejected(check, "settings/options.json", "target of .claude)")

    def test_child_of_a_target_past_the_alias_depth(self) -> None:
        """A file under a link reached through nine directory links is refused."""
        depth = 9  # protected_links.MAX_LINK_CHAIN + 1
        symlinks = {".claude": "d1"}
        symlinks |= {f"d{i}/l": f"../d{i + 1}" for i in range(1, depth)}
        edited = f"d{depth}/settings.json"
        check = self.propose(
            self.fixture, {edited: "changed\n"}, symlinks, {edited: "original\n"}
        )
        self.assert_rejected(check, edited, "target of .claude)")

    def test_child_of_a_target_through_a_repeated_link(self) -> None:
        """A link followed nine times on a finite chain still protects its end."""
        check = self.propose(
            self.fixture,
            {"settings/options.json": "changed\n"},
            {".claude": "a/a/a/a/a/a/a/a/a/settings", "a": "."},
            {"settings/options.json": "original\n"},
        )
        self.assert_rejected(check, "settings/options.json", "target of .claude)")

    def test_link_reached_under_two_names(self) -> None:
        """A link inside a linked .claude counts under the .claude name."""
        check = self.propose(
            self.fixture,
            {"settings/options.json": "changed\n"},
            {".claude": "config", "config/AGENTS.md": "../settings"},
            {"settings/options.json": "original\n"},
        )
        self.assert_rejected(check, "settings/options.json", "target of .claude)")

    def test_unresolvable_file_link_names_its_target(self) -> None:
        """Deleting an unresolvable protected file link names its target."""
        check = self.propose(
            self.fixture, {"AGENTS.md": None}, {"AGENTS.md": "../agents.md"}
        )
        self.assert_rejected(check, "AGENTS.md", "AGENTS.md -> ../agents.md")

    def test_directory_a_link_climbs_out_of_must_stay(self) -> None:
        """Emptying a directory a link climbs out of with .. is refused."""
        check = self.propose(
            self.fixture,
            {"scratch/keep.txt": None},
            {"AGENTS.md": "scratch/../instructions.txt"},
            {"scratch/keep.txt": "keep\n", "instructions.txt": "original\n"},
        )
        self.assert_rejected(
            check, "scratch/keep.txt", "AGENTS.md passes through scratch)"
        )

    def test_nul_bearing_link_target_is_refused(self) -> None:
        """A checkout cuts a link target at NUL, so such a base is refused."""
        fixture = self.fixture
        fixture.link_base({}, {"docs/guide.md": "original\n"})
        stored = self.root / "target.bin"
        stored.write_bytes(b"docs/guide.md\0ignored")
        blob = run_git(fixture.upstream, "hash-object", "-w", str(stored)).strip()
        run_git(
            fixture.upstream,
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob},AGENTS.md",
        )
        run_git(fixture.upstream, "commit", "-q", "-m", "Chore: Link with NUL")
        fixture.base_sha = run_git(fixture.upstream, "rev-parse", "HEAD").strip()
        run_git(fixture.agent, "fetch", "-q", "origin")
        run_git(fixture.agent, "reset", "-q", "--hard", "origin/main")
        check = self.propose(fixture, {"docs/guide.md": "changed\n"})
        self.assertEqual(check.verdict, "rejected", check.reasons)
        self.assertIn("NUL", "; ".join(check.reasons))

    def test_submodule_holding_a_target_cannot_be_deleted(self) -> None:
        """Deleting a gitlink that holds a protected link's target is refused."""
        fixture = self.fixture
        fixture.link_base({"AGENTS.md": "vendor/rules/instructions.md"})
        run_git(
            fixture.upstream,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{fixture.base_sha},vendor/rules",
        )
        run_git(fixture.upstream, "commit", "-q", "-m", "Chore: Add submodule")
        fixture.base_sha = run_git(fixture.upstream, "rev-parse", "HEAD").strip()
        run_git(fixture.agent, "fetch", "-q", "origin")
        run_git(fixture.agent, "reset", "-q", "--hard", "origin/main")
        check = self.propose(fixture, {"vendor/rules": None})
        self.assert_rejected(
            check,
            "vendor/rules",
            "holds vendor/rules/instructions.md, target of AGENTS.md)",
        )


class UsageNumberTest(unittest.TestCase):
    """``usage_number`` accepts finite non-negative reals and nothing else."""

    def test_accepts_plain_numbers(self) -> None:
        """Ints and floats pass through as floats."""
        self.assertEqual(model.usage_number(12), 12.0)
        self.assertEqual(model.usage_number(0.5), 0.5)
        self.assertEqual(model.usage_number(0), 0.0)

    def test_rejects_hostile_values(self) -> None:
        """Booleans, negatives, infinities, NaN, strings and absurd sizes are None."""
        for value in (
            True,
            False,
            -1,
            float("inf"),
            float("nan"),
            "7",
            None,
            1e13,
            10**1000,
        ):
            with self.subTest(value=value):
                self.assertIsNone(model.usage_number(value))

    def test_read_usage_survives_hostile_file(self) -> None:
        """A usage.json full of junk leaves the spend fields unset, not an error."""
        with tempfile.TemporaryDirectory() as holder:
            path = Path(holder) / "usage.json"
            path.write_text(
                '{"totalPremiumRequestCost": 1e309, "totalApiDurationMs": true}',
                encoding="utf-8",
            )
            check = model.Check(
                key="k",
                repository="o/r",
                issue=1,
                branch="b",
                base_sha="a" * 40,
                default_branch="main",
            )
            model.read_usage(path, check)
        self.assertIsNone(check.premium_requests)
        self.assertIsNone(check.agent_seconds)


class ReadUsageModelsTest(unittest.TestCase):
    """``read_usage`` records the models a session ran on (issue #240)."""

    def read(self, text: str) -> Any:
        """Read ``text`` as usage.json into a fresh check."""
        check = model.Check(
            key="k",
            repository="o/r",
            issue=1,
            branch="b",
            base_sha="a" * 40,
            default_branch="main",
        )
        with tempfile.TemporaryDirectory() as holder:
            path = Path(holder) / "usage.json"
            path.write_text(text, encoding="utf-8")
            model.read_usage(path, check)
        return check

    def test_fusion_shape_takes_the_top_level_keys(self) -> None:
        """Agent metrics and the current model add nothing to the list."""
        check = self.read(json.dumps(FUSION_USAGE))
        self.assertEqual(check.models_used, ["claude-opus-5.5", "gpt-6-luna"])
        self.assertEqual(check.models_dropped, 0)
        self.assertEqual(check.premium_requests, 3.0)

    def test_absent_or_unreadable_metrics_leave_the_list_empty(self) -> None:
        """No modelMetrics, a non-object one or a corrupt file: nothing listed."""
        for text in (
            "{}",
            '{"modelMetrics": ["gpt-6-luna"]}',
            '{"modelMetrics": "gpt-6-luna"}',
            "{nope",
        ):
            with self.subTest(text=text):
                check = self.read(text)
                self.assertEqual(check.models_used, [])
                self.assertEqual(check.models_dropped, 0)

    def test_non_object_values_and_bad_ids_are_dropped(self) -> None:
        """Only an id of the pattern's shape with an object value stays."""
        metrics: dict[str, Any] = {
            "gpt-6-luna": {},
            "claude-opus-5.5": 3,
            "Claude-Opus": {},
            "-lead": {},
            "x" * 65: {},
            "a`b": {},
            "a|b": {},
            "a\nb": {},
            "x" * 64: None,
        }
        check = self.read(json.dumps({"modelMetrics": metrics}))
        self.assertEqual(check.models_used, ["gpt-6-luna"])
        self.assertEqual(check.models_dropped, len(metrics) - 1)

    def test_at_most_ten_sorted(self) -> None:
        """Past the bound the sorted list stops; the rest are counted."""
        metrics: dict[str, Any] = {f"m-{n:02d}": {} for n in range(12, 0, -1)}
        check = self.read(json.dumps({"modelMetrics": metrics}))
        self.assertEqual(check.models_used, [f"m-{n:02d}" for n in range(1, 11)])
        self.assertEqual(check.models_dropped, 2)

    def test_coauthors_name_copilot_for_fusion(self) -> None:
        """The committed map gives a Fusion run the Copilot trailer."""
        coauthors = json.loads(REAL_COAUTHORS.read_text(encoding="utf-8"))
        identity = checks.selection_identity(
            {"model": "fusion", "bot": {"login": BOT_LOGIN, "email": BOT_EMAIL}},
            coauthors,
        )
        self.assertEqual(identity.coauthor, COPILOT_TRAILER.split(": ", 1)[1])


class CheckSummaryTest(unittest.TestCase):
    """``check_summary`` renders the verdict and commit table."""

    def test_render(self) -> None:
        """Heading, reasons, one row per commit and the workflows line."""
        check = model.Check(
            key=KEY,
            repository=REPOSITORY,
            issue=ISSUE,
            branch=BRANCH,
            base_sha="a" * 40,
            default_branch="main",
            verdict="proposed",
            reasons=["usage.json unreadable: x"],
            needs_workflows=True,
            commits=[
                {
                    "sha": "b" * 40,
                    "headline": "Fix: A | B",
                    "body": "",
                    "additions": [{"path": "a", "blob": "c" * 40, "size": 10}],
                    "deletions": ["old"],
                }
            ],
        )
        summary = model.check_summary(check)
        self.assertIn(f"### {REPOSITORY}#{ISSUE} — `proposed`", summary)
        self.assertIn("- usage.json unreadable: x", summary)
        self.assertIn("| Fix: A \\| B | 2 | 10 |", summary)
        self.assertIn("Needs `workflows: write`: True", summary)
        self.assertIn("Changes `.pre-commit-config.yaml`: False", summary)

    def test_rejected_without_commits(self) -> None:
        """A rejection lists reasons and no table."""
        check = model.Check(
            key=KEY,
            repository=REPOSITORY,
            issue=ISSUE,
            branch=BRANCH,
            base_sha="a" * 40,
            default_branch="main",
            verdict="rejected",
            reasons=["nope"],
        )
        summary = model.check_summary(check)
        self.assertIn("`rejected`", summary)
        self.assertIn("- nope", summary)
        self.assertNotIn("| Commit |", summary)


class SelectionEntryTest(unittest.TestCase):
    """``selection_entry`` validates the trusted entry for a key."""

    @staticmethod
    def selection(**overrides: Any) -> dict[str, Any]:
        """A selection with one entry, overridable."""
        entry: dict[str, Any] = {
            "key": KEY,
            "repository": REPOSITORY,
            "number": ISSUE,
            "base_sha": "a" * 40,
            "branch": BRANCH,
            "default_branch": "main",
            "url": ISSUE_URL,
            "fork_org": "lfreleng-bot-forks",
            "fork_repository": "lfreleng-bot-forks/repo",
        }
        entry.update(overrides)
        return {"issues": [entry]}

    def test_found(self) -> None:
        """The matching entry is returned intact."""
        selection = self.selection()
        self.assertIs(checks.selection_entry(selection, KEY), selection["issues"][0])

    def test_bad_key(self) -> None:
        """A key with path characters is refused before lookup."""
        for bad in ("../x", "a/b", "", "a b"):
            with self.subTest(bad=bad), self.assertRaises(policy.PublishError):
                checks.selection_entry(self.selection(), bad)

    def test_missing_key(self) -> None:
        """No entry for the key is an error."""
        with self.assertRaisesRegex(policy.PublishError, "no entry"):
            checks.selection_entry(self.selection(), "other-1")

    def test_no_issue_list(self) -> None:
        """A selection without an issue list is an error."""
        with self.assertRaises(policy.PublishError):
            checks.selection_entry({"issues": "nope"}, KEY)

    def test_bad_base_sha(self) -> None:
        """A base_sha that is not 40 hex characters is refused."""
        for bad in ("abc", "A" * 40, "g" * 40):
            with self.subTest(bad=bad), self.assertRaises(policy.PublishError):
                checks.selection_entry(self.selection(base_sha=bad), KEY)

    def test_missing_fields(self) -> None:
        """Each required string or the number missing is an error.

        The field checks reuse ``bot_github.require_*`` so a malformed
        local file surfaces as ``GitHubError``; ``publish.main`` catches
        both, so the behaviour is pinned rather than judged here.
        """
        for name in (
            "repository",
            "branch",
            "default_branch",
            "url",
            "number",
            "fork_org",
            "fork_repository",
        ):
            selection = self.selection()
            del selection["issues"][0][name]
            with (
                self.subTest(name=name),
                self.assertRaises((policy.PublishError, github.GitHubError)),
            ):
                checks.selection_entry(selection, KEY)

    def test_fork_must_be_a_bot_pool(self) -> None:
        """A fork organisation outside the pattern, or a stray fork repo, is refused."""
        for overrides in (
            {"fork_org": "evil-org", "fork_repository": "evil-org/repo"},
            {"fork_repository": "other/repo"},
            {"fork_org": REPOSITORY.partition("/")[0]},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(policy.PublishError),
            ):
                checks.selection_entry(self.selection(**overrides), KEY)


class SelectionIdentityTest(unittest.TestCase):
    """``selection_identity`` resolves trailers from the selection."""

    def test_identity(self) -> None:
        """Model prefix and bot fields build the identity."""
        identity = checks.selection_identity(
            {
                "model": "claude-opus-5.5",
                "bot": {"login": BOT_LOGIN, "email": BOT_EMAIL},
            },
            COAUTHORS,
        )
        self.assertEqual(identity.coauthor, "Claude <noreply@anthropic.com>")
        self.assertEqual(identity.sign_off, SIGN_OFF)

    def test_missing_bot(self) -> None:
        """A selection without a bot object is an error."""
        with self.assertRaises(policy.PublishError):
            checks.selection_identity({"model": "claude-opus-5.5"}, COAUTHORS)


class LoadJsonTest(unittest.TestCase):
    """``load_json`` accepts objects only."""

    def test_errors(self) -> None:
        """Missing, malformed and non-object files are PublishErrors."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            with self.assertRaises(policy.PublishError):
                model.load_json(path, "ctx")
            path.write_text("[1]", encoding="utf-8")
            with self.assertRaises(policy.PublishError):
                model.load_json(path, "ctx")
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(policy.PublishError, "ctx: unreadable JSON"):
                model.load_json(path, "ctx")
            path.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(model.load_json(path, "ctx"), {"a": 1})


class RevisionCheckTest(GitCase):
    """A revision verifies the whole chain on either recorded base."""

    def setUp(self) -> None:
        """A pull request head on the original base, and an advanced upstream."""
        super().setUp()
        self.head = self.fixture.commit(
            "Fix(readme): Correct typo", {"README.md": README.replace("typo", "fix")}
        )
        self.tip = self.fixture.advance_upstream()

    def run_revision(self, **extra: Any) -> Any:
        """Run the check with a revision selection entry, plus ``extra`` fields."""
        selection = self.fixture.selection(
            **{**self.fixture.revision_entry(self.tip, self.head), **extra}
        )
        zizmor = stub.StubZizmor(self.root)
        with (
            patch.object(
                checks, "prepare_clone", local_prepare_clone(self.fixture.upstream)
            ),
            patch.dict(os.environ, {"PATH": zizmor.path()}),
        ):
            return checks.run_check(
                selection_path=self.fixture.write_selection(selection),
                key=KEY,
                proposal_dir=self.fixture.proposal,
                workdir=self.fixture.workdir,
                coauthors_path=self.fixture.write_coauthors(),
                run_url=RUN_URL,
                zizmor_token="t0ken",
            )

    def test_chain_on_the_original_base(self) -> None:
        """An amended chain on the pull request's own base is accepted."""
        self.fixture.commit(
            "Fix(readme): Correct typo", {"README.md": "# Repo\n\nfixed\n"}
        )
        self.fixture.bundle()
        self.fixture.revision_manifest()
        check = self.run_revision()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(check.task, "revise")
        self.assertEqual(check.base_sha, self.fixture.base_sha)
        self.assertFalse(check.rebased)
        self.assertEqual(check.head_sha, self.head)
        self.assertEqual(check.pull_number, 41)
        self.assertEqual(check.round, 2)
        self.assertEqual(len(check.commits), 2)
        (response,) = check.responses
        self.assertEqual(response["thread_id"], "PRRT_1")
        self.assertEqual(response["comment_id"], 100)
        self.assertIs(response["resolve"], True)
        # Mentions in agent text are defused before any human reads them.
        self.assertNotIn("@someone", response["reply"])
        self.assertIn("someone", response["reply"])
        self.assertEqual(check.round_summary, "Fixed the typo the reviewer found.")
        self.assertTrue(check.pr_body.startswith(f"Closes #{ISSUE}\n"))
        # No failing check on the head, so the round note names none.
        self.assertNotIn("failing checks", check.pr_body)
        self.assertIn("Round 2", check.pr_body)

    def test_each_reply_ends_with_the_round_marker(self) -> None:
        """The marker is what a rerun looks for before it posts a reply again."""
        self.fixture.commit(
            "Fix(readme): Correct typo", {"README.md": "# Repo\n\nfixed\n"}
        )
        self.fixture.bundle()
        self.fixture.revision_manifest()
        (response,) = self.run_revision().responses
        # The round and the session, the run id, name the reply: a later
        # session at the same round is another reply. ``seen`` is when the
        # selection read the thread: the reply answers no word after it.
        self.assertTrue(
            response["reply"].endswith(
                "\n\n<!-- code-monkey reply round=2 session=39115306869"
                " seen=2026-09-18T00:00:00Z -->"
            ),
            response["reply"],
        )

    def test_chain_rebased_onto_the_default_branch_head(self) -> None:
        """A chain on the recorded rebase commit is accepted and marked rebased."""
        run_git(self.fixture.agent, "fetch", "-q", str(self.fixture.upstream), "main")
        run_git(self.fixture.agent, "rebase", "-q", "FETCH_HEAD")
        self.fixture.bundle(self.tip)
        self.fixture.revision_manifest(base_sha=self.tip)
        check = self.run_revision()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(check.base_sha, self.tip)
        self.assertTrue(check.rebased)

    def test_any_other_base_is_rejected(self) -> None:
        """A chain on neither recorded commit is not this round's work."""
        self.fixture.bundle()
        self.fixture.revision_manifest(base_sha="e" * 40)
        check = self.run_revision()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(any("base_sha" in r for r in check.reasons))

    def test_responses_must_name_the_packets_threads(self) -> None:
        """A reply to a thread the selection never carried is a rejection."""
        self.fixture.bundle()
        self.fixture.revision_manifest(
            responses=[
                {
                    "thread_id": "PRRT_9",
                    "comment_id": 100,
                    "reply": "x",
                    "resolve": True,
                }
            ]
        )
        check = self.run_revision()
        self.assertEqual(check.verdict, "rejected")
        self.assertTrue(any("PRRT_9" in r for r in check.reasons))
        self.fixture.revision_manifest(
            responses=[
                {
                    "thread_id": "PRRT_1",
                    "comment_id": 100,
                    "reply": "x",
                    "resolve": "yes",
                }
            ]
        )
        self.assertEqual(self.run_revision().verdict, "rejected")

    def test_round_summary_required_and_bounded(self) -> None:
        """The summary is the issue comment: required, and cut to the reason cap."""
        self.fixture.bundle()
        self.fixture.revision_manifest(round_summary="")
        self.assertEqual(self.run_revision().verdict, "rejected")
        self.fixture.revision_manifest(round_summary="x" * 5000)
        check = self.run_revision()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertLessEqual(len(check.round_summary), 2000)

    def test_a_stood_down_abstention_is_superseded(self) -> None:
        """The stand-down's ``superseded: true`` reaches check.json (issue #154)."""
        self.fixture.manifest(outcome="abstain", reason="moved", superseded=True)
        check = self.run_revision()
        self.assertEqual(check.verdict, "abstain", check.reasons)
        self.assertIs(check.superseded, True)
        self.assertIs(check.to_json()["superseded"], True)

    def test_only_a_revision_abstention_is_superseded(self) -> None:
        """Any other outcome, or a value but ``true``, leaves the round counted."""
        for outcome, value in (
            ("abstain", "true"),
            ("abstain", 1),
            ("author-failed", True),
        ):
            with self.subTest(outcome=outcome, value=value):
                self.fixture.manifest(outcome=outcome, reason="r", superseded=value)
                self.assertIs(self.run_revision().superseded, False)
        self.fixture.commit(
            "Fix(readme): Correct typo", {"README.md": "# Repo\n\nfixed\n"}
        )
        self.fixture.bundle()
        self.fixture.revision_manifest(superseded=True)
        check = self.run_revision()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertIs(check.superseded, False)

    def test_handoff_outcome_replies_without_a_chain(self) -> None:
        """Disputing every finding is a handoff: replies validated, nothing to replay."""
        self.fixture.revision_manifest(
            outcome="handoff",
            reason="Both findings are wrong about the cause; a human should decide.",
            responses=[
                {
                    "thread_id": "PRRT_1",
                    "comment_id": 100,
                    "reply": "Disputed: ...",
                    "resolve": False,
                }
            ],
        )
        check = self.run_revision()
        self.assertEqual(check.verdict, "handoff", check.reasons)
        self.assertEqual(check.commits, [])
        (response,) = check.responses
        self.assertIs(response["resolve"], False)
        self.assertIn("a human should decide", check.reasons[0])
        self.assertEqual(check.round_summary, "Fixed the typo the reviewer found.")

    def test_handoff_may_resolve_nothing(self) -> None:
        """A handoff that resolves a thread is a contradiction."""
        self.fixture.revision_manifest(
            outcome="handoff",
            reason="r",
            responses=[
                {
                    "thread_id": "PRRT_1",
                    "comment_id": 100,
                    "reply": "x",
                    "resolve": True,
                }
            ],
        )
        self.assertEqual(self.run_revision().verdict, "rejected")
        # And an issue session cannot hand off; it abstains.
        self.fixture.manifest(outcome="handoff", reason="r")
        self.assertEqual(self.fixture.run().verdict, "rejected")

    def test_entry_records_when_its_comments_were_read(self) -> None:
        """The check carries the selection's read time for the round footer."""
        self.fixture.bundle()
        self.fixture.revision_manifest()
        check = self.run_revision()
        self.assertEqual(check.collected_at, "2026-09-18T00:00:00Z")

    def test_round_note_names_the_failing_checks(self) -> None:
        """The description says which failing checks the round set out to fix."""
        self.fixture.bundle()
        self.fixture.revision_manifest()
        check = self.run_revision(
            checks=[
                {"name": "Test local GitHub Action", "conclusion": "failure"},
                {"name": "<b>@team</b>", "conclusion": "timed_out"},
            ]
        )
        self.assertEqual(
            check.failing_checks, ["Test local GitHub Action", "<b>@team</b>"]
        )
        self.assertIn(
            "It set out to fix the failing checks: Test local GitHub Action, "
            "&lt;b&gt;@\u200bteam&lt;/b&gt;.",
            check.pr_body,
        )
        self.assertEqual(check.to_json()["failing_checks"], check.failing_checks)

    def test_round_note_leaves_infrastructure_failures_out(self) -> None:
        """A run the egress agent failed is not one the round set out to fix."""
        self.fixture.bundle()
        self.fixture.revision_manifest()
        check = self.run_revision(
            checks=[
                {"name": "Agent", "conclusion": "failure", "kind": "infrastructure"},
                {"name": "Test", "conclusion": "failure", "kind": "defect"},
            ]
        )
        self.assertEqual(check.failing_checks, ["Test"])

    def test_round_note_lists_inherited_failures_apart(self) -> None:
        """A failure main fails too is named apart, with main's head (#258)."""
        self.fixture.bundle()
        self.fixture.revision_manifest()
        main = "d" * 40
        check = self.run_revision(
            checks=[
                {"name": "Lint", "conclusion": "failure", "inherited": True},
                {"name": "Test", "conclusion": "failure", "inherited": False},
            ],
            checks_default_sha=main,
        )
        self.assertEqual(check.failing_checks, ["Test"])
        self.assertEqual(check.inherited_checks, ["Lint"])
        self.assertEqual(check.to_json()["inherited_sha"], main)
        self.assertIn(
            "It set out to fix the failing checks: Test. Failing checks "
            "inherited from `main` at dddddddddddd, not this pull request's: Lint.",
            check.pr_body,
        )

    def test_round_note_names_checks_still_running_at_selection(self) -> None:
        """The description says how many checks had not finished at selection."""
        self.fixture.bundle()
        self.fixture.revision_manifest()
        check = self.run_revision(checks_unsettled=2)
        self.assertEqual(check.checks_unsettled, 2)
        self.assertEqual(check.to_json()["checks_unsettled"], 2)
        self.assertIn("2 check(s) still running at selection.", check.pr_body)
        (entry,) = self.fixture.selection(
            **self.fixture.revision_entry(self.tip, self.head)
        )["issues"]
        for value in (0, -1, True, "2", 10**9, None):
            quiet = model.Check.from_entry(KEY, {**entry, "checks_unsettled": value})
            self.assertEqual(quiet.checks_unsettled, 0)
            self.assertNotIn("still running", revision_check.round_note(quiet))

    def test_unanswered_thread_is_a_warning_not_a_rejection(self) -> None:
        """A thread the agent left alone stays open for a human; the check says so."""
        self.fixture.bundle()
        self.fixture.revision_manifest(responses=[])
        check = self.run_revision()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(check.responses, [])
        self.assertTrue(any("1 thread" in w for w in check.warnings))


if __name__ == "__main__":
    unittest.main()
