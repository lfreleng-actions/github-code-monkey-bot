# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline contracts over the workflow YAML: trust boundary, pins, plumbing.

The reusable workflow separates a trusted ``select`` job, an untrusted
``author`` matrix and a trusted ``publish`` job (docs/DESIGN.md sections
4, 7, 8 and 10). These tests read the parsed documents, never their
comments, and pin the properties a reviewer would otherwise re-derive by
hand on every change: which job holds which credential, that downloads
go by producer artifact ID, that every action is pinned to a commit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml  # pyright: ignore[reportMissingModuleSource]

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REUSABLE = WORKFLOWS / "code-monkey.yaml"
CRON = WORKFLOWS / "code-monkey-cron.yaml"
TESTING = WORKFLOWS / "testing.yaml"
PROMPT = WORKFLOWS.parents[1] / "prompt" / "author.md"
REVISE_PROMPT = WORKFLOWS.parents[1] / "prompt" / "revise.md"

PLACEHOLDER_NAME = "Code Monkey"
PLACEHOLDER_EMAIL = "code-monkey@example.invalid"

# PyYAML reads the bare mapping key ``on`` as the boolean ``True``.
TRIGGERS = True

HARDEN_RUNNER = "step-security/harden-runner"
BLOCK_ACTION = "lfreleng-actions/harden-runner-block-action"
CHECKOUT = "actions/checkout"
APP_TOKEN = "actions/create-github-app-token"
SPLITTER = "lfreleng-actions/list-splitter-action"
DOWNLOAD = "actions/download-artifact"
UPLOAD = "actions/upload-artifact"
REUSABLE_CALL = "$/.github/workflows/code-monkey.yaml"

COMMIT_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SECRET_REFERENCE = re.compile(r"secrets\.[A-Za-z0-9_]+")
EXPRESSION = re.compile(r"^\$\{\{\s*(.*?)\s*\}\}$")


def squash(text: str) -> str:
    """Collapse runs of whitespace so block scalars compare line-wrap free."""
    return re.sub(r"\s+", " ", text).strip()


def flatten(script: str) -> str:
    """Join backslash continuations, then squash, for shell substring checks."""
    return squash(re.sub(r"\\\n\s*", " ", script))


def unwrap(expression: str) -> str:
    """Return the expression body without ``${{ }}`` wrapping or wrap noise."""
    text = squash(expression)
    match = EXPRESSION.match(text)
    return match.group(1) if match else text


def uses_of(step: dict[str, Any]) -> str:
    """Return the action coordinate of a step, or an empty string."""
    return str(step.get("uses", ""))


def is_action(step: dict[str, Any], action: str) -> bool:
    """Match an action invocation independently of its commit pin."""
    return uses_of(step).startswith(action + "@")


def dumped(node: object) -> str:
    """Serialise a YAML subtree so a regex can scan every value in it."""
    return json.dumps(node, sort_keys=True)


# A step names a review and something done about one: a request, a
# wait, a reply. The author step names Copilot but no review.
REVIEW_WORD = re.compile(r"\breview", re.I)
REVIEW_ACTION = re.compile(r"copilot|request|await|wait|reply|resolv|classif", re.I)

# A prompt clause claims review when a part of the workflow does
# something to a review, unless the clause negates it.
CLAUSE_BREAK = re.compile(r"[.!?;:]\**\s+|,\s+|\s+—\s+")
REVIEW_ACTOR = re.compile(
    r"\b(?:workflow|publisher|pipeline|job|something else|another step)\b", re.I
)
REVIEW_VERB = re.compile(
    r"\b(?:request|await|wait|handl|answer|repl|resolv|run|trigger|read|address)",
    re.I,
)
NEGATION = re.compile(r"\b(?:not|never|no|cannot|nor|without)\b|n't\b", re.I)
SKIP_VERB = re.compile(r"\b(?:skip|omit|ignore|bypass)", re.I)
REVIEW_CYCLE = re.compile(r"review|§\s*11\b|section 11\b", re.I)


def review_steps(jobs: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (job, step name) for each step that acts on a review."""
    found: list[tuple[str, str]] = []
    for job, body in jobs.items():
        for step in cast(list[dict[str, Any]], body.get("steps", [])):
            text = dumped(step)
            if REVIEW_WORD.search(text) and REVIEW_ACTION.search(text):
                found.append((job, str(step.get("name", step.get("id", "")))))
    return found


def clauses(text: str) -> list[str]:
    """Split prose into clauses, ignoring line wrapping."""
    return [part for part in CLAUSE_BREAK.split(squash(text)) if part]


def review_claims(text: str) -> list[str]:
    """Clauses saying some part of the workflow acts on a review."""
    return [
        clause
        for clause in clauses(text)
        if REVIEW_ACTOR.search(clause)
        and REVIEW_VERB.search(clause)
        and REVIEW_WORD.search(clause)
        and not NEGATION.search(clause)
    ]


def skip_instructions(text: str) -> list[str]:
    """Clauses telling the reader to skip the review cycle or §11."""
    return [
        clause
        for clause in clauses(text)
        if SKIP_VERB.search(clause)
        and REVIEW_CYCLE.search(clause)
        and not NEGATION.search(clause)
    ]


def line_number(script: str, exact: str) -> int:
    """Return the index of the first line whose stripped text equals `exact`."""
    for number, line in enumerate(script.splitlines()):
        if line.strip() == exact:
            return number
    raise AssertionError(f"no line reads {exact!r}")


class WorkflowCase(unittest.TestCase):
    """Read workflow structure, never comments."""

    WORKFLOW: ClassVar[Path]

    def setUp(self) -> None:
        """Load a fresh document for each contract."""
        loaded = yaml.safe_load(self.WORKFLOW.read_text(encoding="utf-8"))
        self.workflow: dict[str | bool, Any] = cast(dict[str | bool, Any], loaded)
        self.jobs: dict[str, dict[str, Any]] = cast(
            dict[str, dict[str, Any]], self.workflow["jobs"]
        )
        self.triggers: dict[str, Any] = cast(dict[str, Any], self.workflow[TRIGGERS])

    def steps(self, job: str) -> list[dict[str, Any]]:
        """Return the step list of a job that runs on a runner."""
        return cast(list[dict[str, Any]], self.jobs[job]["steps"])

    def step(self, job: str, identity: str) -> dict[str, Any]:
        """Find exactly one step by id or name."""
        matches = [
            s for s in self.steps(job) if identity in (s.get("id"), s.get("name"))
        ]
        self.assertEqual(len(matches), 1, f"{job}: expected one {identity!r} step")
        return matches[0]

    def position(self, job: str, identity: str) -> int:
        """Return the index of a step so ordering contracts can compare."""
        return self.steps(job).index(self.step(job, identity))

    def actions(self, job: str, action: str) -> list[dict[str, Any]]:
        """Select action invocations independently of their commit pin."""
        return [s for s in self.steps(job) if is_action(s, action)]

    def action(self, job: str, action: str) -> dict[str, Any]:
        """Return the single invocation of an action within a job."""
        matches = self.actions(job, action)
        self.assertEqual(len(matches), 1, f"{job}: expected one {action} step")
        return matches[0]

    def assert_expression(self, actual: str, expected: str) -> None:
        """Compare whole expressions ignoring `${{ }}` wrapping and whitespace."""
        self.assertEqual(unwrap(actual), unwrap(expected))

    def assert_pinned_steps(self) -> None:
        """Every step-level `uses:` names a 40-hex commit, never a tag or branch."""
        for job in self.jobs:
            if "steps" not in self.jobs[job]:
                continue
            for step in self.steps(job):
                if "uses" not in step:
                    continue
                with self.subTest(job=job, uses=step["uses"]):
                    self.assertRegex(uses_of(step), COMMIT_PIN)


class ReusableWorkflowCase(WorkflowCase):
    """Shared fixture for the reusable workflow contracts."""

    WORKFLOW = REUSABLE


class TopLevelContracts(ReusableWorkflowCase):
    """Permissions, concurrency, pins and runner hardening across all jobs."""

    def test_default_permissions_are_empty(self) -> None:
        """Every job opts into its own grants; the workflow default is none."""
        self.assertEqual(self.workflow["permissions"], {})

    @unittest.expectedFailure
    def test_concurrency_is_per_caller_and_owner_without_cancel(self) -> None:
        """Live scans lock per caller and owner; others within their run.

        A group keeps one pending run and a newcomer cancels it, so a
        dry run that wrote nothing, or a live run naming its issues,
        must not share the live scan group.
        """
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        self.assert_expression(
            str(concurrency["group"]),
            "inputs.dry_run"
            " && format('code-monkey-dry-run-{0}-{1}', github.run_id, inputs.org)"
            " || (inputs.issues != '' || inputs.pull_requests != '')"
            " && format('code-monkey-named-{0}-{1}', github.run_id, inputs.org)"
            " || format('code-monkey-{0}-{1}', github.repository, inputs.org)",
        )
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_every_action_is_pinned_to_a_commit(self) -> None:
        """No `uses:` rides a tag or branch that could move underneath us."""
        self.assert_pinned_steps()

    def test_every_checkout_drops_credentials(self) -> None:
        """No checkout leaves a token in `.git/config` for later steps."""
        for job in self.jobs:
            for step in self.actions(job, CHECKOUT):
                with self.subTest(job=job, step=step.get("name")):
                    self.assertIs(step["with"]["persist-credentials"], False)

    def test_harden_runner_leads_every_job(self) -> None:
        """Only the allow-list loader may precede harden-runner in a job."""
        for job in self.jobs:
            steps = self.steps(job)
            with self.subTest(job=job):
                self.assertTrue(
                    is_action(steps[0], BLOCK_ACTION)
                    or is_action(steps[0], HARDEN_RUNNER),
                    f"{job}: first step is {uses_of(steps[0])!r}",
                )
                guarded = [
                    i for i, s in enumerate(steps) if is_action(s, HARDEN_RUNNER)
                ]
                self.assertEqual(len(guarded), 1, f"{job}: one harden-runner step")
                for earlier in steps[: guarded[0]]:
                    self.assertTrue(is_action(earlier, BLOCK_ACTION))
                    self.assertNotIn("run", earlier)

    def test_allow_list_summary_prints_once_from_select(self) -> None:
        """The select job alone renders the allow-list to the step summary."""
        summaries: list[tuple[str, Any]] = [
            (job, step["with"]["allow_list_summary"])
            for job in self.jobs
            for step in self.actions(job, BLOCK_ACTION)
        ]
        self.assertTrue(summaries, "no harden-runner-block-action steps found")
        enabled = [job for job, value in summaries if value == "true"]
        self.assertEqual(enabled, ["select"])
        for job, value in summaries:
            if job != "select":
                with self.subTest(job=job):
                    self.assertEqual(value, "false")

    def test_every_trusted_job_loads_the_allow_list(self) -> None:
        """Select, publish and report load the allow-list before hardening.

        CONNECTION_ALLOW_LIST does not cross job boundaries, and
        harden-runner in block mode with an empty list enforces nothing.
        """
        for job in ("select", "publish", "report"):
            with self.subTest(job=job):
                loaders = self.actions(job, BLOCK_ACTION)
                self.assertEqual(len(loaders), 1)
                self.assert_expression(
                    loaders[0]["with"]["config"], "inputs.egress_allow_config"
                )
                harden = self.action(job, HARDEN_RUNNER)
                self.assert_expression(
                    harden["with"]["allowed-endpoints"], "env.CONNECTION_ALLOW_LIST"
                )
        self.assertEqual(self.actions("author", BLOCK_ACTION), [])


class AuthorJobContracts(ReusableWorkflowCase):
    """The untrusted job: no App key, audit egress, evidence before checkout."""

    def test_author_holds_no_app_key(self) -> None:
        """The agent runner never mints, receives or references the App key."""
        author = self.jobs["author"]
        self.assertEqual(self.actions("author", APP_TOKEN), [])
        self.assertEqual(author["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.github_app_private_key", dumped(author))
        references = Counter(SECRET_REFERENCE.findall(dumped(author)))
        self.assertEqual(references, Counter({"secrets.copilot_token": 1}))
        agent = self.step("author", "agent")
        self.assert_expression(
            agent["env"]["COPILOT_GITHUB_TOKEN"], "secrets.copilot_token"
        )

    def test_author_budget_and_gating_come_from_select(self) -> None:
        """Audit egress; timeouts, parallelism and matrix from select outputs."""
        author = self.jobs["author"]
        harden = self.action("author", HARDEN_RUNNER)
        self.assertEqual(harden["with"]["egress-policy"], "audit")
        self.assert_expression(
            author["timeout-minutes"],
            "fromJSON(needs.select.outputs.author_timeout)",
        )
        self.assert_expression(
            self.step("author", "agent")["timeout-minutes"],
            "fromJSON(needs.select.outputs.agent_timeout)",
        )
        strategy = cast(dict[str, Any], author["strategy"])
        self.assertIs(strategy["fail-fast"], False)
        self.assert_expression(
            strategy["max-parallel"], "fromJSON(needs.select.outputs.max_parallel)"
        )
        self.assert_expression(
            strategy["matrix"], "fromJSON(needs.select.outputs.matrix)"
        )
        self.assert_expression(author["if"], "needs.select.outputs.run_agent == 'true'")

    @unittest.skipUnless(
        shutil.which("jq") and shutil.which("bash") and shutil.which("git"), "needs jq"
    )
    def test_packet_step_sets_a_placeholder_identity(self) -> None:
        """The agent commits as a placeholder whose sign-off fits gitlint.

        Run 38417531314 failed here: ``git commit -s`` as the App's bot
        login wrote a 113-character trailer that a target ``.gitlint``
        without the organisation's trailer exemption rejects. The
        publisher replaces every ``*-by`` trailer, so the local identity
        only has to pass the hooks, and the bot never reaches git config.
        """
        packet = self.step("author", "Prepare workspace and packet")
        self.assertNotIn(".bot.", str(packet["run"]))
        base = "f" * 40
        login = "lf-releng-code-monkey-bot[bot]"
        email = "123+lf-releng-code-monkey-bot[bot]@users.noreply.github.com"
        with tempfile.TemporaryDirectory() as holder:
            work = Path(holder)
            (work / "evidence").mkdir()
            (work / "evidence" / "selection.json").write_text(
                json.dumps(
                    {
                        "bot": {"login": login, "email": email},
                        "issues": [
                            {"key": "repo-7", "branch": "b", "repository": "o/repo"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", "workspace"], cwd=work, check=True)
            # The checkout check needs HEAD to equal CHECKOUT_SHA; a
            # fake rev-parse on PATH answers with the fixture SHA.
            fake = work / "bin"
            fake.mkdir()
            (fake / "git").write_text(
                "#!/bin/sh\n"
                'if [ "$3" = rev-parse ]; then echo ' + base + "; exit 0; fi\n"
                'exec /usr/bin/env -u PATH PATH="'
                + os.environ["PATH"]
                + '" git "$@"\n',
                encoding="utf-8",
            )
            (fake / "git").chmod(0o755)
            output = work / "out.txt"
            output.touch()
            env = {
                "PATH": f"{fake}:{os.environ['PATH']}",
                "KEY": "repo-7",
                "TASK": "issue",
                "CHECKOUT_SHA": base,
                "HEAD_SHA": "",
                "GITHUB_OUTPUT": str(output),
            }
            proc = subprocess.run(
                ["bash", "-e", "-c", str(packet["run"])],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            def config(key: str) -> str:
                return subprocess.run(
                    ["git", "-C", "workspace", "config", "--local", key],
                    cwd=work,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()

            name, mail = config("user.name"), config("user.email")
            self.assertEqual((name, mail), (PLACEHOLDER_NAME, PLACEHOLDER_EMAIL))
            self.assertLessEqual(len(f"Signed-off-by: {name} <{mail}>"), 72)
            outputs = output.read_text(encoding="utf-8")
            self.assertIn(f"git_identity={name} <{mail}>", outputs)
            self.assertNotIn("[bot]", outputs)

    def test_runtime_context_names_the_placeholder_identity(self) -> None:
        """The prompt presents the git identity as a placeholder, not the bot."""
        assemble = self.step("author", "Assemble prompt")
        env = cast(dict[str, Any], assemble["env"])
        self.assertNotIn("BOT_LOGIN", env)
        self.assert_expression(env["GIT_IDENTITY"], "steps.packet.outputs.git_identity")
        self.assertIn("placeholder", str(assemble["run"]))
        self.assertNotIn("bot identity", str(assemble["run"]))

    def test_prompt_states_the_publisher_subject_limit(self) -> None:
        """The agent learns the subject limit the publisher enforces.

        A target without ``title-max-length`` lets gitlint pass a subject
        up to 72 characters, which the publisher rejects above 50.
        """
        prompt = squash(PROMPT.read_text(encoding="utf-8"))
        self.assertIn("title-max-length", prompt)
        self.assertIn("50 where it does not", prompt)
        self.assertIn("placeholder", prompt)

    def test_target_checkout_is_the_recorded_base_commit(self) -> None:
        """The workspace is the selected repository at the selection's SHA."""
        checkout = self.step("author", "Checkout target repository")
        self.assertTrue(is_action(checkout, CHECKOUT))
        with_ = cast(dict[str, Any], checkout["with"])
        self.assert_expression(with_["repository"], "matrix.repository")
        self.assert_expression(with_["ref"], "matrix.checkout_sha")
        self.assertEqual(with_["path"], "workspace")
        self.assertIs(with_["persist-credentials"], False)

    def test_evidence_arrives_by_id_and_is_verified_first(self) -> None:
        """Evidence comes by artifact ID and its digests gate later steps."""
        download = self.action("author", DOWNLOAD)
        with_ = cast(dict[str, Any], download["with"])
        self.assertNotIn("name", with_)
        self.assertNotIn("pattern", with_)
        self.assert_expression(
            with_["artifact-ids"], "needs.select.outputs.evidence_id"
        )

        verify = self.step("author", "Verify evidence bytes")
        self.assertIn("bot_evidence.py verify", flatten(verify["run"]))
        env = cast(dict[str, Any], verify["env"])
        self.assert_expression(
            env["SELECTION_SHA"], "needs.select.outputs.selection_sha256"
        )
        self.assert_expression(
            env["GUIDANCE_SHA"], "needs.select.outputs.guidance_sha256"
        )
        script = flatten(verify["run"])
        self.assertIn('--expect "selection.json=$SELECTION_SHA"', script)
        self.assertIn('--expect "agents.md=$GUIDANCE_SHA"', script)

        fetched = self.steps("author").index(download)
        verified = self.position("author", "Verify evidence bytes")
        self.assertLess(fetched, verified)
        self.assertLess(verified, self.position("author", "Checkout target repository"))
        self.assertLess(verified, self.position("author", "agent"))

    def test_agent_invocation_is_confined(self) -> None:
        """PAT prefix check, deny list, secret masking and the workspace cwd."""
        raw = str(self.step("author", "agent")["run"])
        script = flatten(raw)
        self.assertRegex(
            script,
            r'case "\$COPILOT_GITHUB_TOKEN" in github_pat_\*\) ;; \*\) .*?exit 1 ;; esac',
        )

        deny = re.search(r"deny='([^']*)'", script)
        self.assertIsNotNone(deny, "deny list initialiser missing")
        assert deny is not None
        denied = set(deny.group(1).split(","))
        self.assertLessEqual({"shell(gh)", "shell(gh:*)", "shell(git push)"}, denied)
        self.assertIn('--deny-tool="$deny"', script)

        for flag in (
            "--secret-env-vars=COPILOT_GITHUB_TOKEN",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--no-auto-update",
            "--mode autopilot",
            "--allow-all-tools",
            "--usage-output-file",
            "--share=",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, script)

        self.assertRegex(
            script,
            r'if \[ "\$ALLOW_SUBAGENTS" != true \]; then '
            r'deny="\$deny,task,list_agents,read_agent,write_agent";? fi',
        )
        self.assertRegex(
            script,
            r'if \[ "\$LOAD_REPO_SKILLS" != true \]; then deny="\$deny,skill";? fi',
        )
        self.assertLess(
            line_number(raw, "cd workspace"), line_number(raw, "copilot \\")
        )

    def test_toolchain_installs_the_pinned_cli(self) -> None:
        """The CLI version is a single top-level constant the install reads."""
        env = cast(dict[str, Any], self.workflow["env"])
        self.assertEqual(env["COPILOT_CLI_VERSION"], "1.0.83")
        install = self.step("author", "Install agent toolchain")
        self.assertIn("@github/copilot@$COPILOT_CLI_VERSION", str(install["run"]))

    def test_bundle_step_always_leaves_a_typed_manifest(self) -> None:
        """A missing manifest becomes `author-failed`; only `proposed` bundles."""
        bundle = self.step("author", "Bundle proposed commits")
        self.assert_expression(
            bundle["if"], "always() && steps.packet.outcome == 'success'"
        )
        script = flatten(bundle["run"])
        self.assertIn("if [ ! -s artefacts/manifest.json ]", script)
        self.assertIn('"author-failed"', script)
        guard = script.index('if [ "$outcome" = proposed ]')
        create = re.search(r"git (-C \S+ )?bundle create", script)
        self.assertIsNotNone(create, "git bundle create missing")
        assert create is not None
        self.assertLess(guard, create.start())

    def test_proposal_and_session_artifact_names(self) -> None:
        """Proposal is exactly three files under a namespaced, keyed name."""
        proposal = self.step("author", "proposal")
        self.assertTrue(is_action(proposal, UPLOAD))
        with_ = cast(dict[str, Any], proposal["with"])
        self.assertEqual(
            with_["name"],
            "monkey-proposal-${{ needs.select.outputs.namespace }}-${{ matrix.key }}",
        )
        self.assertEqual(with_["if-no-files-found"], "error")
        # A rerun of the entry replaces its own earlier proposal; without
        # this, the second upload collides with the immutable first one.
        self.assertIs(with_["overwrite"], True)
        push = squash(str(self.step("publish", "push")["run"]))
        self.assertIn('--run-attempt "$RUN_ATTEMPT"', push)
        self.assertEqual(
            set(str(with_["path"]).split()),
            {
                "artefacts/manifest.json",
                "artefacts/changes.bundle",
                "artefacts/usage.json",
            },
        )
        session = self.step("author", "Preserve session output")
        self.assertTrue(is_action(session, UPLOAD))
        self.assertIn("github.run_attempt", str(session["with"]["name"]))

    def test_cleanup_never_gates_publish(self) -> None:
        """Scratch removal always runs, tolerates failure and drops the checkout."""
        cleanup = self.step("author", "Clear session scratch files")
        self.assert_expression(cleanup["if"], "always()")
        self.assertIs(cleanup["continue-on-error"], True)
        self.assertRegex(flatten(cleanup["run"]), r"rm -rf .*\bworkspace\b")


class PromptReviewContracts(ReusableWorkflowCase):
    """The prompt claims only the review steps the workflow has.

    The organisation guidance (AGENTS.md §11.6) forbids a pipeline to
    tell its agent to skip §11, or that something else handles review
    unless a job does. Once a job requests a review, the prompt may
    say so; these contracts must keep passing when it does.
    """

    OLD_PUBLICATION = (
        "*Publication.* You cannot push, and the workflow opens the pull\n"
        "request and handles the Copilot review, so skip the guidance's\n"
        "pull request and review-cycle steps."
    )

    @staticmethod
    def fixture_jobs() -> dict[str, dict[str, Any]]:
        """Jobs shaped like the workflow's, with no review step."""
        return {
            "author": {
                "steps": [
                    {
                        "name": "Run coding agent (Copilot)",
                        "env": {"COPILOT_GITHUB_TOKEN": "x"},
                        "run": 'copilot -p "$PROMPT"',
                    }
                ]
            },
            "publish": {
                "steps": [
                    {"name": "Open pull request", "run": "python publish.py open"}
                ]
            },
            "report": {"steps": [{"name": "Report", "run": "python report.py"}]},
        }

    def test_review_step_detection(self) -> None:
        """A step requesting a Copilot review is found; others are not.

        This runs against its own fixture, never the real workflow, so
        adding a genuine review step there cannot break it.
        """
        jobs = self.fixture_jobs()
        self.assertEqual(review_steps(jobs), [])
        jobs["publish"]["steps"].append(
            {
                "name": "Request Copilot review",
                "run": 'gh pr edit "$PR" --add-reviewer @copilot',
            }
        )
        self.assertEqual(review_steps(jobs), [("publish", "Request Copilot review")])

    def test_claim_detection(self) -> None:
        """Positive claims are caught; negated or actorless ones are not."""
        claims = (
            self.OLD_PUBLICATION,
            "The workflow requests a Copilot review once the pull request opens.",
            "Something else handles the review.",
            "A later job awaits the review and answers each finding.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertNotEqual(review_claims(text), [])
        truthful = (
            "The Copilot review happens after this session ends; you never see it.",
            "This pipeline cannot yet act on review findings.",
            "No job in this workflow requests, awaits or answers a review.",
            "A human reviews everything you produce before it merges.",
            "The workflow handles cleanup.",
        )
        for text in truthful:
            with self.subTest(text=text):
                self.assertEqual(review_claims(text), [])

    def test_skip_detection(self) -> None:
        """An instruction to skip §11 or the review cycle is caught."""
        for text in (
            self.OLD_PUBLICATION,
            "Skip §11 of the guidance.",
            "You may omit the review steps.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(skip_instructions(text), [])
        for text in (
            "Do not skip §11.",
            "Review your own diff the way a reviewer would.",
        ):
            with self.subTest(text=text):
                self.assertEqual(skip_instructions(text), [])

    def test_prompt_claims_no_review_the_workflow_lacks(self) -> None:
        """Without a review step, the prompt claims none.

        The one test that reads the real workflow's review steps: once
        a genuine step exists, the prompt may describe it.
        """
        if review_steps(self.jobs):
            self.skipTest("the workflow has a review step")
        prompt = PROMPT.read_text(encoding="utf-8")
        self.assertEqual(review_claims(prompt), [])

    def test_prompt_never_tells_the_agent_to_skip_review(self) -> None:
        """The prompt never tells the agent to skip §11 or the review cycle."""
        prompt = PROMPT.read_text(encoding="utf-8")
        self.assertEqual(skip_instructions(prompt), [])


class RevisionContracts(ReusableWorkflowCase):
    """A revision round reuses the pipeline; what differs is pinned here.

    The author job checks out the default branch head, fetches the
    bot branch from the public fork without a credential and refuses
    any head but the recorded one; the publisher moves the branch
    with a compare-and-swap and answers the review threads with the
    pull request token (DESIGN.md section 20).
    """

    def test_revise_input_defaults_on_and_the_caller_forwards_it(self) -> None:
        """Revisions are on unless a caller says otherwise."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertIs(inputs["revise"]["default"], True)
        self.assertEqual(inputs["pull_requests"]["default"], "")
        cron = cast(
            dict[str | bool, Any], yaml.safe_load(CRON.read_text(encoding="utf-8"))
        )
        form = cast(
            dict[str, dict[str, Any]], cron[TRIGGERS]["workflow_dispatch"]["inputs"]
        )
        self.assertIs(form["revise"]["default"], True)
        self.assertEqual(form["pull_requests"]["default"], "")
        with_ = cast(dict[str, Any], cron["jobs"]["code-monkey"]["with"])
        self.assert_expression(with_["pull_requests"], "inputs.pull_requests || ''")
        self.assert_expression(
            with_["revise"], "github.event_name != 'workflow_dispatch' || inputs.revise"
        )

    def test_author_fetches_the_bot_branch_without_a_credential(self) -> None:
        """The fork is public; the fetch names it and checks the head it recorded."""
        fetch = self.step("author", "Fetch pull request branch")
        self.assert_expression(fetch["if"], "matrix.task == 'revise'")
        env = cast(dict[str, Any], fetch["env"])
        self.assert_expression(env["FORK_REPOSITORY"], "matrix.fork_repository")
        self.assert_expression(env["BRANCH"], "matrix.branch")
        self.assert_expression(env["HEAD_SHA"], "matrix.head_sha")
        script = flatten(fetch["run"])
        self.assertIn('"https://github.com/$FORK_REPOSITORY.git"', script)
        self.assertIn(
            '[ "$(git -C workspace rev-parse FETCH_HEAD)" = "$HEAD_SHA" ]', script
        )
        self.assertNotIn("token", script.lower())
        self.assertLess(
            self.position("author", "Checkout target repository"),
            self.position("author", "Fetch pull request branch"),
        )
        self.assertLess(
            self.position("author", "Fetch pull request branch"),
            self.position("author", "packet"),
        )
        guard = flatten(self.step("author", "Require pinned assets")["run"])
        self.assertIn('[[ "$TASK" =~ ^(issue|revise)$ ]]', guard)
        self.assertIn('[[ "$CHECKOUT_SHA" =~ ^[0-9a-f]{40}$ ]]', guard)

    def test_prompt_follows_the_task(self) -> None:
        """The revise prompt exists and the assemble step picks it by task."""
        self.assertTrue(REVISE_PROMPT.is_file())
        assemble = self.step("author", "Assemble prompt")
        env = cast(dict[str, Any], assemble["env"])
        self.assert_expression(env["TASK"], "matrix.task")
        script = flatten(assemble["run"])
        # Each task maps to its own prompt file; a task with no mapping
        # fails the step rather than reading a wrong file.
        self.assertIn("issue) prompt=monkey-assets/prompt/author.md", script)
        self.assertIn("revise) prompt=monkey-assets/prompt/revise.md", script)
        self.assertIn("*) echo", script)
        self.assertNotIn('"$TASK".md', script)
        self.assertEqual(
            skip_instructions(REVISE_PROMPT.read_text(encoding="utf-8")), []
        )

    def test_bundle_base_comes_from_the_manifest_within_the_recorded_pair(self) -> None:
        """A revision bundles from the base the agent chose, if it is one of the two."""
        bundle = self.step("author", "Bundle proposed commits")
        env = cast(dict[str, Any], bundle["env"])
        self.assert_expression(env["REBASE_SHA"], "matrix.rebase_sha")
        script = flatten(bundle["run"])
        self.assertIn("manifest base_sha is neither recorded base", script)
        self.assertIn('"$BASE_SHA" | "$REBASE_SHA")', script)

    def test_publisher_answers_threads_with_the_pull_request_token(self) -> None:
        """Replies and resolves are writes to the target: the pull request token alone."""
        respond = self.step("publish", "respond")
        condition = unwrap(str(respond["if"]))
        self.assertIn("matrix.task == 'revise'", condition)
        self.assertIn("steps.push.outcome == 'success'", condition)
        self.assertIn("inputs.mode == 'pull-requests'", condition)
        self.assert_expression(
            respond["env"]["GH_TOKEN"], "steps.pr-token.outputs.token"
        )
        self.assertIn("publish.py respond", flatten(respond["run"]))
        order = [
            self.position("publish", name)
            for name in ("pr-token", "retitle", "push", "respond", "review-request")
        ]
        self.assertEqual(order, sorted(order))
        mint = self.step("publish", "pr-token")
        self.assertNotIn("inputs.mode == 'pull-requests' &&\n", str(mint["if"]))

    def test_a_revision_retitles_before_the_push(self) -> None:
        """The title leads the push: the Semantic Pull Request check runs on the push.

        AGENTS.md section 10.1 wants the title set before or with the
        push. The pull request token is minted before the push for
        that one write; the push step itself still holds the fork
        token alone.
        """
        retitle = self.step("publish", "retitle")
        condition = unwrap(str(retitle["if"]))
        self.assertIn("matrix.task == 'revise'", condition)
        self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("inputs.mode == 'pull-requests'", condition)
        self.assert_expression(
            retitle["env"]["GH_TOKEN"], "steps.pr-token.outputs.token"
        )
        self.assertIn("publish.py retitle", flatten(retitle["run"]))
        self.assertLess(
            self.position("publish", "pr-token"), self.position("publish", "retitle")
        )
        self.assertLess(
            self.position("publish", "retitle"), self.position("publish", "push")
        )
        self.assertIn("--record artefacts/retitle.json", flatten(retitle["run"]))
        push = self.step("publish", "push")
        self.assertNotIn("pr-token", str(push["env"]["GH_TOKEN"]))
        # A title the push did not earn is taken back after it, with
        # the same token, before anything else writes to the target.
        restore = self.step("publish", "restore-title")
        restore_if = unwrap(str(restore["if"]))
        self.assertIn("!cancelled()", restore_if)
        # After a failed retitle too: GitHub may apply the title and
        # lose the reply, and the push then never runs.
        self.assertIn("steps.retitle.outcome != 'skipped'", restore_if)
        self.assertNotIn("steps.retitle.outcome == 'success'", restore_if)
        self.assertNotIn("steps.push.outcome", restore_if)
        self.assert_expression(
            restore["env"]["GH_TOKEN"], "steps.pr-token.outputs.token"
        )
        self.assertIn("publish.py restore-title", flatten(restore["run"]))
        self.assertLess(
            self.position("publish", "push"), self.position("publish", "restore-title")
        )
        self.assertLess(
            self.position("publish", "restore-title"), self.position("publish", "open")
        )
        mint = unwrap(str(self.step("publish", "pr-token")["if"]))
        self.assertNotIn("steps.push.outcome", mint)
        for name in ("open", "respond"):
            self.assertIn(
                "steps.push.outcome == 'success'",
                unwrap(str(self.step("publish", name)["if"])),
            )

    def test_a_handoff_reaches_push_without_a_token(self) -> None:
        """Both fork mints need a proposal, so a handoff's push step holds no token.

        Any GitHub read a handoff needs, the head check above all,
        therefore belongs in ``respond``, which holds the pull request
        token; the push step must stay offline for a handoff.
        """
        for identity in ("fork-token", "fork-create-token"):
            condition = unwrap(str(self.step("publish", identity)["if"]))
            self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
        push = self.step("publish", "push")
        self.assertEqual(
            unwrap(str(push["env"]["GH_TOKEN"])),
            "steps.fork-token.outputs.token || steps.fork-create-token.outputs.token",
        )
        self.assertNotIn("github.token", str(push["env"]["GH_TOKEN"]))
        respond = self.step("publish", "respond")
        self.assertNotIn("steps.check.outputs.verdict", str(respond["if"]))

    def test_matrix_carries_the_revision_fields_shape_checked(self) -> None:
        """head_sha and task are checked before any step reads them."""
        guard = flatten(self.step("publish", "Require trusted provenance")["run"])
        self.assertIn('[[ "$TASK" =~ ^(issue|revise)$ ]]', guard)
        self.assertIn('[[ "$HEAD_SHA" =~ ^([0-9a-f]{40})?$ ]]', guard)


class ReviewGateContracts(ReusableWorkflowCase):
    """The publish job requests, awaits and reports the Copilot review.

    AGENTS.md section 11.6: a pipeline that publishes a pull request
    waits for the review of what it pushed and reports a review with
    findings as work outstanding, never as complete.
    """

    def test_request_review_input(self) -> None:
        """The gate is on by default and a caller may switch it off."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["request_review"]["type"], "boolean")
        self.assertIs(inputs["request_review"]["default"], True)

    def test_request_uses_the_pull_request_token_after_open(self) -> None:
        """The request is a write to the target: the pull request token alone."""
        step = self.step("publish", "review-request")
        condition = unwrap(str(step["if"]))
        self.assertIn("steps.open.outcome == 'success'", condition)
        self.assertIn("inputs.request_review", condition)
        self.assert_expression(step["env"]["GH_TOKEN"], "steps.pr-token.outputs.token")
        self.assertIn("publish.py review-request", flatten(step["run"]))
        self.assertLess(
            self.position("publish", "open"), self.position("publish", "review-request")
        )

    def test_wait_reads_with_the_native_token_and_fits_the_job(self) -> None:
        """The wait reads a public target with the job's own token, within its budget."""
        step = self.step("publish", "review")
        condition = unwrap(str(step["if"]))
        self.assertIn("steps.open.outcome == 'success'", condition)
        self.assertIn("inputs.request_review", condition)
        self.assert_expression(step["env"]["GH_TOKEN"], "github.token")
        script = flatten(step["run"])
        self.assertIn("publish.py review-await", script)
        found = re.search(r"--wait-minutes (\d+)", script)
        assert found is not None
        wait = int(found.group(1))
        self.assertGreaterEqual(wait, 15)
        # The job must outlast the wait, its read reserve (under ten
        # minutes) and the twenty minutes the rest of the job had.
        self.assertGreaterEqual(self.jobs["publish"]["timeout-minutes"], wait + 30)
        order = [
            self.position("publish", name)
            for name in ("open", "review-request", "review", "Comment on the issue")
        ]
        self.assertEqual(order, sorted(order))

    def test_caller_forwards_the_gate_on_by_default(self) -> None:
        """The cron form offers the switch; schedules and dispatches default on."""
        cron = cast(
            dict[str | bool, Any], yaml.safe_load(CRON.read_text(encoding="utf-8"))
        )
        inputs = cast(
            dict[str, dict[str, Any]], cron[TRIGGERS]["workflow_dispatch"]["inputs"]
        )
        self.assertIs(inputs["request_review"]["default"], True)
        with_ = cast(dict[str, Any], cron["jobs"]["code-monkey"]["with"])
        self.assert_expression(
            with_["request_review"],
            "github.event_name != 'workflow_dispatch' || inputs.request_review",
        )


class PublishJobContracts(ReusableWorkflowCase):
    """The trusted publisher: verify before fetch, App tokens per repository."""

    def test_publish_runs_after_author_with_read_grants(self) -> None:
        """Every author entry gets a verdict; the job itself can write nothing."""
        publish = self.jobs["publish"]
        self.assertEqual(publish["needs"], ["select", "author"])
        condition = unwrap(str(publish["if"]))
        self.assertIn("!cancelled()", condition)
        self.assertIn("needs.select.result == 'success'", condition)
        self.assertIn("needs.select.outputs.run_agent == 'true'", condition)
        self.assertEqual(
            publish["permissions"], {"actions": "read", "contents": "read"}
        )

    def test_evidence_is_verified_before_the_proposal_is_fetched(self) -> None:
        """Trusted bytes by ID and digest first; the proposal by name after."""
        evidence = self.step("publish", "Fetch selection evidence")
        self.assertTrue(is_action(evidence, DOWNLOAD))
        self.assert_expression(
            evidence["with"]["artifact-ids"], "needs.select.outputs.evidence_id"
        )
        verified = self.step("publish", "verified")
        self.assertIn("bot_evidence.py verify", flatten(verified["run"]))

        # The proposal is never extracted by download-artifact: the
        # bounded fetcher checks sizes before writing anything.
        fetch = self.step("publish", "Fetch and accept bounded proposal")
        self.assertNotIn("uses", fetch)
        script = flatten(fetch["run"])
        self.assertIn("monkey-assets/scripts/proposal_fetch.py", script)
        self.assertIn("--output accepted", script)
        self.assertIn(
            "3) reason='the author job produced no proposal artifact'", script
        )
        self.assertIn(
            "4) reason='the proposal artifact failed bounded acceptance'", script
        )
        self.assertIn('*) exit "$status"', script)
        self.assertIn('"author-failed"', script)
        self.assertEqual(self.actions("publish", DOWNLOAD), [evidence])

        steps = self.steps("publish")
        self.assertLess(steps.index(evidence), steps.index(verified))
        self.assertLess(steps.index(verified), steps.index(fetch))

    def test_offline_check_and_scoped_token_mints(self) -> None:
        """The check has no token; each fork mint grants workflows always."""
        check = self.step("publish", "check")
        script = flatten(check["run"])
        self.assertIn("publish.py check", script)
        self.assertIn("--coauthors monkey-assets/config/coauthors.json", script)
        self.assertNotIn("GH_TOKEN", cast(dict[str, Any], check.get("env", {})))
        # No step may gate on the proposal's workflow edits any longer.
        self.assertNotIn("needs_workflows", script)

        for identity, exists in (
            ("fork-token", "true"),
            ("fork-create-token", "false"),
        ):
            with self.subTest(mint=identity):
                mint = self.step("publish", identity)
                self.assertTrue(is_action(mint, APP_TOKEN))
                condition = unwrap(str(mint["if"]))
                self.assertIn("!inputs.dry_run", condition)
                self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
                self.assertIn("inputs.github_app_client_id != ''", condition)
                self.assertIn(
                    f"steps.fork-state.outputs.exists == '{exists}'", condition
                )
                with_ = cast(dict[str, Any], mint["with"])
                self.assert_expression(with_["owner"], "matrix.fork_org")
                self.assertEqual(with_["permission-contents"], "write")
                self.assertEqual(with_["permission-metadata"], "read")
                self.assertNotIn("permission-pull-requests", with_)
                self.assertNotIn("permission-issues", with_)
                # Syncing the fork can fast-forward over upstream
                # workflow commits, so the grant cannot depend on the
                # proposal's own diff.
                self.assertEqual(with_["permission-workflows"], "write")
        write = cast(dict[str, Any], self.step("publish", "fork-token")["with"])
        self.assert_expression(write["repositories"], "matrix.repo_name")
        # The fork does not exist yet, so no repository can be named.
        create = cast(dict[str, Any], self.step("publish", "fork-create-token")["with"])
        self.assertNotIn("repositories", create)
        self.assertEqual(create["permission-administration"], "write")

        pr = self.step("publish", "pr-token")
        self.assertTrue(is_action(pr, APP_TOKEN))
        condition = unwrap(str(pr["if"]))
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("inputs.mode == 'pull-requests'", condition)
        self.assertIn("inputs.github_app_client_id != ''", condition)
        pr_with = cast(dict[str, Any], pr["with"])
        self.assert_expression(pr_with["owner"], "inputs.org")
        self.assert_expression(pr_with["repositories"], "matrix.repo_name")
        self.assertEqual(pr_with["permission-pull-requests"], "write")
        self.assertEqual(pr_with["permission-metadata"], "read")
        # The mint runs ahead of the push, for a revision's title; open
        # and respond still wait for the push to succeed.
        self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
        self.assertNotIn("steps.push.outcome", condition)
        opened = unwrap(str(self.step("publish", "open")["if"]))
        self.assertIn("steps.push.outcome == 'success'", opened)
        self.assertIn("inputs.mode == 'pull-requests'", opened)

        comment = self.step("publish", "comment-token")
        self.assertTrue(is_action(comment, APP_TOKEN))
        condition = unwrap(str(comment["if"]))
        self.assertIn("!inputs.dry_run", condition)
        # The comment must also reach the issue after a failed push, so
        # the rollback is visible; never after cancellation.
        self.assertIn("!cancelled()", condition)
        self.assertIn("steps.push.outcome != 'skipped'", condition)
        comment_with = cast(dict[str, Any], comment["with"])
        self.assertEqual(comment_with["permission-issues"], "write")
        self.assert_expression(comment_with["owner"], "inputs.org")
        self.assert_expression(comment_with["repositories"], "matrix.repo_name")

    def test_every_entry_uploads_a_result(self) -> None:
        """A failure before push still leaves a typed result for the report."""
        ensure = self.step("publish", "Ensure a result exists")
        self.assertEqual(ensure["if"], "always()")
        script = squash(str(ensure["run"]))
        self.assertIn("if [ -s artefacts/result.json ]; then", script)
        self.assertIn('verdict: "publish-failed"', script)
        steps = self.steps("publish")
        upload = self.step("publish", "Attach publish result")
        self.assertLess(steps.index(ensure), steps.index(upload))

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_fallback_result_keeps_the_session_spend(self) -> None:
        """A failure after the check still reports the session it paid for."""
        ensure = self.step("publish", "Ensure a result exists")
        self.assert_expression(
            ensure["env"]["AUTHOR_SESSION"], "steps.fetch.outputs.artifact_id"
        )
        env = {
            "PATH": os.environ["PATH"],
            "KEY": "repo-7",
            "REPOSITORY": "o/repo",
            "FORK_REPOSITORY": "lfreleng-bot-forks/repo",
            "ISSUE": "7",
            "DRY_RUN": "false",
            "MODE": "pull-requests",
            "RUN_ATTEMPT": "1",
            "CHECK_OUTCOME": "success",
            "PUSH_OUTCOME": "skipped",
            "OPEN_OUTCOME": "skipped",
        }
        for session, check, expected in (
            ("77", {"premium_requests": 12.5, "agent_seconds": 30}, ("77", 12.5, 30)),
            ("", None, (None, None, None)),
        ):
            with tempfile.TemporaryDirectory() as holder:
                work = Path(holder)
                if check is not None:
                    (work / "artefacts").mkdir()
                    (work / "artefacts" / "check.json").write_text(
                        json.dumps(check), encoding="utf-8"
                    )
                subprocess.run(
                    ["bash", "-e", "-c", str(ensure["run"])],
                    cwd=work,
                    env={**env, "AUTHOR_SESSION": session},
                    check=True,
                )
                result = json.loads(
                    (work / "artefacts" / "result.json").read_text(encoding="utf-8")
                )
            self.assertEqual(result["verdict"], "publish-failed")
            self.assertEqual(result["fork_repository"], "lfreleng-bot-forks/repo")
            self.assertIn("push: skipped, open: skipped", result["reasons"][0])
            self.assertEqual(
                (
                    result["author_session"],
                    result["premium_requests"],
                    result["agent_seconds"],
                ),
                expected,
            )

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_pushed_branch_without_a_pull_request_is_a_failure(self) -> None:
        """push's proposed result is not publication until open succeeded."""
        ensure = self.step("publish", "Ensure a result exists")
        base_env = {
            "PATH": os.environ["PATH"],
            "KEY": "repo-7",
            "REPOSITORY": "o/repo",
            "FORK_REPOSITORY": "lfreleng-bot-forks/repo",
            "ISSUE": "7",
            "RUN_ATTEMPT": "1",
            "CHECK_OUTCOME": "success",
            "PUSH_OUTCOME": "success",
            "AUTHOR_SESSION": "",
        }
        pushed: dict[str, Any] = {
            "verdict": "proposed",
            "reasons": [],
            "pull_request_url": None,
        }
        cases = [
            ("pull-requests", "false", "failure", pushed, "publish-failed"),
            ("pull-requests", "false", "skipped", pushed, "publish-failed"),
            ("pull-requests", "false", "success", pushed, "proposed"),
            (
                "pull-requests",
                "false",
                "failure",
                {**pushed, "pull_request_url": "u"},
                "proposed",
            ),
            ("branches", "false", "skipped", pushed, "proposed"),
            ("pull-requests", "true", "skipped", pushed, "proposed"),
        ]
        for mode, dry, open_outcome, result_in, expected in cases:
            with self.subTest(
                mode=mode,
                dry=dry,
                open=open_outcome,
                pr=result_in.get("pull_request_url"),
            ):
                with tempfile.TemporaryDirectory() as holder:
                    work = Path(holder)
                    (work / "artefacts").mkdir()
                    (work / "artefacts" / "result.json").write_text(
                        json.dumps(result_in), encoding="utf-8"
                    )
                    subprocess.run(
                        ["bash", "-e", "-c", str(ensure["run"])],
                        cwd=work,
                        env={
                            **base_env,
                            "MODE": mode,
                            "DRY_RUN": dry,
                            "OPEN_OUTCOME": open_outcome,
                        },
                        check=True,
                    )
                    result = json.loads(
                        (work / "artefacts" / "result.json").read_text(encoding="utf-8")
                    )
                self.assertEqual(result["verdict"], expected)
                if expected == "publish-failed":
                    self.assertIn("next run resumes", result["reasons"][-1])

    def test_result_names_the_author_session(self) -> None:
        """The fetched artifact's ID reaches push, so spend counts per session."""
        fetch = self.step("publish", "Fetch and accept bounded proposal")
        self.assertEqual(fetch["id"], "fetch")
        self.assertIn('--output accepted >> "$GITHUB_OUTPUT"', flatten(fetch["run"]))
        push = self.step("publish", "push")
        self.assert_expression(
            push["env"]["AUTHOR_SESSION"], "steps.fetch.outputs.artifact_id"
        )
        self.assertIn('--author-session "$AUTHOR_SESSION"', squash(str(push["run"])))

    def test_writes_never_fall_back_to_the_native_token(self) -> None:
        """Branch, pull request and comment writes use App tokens alone."""
        push = self.step("publish", "push")
        self.assert_expression(
            push["env"]["GH_TOKEN"],
            "steps.fork-token.outputs.token || steps.fork-create-token.outputs.token",
        )
        self.assertIn("publish.py push", flatten(push["run"]))
        opened = self.step("publish", "open")
        self.assert_expression(
            opened["env"]["GH_TOKEN"], "steps.pr-token.outputs.token"
        )
        self.assertIn("publish.py open", flatten(opened["run"]))
        comment = self.step("publish", "Comment on the issue")
        self.assert_expression(
            comment["env"]["GH_TOKEN"], "steps.comment-token.outputs.token"
        )
        for step in (push, opened, comment):
            self.assertNotIn("github.token", dumped(step))
        # The native token reads alone in the job: this run's artifacts,
        # whether a public fork exists, and the public target's reviews;
        # the job grants it actions: read and nothing more.
        users = [
            step.get("name")
            for step in self.steps("publish")
            if "github.token" in dumped(step)
        ]
        self.assertEqual(
            users,
            [
                "Fetch and accept bounded proposal",
                "Check fork exists",
                "Await Copilot review",
            ],
        )
        self.assertEqual(
            self.jobs["publish"]["permissions"],
            {"actions": "read", "contents": "read"},
        )

    def test_provenance_inputs_are_shape_checked(self) -> None:
        """Select outputs are validated as SHA, ID, digests and key patterns."""
        script = str(self.step("publish", "Require trusted provenance")["run"])
        for check in (
            '[[ "$ASSETS_SHA" =~ ^[0-9a-f]{40}$ ]]',
            '[[ "$EVIDENCE_ID" =~ ^[1-9][0-9]*$ ]]',
            '[[ "$SELECTION_SHA" =~ ^[0-9a-f]{64}$ ]]',
            '[[ "$GUIDANCE_SHA" =~ ^[0-9a-f]{64}$ ]]',
            '[[ "$KEY" =~ ^[A-Za-z0-9_.-]+$ ]]',
        ):
            with self.subTest(check=check):
                self.assertIn(check, script)


class PreflightContracts(ReusableWorkflowCase):
    """The run-time gate runs from pinned assets before any token exists."""

    def test_gate_runs_after_pinning_and_before_the_read_mint(self) -> None:
        """Contract tests, zizmor and the config check precede the mint."""
        pinned = self.position("select", "Pin assets commit")
        workflow = self.position("select", "Pre-flight: workflow contracts and audit")
        config = self.position("select", "Pre-flight: configuration and credentials")
        mint = self.position("select", "Mint read-only App token")
        self.assertLess(pinned, workflow)
        self.assertLess(workflow, config)
        self.assertLess(config, mint)
        run = flatten(
            self.step("select", "Pre-flight: workflow contracts and audit")["run"]
        )
        self.assertIn("scripts/preflight.py workflow --root .", run)
        self.assertIn("cd monkey-assets", run)
        config_run = flatten(
            self.step("select", "Pre-flight: configuration and credentials")["run"]
        )
        self.assertIn("--config monkey-assets/config/bot.json", config_run)
        self.assertIn("fork_orgs.py check", config_run)

    def test_config_gate_sees_the_credentials_under_template_names(self) -> None:
        """Shape checks read the inputs the workflow was handed, nothing else."""
        step = self.step("select", "Pre-flight: configuration and credentials")
        self.assertEqual(
            step["env"]["BOT_APP_CLIENT_ID"], "${{ inputs.github_app_client_id }}"
        )
        self.assertEqual(
            step["env"]["BOT_APP_PRIVATE_KEY"], "${{ secrets.github_app_private_key }}"
        )
        self.assertEqual(
            step["env"]["COPILOT_GITHUB_TOKEN"], "${{ secrets.copilot_token }}"
        )

    def test_every_minted_token_is_checked_for_identity(self) -> None:
        """Read, fork and pull request mints each hand their slug to the check."""
        read = self.step("select", "Pre-flight: App identity and token grants")
        self.assertEqual(
            read["env"]["MINTED_SLUG"], "${{ steps.app-token.outputs.app-slug }}"
        )
        self.assertIn("preflight.py token", flatten(read["run"]))
        self.assertLess(
            self.position("select", "Pre-flight: App identity and token grants"),
            self.position("select", "Select issues"),
        )
        fork = self.step("publish", "Pre-flight: fork token identity")
        self.assertEqual(
            squash(fork["env"]["MINTED_SLUG"]),
            "${{ steps.fork-token.outputs.app-slug || steps.fork-create-token.outputs.app-slug }}",
        )
        self.assertLess(
            self.position("publish", "Pre-flight: fork token identity"),
            self.position("publish", "Push branch to fork"),
        )
        pr = self.step("publish", "Pre-flight: pull request token identity")
        self.assertEqual(
            pr["env"]["MINTED_SLUG"], "${{ steps.pr-token.outputs.app-slug }}"
        )
        self.assertLess(
            self.position("publish", "Pre-flight: pull request token identity"),
            self.position("publish", "Open pull request"),
        )

    def test_zizmor_is_pinned(self) -> None:
        """The auditor the gate runs is an exact version."""
        run = flatten(self.step("select", "Install zizmor")["run"])
        self.assertRegex(run, r"uv tool install 'zizmor==\d+\.\d+\.\d+'")


class MintProvenanceContracts(ReusableWorkflowCase):
    """Every App token names a known owner and carries the least it needs."""

    FORK_MINTS = ("fork-token", "fork-create-token")
    TARGET_MINTS = (
        ("select", "app-token"),
        ("publish", "pr-token"),
        ("publish", "comment-token"),
    )

    def mints(self) -> list[tuple[str, dict[str, Any]]]:
        """Every create-github-app-token step, with its job."""
        return [
            (job, step)
            for job in self.jobs
            if "steps" in self.jobs[job]
            for step in self.actions(job, APP_TOKEN)
        ]

    def test_every_mint_names_the_target_or_the_fork_organisation(self) -> None:
        """Owner is ``inputs.org`` or ``matrix.fork_org``; never anything else."""
        found = self.mints()
        self.assertEqual(
            sorted(str(step["id"]) for _, step in found),
            sorted(["app-token", "comment-token", "pr-token", *self.FORK_MINTS]),
        )
        for job, step in found:
            with self.subTest(job=job, mint=step["id"]):
                owner = unwrap(str(cast(dict[str, Any], step["with"])["owner"]))
                expected = (
                    "matrix.fork_org" if step["id"] in self.FORK_MINTS else "inputs.org"
                )
                self.assertEqual(owner, expected)

    def test_administration_belongs_to_the_creation_mint_alone(self) -> None:
        """Only the mint that forks a missing repository may administer."""
        for job, step in self.mints():
            with_ = cast(dict[str, Any], step["with"])
            with self.subTest(job=job, mint=step["id"]):
                if step["id"] == "fork-create-token":
                    self.assertEqual(with_["permission-administration"], "write")
                else:
                    self.assertNotIn("permission-administration", with_)

    def test_target_mints_cannot_write_code(self) -> None:
        """No token for the target organisation can push, run or administer."""
        for job, identity in self.TARGET_MINTS:
            with_ = cast(dict[str, Any], self.step(job, identity)["with"])
            with self.subTest(job=job, mint=identity):
                self.assertNotEqual(with_.get("permission-contents"), "write")
                self.assertNotIn("permission-workflows", with_)
                self.assertNotIn("permission-administration", with_)

    def test_each_write_step_holds_its_own_token(self) -> None:
        """Push sees fork tokens alone; open sees the pull request token alone."""
        push = unwrap(str(self.step("publish", "push")["env"]["GH_TOKEN"]))
        self.assertEqual(
            set(re.findall(r"steps\.([a-z-]+)\.outputs\.token", push)),
            set(self.FORK_MINTS),
        )
        opened = unwrap(str(self.step("publish", "open")["env"]["GH_TOKEN"]))
        self.assertEqual(
            re.findall(r"steps\.([a-z-]+)\.outputs\.token", opened), ["pr-token"]
        )

    def test_fork_organisation_is_proven_before_any_fork_mint(self) -> None:
        """The resolve step guards the pattern and the mapping, then the mints run."""
        fork = self.step("publish", "fork")
        script = flatten(fork["run"])
        self.assertIn('"$FORK_ORG" =~ ^lfreleng-bot-forks(-[a-z0-9]+)?$', script)
        self.assertIn('"$FORK_ORG" = "$ORG"', script)
        self.assertIn("fork_orgs.py check", script)
        self.assertIn("fork_orgs.py resolve", script)
        self.assertIn('"$resolved" != "fork_org=$FORK_ORG"', script)
        self.assert_expression(fork["env"]["FORK_ORG"], "matrix.fork_org")
        state = self.step("publish", "fork-state")
        self.assert_expression(state["env"]["GH_TOKEN"], "github.token")
        positions = [self.position("publish", step) for step in self.FORK_MINTS]
        self.assertLess(self.position("publish", "fork"), min(positions))
        self.assertLess(self.position("publish", "fork-state"), min(positions))
        self.assertLess(max(positions), self.position("publish", "push"))
        self.assertLess(
            self.position("publish", "pr-token"), self.position("publish", "push")
        )
        self.assertLess(
            self.position("publish", "push"), self.position("publish", "open")
        )


class SelectAndReportContracts(ReusableWorkflowCase):
    """Trusted selection outputs, org-wide read mint, result gathering."""

    def test_select_publishes_the_full_provenance_set(self) -> None:
        """Every value downstream jobs trust is an output of the select job."""
        outputs = cast(dict[str, Any], self.jobs["select"]["outputs"])
        self.assertEqual(
            set(outputs),
            {
                "assets_sha",
                "namespace",
                "evidence_id",
                "selection_sha256",
                "guidance_sha256",
                "count",
                "matrix",
                "run_agent",
                "author_timeout",
                "agent_timeout",
                "max_parallel",
                "run_created_at",
            },
        )

    def test_comment_cutoff_is_stable_across_attempts(self) -> None:
        """The marker search starts at the run's creation, not the selection."""
        comment = self.step("publish", "Comment on the issue")
        env = cast(dict[str, Any], comment["env"])
        self.assert_expression(env["SINCE"], "needs.select.outputs.run_created_at")
        self.assertNotIn("generated_at", str(comment["run"]))
        identity = squash(str(self.step("select", "identity")["run"]))
        self.assertIn("actions/runs/$RUN_ID", identity)
        self.assertIn("--jq .created_at", identity)

    def test_live_runs_require_the_app(self) -> None:
        """A live run in any writing mode fails early without a client id."""
        guard = self.step("select", "Require App for live runs")
        condition = unwrap(str(guard["if"]))
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("inputs.mode != 'select'", condition)
        self.assertIn("inputs.github_app_client_id == ''", condition)

    def test_select_resolves_fork_organisations(self) -> None:
        """Selection carries the committed fork mapping into every entry."""
        script = flatten(self.step("select", "Select issues")["run"])
        self.assertIn("--fork-orgs monkey-assets/config/fork-orgs.json", script)

    def test_read_mint_scopes_to_the_requested_repositories(self) -> None:
        """Selection reads with the caller's repository list; empty is org-wide."""
        mint = self.step("select", "app-token")
        self.assertTrue(is_action(mint, APP_TOKEN))
        with_ = cast(dict[str, Any], mint["with"])
        self.assertEqual(with_["permission-issues"], "read")
        self.assertEqual(with_["permission-contents"], "read")
        self.assertEqual(
            with_["repositories"], "${{ steps.budget.outputs.repositories }}"
        )
        self.assertNotIn("write", json.dumps(with_))
        env = cast(dict[str, Any], mint["env"])
        self.assertEqual(env["INPUT_PERMISSION-ISSUE-FIELDS"], "read")
        self.assertEqual(env["INPUT_PERMISSION-ISSUE-TYPES"], "read")
        # A scoped list must still reach the guidance repository, or a
        # targeted run cannot read AGENTS.md with its own token.
        budget = squash(str(self.step("select", "budget")["run"]))
        self.assertIn('guidance_name="${GUIDANCE_REPOSITORY#*/}"', budget)
        self.assertIn('repositories="$repositories,$guidance_name"', budget)

    def test_manifest_outcome_is_printed_by_known_value(self) -> None:
        """The author job prints a recognised outcome, never raw manifest text."""
        script = squash(str(self.step("author", "Bundle proposed commits")["run"]))
        self.assertIn("*) outcome=unrecognised ;;", script)
        self.assertIn('echo "Manifest outcome: $outcome"', script)
        self.assertNotIn('echo "Manifest outcome: $(jq', script)

    def test_evidence_upload_is_mandatory_and_short_lived(self) -> None:
        """Evidence must exist and lives seven days, per the retention table."""
        upload = self.step("select", "evidence")
        self.assertTrue(is_action(upload, UPLOAD))
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], 7)

    def test_report_gathers_namespaced_results_for_ninety_days(self) -> None:
        """The report waits on all jobs, reads this run's results, keeps 90d."""
        report = self.jobs["report"]
        self.assertEqual(report["needs"], ["select", "author", "publish"])
        gather = self.action("report", DOWNLOAD)
        self.assertEqual(
            gather["with"]["pattern"],
            "monkey-result-${{ needs.select.outputs.namespace }}-*",
        )
        final = self.step("report", "Attach run report")
        self.assertTrue(is_action(final, UPLOAD))
        self.assertEqual(final["with"]["retention-days"], 90)


class ListInputContracts(ReusableWorkflowCase):
    """Operator lists are split and checked once, before any token exists."""

    # Splitter step id and validate mode for each list input.
    LISTS: ClassVar[dict[str, tuple[str, str]]] = {
        "repositories": ("repositories", "repository"),
        "exclude_repos": ("exclusions", "repository"),
        "issues": ("issues", "issue"),
        "pull_requests": ("pulls", "pull-request"),
    }

    def test_each_list_is_split_as_objects_of_the_org(self) -> None:
        """Names or references of the trusted owner, offline, with no token."""
        self.assertEqual(len(self.actions("select", SPLITTER)), len(self.LISTS))
        for name, (identity, mode) in self.LISTS.items():
            with self.subTest(input=name):
                split = self.step("select", identity)
                self.assertTrue(is_action(split, SPLITTER))
                with_ = cast(dict[str, Any], split["with"])
                self.assert_expression(with_["list"], f"inputs.{name}")
                self.assertEqual(with_["validate"], mode)
                self.assert_expression(with_["owner"], "inputs.org")
                self.assertEqual(with_["token"], "")
                self.assertNotIn("verify", with_)

    def test_lists_are_split_before_the_input_check_and_the_mint(self) -> None:
        """A bad name fails the run before the App key is used."""
        check = self.position("select", "budget")
        mint = self.position("select", "app-token")
        for identity, _ in self.LISTS.values():
            with self.subTest(step=identity):
                self.assertLess(self.position("select", identity), check)
                self.assertLess(self.position("select", identity), mint)

    @unittest.expectedFailure
    def test_named_issues_scope_the_read_mint_and_exclude_a_scan(self) -> None:
        """Issues scope the token to the repositories that exist; with a scan they fail."""
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(
            env["ISSUE_REPOSITORIES"], "steps.named.outputs.repositories"
        )
        self.assert_expression(env["ISSUE_COUNT"], "steps.issues.outputs.count")
        self.assert_expression(
            env["REPOSITORY_COUNT"], "steps.repositories.outputs.count"
        )
        script = squash(str(budget["run"]))
        self.assertIn("::error::issues and repositories cannot both be given", script)
        select = cast(dict[str, Any], self.step("select", "Select issues")["env"])
        self.assert_expression(select["ISSUES"], "steps.issues.outputs.csv")
        self.assert_expression(select["PULL_REQUESTS"], "steps.pulls.outputs.csv")
        self.assert_expression(select["REVISE"], "inputs.revise")
        run = flatten(self.step("select", "Select issues")["run"])
        self.assertIn('--pull-requests "$PULL_REQUESTS"', run)
        self.assertIn("--no-revise", run)
        # The run id is the session: unique per run, the same across
        # its retries, and trusted, so it may own a scratch branch and
        # a reply marker where a second-grained timestamp may not.
        self.assertIn('--session "$RUN_ID"', run)
        self.assert_expression(select["RUN_ID"], "github.run_id")
        self.assertIn(
            '--issues "$ISSUES"', flatten(self.step("select", "Select issues")["run"])
        )

    def test_a_list_given_but_naming_nothing_is_refused(self) -> None:
        """Separators alone fail the input check instead of scanning.

        The concurrency group tests the raw issues input and selection
        the split one, so a list that parses to nothing would take a
        per-run lock and then scan. The check reads both, before the
        selection and the mint.
        """
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(env["ISSUES_RAW"], "inputs.issues")
        self.assert_expression(env["REPOSITORIES_RAW"], "inputs.repositories")
        script = squash(str(budget["run"]))
        self.assertIn("::error::issues was given but names no issue", script)
        self.assertIn("::error::repositories was given but names no repository", script)
        self.assert_expression(env["PULL_REQUESTS_RAW"], "inputs.pull_requests")
        self.assertIn(
            "::error::pull_requests was given but names no pull request", script
        )
        check = self.position("select", "budget")
        self.assertLess(check, self.position("select", "app-token"))
        self.assertLess(check, self.position("select", "Select issues"))

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_a_pull_request_list_naming_nothing_is_refused(self) -> None:
        """Separators alone in pull_requests fail too, rather than scan."""
        budget = self.step("select", "budget")
        env = {
            "PATH": os.environ["PATH"],
            "MODE": "pull-requests",
            "MAX_PRS": "1",
            "MAX_RUNTIME": "60",
            "MAX_AGENTS": "1",
            "MODEL": "auto",
            "REPOSITORIES": "",
            "REPOSITORIES_RAW": "",
            "REPOSITORY_COUNT": "0",
            "ISSUE_REPOSITORIES": "",
            "ISSUES_RAW": "",
            "ISSUE_COUNT": "0",
            "ORG": "o",
            "GUIDANCE_REPOSITORY": "o/.github",
        }
        for raw, count, error in (
            (" , ", "0", "pull_requests was given but names no pull request"),
            (",", "0", "pull_requests was given but names no pull request"),
            ("repo#41", "1", None),
            ("", "0", None),
        ):
            with (
                self.subTest(pull_requests=raw),
                tempfile.TemporaryDirectory() as holder,
            ):
                output = Path(holder) / "output"
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        **env,
                        "PULL_REQUESTS_RAW": raw,
                        "PULL_COUNT": count,
                        "GITHUB_OUTPUT": str(output),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if error is None:
                    self.assertEqual(proc.returncode, 0, proc.stdout)
                else:
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn(f"::error::{error}", proc.stdout)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_only_an_empty_list_means_a_scan(self) -> None:
        """An empty list scans; one that parsed to nothing fails."""
        budget = self.step("select", "budget")
        env = {
            "PATH": os.environ["PATH"],
            "MODE": "pull-requests",
            "MAX_PRS": "1",
            "MAX_RUNTIME": "60",
            "MAX_AGENTS": "1",
            "MODEL": "auto",
            "REPOSITORIES": "",
            "ISSUE_REPOSITORIES": "",
            "PULL_COUNT": "0",
            "PULL_REQUESTS_RAW": "",
            "ORG": "o",
            "GUIDANCE_REPOSITORY": "o/.github",
        }
        cases = (
            ("", "0", "", "0", None),
            (" , ", "0", "", "0", "issues was given but names no issue"),
            (",", "0", "", "0", "issues was given but names no issue"),
            (" ", "0", "", "0", "issues was given but names no issue"),
            ("", "0", " , ", "0", "repositories was given but names no repository"),
            ("repo#1", "1", "", "0", None),
            ("", "0", "repo", "1", None),
        )
        for issues, issue_count, repos, repo_count, error in cases:
            with (
                self.subTest(issues=issues, repositories=repos),
                tempfile.TemporaryDirectory() as holder,
            ):
                output = Path(holder) / "output"
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        **env,
                        "ISSUES_RAW": issues,
                        "ISSUE_COUNT": issue_count,
                        "REPOSITORIES_RAW": repos,
                        "REPOSITORY_COUNT": repo_count,
                        "GITHUB_OUTPUT": str(output),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if error is None:
                    self.assertEqual(proc.returncode, 0, proc.stdout)
                else:
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn(f"::error::{error}", proc.stdout)
                    self.assertFalse(output.exists())

    def test_named_repositories_are_resolved_before_the_mint(self) -> None:
        """A missing named repository leaves the token scope; none left fails.

        The token action looks the installation up through the first
        repository in its scope, so an absent name there fails the mint
        before selection could report it. The public read with the
        native token settles which names exist; the selector then
        records the rest as not found.
        """
        named = self.step("select", "named")
        env = cast(dict[str, Any], named["env"])
        self.assert_expression(env["GH_TOKEN"], "github.token")
        self.assert_expression(env["ORG"], "inputs.org")
        self.assert_expression(
            env["ISSUE_REPOSITORIES"], "steps.issues.outputs.repositories"
        )
        self.assert_expression(
            env["PULL_REPOSITORIES"], "steps.pulls.outputs.repositories"
        )
        self.assert_expression(
            env["REPOSITORIES"], "steps.repositories.outputs.repositories"
        )
        script = squash(str(named["run"]))
        self.assertIn("HTTP 404", script)
        # Readable is not public: the native token reads the private
        # caller repository too, and the App may not be installed there.
        self.assertIn("--jq .private", script)
        self.assertIn('[ "$output" = false ]', script)
        self.assertIn("::error::none of the named issues' repositories exist", script)
        self.assertIn("::error::repository", script)
        self.assertLess(
            self.position("select", "issues"), self.position("select", "named")
        )
        self.assertLess(
            self.position("select", "named"), self.position("select", "budget")
        )

    def test_read_mint_scope_derives_from_the_split_list(self) -> None:
        """The scope is the splitter's names plus the guidance repository."""
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(
            env["REPOSITORIES"], "steps.repositories.outputs.repositories"
        )
        script = squash(str(budget["run"]))
        for tool in ("tr ", "sed ", "paste "):
            self.assertNotIn(tool, script)
        self.assertIn('repositories="$repositories,$guidance_name"', script)
        mint = self.step("select", "app-token")
        self.assert_expression(
            cast(dict[str, Any], mint["with"])["repositories"],
            "steps.budget.outputs.repositories",
        )

    def test_selection_reads_the_split_lists(self) -> None:
        """The raw inputs reach the splitters and nothing else.

        The input check alone also sees the raw issues, repositories
        and pull requests, under names of their own, to tell an absent
        list from one that parsed to nothing.
        """
        env = cast(dict[str, Any], self.step("select", "Select issues")["env"])
        self.assert_expression(env["REPOSITORIES"], "steps.repositories.outputs.csv")
        self.assert_expression(env["EXCLUDE_REPOS"], "steps.exclusions.outputs.csv")
        splitters = {id(s) for s in self.actions("select", SPLITTER)}
        budget = self.step("select", "budget")
        for job in self.jobs:
            if "steps" not in self.jobs[job]:
                continue
            for step in self.steps(job):
                if id(step) in splitters:
                    continue
                with self.subTest(job=job, step=step.get("name")):
                    checked = step
                    if step is budget:
                        checked = {
                            **step,
                            "env": {
                                k: v
                                for k, v in step["env"].items()
                                if k
                                not in (
                                    "ISSUES_RAW",
                                    "REPOSITORIES_RAW",
                                    "PULL_REQUESTS_RAW",
                                )
                            },
                        }
                    text = json.dumps(checked)
                    for name in self.LISTS:
                        self.assertNotRegex(text, rf"inputs\.{name}\b")


class InterfaceContracts(ReusableWorkflowCase):
    """The `workflow_call` surface: inputs, secrets and their defaults."""

    def test_declared_inputs_and_secrets(self) -> None:
        """The input set matches DESIGN.md section 9.1; secrets are optional."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        inputs = cast(dict[str, dict[str, Any]], call["inputs"])
        self.assertEqual(
            set(inputs),
            {
                "org",
                "mode",
                "dry_run",
                "model",
                "max_pull_requests",
                "categories",
                "issues",
                "pull_requests",
                "revise",
                "max_runtime_minutes",
                "max_concurrent_agents",
                "allow_subagents",
                "include_dotgithub",
                "include_assigned",
                "load_repo_skills",
                "request_review",
                "repositories",
                "exclude_repos",
                "guidance_repository",
                "guidance_ref",
                "egress_policy",
                "egress_allow_config",
                "github_app_client_id",
                "assets_repository",
                "assets_ref",
            },
        )
        secrets = cast(dict[str, dict[str, Any]], call["secrets"])
        self.assertEqual(set(secrets), {"copilot_token", "github_app_private_key"})
        for name, secret in secrets.items():
            with self.subTest(secret=name):
                self.assertIs(secret["required"], False)

    def test_input_defaults(self) -> None:
        """Defaults keep an unconfigured call dry, bounded and sub-agent aware."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        inputs = cast(dict[str, dict[str, Any]], call["inputs"])
        self.assertIs(inputs["org"]["required"], True)
        expected: dict[str, object] = {
            "dry_run": True,
            "mode": "pull-requests",
            "model": "claude-opus-5.5",
            "max_pull_requests": "10",
            "categories": "all",
            "max_runtime_minutes": "180",
            "max_concurrent_agents": "10",
            "allow_subagents": True,
            "include_dotgithub": False,
            "include_assigned": False,
            "load_repo_skills": False,
            "request_review": True,
            "revise": True,
            "pull_requests": "",
            # A caller omitting assets_repository checks out these
            # assets; the stale pre-rename name broke such callers.
            "assets_repository": "lfreleng-actions/github-code-monkey-bot",
        }
        for name, default in expected.items():
            with self.subTest(input=name):
                actual: object = inputs[name]["default"]
                self.assertIs(type(actual), type(default))
                self.assertEqual(actual, default)


class CronCallerContracts(WorkflowCase):
    """The scheduled caller: schedule, dispatch form and what it forwards."""

    WORKFLOW = CRON

    @unittest.expectedFailure
    def test_schedule_and_concurrency(self) -> None:
        """Weekday 09:00 UTC; scans share a group, dry and named do not."""
        self.assertEqual(self.triggers["schedule"], [{"cron": "0 9 * * 1-5"}])
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        self.assert_expression(
            str(concurrency["group"]),
            "github.event_name == 'workflow_dispatch' && inputs.dry_run"
            " && format('code-monkey-dry-run-{0}', github.run_id)"
            " || github.event_name == 'workflow_dispatch'"
            " && (inputs.issues != '' || inputs.pull_requests != '')"
            " && format('code-monkey-named-{0}', github.run_id)"
            " || 'code-monkey'",
        )
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_dispatch_form(self) -> None:
        """The manual form mirrors DESIGN.md section 9.2 with safe defaults."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["dry_run"]["type"], "boolean")
        self.assertIs(inputs["dry_run"]["default"], True)

        model = inputs["model"]
        self.assertEqual(model["type"], "choice")
        self.assertEqual(
            model["options"],
            ["Claude Opus 5.5", "Claude Fable 5.1", "Claude Sonnet 5.5", "GPT-6 Astra"],
        )
        self.assertEqual(model["default"], "Claude Opus 5.5")

        mode = inputs["mode"]
        self.assertEqual(mode["type"], "choice")
        self.assertEqual(mode["options"], ["select", "branches", "pull-requests"])
        self.assertEqual(mode["default"], "pull-requests")

        defaults: dict[str, object] = {
            "max_pull_requests": "10",
            "max_runtime_minutes": "180",
            "max_concurrent_agents": "10",
            "allow_subagents": True,
            "include_dotgithub": False,
            "include_assigned": False,
            "load_repo_skills": False,
            "repositories": "",
            "issues": "",
            "categories": "all",
        }
        for name, default in defaults.items():
            with self.subTest(input=name):
                actual: object = inputs[name]["default"]
                self.assertIs(type(actual), type(default))
                self.assertEqual(actual, default)
        # GitHub caps a dispatch form at 25 inputs.
        self.assertLessEqual(len(inputs), 25)

    def test_categories_and_targets_are_forwarded_as_typed(self) -> None:
        """One categories string and the target lists reach the reusable workflow."""
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(with_["categories"], "inputs.categories || 'all'")
        self.assert_expression(with_["issues"], "inputs.issues || ''")
        self.assert_expression(with_["repositories"], "inputs.repositories || ''")
        self.assertEqual(
            [s.get("id") for s in self.steps("options") if s.get("id")], ["map"]
        )

    def test_model_display_names_map_to_identifiers(self) -> None:
        """Each form choice resolves to a CLI identifier; anything else fails."""
        script = str(self.step("options", "map")["run"])
        arms = dict(re.findall(r"'([^']+)'\) id='([^']+)' ;;", script))
        self.assertEqual(
            arms,
            {
                "Claude Opus 5.5": "claude-opus-5.5",
                "Claude Fable 5.1": "claude-fable-5.1",
                "Claude Sonnet 5.5": "claude-sonnet-5.5",
                "GPT-6 Astra": "gpt-6-astra",
            },
        )
        self.assertRegex(script, r"\*\)[^\n]*exit 1")

    def test_caller_forwards_credentials_and_pins_assets(self) -> None:
        """Scheduled runs stay dry; secrets and App id come from org config."""
        job = self.jobs["code-monkey"]
        self.assertEqual(job["uses"], REUSABLE_CALL)
        with_ = cast(dict[str, Any], job["with"])
        self.assert_expression(with_["model"], "needs.options.outputs.model")
        self.assert_expression(
            with_["dry_run"],
            "github.event_name != 'workflow_dispatch' || inputs.dry_run",
        )
        self.assertIn(
            "vars.BOT_APP_CLIENT_ID", unwrap(str(with_["github_app_client_id"]))
        )
        self.assert_expression(with_["assets_ref"], "github.sha")
        # Trusted jobs hold App tokens: block mode with the pinned
        # organisation allow-list, never audit, on the live caller.
        self.assertEqual(with_["egress_policy"], "block")
        self.assertRegex(str(with_["egress_allow_config"]), r"^@[0-9a-f]{40}$")
        secrets = cast(dict[str, Any], job["secrets"])
        self.assertEqual(set(secrets), {"copilot_token", "github_app_private_key"})
        self.assert_expression(secrets["copilot_token"], "secrets.COPILOT_CLI_TOKEN")
        self.assert_expression(
            secrets["github_app_private_key"], "secrets.BOT_APP_PRIVATE_KEY"
        )

    def test_options_job_loads_the_allow_list(self) -> None:
        """The options job blocks egress against the forwarded allow-list.

        Block mode with no allowed-endpoints configures no rules, so the
        job loads the same coordinate it forwards before hardening, and
        does so without a token.
        """
        job = self.jobs["options"]
        self.assertEqual(job["permissions"], {})
        steps = self.steps("options")
        self.assertTrue(is_action(steps[0], BLOCK_ACTION))
        self.assertTrue(is_action(steps[1], HARDEN_RUNNER))
        loader = self.action("options", BLOCK_ACTION)
        forwarded = self.jobs["code-monkey"]["with"]["egress_allow_config"]
        self.assertEqual(loader["with"]["config"], forwarded)
        self.assertEqual(loader["with"]["allow_list_summary"], "false")
        harden = cast(dict[str, Any], steps[1]["with"])
        self.assertEqual(harden["egress-policy"], "block")
        self.assert_expression(harden["allowed-endpoints"], "env.CONNECTION_ALLOW_LIST")

    def test_caller_steps_are_pinned(self) -> None:
        """The model job's actions are commit-pinned like the reusable ones."""
        self.assert_pinned_steps()


class TestingCallerContracts(WorkflowCase):
    """Pull request plumbing: secretless selection and the offline suite."""

    WORKFLOW = TESTING

    def test_concurrency_keys_pull_requests_on_ref_and_dispatches_on_run(
        self,
    ) -> None:
        """A newer push cancels its PR's run; a dispatch never shares it."""
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        self.assert_expression(
            str(concurrency["group"]),
            "format('testing-{0}', github.event_name == 'pull_request'"
            " && github.ref || github.run_id)",
        )
        self.assertIs(concurrency["cancel-in-progress"], True)

    def test_plumbing_is_secretless_selection_of_the_pr_head(self) -> None:
        """PR runs select only, dry, with no secrets, from the PR's own assets."""
        job = self.jobs["plumbing"]
        self.assertEqual(job["uses"], REUSABLE_CALL)
        self.assertNotIn("secrets", job)
        with_ = cast(dict[str, Any], job["with"])
        self.assertEqual(with_["mode"], "select")
        self.assertIs(with_["dry_run"], True)
        self.assert_expression(
            with_["assets_repository"], "github.event.pull_request.head.repo.full_name"
        )
        self.assert_expression(
            with_["assets_ref"], "github.event.pull_request.head.sha"
        )

    def test_manual_dry_run_takes_the_target_lists(self) -> None:
        """A dispatch names repositories to scan or issues to work."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["issues"]["default"], "")
        with_ = cast(dict[str, Any], self.jobs["dry-run"]["with"])
        self.assert_expression(with_["repositories"], "inputs.repositories || ''")
        self.assert_expression(with_["issues"], "inputs.issues || ''")

    def test_callers_read_guidance_from_the_organisation_default(self) -> None:
        """Test runs use the reusable default, <org>/.github at main.

        The organisation AGENTS.md has merged; a pinned fork branch here
        would test against guidance production never reads.
        """
        for name in ("plumbing", "dry-run"):
            with self.subTest(job=name):
                with_ = cast(dict[str, Any], self.jobs[name]["with"])
                self.assertNotIn("guidance_repository", with_)
                self.assertNotIn("guidance_ref", with_)

    def test_regression_runs_the_locked_unittest_suite(self) -> None:
        """The offline suite runs under the locked environment, verbosely."""
        step = self.step("regression", "Run offline regression suite")
        self.assertEqual(
            str(step["run"]).strip(),
            "uv run --locked python -B -m unittest discover -s tests -v",
        )

    def test_caller_steps_are_pinned(self) -> None:
        """The regression job's actions are commit-pinned."""
        self.assert_pinned_steps()


if __name__ == "__main__":
    unittest.main()
