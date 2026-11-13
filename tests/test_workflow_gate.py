# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which workflow files a proposal may change (DESIGN.md sections 8 and 20.3).

A new proposal may add, edit or delete a file under
``.github/workflows/`` only when the trusted selection sets
``allow_workflow_edits``. A revision round may change only the workflow
files its pull request already changes, whatever the switch says. Each
test builds real repositories with the ``test_proposal_check`` fixture
and runs ``proposal_check.run_check`` against them; the pull request's
head comes from the agent clone, which stands in for the bot fork.
"""

from __future__ import annotations

import os
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_proposal_check import (  # noqa: E402
    KEY,
    README,
    RUN_URL,
    Fixture,
    GitCase,
    local_prepare_clone,
    run_git,
)

checks = import_module("proposal_check")
stub = import_module("zizmor_stub")

CHANGED = ".github/workflows/ci.yaml"
OTHER = ".github/workflows/other.yaml"
ADDED = ".github/workflows/new.yaml"
WORKFLOW = "on: push\njobs: {}\n"
EDITED = "on: [push, pull_request]\njobs: {}\n"
FORK = "lfreleng-bot-forks/repo"
GLOB = ".github/workflows/ci*.yaml"
SIBLING = ".github/workflows/ci-other.yaml"
JOBS = (
    b"on: push\n"
    b"jobs:\n"
    b"  a:\n"
    b"    permissions:\n"
    b"      contents: read\n"
    b"    runs-on: ubuntu-latest\n"
    b"  b:\n"
    b"    permissions:\n"
    b"      contents: read\n"
    b"    runs-on: ubuntu-latest\n"
)


def seed_workflows(fixture: Any) -> None:
    """Make the base hold two workflow files, and the agent branch start there."""
    for path in (CHANGED, OTHER):
        target = fixture.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(WORKFLOW, encoding="utf-8")
    run_git(fixture.upstream, "add", "-A")
    run_git(fixture.upstream, "commit", "-q", "-m", "CI: Add workflows")
    fixture.base_sha = run_git(fixture.upstream, "rev-parse", "HEAD").strip()
    run_git(fixture.agent, "fetch", "-q", str(fixture.upstream), "main")
    run_git(fixture.agent, "reset", "-q", "--hard", "FETCH_HEAD")


def run_check(
    fixture: Any,
    root: Path,
    *,
    entry: dict[str, Any] | None = None,
    switch: Any = None,
) -> Any:
    """Run the check with the clone on the local upstream.

    ``switch``, when given, is written as the selection's
    ``allow_workflow_edits`` value verbatim, whatever its type.
    """
    selection = fixture.selection(**(entry or {}))
    if switch is not None:
        selection["allow_workflow_edits"] = switch
    zizmor = stub.StubZizmor(root)
    with (
        patch.object(
            checks,
            "prepare_clone",
            local_prepare_clone(fixture.upstream),
        ),
        patch.dict(os.environ, {"PATH": zizmor.path()}),
    ):
        return checks.run_check(
            selection_path=fixture.write_selection(selection),
            key=KEY,
            proposal_dir=fixture.proposal,
            workdir=fixture.workdir,
            coauthors_path=fixture.write_coauthors(),
            run_url=RUN_URL,
            zizmor_token="t0ken",
        )


class NewProposalSwitchTest(GitCase):
    """``allow_workflow_edits`` decides whether a new proposal may touch workflows."""

    def setUp(self) -> None:
        """A base that already holds workflow files."""
        super().setUp()
        seed_workflows(self.fixture)

    def propose(self, files: dict[str, str | None]) -> Any:
        """One commit changing ``files``, bundled and checked."""
        self.fixture.commit("CI: Change workflows\n\nBody.\n", files)
        self.fixture.bundle()
        self.fixture.manifest(pr_title="CI: Change workflows")
        return run_check(self.fixture, self.root)

    @unittest.expectedFailure
    def test_switch_absent_rejects_an_addition(self) -> None:
        """A selection without the switch keeps it off: the default is false."""
        check = self.propose({ADDED: WORKFLOW})
        self.assertEqual(check.verdict, "rejected")
        self.assertEqual(check.commits, [])
        (reason,) = check.reasons
        self.assertIn(ADDED, reason)
        self.assertIn("allow_workflow_edits", reason)
        self.assertTrue(check.needs_workflows)

    @unittest.expectedFailure
    def test_switch_off_rejects_an_edit(self) -> None:
        """An edit to an existing workflow file is a workflow edit."""
        self.fixture.allow_workflow_edits = False
        check = self.propose({CHANGED: EDITED, "README.md": "fixed\n"})
        self.assertEqual(check.verdict, "rejected")
        (reason,) = check.reasons
        self.assertIn(CHANGED, reason)
        self.assertNotIn("README.md", reason)

    @unittest.expectedFailure
    def test_switch_off_rejects_a_deletion(self) -> None:
        """Deleting a workflow file is gated as much as writing one."""
        self.fixture.allow_workflow_edits = False
        check = self.propose({OTHER: None})
        self.assertEqual(check.verdict, "rejected")
        (reason,) = check.reasons
        self.assertIn(OTHER, reason)

    @unittest.expectedFailure
    def test_only_a_boolean_true_turns_the_switch_on(self) -> None:
        """A string or a number in the selection is not the switch."""
        for index, value in enumerate(("true", 1)):
            with self.subTest(value=value):
                root = self.root / f"case-{index}"
                root.mkdir()
                fixture = Fixture(root)
                seed_workflows(fixture)
                fixture.commit("CI: Add a workflow\n\nBody.\n", {ADDED: WORKFLOW})
                fixture.bundle()
                fixture.manifest(pr_title="CI: Add a workflow")
                check = run_check(fixture, root, switch=value)
                self.assertEqual(check.verdict, "rejected")

    def test_switch_on_accepts_additions_edits_and_deletions(self) -> None:
        """With the switch on, workflow changes pass as they always did."""
        self.fixture.allow_workflow_edits = True
        check = self.propose({ADDED: WORKFLOW, CHANGED: EDITED, OTHER: None})
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.needs_workflows)

    def test_switch_off_leaves_other_changes_alone(self) -> None:
        """A proposal touching no workflow file is unaffected by the switch."""
        self.fixture.allow_workflow_edits = False
        check = self.propose({"README.md": README.replace("typo", "fix")})
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertFalse(check.needs_workflows)


class RevisionWorkflowTest(GitCase):
    """A round may change only the workflow files its pull request changes."""

    def setUp(self) -> None:
        """A pull request whose head edits ``CHANGED`` and the readme, not ``OTHER``."""
        super().setUp()
        seed_workflows(self.fixture)
        self.head = self.fixture.commit(
            "CI: Run on pull requests",
            {CHANGED: EDITED, "README.md": README.replace("typo", "fix")},
        )
        self.tip = self.fixture.advance_upstream()

    def revise(
        self, files: dict[str, str | None], *, base: str | None = None
    ) -> tuple[Any, MagicMock]:
        """Add a round's commit changing ``files``; check it with the fork stubbed."""
        self.fixture.commit("CI: Answer the review\n\nBody.\n", files)
        self.fixture.bundle(base)
        self.fixture.revision_manifest(
            pr_title="CI: Run on pull requests", base_sha=base or self.fixture.base_sha
        )
        fork = MagicMock(return_value=str(self.fixture.agent))
        with patch("workflow_gate.fork_url", fork):
            check = run_check(
                self.fixture,
                self.root,
                entry=self.fixture.revision_entry(self.tip, self.head),
            )
        return check, fork

    @unittest.expectedFailure
    def test_edit_to_a_workflow_the_pull_request_changes_passes(self) -> None:
        """The pull request already changes the file; the round may too."""
        check, fork = self.revise({CHANGED: "on: pull_request\njobs: {}\n"})
        self.assertEqual(check.verdict, "proposed", check.reasons)
        fork.assert_called_with(FORK)

    @unittest.expectedFailure
    def test_deleting_a_workflow_the_pull_request_changes_passes(self) -> None:
        """Deleting a file the pull request already changes is within it."""
        check, _fork = self.revise({CHANGED: None})
        self.assertEqual(check.verdict, "proposed", check.reasons)

    @unittest.expectedFailure
    def test_edit_to_another_workflow_is_rejected(self) -> None:
        """A file the pull request leaves alone is outside the round's reach."""
        check, _fork = self.revise({OTHER: EDITED})
        self.assertEqual(check.verdict, "rejected")
        self.assertEqual(check.commits, [])
        (reason,) = check.reasons
        self.assertIn(OTHER, reason)
        self.assertNotIn(CHANGED, reason)

    @unittest.expectedFailure
    def test_deleting_another_workflow_is_rejected(self) -> None:
        """A deletion widens the pull request as much as an edit."""
        check, _fork = self.revise({OTHER: None})
        self.assertEqual(check.verdict, "rejected")
        (reason,) = check.reasons
        self.assertIn(OTHER, reason)

    @unittest.expectedFailure
    def test_adding_a_workflow_is_rejected(self) -> None:
        """A new workflow file is one the pull request does not change."""
        check, _fork = self.revise({ADDED: WORKFLOW})
        self.assertEqual(check.verdict, "rejected")
        (reason,) = check.reasons
        self.assertIn(ADDED, reason)

    @unittest.expectedFailure
    def test_the_pull_request_is_read_once_per_round(self) -> None:
        """Several workflow paths share one fetch and diff of the pull request."""
        with patch(
            "workflow_gate.pull_request_workflows",
            wraps=import_module("workflow_gate").pull_request_workflows,
        ) as spy:
            check, _fork = self.revise({OTHER: EDITED, ADDED: WORKFLOW})
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])
        self.assertIn(ADDED, check.reasons[0])
        spy.assert_called_once()

    @unittest.expectedFailure
    def test_the_switch_does_not_widen_a_round(self) -> None:
        """``allow_workflow_edits`` governs new proposals, never a round."""
        self.fixture.allow_workflow_edits = True
        check, _fork = self.revise({OTHER: EDITED})
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_the_switch_off_does_not_narrow_a_round(self) -> None:
        """A round within its pull request's workflows passes with the switch off."""
        self.fixture.allow_workflow_edits = False
        check, _fork = self.revise({CHANGED: "on: pull_request\njobs: {}\n"})
        self.assertEqual(check.verdict, "proposed", check.reasons)

    @unittest.expectedFailure
    def test_upstream_workflow_changes_a_rebase_brings_do_not_count(self) -> None:
        """Only the round's own changes are tested, not those of the new base.

        The default branch edits ``OTHER`` after the pull request was
        cut; the round rebases onto it and edits ``CHANGED`` alone.
        """
        (self.fixture.upstream / OTHER).write_text(EDITED, encoding="utf-8")
        run_git(self.fixture.upstream, "commit", "-q", "-am", "CI: Edit upstream")
        self.tip = run_git(self.fixture.upstream, "rev-parse", "HEAD").strip()
        run_git(self.fixture.agent, "fetch", "-q", str(self.fixture.upstream), "main")
        run_git(self.fixture.agent, "rebase", "-q", "FETCH_HEAD")
        check, fork = self.revise(
            {CHANGED: "on: pull_request\njobs: {}\n"}, base=self.tip
        )
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.rebased)
        fork.assert_called_with(FORK)

    def test_a_round_touching_no_workflow_reads_no_fork(self) -> None:
        """No workflow path in the chain: nothing to compare, nothing fetched.

        The fork is left unpatched, so a fetch would fail the check.
        """
        run_git(self.fixture.agent, "reset", "-q", "--hard", self.fixture.base_sha)
        self.head = self.fixture.commit(
            "Fix(readme): Correct typo", {"README.md": README.replace("typo", "fix")}
        )
        self.fixture.commit(
            "Fix(readme): Answer review\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.revision_manifest(pr_title="Fix(readme): Correct typo")
        check = run_check(
            self.fixture,
            self.root,
            entry=self.fixture.revision_entry(self.tip, self.head),
        )
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertFalse(check.needs_workflows)


class InheritedWorkflowTest(GitCase):
    """Workflow writes a round carries over from its pull request's commits.

    The pull request edits ``OTHER``, reverts the edit and then fixes
    the readme, so its net diff leaves ``OTHER`` alone while two of
    its commits touch it.
    """

    def setUp(self) -> None:
        """Publish the edit, its revert and the readme fix."""
        super().setUp()
        seed_workflows(self.fixture)
        self.fixture.commit("CI: Try the other workflow", {OTHER: EDITED})
        self.fixture.commit("Revert: Restore the other workflow", {OTHER: WORKFLOW})
        self.head = self.fixture.commit(
            "Fix(readme): Correct typo", {"README.md": README.replace("typo", "fix")}
        )
        self.tip = self.fixture.advance_upstream()

    def revise(self, *, base: str | None = None) -> Any:
        """Add a readme-only round's commit and check the chain."""
        self.fixture.commit(
            "Fix(readme): Answer review\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle(base)
        self.fixture.revision_manifest(
            pr_title="Fix(readme): Correct typo", base_sha=base or self.fixture.base_sha
        )
        with patch("workflow_gate.fork_url", return_value=str(self.fixture.agent)):
            return run_check(
                self.fixture,
                self.root,
                entry=self.fixture.revision_entry(self.tip, self.head),
            )

    def rewrite(self, files: list[tuple[str, dict[str, str | None]]]) -> None:
        """Replace the branch with ``files``' commits on the base."""
        run_git(self.fixture.agent, "reset", "-q", "--hard", self.fixture.base_sha)
        for message, change in files:
            self.fixture.commit(message, change)

    @unittest.expectedFailure
    def test_preserved_edit_and_revert_pass(self) -> None:
        """A readme-only round keeping the pull request's commits passes."""
        check = self.revise()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.needs_workflows)

    @unittest.expectedFailure
    def test_preserved_edit_and_revert_pass_after_a_rebase(self) -> None:
        """Carried-over writes match whichever base the chain sits on."""
        run_git(self.fixture.agent, "fetch", "-q", str(self.fixture.upstream), "main")
        run_git(self.fixture.agent, "rebase", "-q", "FETCH_HEAD")
        check = self.revise(base=self.tip)
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.rebased)

    @unittest.expectedFailure
    def test_carried_writes_pass_after_an_upstream_edit_to_the_file(self) -> None:
        """A rebase over an upstream edit elsewhere in the file still passes.

        The default branch appends a line to ``OTHER``; the rebased
        edit and revert then write blobs the published commits never
        held, but each makes the same line change.
        """
        (self.fixture.upstream / OTHER).write_text(
            WORKFLOW + "# upstream\n", encoding="utf-8"
        )
        run_git(self.fixture.upstream, "commit", "-q", "-am", "CI: Edit upstream")
        self.tip = run_git(self.fixture.upstream, "rev-parse", "HEAD").strip()
        run_git(self.fixture.agent, "fetch", "-q", str(self.fixture.upstream), "main")
        run_git(self.fixture.agent, "rebase", "-q", "FETCH_HEAD")
        check = self.revise(base=self.tip)
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.rebased)

    @unittest.expectedFailure
    def test_repeating_the_carried_edit_and_revert_is_rejected(self) -> None:
        """A second edit and revert of the same file is the round's own."""
        self.rewrite(
            [
                ("CI: Try the other workflow", {OTHER: EDITED}),
                ("Revert: Restore the other workflow", {OTHER: WORKFLOW}),
                ("CI: Try the other workflow again", {OTHER: EDITED}),
                ("Revert: Restore the other workflow again", {OTHER: WORKFLOW}),
            ]
        )
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_dropping_the_revert_is_rejected(self) -> None:
        """Every write was published, but the chain now changes the file."""
        self.rewrite(
            [
                ("CI: Try the other workflow", {OTHER: EDITED}),
                ("Fix(readme): Correct typo", {"README.md": "fix\n"}),
            ]
        )
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_rewriting_the_carried_edit_is_rejected(self) -> None:
        """A write the published commits never made is the round's own."""
        self.rewrite(
            [
                ("CI: Try the other workflow", {OTHER: "on: workflow_dispatch\n"}),
                ("Revert: Restore the other workflow", {OTHER: WORKFLOW}),
                ("Fix(readme): Correct typo", {"README.md": "fix\n"}),
            ]
        )
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_deleting_and_restoring_another_workflow_is_rejected(self) -> None:
        """A round may not add a delete-and-restore pair of its own."""
        self.rewrite(
            [
                ("CI: Try the other workflow", {OTHER: EDITED}),
                ("Revert: Restore the other workflow", {OTHER: WORKFLOW}),
                ("CI: Drop the workflow", {CHANGED: None}),
                ("Revert: Restore the workflow", {CHANGED: WORKFLOW}),
            ]
        )
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(CHANGED, check.reasons[0])
        self.assertNotIn(OTHER, check.reasons[0])

    def write_other(self, message: str, content: bytes) -> str:
        """Commit ``content`` to ``OTHER`` byte for byte; return the SHA."""
        (self.fixture.agent / OTHER).write_bytes(content)
        run_git(self.fixture.agent, "add", OTHER)
        return self.fixture.commit(message)

    def seed_base(self, files: dict[str, bytes]) -> None:
        """Move the base to a commit holding ``files``; the branch starts there."""
        upstream = self.fixture.upstream
        for path, content in files.items():
            target = upstream / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        run_git(upstream, "add", "-A")
        run_git(upstream, "commit", "-q", "-m", "CI: Seed workflows")
        self.fixture.base_sha = run_git(upstream, "rev-parse", "HEAD").strip()
        (upstream / "CHANGES.md").write_text("seeded\n", encoding="utf-8")
        run_git(upstream, "add", "-A")
        run_git(upstream, "commit", "-q", "-m", "Chore: Advance main")
        self.tip = run_git(upstream, "rev-parse", "HEAD").strip()
        run_git(self.fixture.agent, "fetch", "-q", str(upstream), "main")

    def publish_edit(
        self, published: bytes, chain: bytes, *, original: bytes = WORKFLOW.encode()
    ) -> None:
        """Publish an edit and revert of ``OTHER``; the chain carries ``chain``.

        The pull request's head writes ``published`` and restores the
        file to ``original``; the agent branch is then rebuilt with
        ``chain`` in its place, so the two differ only in the edit's
        bytes.
        """
        for edit in (published, chain):
            run_git(self.fixture.agent, "reset", "-q", "--hard", self.fixture.base_sha)
            self.write_other("CI: Try the other workflow", edit)
            self.write_other("Revert: Restore the other workflow", original)
            tip = self.fixture.commit(
                "Fix(readme): Correct typo",
                {"README.md": README.replace("typo", "fix")},
            )
            if edit is published:
                self.head = tip

    @unittest.expectedFailure
    def test_carried_edit_with_non_utf8_bytes_passes(self) -> None:
        """The very same non-UTF-8 edit and revert, carried over, pass."""
        edit = b"on: push # \xff\njobs: {}\n"
        self.publish_edit(edit, edit)
        check = self.revise()
        self.assertEqual(check.verdict, "proposed", check.reasons)

    @unittest.expectedFailure
    def test_rewriting_a_carried_non_utf8_edit_is_rejected(self) -> None:
        """Bytes that decode to the same replacement text still differ."""
        self.publish_edit(
            b"on: push # \xff\njobs: {}\n", b"on: push # \xfe\njobs: {}\n"
        )
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_carried_edit_with_a_carriage_return_passes(self) -> None:
        """The very same edit holding a lone carriage return passes."""
        edit = b"on: pull_request\r+jobs: {a: 1}\n"
        self.publish_edit(edit, edit)
        check = self.revise()
        self.assertEqual(check.verdict, "proposed", check.reasons)

    @unittest.expectedFailure
    def test_a_carriage_return_inside_a_line_is_not_a_line_break(self) -> None:
        """A one-line edit holding a carriage return is not a two-line edit."""
        self.publish_edit(
            b"on: pull_request\r+jobs: {a: 1}\n", b"on: pull_request\njobs: {a: 1}\n"
        )
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_the_same_edit_in_another_job_is_rejected(self) -> None:
        """A carried change must sit where the published one did.

        The base holds the same permissions line in two jobs. The pull
        request edits and reverts it in the first job; the chain does
        the same in the second, which the pull request never touched.
        """
        self.seed_base({OTHER: JOBS})
        first = JOBS.replace(b"read", b"write", 1)
        second = JOBS[::-1].replace(b"daer", b"etirw", 1)[::-1]
        self.assertNotEqual(first, second)
        self.publish_edit(first, second, original=JOBS)
        check = self.revise()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn(OTHER, check.reasons[0])

    @unittest.expectedFailure
    def test_the_same_edit_in_the_same_job_passes(self) -> None:
        """The published edit and revert of one job's line, carried over, pass."""
        self.seed_base({OTHER: JOBS})
        first = JOBS.replace(b"read", b"write", 1)
        self.publish_edit(first, first, original=JOBS)
        check = self.revise()
        self.assertEqual(check.verdict, "proposed", check.reasons)

    @unittest.expectedFailure
    def test_a_wildcard_in_a_path_names_that_file_alone(self) -> None:
        """A path holding a glob character matches no other workflow file.

        The pull request edits and reverts ``GLOB``, then edits
        ``SIBLING``, which the glob would match. The chain carries the
        edit to ``GLOB`` in a commit that also changes ``SIBLING``, a
        file the pull request changes and the round may change.
        """
        self.seed_base({GLOB: WORKFLOW.encode(), SIBLING: WORKFLOW.encode()})
        agent = self.fixture.agent
        steps = [
            [
                ("CI: Try the glob workflow", {GLOB: EDITED}),
                ("Revert: Restore the glob workflow", {GLOB: WORKFLOW}),
                ("CI: Edit the sibling workflow", {SIBLING: EDITED}),
            ],
            [
                (
                    "CI: Try the glob workflow",
                    {GLOB: EDITED, SIBLING: "on: pull_request\njobs: {}\n"},
                ),
                ("Revert: Restore the glob workflow", {GLOB: WORKFLOW}),
                ("CI: Edit the sibling workflow", {SIBLING: EDITED}),
            ],
        ]
        for index, commits in enumerate(steps):
            run_git(agent, "reset", "-q", "--hard", self.fixture.base_sha)
            for message, files in commits:
                for path, content in files.items():
                    (agent / path).write_text(content, encoding="utf-8")
                    run_git(agent, "add", "--", f":(literal){path}")
                self.fixture.commit(message)
            if index == 0:
                self.head = run_git(agent, "rev-parse", "HEAD").strip()
        check = self.revise()
        self.assertEqual(check.verdict, "proposed", check.reasons)


if __name__ == "__main__":
    unittest.main()
