# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline contracts over the workflow YAML: trust boundary, pins, plumbing.

The reusable workflow separates a trusted ``select`` job, an untrusted
``author`` matrix and a trusted ``publish`` job (docs/DESIGN.md sections
4, 7, 8 and 10). These tests read parsed YAML and selected release
comments to check which job holds each credential, that downloads go by
producer artifact ID, and that actions are pinned to commits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from functools import cached_property
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar, NamedTuple, cast

import yaml  # pyright: ignore[reportMissingModuleSource]

sys.path.insert(0, str(Path(__file__).resolve().parent))
clean_git_env = import_module("git_env").clean_git_env

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REUSABLE = WORKFLOWS / "code-monkey.yaml"
CRON = WORKFLOWS / "code-monkey-cron.yaml"
EXPIRY = WORKFLOWS / "fork-pool-token-expiry.yaml"
FUSION_TIERS = ("standard", "high", "max")
TESTING = WORKFLOWS / "testing.yaml"
LINTING = WORKFLOWS / "linting.yaml"
PROMPT = WORKFLOWS.parents[1] / "prompt" / "author.md"
REVISE_PROMPT = WORKFLOWS.parents[1] / "prompt" / "revise.md"
TOOLCHAIN_PIN = WORKFLOWS.parents[1] / "tools" / "copilot-cli"
# The one place the select and publish jobs read the zizmor release from.
ZIZMOR_VERSION_PATH = "config/zizmor-version.txt"
ZIZMOR_VERSION_FILE = WORKFLOWS.parents[1] / ZIZMOR_VERSION_PATH
PRE_COMMIT_CONFIG = WORKFLOWS.parents[1] / ".pre-commit-config.yaml"

PLACEHOLDER_NAME = "Code Monkey"
PLACEHOLDER_EMAIL = "code-monkey@example.invalid"

# PyYAML reads the bare mapping key ``on`` as the boolean ``True``.
TRIGGERS = True

HARDEN_RUNNER = "step-security/harden-runner"
BLOCK_ACTION = "lfreleng-actions/harden-runner-block-action"
SLACK_ACTION = "slackapi/slack-github-action"
CHECKOUT = "actions/checkout"
APP_TOKEN = "actions/create-github-app-token"
SPLITTER = "lfreleng-actions/list-splitter-action"
# The input check's refusal of a run that names nothing (#196).
SCAN_REFUSED = (
    "scanning for new issues is off; name issues or pull_requests, or set revise_only"
)
DOWNLOAD = "actions/download-artifact"
UPLOAD = "actions/upload-artifact"
SETUP_UV = "astral-sh/setup-uv"
REUSABLE_CALL = "$/.github/workflows/code-monkey.yaml"
PREFLIGHT = "Pre-flight: workflow contracts and audit"
# The select job's uv steps, which the planted-workspace job runs verbatim.
UV_STEPS = ("Set up uv", "Install Python", "Install zizmor", PREFLIGHT)
SETUP_UV_WORKDIR = "${{ runner.temp }}"
PYTHON_INSTALL_DIR = "${{ runner.temp }}/uv-python"
SETUP_UV_ENV = {
    "UV_NO_CONFIG": "1",
    "UV_NO_PROJECT": "1",
    "UV_PYTHON_PREFERENCE": "lookup-disabled",
    "UV_PYTHON_ARCH": "",
    "UV_PYTHON_INSTALL_DIR": PYTHON_INSTALL_DIR,
}
SETUP_UV_WITH = {
    "working-directory": SETUP_UV_WORKDIR,
    "tool-bin-dir": "${{ runner.temp }}/uv-tool-bin",
    "enable-cache": False,
}
INSTALL_PYTHON = "Install Python"
UV_PATH = "${{ steps.setup-uv.outputs.uv-path }}"
TRUSTED_PYTHON = "${{ steps.python.outputs.path }}"
# uv by name resolves on PATH, which any earlier step can prepend to.
BARE_UV = re.compile(r"(?:^|[\s;&|(])uv\s+(?:run|venv|sync|export|pip|tool|python)\b")
# The uv calls that choose an interpreter unless given an absolute one.
UV_CALL = re.compile(r'"\$UV" (?:run|venv|sync|export|pip|tool)\b')

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


def load_jobs(path: Path) -> dict[str, dict[str, Any]]:
    """Parse a workflow file and return its jobs."""
    loaded = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    return cast(dict[str, dict[str, Any]], loaded["jobs"])


def runnable(step: dict[str, Any]) -> dict[str, Any]:
    """Keep the keys that decide what a step does, dropping its name and id."""
    return {
        key: step.get(key)
        for key in ("uses", "with", "env", "run", "working-directory")
    }


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


def command_lines(script: str) -> list[str]:
    """Return a script's lines with continuations joined, comments dropped."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def setup_uv_jobs() -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Return (workflow, job, steps) for every job that runs setup-uv."""
    found: list[tuple[str, str, list[dict[str, Any]]]] = []
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        for job, body in load_jobs(path).items():
            steps = cast(list[dict[str, Any]], body.get("steps", []))
            if any(is_action(step, SETUP_UV) for step in steps):
                found.append((path.name, job, steps))
    return found


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

    def test_concurrency_is_per_caller_and_owner_without_cancel(self) -> None:
        """Live scans lock per caller and owner; others within their run.

        Pending runs queue in order rather than a newcomer cancelling
        the one pending, but a dry run that writes nothing, or a live
        run naming its issues, must still not wait on the live scan.
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
        self.assertEqual(concurrency["queue"], "max")

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
        """Every job that holds a token loads the allow-list first.

        CONNECTION_ALLOW_LIST does not cross job boundaries, and
        harden-runner in block mode with an empty list enforces nothing.
        """
        for job in (
            "select",
            "forks",
            "publish",
            "report",
            "antagonistic",
            "antagonistic-notice",
        ):
            with self.subTest(job=job):
                loaders = self.actions(job, BLOCK_ACTION)
                self.assertEqual(len(loaders), 1)
                self.assert_expression(
                    loaders[0]["with"]["config"], "inputs.harden_runner_allowlist"
                )
                harden = self.action(job, HARDEN_RUNNER)
                self.assert_expression(
                    harden["with"]["allowed-endpoints"], "env.CONNECTION_ALLOW_LIST"
                )
        self.assertEqual(self.actions("author", BLOCK_ACTION), [])

    def test_every_loader_passes_the_supplemental_inputs(self) -> None:
        """Every loader forwards all five allow-list keys, even when empty.

        A key passed at one site and not another would leave that job
        blocking endpoints the caller allowed.
        """
        expected = {
            "config": "inputs.harden_runner_allowlist",
            "supplemental_config": "inputs.harden_runner_supplemental_allowlist",
            "supplemental_required": (
                "inputs.harden_runner_supplemental_allowlist != '' && 'true' || 'false'"
            ),
            "supplemental_unpinned": (
                "inputs.harden_runner_supplemental_unpinned && 'true' || 'false'"
            ),
            "extra_endpoints": "inputs.harden_runner_extra_endpoints",
        }
        loaders = [
            (job, step) for job in self.jobs for step in self.actions(job, BLOCK_ACTION)
        ]
        self.assertEqual(len(loaders), 6)
        for job, step in loaders:
            with_ = cast(dict[str, Any], step["with"])
            self.assertEqual(set(with_) - {"allow_list_summary"}, set(expected))
            for key, value in expected.items():
                with self.subTest(job=job, key=key):
                    self.assert_expression(with_[key], value)
            with self.subTest(job=job, key="if"):
                self.assert_expression(
                    step["if"], "inputs.harden_runner_egress == 'block'"
                )
            harden = self.action(job, HARDEN_RUNNER)
            with self.subTest(job=job, key="egress-policy"):
                self.assert_expression(
                    harden["with"]["egress-policy"], "inputs.harden_runner_egress"
                )


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
            subprocess.run(
                ["git", "init", "-q", "workspace"],
                cwd=work,
                check=True,
                env=clean_git_env(),
            )
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
                    env=clean_git_env(),
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

    def test_prompts_address_the_previous_rejection(self) -> None:
        """Both prompts point the agent at the reasons the publisher gave.

        The selection carries the last rejection's reasons as
        ``previous_rejection`` (DESIGN.md 6); unread, the next attempt
        repeats what the publisher refused.
        """
        for path in (PROMPT, REVISE_PROMPT):
            with self.subTest(prompt=path.name):
                prompt = squash(path.read_text(encoding="utf-8"))
                self.assertIn("the packet carries `previous_rejection`", prompt)
                self.assertIn(
                    "address each reason it lists, or abstain naming the "
                    "reason you cannot address",
                    prompt,
                )

    def test_prompts_state_the_executable_file_rule(self) -> None:
        """Both prompts forbid editing executables and ask for a mode self-check.

        ``check_change_mode`` rejects a content-only edit to an existing
        executable file, so a prompt that only forbids setting the bit
        lets the agent propose a change the publisher must refuse.
        """
        for path in (PROMPT, REVISE_PROMPT):
            with self.subTest(prompt=path.name):
                prompt = squash(path.read_text(encoding="utf-8"))
                self.assertIn(
                    "cannot add or edit an executable file, a symlink or a "
                    "submodule, or change a file's mode",
                    prompt,
                )
                self.assertIn(
                    "rejects any edit to an existing executable file, even "
                    "one that keeps its mode",
                    prompt,
                )
                self.assertIn("leave executable files untouched", prompt)
                self.assertIn("abstain and name the file", prompt)
                self.assertIn("git log --raw --format=%h <base", prompt)
                self.assertIn("each commit against its parent", prompt)
                self.assertIn("rewrite the commit that introduced it", prompt)
                self.assertNotIn("git diff --raw", prompt)
                self.assertIn("`100755`, `120000` or `160000`", prompt)
                self.assertIn("no path may change mode", prompt)
                self.assertNotIn("cannot set an executable bit", prompt)
                self.assertNotIn("no executable bits", prompt)

    def test_prompts_require_the_pull_request_title(self) -> None:
        """Both prompts require ``pr_title`` on every proposal.

        ``check_pull_request_text`` rejects a proposal without a title
        however many commits it holds, so a template that reads as
        "only when there is one commit" costs a multi-commit round.
        """
        for path in (PROMPT, REVISE_PROMPT):
            with self.subTest(prompt=path.name):
                prompt = squash(path.read_text(encoding="utf-8"))
                self.assertNotIn("when there is one commit", prompt)
                self.assertIn(
                    '"pr_title": "<required: the PR title; on one commit, '
                    'its subject>"',
                    prompt,
                )
                self.assertIn(
                    "`pr_title` and `pr_body` are **required** whenever "
                    "`outcome` is `proposed`",
                    prompt,
                )
                self.assertIn("whatever the commit count", prompt)
                self.assertIn("read `manifest.json` back", prompt)
        author = squash(PROMPT.read_text(encoding="utf-8"))
        self.assertIn(
            "confirm the file parses as JSON and, when `outcome` is "
            "`proposed`, carries a non-empty `pr_title` and `pr_body`",
            author,
        )
        revise = squash(REVISE_PROMPT.read_text(encoding="utf-8"))
        self.assertIn(
            "confirm the file parses as JSON, carries a non-empty "
            "`round_summary` unless `outcome` is `abstain`, and, when "
            "`outcome` is `proposed`, carries a non-empty `pr_title` and "
            "`pr_body`",
            revise,
        )
        self.assertIn("keep the pull request's current title", revise)

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

    def test_toolchain_installs_from_the_committed_lockfile(self) -> None:
        """npm ci from the verified assets checkout's lockfile, never a version."""
        self.assertNotIn("COPILOT_CLI_VERSION", self.workflow.get("env") or {})
        install = self.step("author", "Install agent toolchain")
        env = cast(dict[str, Any], install["env"])
        self.assertRegex(str(env["PIN_DIR"]), r"^monkey-assets/")
        script = flatten(str(install["run"]))
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", script)
        self.assertNotIn("npm install -g", script)
        self.assertIn(
            """"$UV" tool install --python "$PYTHON" 'prek==0.4.10'""", script
        )
        self.assertEqual(install["env"]["UV"], UV_PATH)
        self.assertEqual(install["env"]["PYTHON"], TRUSTED_PYTHON)

    def test_no_workflow_installs_an_npm_package_by_version(self) -> None:
        """A version pin leaves its dependency tree to resolve live."""
        for path in sorted(WORKFLOWS.glob("*.yaml")):
            for job, body in load_jobs(path).items():
                for step in cast(list[dict[str, Any]], body.get("steps", [])):
                    with self.subTest(workflow=path.name, job=job):
                        self.assertNotRegex(
                            flatten(str(step.get("run", ""))),
                            r"\bnpm (install|i)\b",
                        )

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

    def test_prompts_say_how_a_check_is_recorded(self) -> None:
        """Both prompts carry the four rules on ``commands`` (issue #224).

        Rule 3's ``SKIP=`` sits beside no word for review, so the
        skip detector still finds nothing in either prompt.
        """
        phrases = (
            "**The exit status is the check's own.**",
            "`$?` belongs to the last stage",
            "**Explain a failure from its whole output.**",
            "until you have excluded that failure and run the check again",
            "**Exclude by id a hook this runner cannot run.**",
            "`SKIP=<id> prek run …`",
            "Never exclude a hook that ran and reported a finding.",
            "**The last hook run listed is the one the proposal stands on.**",
            "taken after your last commit and written as it ran, `SKIP=` included",
            "a warning in its run report naming that run and its exit code",
        )
        for path in (PROMPT, REVISE_PROMPT):
            text = squash(path.read_text(encoding="utf-8"))
            for phrase in phrases:
                with self.subTest(prompt=path.name, phrase=phrase):
                    self.assertIn(phrase, text)
            self.assertEqual(skip_instructions(text), [])


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
        self.assert_expression(with_["pull_requests"], "inputs.pull_requests")
        self.assert_expression(with_["revise"], "inputs.revise")

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
        self.assertIn('now="$(git -C workspace rev-parse FETCH_HEAD)"', script)
        self.assertIn('if [ "$now" = "$HEAD_SHA" ]', script)
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

    def test_a_moved_branch_warns_and_never_errors(self) -> None:
        """A moved or missing branch is a skip: a warning and an output, never an error."""
        fetch = self.step("author", "fetch-branch")
        self.assertEqual(fetch["name"], "Fetch pull request branch")
        script = flatten(fetch["run"])
        self.assertNotIn("::error::", script)
        self.assertIn('echo "::warning::Skipped: $reason"', script)
        self.assertIn("echo 'moved=true'", script)
        self.assertIn('echo "reason=$reason"', script)
        self.assertIn('"$GITHUB_OUTPUT"', script)

    def test_every_session_step_skips_when_the_branch_moved(self) -> None:
        """No toolchain, prompt or session after the round stood down.

        The packet step runs, since the bundle step reads its outputs,
        and the bundle step runs to write the abstention.
        """
        guard = "steps.fetch-branch.outputs.moved != 'true'"
        steps = self.steps("author")
        start = self.position("author", "fetch-branch") + 1
        end = self.position("author", "Bundle proposed commits")
        self.assertIn(
            "Run coding agent (Copilot)", [s.get("name") for s in steps[start:end]]
        )
        for step in steps[start:end]:
            if step.get("id") == "packet":
                continue
            with self.subTest(step=step.get("name")):
                self.assert_expression(str(step.get("if", "")), guard)
        packet = flatten(self.step("author", "packet")["run"])
        self.assertIn('if [ "$TASK" = revise ] && [ "$MOVED" != true ]', packet)
        self.assert_expression(
            self.step("author", "packet")["env"]["MOVED"],
            "steps.fetch-branch.outputs.moved",
        )

    def fetch_fixture(self, holder: Path) -> tuple[dict[str, str], str, str]:
        """A fork with the bot branch at two commits, and a checkout to fetch into.

        Returns the step environment, which points github.com at the
        fixture, and the first and second commits on the branch.
        """
        env = clean_git_env()
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.file://{holder}/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            }
        )

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        fork = holder / "forks" / "repo.git"
        source = holder / "source"
        git("init", "-q", "-b", "main", str(source))
        git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "Base")
        git("-C", str(source), "switch", "-q", "-c", "code-monkey/issue-7")
        git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "One")
        first = git("-C", str(source), "rev-parse", "HEAD")
        git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "Two")
        second = git("-C", str(source), "rev-parse", "HEAD")
        git("-C", str(source), "switch", "-q", "main")
        git("clone", "-q", "--bare", str(source), str(fork))
        git("clone", "-q", str(source), str(holder / "run" / "workspace"))
        env.update(
            {
                "FORK_REPOSITORY": "forks/repo",
                "BRANCH": "code-monkey/issue-7",
                "GITHUB_OUTPUT": str(holder / "output"),
            }
        )
        return env, first, second

    def run_fetch(self, env: dict[str, str], holder: Path) -> tuple[int, str, str]:
        """Run the fetch step; return its exit code, stdout and outputs."""
        fetch = self.step("author", "Fetch pull request branch")
        output = holder / "output"
        output.write_text("", encoding="utf-8")
        proc = subprocess.run(
            ["bash", "-e", "-c", str(fetch["run"])],
            cwd=holder / "run",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout, output.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git")
    def test_fetch_stands_down_on_a_moved_or_missing_branch(self) -> None:
        """The recorded head checks out; a moved or gone branch warns and exits 0."""
        with tempfile.TemporaryDirectory() as name:
            holder = Path(name)
            env, first, second = self.fetch_fixture(holder)

            code, stdout, outputs = self.run_fetch({**env, "HEAD_SHA": second}, holder)
            self.assertEqual((code, stdout, outputs), (0, "", ""))
            current = subprocess.run(
                ["git", "-C", str(holder / "run" / "workspace"), "branch", "--show"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(current, "code-monkey/issue-7")

            code, stdout, outputs = self.run_fetch({**env, "HEAD_SHA": first}, holder)
            moved = (
                f"code-monkey/issue-7 in forks/repo moved from {first[:7]} to "
                f"{second[:7]} after this run selected it. Another run has "
                "revised this pull request since (a named or revise-only "
                "dispatch); its work stands, and this round made no change. "
                "A later round starts from the pull request's new head."
            )
            self.assertEqual(code, 0)
            self.assertEqual(stdout, f"::warning::Skipped: {moved}\n")
            self.assertEqual(outputs, f"moved=true\nreason={moved}\n")

            gone = {**env, "HEAD_SHA": first, "BRANCH": "code-monkey/issue-8"}
            code, stdout, outputs = self.run_fetch(gone, holder)
            vanished = (
                "code-monkey/issue-8 in forks/repo no longer exists; the pull "
                "request was merged or closed since this run selected it."
            )
            self.assertEqual(code, 0)
            self.assertEqual(stdout, f"::warning::Skipped: {vanished}\n")
            self.assertEqual(outputs, f"moved=true\nreason={vanished}\n")

            # Any other failure is a failure, not a skip.
            broken = {**env, "HEAD_SHA": first, "FORK_REPOSITORY": "forks/absent"}
            code, stdout, outputs = self.run_fetch(broken, holder)
            self.assertNotEqual(code, 0)
            self.assertNotIn("::warning::", stdout)
            self.assertEqual(outputs, "")

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_a_stood_down_round_bundles_an_abstention(self) -> None:
        """The bundle step writes `abstain` with the fetch step's reason."""
        bundle = self.step("author", "Bundle proposed commits")
        env = cast(dict[str, Any], bundle["env"])
        self.assert_expression(env["MOVED"], "steps.fetch-branch.outputs.moved")
        self.assert_expression(env["MOVED_REASON"], "steps.fetch-branch.outputs.reason")
        reason = "code-monkey/issue-7 in forks/repo moved from aaaaaaa to bbbbbbb."
        with tempfile.TemporaryDirectory() as name:
            work = Path(name)
            (work / "artefacts").mkdir()
            proc = subprocess.run(
                ["bash", "-e", "-c", str(bundle["run"])],
                cwd=work,
                env={
                    "PATH": os.environ["PATH"],
                    "BRANCH": "code-monkey/issue-7",
                    "BASE_SHA": "a" * 40,
                    "REBASE_SHA": "c" * 40,
                    "REPOSITORY": "owner/repo",
                    "ISSUE": "7",
                    "AGENT_OUTCOME": "skipped",
                    "MOVED": "true",
                    "MOVED_REASON": reason,
                },
                capture_output=True,
                text=True,
                check=True,
            )
            manifest = json.loads(
                (work / "artefacts" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse((work / "artefacts" / "changes.bundle").exists())
        self.assertEqual(
            manifest,
            {
                "schema": 1,
                "outcome": "abstain",
                "repository": "owner/repo",
                "issue": 7,
                "base_sha": "a" * 40,
                "branch": "code-monkey/issue-7",
                "reason": reason,
                "superseded": True,
            },
        )
        self.assertEqual(proc.stdout, "Manifest outcome: abstain\n")

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_only_a_stood_down_round_is_superseded(self) -> None:
        """The stand-down marks its manifest; an agent's mark is removed (issue #154)."""
        bundle = self.step("author", "Bundle proposed commits")

        def run(moved: str, agent: dict[str, Any] | None) -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as name:
                work = Path(name)
                (work / "artefacts").mkdir()
                if agent is not None:
                    (work / "artefacts" / "manifest.json").write_text(
                        json.dumps(agent), encoding="utf-8"
                    )
                subprocess.run(
                    ["bash", "-e", "-c", str(bundle["run"])],
                    cwd=work,
                    env={
                        "PATH": os.environ["PATH"],
                        "BRANCH": "code-monkey/issue-7",
                        "BASE_SHA": "a" * 40,
                        "REBASE_SHA": "c" * 40,
                        "REPOSITORY": "owner/repo",
                        "ISSUE": "7",
                        "AGENT_OUTCOME": "success",
                        "MOVED": moved,
                        "MOVED_REASON": "moved",
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return json.loads(
                    (work / "artefacts" / "manifest.json").read_text(encoding="utf-8")
                )

        self.assertIs(run("true", None)["superseded"], True)
        forged = {
            "schema": 1,
            "outcome": "abstain",
            "repository": "owner/repo",
            "issue": 7,
            "base_sha": "a" * 40,
            "branch": "code-monkey/issue-7",
            "reason": "r",
            "superseded": True,
        }
        cleaned = run("", forged)
        self.assertNotIn("superseded", cleaned)
        self.assertEqual(
            cleaned, {k: v for k, v in forged.items() if k != "superseded"}
        )
        # A manifest without the field passes as it was.
        self.assertEqual(run("", {"outcome": "abstain"}), {"outcome": "abstain"})

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
        """The fork mint needs a proposal, so a handoff's push step holds no token.

        Any GitHub read a handoff needs, the head check above all,
        therefore belongs in ``respond``, which holds the pull request
        token; the push step must stay offline for a handoff.
        """
        condition = unwrap(str(self.step("publish", "fork-token")["if"]))
        self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
        push = self.step("publish", "push")
        self.assertEqual(
            unwrap(str(push["env"]["GH_TOKEN"])), "steps.fork-token.outputs.token"
        )
        self.assertNotIn("github.token", str(push["env"]["GH_TOKEN"]))
        respond = self.step("publish", "respond")
        self.assertNotIn("steps.check.outputs.verdict", str(respond["if"]))

    def test_publish_serialises_per_pull_request(self) -> None:
        """One publish at a time per pull request, across named and scanned runs.

        The workflow-level groups keep runs apart by their target
        lists; two lists that overlap on one pull request, or a scan
        and a named run, still reach it together. The retitle, push,
        restore and respond writes of a revision must not interleave
        with another run's, so the publish job locks on the pull
        request it revises, and a new issue's publish on its issue.
        A dry run writes nothing and locks within its own run.
        """
        publish = self.jobs["publish"]
        concurrency = cast(dict[str, Any], publish["concurrency"])
        group = unwrap(str(concurrency["group"]))
        self.assertIn("inputs.dry_run", group)
        self.assertIn("github.run_id", group)
        self.assertIn("matrix.repository", group)
        self.assertIn("matrix.task", group)
        self.assertIn("matrix.pull_number", group)
        self.assertIn("matrix.number", group)
        self.assertIs(concurrency["cancel-in-progress"], False)
        self.assertEqual(concurrency["queue"], "max")

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
        """The request runs once the pull request is open, falling back to its token."""
        step = self.step("publish", "review-request")
        condition = unwrap(str(step["if"]))
        self.assertIn("steps.open.outcome == 'success'", condition)
        self.assertIn("needs.select.outputs.request_review == 'true'", condition)
        self.assertIn("steps.pr-token.outputs.token", unwrap(step["env"]["GH_TOKEN"]))
        self.assertIn("publish.py review-request", flatten(step["run"]))
        self.assertLess(
            self.position("publish", "open"), self.position("publish", "review-request")
        )

    def test_wait_reads_with_the_native_token_and_fits_the_job(self) -> None:
        """The wait reads a public target with the job's own token, within its budget."""
        step = self.step("publish", "review")
        condition = unwrap(str(step["if"]))
        self.assertIn("steps.open.outcome == 'success'", condition)
        self.assertIn("needs.select.outputs.request_review == 'true'", condition)
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

    def test_request_prefers_the_review_token(self) -> None:
        """A Copilot user's token asks when the caller holds one; else the App's.

        An installation token is no Copilot user, so GitHub accepts its
        request without adding the reviewer (DESIGN.md section 17). The
        step logs which credential it used, never the token.
        """
        step = self.step("publish", "review-request")
        env = cast(dict[str, Any], step["env"])
        self.assert_expression(
            env["GH_TOKEN"],
            "secrets.copilot_review_token != '' && secrets.copilot_review_token"
            " || steps.pr-token.outputs.token",
        )
        self.assert_expression(
            env["REVIEW_CREDENTIAL"],
            "secrets.copilot_review_token != '' && 'review token' || 'App token'",
        )
        self.assertIn('--credential "$REVIEW_CREDENTIAL"', flatten(step["run"]))

    def test_review_token_reaches_the_request_step_alone(self) -> None:
        """No other step, no job-level key and no output sees the review token."""
        holders: list[tuple[str, str]] = []
        for name, job in self.jobs.items():
            rest = {key: value for key, value in job.items() if key != "steps"}
            with self.subTest(job=name):
                self.assertNotIn("secrets.copilot_review_token", dumped(rest))
            for step in cast(list[dict[str, Any]], job.get("steps", [])):
                if "secrets.copilot_review_token" in dumped(step):
                    holders.append((name, str(step.get("id"))))
        self.assertEqual(holders, [("publish", "review-request")])
        step = self.step("publish", "review-request")
        outside = {key: value for key, value in step.items() if key != "env"}
        self.assertNotIn("secrets.copilot_review_token", dumped(outside))
        # The wait keeps the job's own token: the target is public.
        self.assertEqual(
            SECRET_REFERENCE.findall(dumped(self.step("publish", "review"))), []
        )
        self.assertEqual(
            Counter(SECRET_REFERENCE.findall(dumped(self.jobs["author"]))),
            Counter({"secrets.copilot_token": 1}),
        )

    def test_review_token_is_an_optional_secret_the_caller_forwards(self) -> None:
        """Declared optional, so a caller without it keeps the App's request."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        secret = cast(dict[str, Any], call["secrets"]["copilot_review_token"])
        self.assertIs(secret["required"], False)
        self.assertIn("Pull requests", str(secret["description"]))
        cron = cast(
            dict[str | bool, Any], yaml.safe_load(CRON.read_text(encoding="utf-8"))
        )
        secrets = cast(dict[str, Any], cron["jobs"]["code-monkey"]["secrets"])
        self.assert_expression(
            secrets["copilot_review_token"], "secrets.COPILOT_REVIEW_TOKEN"
        )

    def test_caller_forwards_the_gate_on_by_default(self) -> None:
        """The cron form offers the switch, on by default, and forwards it."""
        cron = cast(
            dict[str | bool, Any], yaml.safe_load(CRON.read_text(encoding="utf-8"))
        )
        inputs = cast(
            dict[str, dict[str, Any]], cron[TRIGGERS]["workflow_dispatch"]["inputs"]
        )
        self.assertIs(inputs["request_review"]["default"], True)
        with_ = cast(dict[str, Any], cron["jobs"]["code-monkey"]["with"])
        self.assert_expression(with_["request_review"], "inputs.request_review")

    def test_wait_reads_the_checks_with_the_native_token_alone(self) -> None:
        """The checks share the wait's step and token; no secret reaches it (#110)."""
        step = self.step("publish", "review")
        self.assert_expression(step["env"]["GH_TOKEN"], "github.token")
        self.assertEqual(set(step["env"]), {"GH_TOKEN", "STALE_MINUTES"})
        self.assertNotIn("secrets.", dumped(step))
        self.assertNotIn("continue-on-error", step)

    def test_probe_reads_mergeability_after_the_review_gate(self) -> None:
        """The probe follows the wait, precedes the comment and only reads (#104)."""
        step = self.step("publish", "merge-probe")
        self.assertEqual(step["name"], "Probe mergeability")
        condition = unwrap(str(step["if"]))
        self.assertIn("!cancelled()", condition)
        self.assertIn("steps.open.outcome == 'success'", condition)
        self.assert_expression(step["env"]["GH_TOKEN"], "github.token")
        self.assertEqual(set(step["env"]), {"GH_TOKEN"})
        self.assertNotIn("secrets.", dumped(step))
        script = flatten(step["run"])
        self.assertIn("scripts/merge_state.py", script)
        self.assertIn("--result artefacts/result.json", script)
        self.assertIn("--check artefacts/check.json", script)
        self.assertIn("--output artefacts/result.json", script)
        order = [
            self.position("publish", name)
            for name in ("open", "review", "merge-probe", "comment-token")
        ]
        self.assertEqual(order, sorted(order))


class PublishJobContracts(ReusableWorkflowCase):
    """The trusted publisher: verify before fetch, App tokens per repository."""

    def test_publish_runs_after_author_with_read_grants(self) -> None:
        """Every author entry gets a verdict; the job itself can write nothing."""
        publish = self.jobs["publish"]
        self.assertEqual(publish["needs"], ["select", "author", "forks"])
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
        """The check has no token; the fork mint names one fork, workflows always."""
        check = self.step("publish", "check")
        script = flatten(check["run"])
        self.assertIn("publish.py check", script)
        self.assertIn("--coauthors monkey-assets/config/coauthors.json", script)
        self.assertNotIn("GH_TOKEN", cast(dict[str, Any], check.get("env", {})))
        # No step may gate on the proposal's workflow edits any longer.
        self.assertNotIn("needs_workflows", script)

        mint = self.step("publish", "fork-token")
        self.assertTrue(is_action(mint, APP_TOKEN))
        condition = unwrap(str(mint["if"]))
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
        self.assertIn("inputs.github_app_client_id != ''", condition)
        # The forks job made the fork; with none, nothing is minted.
        self.assertIn("steps.fork-state.outputs.exists == 'true'", condition)
        with_ = cast(dict[str, Any], mint["with"])
        self.assert_expression(with_["owner"], "matrix.fork_org")
        self.assert_expression(with_["repositories"], "matrix.repo_name")
        self.assertEqual(with_["permission-contents"], "write")
        self.assertEqual(with_["permission-metadata"], "read")
        self.assertNotIn("permission-pull-requests", with_)
        self.assertNotIn("permission-issues", with_)
        # Syncing the fork can fast-forward over upstream workflow
        # commits, so the grant cannot depend on the proposal's own diff.
        self.assertEqual(with_["permission-workflows"], "write")

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

    def test_fork_mint_scope_is_one_fork_with_fixed_grants(self) -> None:
        """Naming the installation on failure leaves the mint's request alone."""
        mint = self.step("publish", "fork-token")
        self.assertEqual(
            mint["with"],
            {
                "client-id": "${{ inputs.github_app_client_id }}",
                "private-key": "${{ secrets.github_app_private_key }}",
                "owner": "${{ matrix.fork_org }}",
                "repositories": "${{ matrix.repo_name }}",
                "permission-contents": "write",
                "permission-metadata": "read",
                "permission-workflows": "write",
            },
        )
        self.assertNotIn("continue-on-error", mint)
        identity = self.step("publish", "Pre-flight: fork token identity")
        self.assertEqual(identity["if"], "steps.fork-token.outcome == 'success'")

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_failed_fork_mint_names_the_installation(self) -> None:
        """A mint refused for an existing fork points at the installation."""
        ensure = self.step("publish", "Ensure a result exists")
        env_ = cast(dict[str, str], ensure["env"])
        self.assert_expression(env_["FORK_ORG"], "matrix.fork_org")
        self.assert_expression(env_["FORK_EXISTS"], "steps.fork-state.outputs.exists")
        self.assert_expression(env_["MINT_OUTCOME"], "steps.fork-token.outcome")
        named = (
            "could not mint a token for lfreleng-bot-forks/repo; the App's"
            " installation on lfreleng-bot-forks must cover all repositories"
        )
        base_env = {
            "PATH": os.environ["PATH"],
            "KEY": "repo-7",
            "REPOSITORY": "o/repo",
            "FORK_REPOSITORY": "lfreleng-bot-forks/repo",
            "FORK_ORG": "lfreleng-bot-forks",
            "ISSUE": "7",
            "DRY_RUN": "false",
            "MODE": "pull-requests",
            "RUN_ATTEMPT": "1",
            "CHECK_OUTCOME": "success",
            "OPEN_OUTCOME": "skipped",
            "AUTHOR_SESSION": "",
        }
        cases = [
            ("true", "failure", "skipped", True),
            ("true", "success", "failure", False),
            ("true", "skipped", "skipped", False),
            ("false", "skipped", "failure", False),
        ]
        for exists, mint, push, expected in cases:
            with self.subTest(exists=exists, mint=mint, push=push):
                with tempfile.TemporaryDirectory() as holder:
                    work = Path(holder)
                    summary = work / "summary.md"
                    summary.touch()
                    subprocess.run(
                        ["bash", "-e", "-c", str(ensure["run"])],
                        cwd=work,
                        env={
                            **base_env,
                            "FORK_EXISTS": exists,
                            "MINT_OUTCOME": mint,
                            "PUSH_OUTCOME": push,
                            "GITHUB_STEP_SUMMARY": str(summary),
                        },
                        check=True,
                        capture_output=True,
                    )
                    result = json.loads(
                        (work / "artefacts" / "result.json").read_text(encoding="utf-8")
                    )
                    written = summary.read_text(encoding="utf-8")
                self.assertEqual(result["verdict"], "publish-failed")
                if expected:
                    self.assertEqual(result["reasons"], [named])
                    self.assertIn(named, written)
                else:
                    self.assertIn("publish did not complete", result["reasons"][0])
                    self.assertNotIn("could not mint", result["reasons"][0])
                    self.assertEqual(written, "")

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
            push["env"]["GH_TOKEN"], "steps.fork-token.outputs.token"
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
        # public actions for the online zizmor audit, whether a public
        # fork exists, and the public target's reviews,
        # check runs and mergeability; the job grants it actions: read
        # and nothing more.
        users = [
            step.get("name")
            for step in self.steps("publish")
            if "github.token" in dumped(step)
        ]
        self.assertEqual(
            users,
            [
                "Fetch and accept bounded proposal",
                "Check proposal offline",
                "Check fork exists",
                "Await Copilot review",
                "Probe mergeability",
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

    def test_issue_author_list_is_checked_before_any_token(self) -> None:
        """An empty or malformed list fails the run before the read mint."""
        step = self.step("select", "Pre-flight: configuration and credentials")
        self.assertEqual(step["env"]["ISSUE_AUTHORS"], "${{ inputs.issue_authors }}")
        self.assertNotIn("GH_TOKEN", step["env"])
        self.assertIn(
            'issue_authors.py check --authors="$ISSUE_AUTHORS"', flatten(step["run"])
        )
        inputs = cast(dict[str, Any], self.triggers["workflow_call"]["inputs"])
        self.assertEqual(inputs["issue_authors"]["default"], "")

    def test_selection_reads_through_the_issue_author_list(self) -> None:
        """Select hands the same list on; it records it for the publisher."""
        step = self.step("select", "Select issues")
        self.assertEqual(step["env"]["ISSUE_AUTHORS"], "${{ inputs.issue_authors }}")
        self.assertIn('--issue-authors="$ISSUE_AUTHORS"', flatten(step["run"]))

    def test_only_known_authors_is_checked_before_any_token(self) -> None:
        """The flag is resolved with the list at pre-flight, before the read mint."""
        config = self.position("select", "Pre-flight: configuration and credentials")
        self.assertLess(config, self.position("select", "Mint read-only App token"))
        step = self.step("select", "Pre-flight: configuration and credentials")
        self.assertEqual(
            step["env"]["ONLY_KNOWN_AUTHORS"], "${{ inputs.only_known_authors }}"
        )
        self.assertIn(
            'issue_authors.py check --authors="$ISSUE_AUTHORS"'
            ' --only-known-authors="$ONLY_KNOWN_AUTHORS"',
            flatten(step["run"]),
        )
        inputs = cast(dict[str, Any], self.triggers["workflow_call"]["inputs"])
        self.assertEqual(inputs["only_known_authors"]["type"], "string")
        self.assertEqual(inputs["only_known_authors"]["default"], "")

    def test_selection_resolves_the_same_flag(self) -> None:
        """Select records the mode it ran with for the publisher."""
        step = self.step("select", "Select issues")
        self.assertEqual(
            step["env"]["ONLY_KNOWN_AUTHORS"], "${{ inputs.only_known_authors }}"
        )
        self.assertIn(
            '--only-known-authors="$ONLY_KNOWN_AUTHORS"', flatten(step["run"])
        )

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
            "${{ steps.fork-token.outputs.app-slug }}",
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
        """The auditor the gate runs is the exact version one file names."""
        lines = [
            line.strip()
            for line in ZIZMOR_VERSION_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\d+\.\d+\.\d+$")
        run = flatten(self.step("select", "Install zizmor")["run"])
        self.assertIn(f"monkey-assets/{ZIZMOR_VERSION_PATH}", run)
        self.assertIn('[[ ! "$version" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]]', run)
        self.assertIn('"$UV" tool install --python "$PYTHON" "zizmor==$version"', run)
        self.assertNotRegex(run, r"zizmor==\d")

    def test_preflight_uv_run_sees_only_the_assets_checkout(self) -> None:
        """Nothing but the pinned assets lands in the workspace before uv runs.

        uv in the assets directory takes a pyproject.toml above it that
        declares a uv workspace as its workspace root, lock file
        included, so the job must not check out or download anything
        else first.
        """
        before = self.steps("select")[: self.position("select", PREFLIGHT)]
        checkouts = [s for s in before if is_action(s, CHECKOUT)]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]["with"]["path"], "monkey-assets")
        self.assertEqual(
            checkouts[0]["with"]["repository"], "${{ inputs.assets_repository }}"
        )
        self.assertEqual([s for s in before if is_action(s, DOWNLOAD)], [])
        run = str(self.step("select", PREFLIGHT)["run"])
        self.assertIn("\ncd monkey-assets\n", run)

    def test_preflight_environment_is_built_outside_the_workspace(self) -> None:
        """uv run gets the trusted interpreter and an env in RUNNER_TEMP.

        uv run in a project runs the interpreter of the project's .venv
        even when given an absolute --python; only a project environment
        outside the checkout keeps it from doing so.
        """
        step = self.step("select", PREFLIGHT)
        run = flatten(step["run"])
        gate = (
            'UV_PROJECT_ENVIRONMENT="$RUNNER_TEMP/preflight-venv"'
            ' "$UV" run --locked --python "$PYTHON" python -B scripts/preflight.py'
        )
        self.assertEqual(step["env"], {"UV": UV_PATH, "PYTHON": TRUSTED_PYTHON})
        self.assertIn(gate, run)
        self.assertNotIn("python find", run)
        self.assertLess(run.index("cd monkey-assets"), run.index(gate))
        self.assertEqual(run.count("uv run --"), 0)
        self.assertEqual(run.count('"$UV" run --'), 1)
        self.assertLess(
            self.position("select", INSTALL_PYTHON), self.position("select", PREFLIGHT)
        )


class PublishAuditInstallContracts(ReusableWorkflowCase):
    """The publish job installs zizmor exactly as the select job does."""

    INSTALL_STEPS = ("Set up uv", "Install Python", "Install zizmor")

    def test_publish_runs_the_select_install_steps(self) -> None:
        """Same action, settings, environment, script and working directory."""
        for name in self.INSTALL_STEPS:
            with self.subTest(step=name):
                publish = self.step("publish", name)
                select = self.step("select", name)
                self.assertEqual(runnable(publish), runnable(select))
                self.assertEqual(publish.get("id"), select.get("id"))
                self.assertEqual(publish.get("shell"), select.get("shell"))

    def test_publish_installs_zizmor_before_any_untrusted_input(self) -> None:
        """uv runs beside the publisher checkout alone, before the check.

        The proposal artifact lands in the workspace later, so nothing
        it carries can steer uv; the check needs zizmor on PATH.
        """
        order = [
            self.position("publish", "Checkout trusted publisher"),
            *(self.position("publish", name) for name in self.INSTALL_STEPS),
            self.position("publish", "Fetch selection evidence"),
            self.position("publish", "Fetch and accept bounded proposal"),
            self.position("publish", "Check proposal offline"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(
            order[: len(self.INSTALL_STEPS) + 1],
            list(range(order[0], order[0] + len(self.INSTALL_STEPS) + 1)),
        )

    def test_check_audits_online_with_the_native_token(self) -> None:
        """The check step hands zizmor the native token, read-only.

        The job grants it ``actions: read`` and ``contents: read``:
        enough for zizmor's online audits on public actions, and no
        write anywhere.
        """
        check = self.step("publish", "Check proposal offline")
        self.assert_expression(check["env"]["ZIZMOR_GITHUB_TOKEN"], "github.token")
        self.assertNotIn("GH_TOKEN", check["env"])
        self.assertNotIn("GITHUB_TOKEN", check["env"])


class MintProvenanceContracts(ReusableWorkflowCase):
    """Every App token names a known owner and carries the least it needs."""

    FORK_MINTS = ("fork-token",)
    TARGET_MINTS = (
        ("select", "app-token"),
        ("publish", "pr-token"),
        ("publish", "comment-token"),
        ("antagonistic-notice", "notice-token"),
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
            sorted(
                [
                    "app-token",
                    "comment-token",
                    "notice-token",
                    "pr-token",
                    *self.FORK_MINTS,
                ]
            ),
        )
        for job, step in found:
            with self.subTest(job=job, mint=step["id"]):
                owner = unwrap(str(cast(dict[str, Any], step["with"])["owner"]))
                expected = (
                    "matrix.fork_org" if step["id"] in self.FORK_MINTS else "inputs.org"
                )
                self.assertEqual(owner, expected)

    def test_no_mint_gains_a_permission(self) -> None:
        """Reading the head's checks adds no App permission anywhere (#110)."""
        granted = {
            str(step["id"]): {
                key: value
                for key, value in cast(dict[str, Any], step["with"]).items()
                if key.startswith("permission-")
            }
            for _, step in self.mints()
        }
        fork = {
            "permission-contents": "write",
            "permission-metadata": "read",
            "permission-workflows": "write",
        }
        self.assertEqual(
            granted,
            {
                "app-token": {
                    "permission-issues": "read",
                    "permission-metadata": "read",
                    "permission-contents": "read",
                    "permission-pull-requests": "read",
                    "permission-checks": "read",
                },
                "fork-token": fork,
                "pr-token": {
                    "permission-pull-requests": "write",
                    "permission-metadata": "read",
                },
                "comment-token": {
                    "permission-issues": "write",
                    "permission-metadata": "read",
                },
                "notice-token": {
                    "permission-pull-requests": "write",
                    "permission-metadata": "read",
                },
            },
        )

    def test_no_mint_requests_actions(self) -> None:
        """No App token reads job logs: no mint requests Actions at any level."""
        for job, step in self.mints():
            with self.subTest(job=job, mint=step["id"]):
                with_ = cast(dict[str, Any], step["with"])
                env = cast(dict[str, Any], step.get("env") or {})
                self.assertNotIn("permission-actions", with_)
                self.assertNotIn("INPUT_PERMISSION-ACTIONS", env)

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


POOL_TOKEN = "secrets.fork_pool_token"


class ForkPreparationContracts(ReusableWorkflowCase):
    """Forks are made outside the App, so no mint may administer (#202).

    Creating or renaming a fork needs Administration: write, which an
    App holds on every installation at once, the target organisation
    included. A machine account's token does it in a job of its own.
    """

    def test_forks_job_runs_beside_the_author_on_live_writing_runs(self) -> None:
        """After select, before publish, never in a dry run or select mode."""
        job = self.jobs["forks"]
        self.assertEqual(job["name"], "Prepare forks")
        self.assertEqual(job["needs"], "select")
        self.assert_expression(
            job["if"],
            "needs.select.outputs.run_agent == 'true' &&"
            " !inputs.dry_run && inputs.mode != 'select'",
        )
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(self.actions("forks", APP_TOKEN), [])
        self.assertEqual(self.jobs["publish"]["needs"], ["select", "author", "forks"])

    def test_forks_job_is_hardened_like_the_other_trusted_jobs(self) -> None:
        """The allow-list loads first and harden-runner blocks against it."""
        steps = self.steps("forks")
        self.assertTrue(is_action(steps[0], BLOCK_ACTION))
        self.assertTrue(is_action(steps[1], HARDEN_RUNNER))
        loader = cast(dict[str, Any], steps[0]["with"])
        self.assert_expression(loader["config"], "inputs.harden_runner_allowlist")
        self.assertEqual(loader["allow_list_summary"], "false")
        self.assert_expression(
            steps[1]["with"]["allowed-endpoints"], "env.CONNECTION_ALLOW_LIST"
        )

    def test_forks_job_runs_trusted_assets_alone(self) -> None:
        """It checks out the pinned assets, never a target, and reads the matrix."""
        guard = flatten(self.step("forks", "Require trusted provenance")["run"])
        self.assertIn('[[ "$ASSETS_SHA" =~ ^[0-9a-f]{40}$ ]]', guard)
        checkout = self.action("forks", CHECKOUT)
        with_ = cast(dict[str, Any], checkout["with"])
        self.assert_expression(with_["repository"], "inputs.assets_repository")
        self.assert_expression(with_["ref"], "needs.select.outputs.assets_sha")
        self.assertEqual(with_["path"], "monkey-assets")
        prepare = self.step("forks", "prepare")
        script = flatten(prepare["run"])
        self.assertIn("monkey-assets/scripts/fork_pool.py prepare", script)
        self.assertIn("--config monkey-assets/config/fork-orgs.json", script)
        env = cast(dict[str, Any], prepare["env"])
        self.assert_expression(env["MATRIX"], "needs.select.outputs.matrix")
        self.assert_expression(env["ORG"], "inputs.org")
        self.assertLess(
            self.position("forks", "Require trusted provenance"),
            self.position("forks", "prepare"),
        )

    def test_pool_token_reaches_one_step_of_one_job(self) -> None:
        """The fork pool token is optional and the prepare step's alone."""
        secrets = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["secrets"]
        )
        self.assertIs(secrets["fork_pool_token"]["required"], False)
        holders = [
            (job, step.get("id"))
            for job in self.jobs
            for step in self.jobs[job].get("steps", [])
            if "fork_pool_token" in dumped(step)
        ]
        self.assertEqual(holders, [("forks", "prepare")])
        for job, body in self.jobs.items():
            outside = {k: v for k, v in body.items() if k != "steps"}
            with self.subTest(job=job):
                self.assertNotIn("fork_pool_token", dumped(outside))
        prepare = self.step("forks", "prepare")
        self.assert_expression(prepare["env"]["GH_TOKEN"], POOL_TOKEN)
        self.assertEqual(
            Counter(SECRET_REFERENCE.findall(dumped(self.jobs["forks"]))),
            Counter({POOL_TOKEN: 1}),
        )

    def test_prepare_step_reads_the_token_variables_through_env(self) -> None:
        """The lifetime and recorded expiry arrive as env, never in the script."""
        prepare = self.step("forks", "prepare")
        env = cast(dict[str, Any], prepare["env"])
        self.assert_expression(
            env["FORK_POOL_TOKEN_MAX_DAYS"], "inputs.fork_pool_token_max_days"
        )
        self.assert_expression(
            env["FORK_POOL_TOKEN_EXPIRES"], "inputs.fork_pool_token_expires"
        )
        self.assertNotIn("${{", str(prepare["run"]))

    def test_no_mint_requests_administration(self) -> None:
        """The App never administers: no mint asks for it at any level."""
        mints = [
            (job, step)
            for job in self.jobs
            if "steps" in self.jobs[job]
            for step in self.actions(job, APP_TOKEN)
        ]
        self.assertTrue(mints)
        for job, step in mints:
            with self.subTest(job=job, mint=step["id"]):
                self.assertNotIn("permission-administration", step["with"])
                self.assertNotIn(
                    "INPUT_PERMISSION-ADMINISTRATION", step.get("env") or {}
                )

    def test_publish_requires_the_fork_and_never_creates_it(self) -> None:
        """Push holds the fork write token alone and learns when it is missing."""
        ids = [step.get("id") for step in self.steps("publish")]
        self.assertNotIn("fork-create-token", ids)
        push = self.step("publish", "push")
        env = cast(dict[str, Any], push["env"])
        self.assert_expression(env["GH_TOKEN"], "steps.fork-token.outputs.token")
        self.assert_expression(env["FORK_EXISTS"], "steps.fork-state.outputs.exists")
        script = flatten(push["run"])
        self.assertIn('if [ "$FORK_EXISTS" = false ]; then', script)
        self.assertIn("extra+=(--fork-missing)", script)
        self.assertLess(
            self.position("publish", "fork-state"), self.position("publish", "push")
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
                "unreviewed_sha256",
                "author_timeout",
                "agent_timeout",
                "max_parallel",
                "run_created_at",
                "model",
                "fusion_tier",
                "stale_status_minutes",
                "allow_subagents",
                "load_repo_skills",
                "request_review",
                "request_antagonistic_review",
                "params",
                "run_parameters",
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

    def test_read_mint_reads_checks(self) -> None:
        """A revision round reads the head's failing checks (DESIGN.md 20.1).

        Never the job logs, which need Actions: no mint requests it.
        """
        with_ = cast(dict[str, Any], self.step("select", "app-token")["with"])
        self.assertEqual(with_["permission-checks"], "read")

    def test_selection_skips_targets_other_runs_hold(self) -> None:
        """The native token reads other runs' selections first (DESIGN.md 6).

        The caller's runs and their evidence live in the caller
        repository, which the App may not cover; the script fails open
        itself, so the step carries no continue-on-error to hide a bug.
        """
        held = self.step("select", "Find targets other runs hold")
        env = cast(dict[str, Any], held["env"])
        self.assert_expression(env["GH_TOKEN"], "github.token")
        self.assert_expression(env["REPOSITORY"], "github.repository")
        self.assert_expression(env["RUN_ID"], "github.run_id")
        run = flatten(held["run"])
        self.assertIn("monkey-assets/scripts/run_overlap.py", run)
        self.assertIn("--output held-targets.json", run)
        self.assertNotIn("continue-on-error", held)
        self.assertLess(
            self.position("select", "Find targets other runs hold"),
            self.position("select", "Select issues"),
        )
        select = flatten(self.step("select", "Select issues")["run"])
        self.assertIn("--held-file held-targets.json", select)
        self.assertEqual(self.jobs["select"]["permissions"]["actions"], "read")

    def test_select_job_vouches_for_its_evidence(self) -> None:
        """Only a notice on the select job's check run vouches (DESIGN.md 6).

        The author job holds the runtime token and can upload a
        look-alike, so a later run trusts an artifact only when this
        job's check run names its id and digest. That run finds the job
        by name, so no other job may share it, and reads annotations
        rather than the log, whose storage host block mode refuses. No
        job may write check runs, and the caller grants the read.
        """
        vouch = self.step("select", "Vouch for selection evidence")
        self.assertLess(
            self.position("select", "Publish selection evidence"),
            self.position("select", "Vouch for selection evidence"),
        )
        env = cast(dict[str, Any], vouch["env"])
        self.assert_expression(env["ARTIFACT_ID"], "steps.evidence.outputs.artifact-id")
        self.assert_expression(env["DIGEST"], "steps.evidence.outputs.artifact-digest")
        evidence = cast(dict[str, Any], self.step("select", "evidence")["with"])
        self.assertEqual(env["NAME"], evidence["name"])
        run = flatten(vouch["run"])
        self.assertIn(
            "printf '::notice title=monkey-evidence-vouch::%s %s sha256:%s\\n'", run
        )
        self.assertIn('[[ "$DIGEST" =~ ^[0-9a-f]{64}$ ]]', run)
        self.assertNotIn("continue-on-error", vouch)
        self.assertEqual(self.jobs["select"]["name"], "Select")
        for job, spec in self.jobs.items():
            if job != "select":
                self.assertNotEqual(str(spec.get("name", job)), "Select", job)
        self.assertTrue(str(self.jobs["author"]["name"]).startswith("Author ("))
        self.assertEqual(self.jobs["select"]["permissions"]["checks"], "read")
        for job, spec in self.jobs.items():
            self.assertNotEqual(spec.get("permissions", {}).get("checks"), "write", job)
        # GitHub rejects a reusable-workflow call whose caller grants less
        # than any callee job asks for, so every local caller must grant
        # the union of the callee's job permissions.
        needed: dict[str, str] = {}
        for spec in self.jobs.values():
            needed.update(spec.get("permissions", {}))
        self.assertEqual(needed.get("checks"), "read")
        callers = 0
        for path in sorted(WORKFLOWS.glob("*.yaml")):
            workflow = cast(
                dict[str | bool, Any], yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            for job, spec in cast(dict[str, Any], workflow["jobs"]).items():
                if not str(spec.get("uses", "")).endswith(f"/{REUSABLE.name}"):
                    continue
                callers += 1
                granted = cast(dict[str, Any], spec.get("permissions", {}))
                for scope, level in needed.items():
                    self.assertEqual(granted.get(scope), level, f"{path.name}:{job}")
        self.assertGreaterEqual(callers, 3)

    def test_select_resolves_fork_organisations(self) -> None:
        """Selection carries the committed fork mapping into every entry."""
        script = flatten(self.step("select", "Select issues")["run"])
        self.assertIn("--fork-orgs monkey-assets/config/fork-orgs.json", script)

    def test_select_reads_rejections_with_the_run_token(self) -> None:
        """A rejecting run's result is read in this repository (DESIGN.md 6)."""
        step = self.step("select", "Select issues")
        env = cast(dict[str, Any], step["env"])
        self.assertEqual(env["RESULTS_TOKEN"], "${{ github.token }}")
        self.assertEqual(env["RESULTS_REPOSITORY"], "${{ github.repository }}")
        script = flatten(step["run"])
        self.assertIn('--results-repository "$RESULTS_REPOSITORY"', script)

    def test_select_reads_the_antagonistic_reviewers(self) -> None:
        """The trusted App list comes from the pinned assets (DESIGN.md 20.1)."""
        script = flatten(self.step("select", "Select issues")["run"])
        self.assertIn(
            "--antagonistic-reviewers monkey-assets/config/antagonistic-reviewers.txt",
            script,
        )

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
        gather = self.step("report", "Gather results")
        self.assertTrue(is_action(gather, DOWNLOAD))
        self.assertEqual(
            gather["with"]["pattern"],
            "monkey-result-${{ needs.select.outputs.namespace }}-*",
        )
        final = self.step("report", "Attach run report")
        self.assertTrue(is_action(final, UPLOAD))
        self.assertEqual(final["with"]["retention-days"], 90)

    def test_pull_request_outputs_come_from_the_report(self) -> None:
        """The workflow's outputs are the report script's, validated by org."""
        declared = dict(cast(dict[str, Any], self.triggers["workflow_call"]["outputs"]))
        # The select job's validated count, so a caller can tell a slot
        # that selected nothing from one whose selections came to no link.
        selected = declared.pop("selected_count")
        self.assert_expression(str(selected["value"]), "jobs.select.outputs.count")
        # What the request job dispatched, not what the report nominated.
        requested = declared.pop("antagonistic_pull_requests")
        # The run parameters the continue job forwards (#241).
        parameters = declared.pop("run_parameters")
        self.assert_expression(
            str(parameters["value"]), "jobs.select.outputs.run_parameters"
        )
        self.assert_expression(
            str(requested["value"]), "jobs.antagonistic.outputs.requested"
        )
        self.assert_expression(
            str(self.jobs["report"]["outputs"]["antagonistic_pull_requests"]),
            "steps.report.outputs.antagonistic_pull_requests",
        )
        self.assertEqual(
            set(declared),
            {
                "pull_requests",
                "pull_request_count",
                "unmergeable_pull_requests",
                "failing_check_pull_requests",
                "continue_pull_requests",
            },
        )
        # Only a live pull-requests run names a pull request to continue.
        self.assert_expression(
            str(self.jobs["report"]["outputs"].pop("continue_pull_requests")),
            "!inputs.dry_run && inputs.mode == 'pull-requests'"
            " && steps.report.outputs.continue_pull_requests || '[]'",
        )
        for name, body in declared.items():
            with self.subTest(output=name):
                self.assert_expression(
                    str(body["value"]), f"jobs.report.outputs.{name}"
                )
                if name == "continue_pull_requests":
                    continue
                self.assert_expression(
                    str(self.jobs["report"]["outputs"][name]),
                    f"steps.report.outputs.{name}",
                )
        build = self.step("report", "report")
        self.assertEqual(build["name"], "Build report")
        self.assert_expression(build["env"]["ORG"], "inputs.org")
        script = flatten(str(build["run"]))
        self.assertIn("publish.py report", script)
        self.assertIn('--org "$ORG" --github-output "$GITHUB_OUTPUT"', script)

    def test_unreviewed_heads_reach_the_report(self) -> None:
        """The select job's list of unreviewed heads, as verified evidence.

        A scan may skip hundreds of heads, more than one environment
        string holds, so the list travels in the evidence artifact and
        the select job vouches for its digest.
        """
        outputs = cast(dict[str, Any], self.jobs["select"]["outputs"])
        self.assert_expression(
            str(outputs["unreviewed_sha256"]), "steps.digests.outputs.unreviewed"
        )
        digests = flatten(str(self.step("select", "digests")["run"]))
        self.assertIn("('unreviewed', 'unreviewed-heads.json')", digests)
        fetch = self.step("report", "Fetch selection evidence")
        self.assertTrue(fetch.get("continue-on-error"))
        self.assert_expression(
            fetch["with"]["artifact-ids"], "needs.select.outputs.evidence_id"
        )
        self.assertLess(
            self.position("report", "Fetch selection evidence"),
            self.position("report", "Build report"),
        )
        build = self.step("report", "report")
        self.assertNotIn("UNREVIEWED_HEADS", build["env"])
        self.assert_expression(
            build["env"]["UNREVIEWED_SHA"], "needs.select.outputs.unreviewed_sha256"
        )
        script = flatten(str(build["run"]))
        self.assertIn(
            "bot_evidence.py verify --directory evidence"
            ' --expect "unreviewed-heads.json=$UNREVIEWED_SHA"',
            script,
        )
        self.assertIn("--unreviewed-heads evidence/unreviewed-heads.json", script)
        self.assertIn('"${unreviewed[@]}"', script)


class ListInputContracts(ReusableWorkflowCase):
    """Operator lists are split and checked once, before any token exists."""

    # Splitter step id and validate mode for each list input.
    LISTS: ClassVar[dict[str, tuple[str, str]]] = {
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

    def test_named_issues_scope_the_read_mint(self) -> None:
        """Issues scope the token to the repositories that exist."""
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(
            env["ISSUE_REPOSITORIES"], "steps.named.outputs.repositories"
        )
        self.assert_expression(env["ISSUE_COUNT"], "steps.issues.outputs.count")
        self.assertNotIn("REPOSITORY_COUNT", env)
        script = squash(str(budget["run"]))
        self.assertIn('repositories="$ISSUE_REPOSITORIES"', script)
        select = cast(dict[str, Any], self.step("select", "Select issues")["env"])
        self.assert_expression(select["ISSUES"], "steps.issues.outputs.csv")
        self.assert_expression(select["PULL_REQUESTS"], "steps.pulls.outputs.csv")
        self.assert_expression(select["REVISE"], "inputs.revise")
        run = flatten(self.step("select", "Select issues")["run"])
        self.assertIn('--pull-requests "$PULL_REQUESTS"', run)
        self.assertIn("--no-revise", run)
        # The run id and the select job's attempt are the session: a
        # full rerun selects and authors anew under the same run id
        # and must not reuse the earlier attempt's replies, while a
        # publish-only retry reads the earlier selection and keeps it.
        self.assertIn('--session "$RUN_ID-$RUN_ATTEMPT"', run)
        self.assert_expression(select["RUN_ID"], "github.run_id")
        self.assert_expression(select["RUN_ATTEMPT"], "github.run_attempt")
        self.assertIn(
            '--issues "$ISSUES"', flatten(self.step("select", "Select issues")["run"])
        )

    def test_a_list_given_but_naming_nothing_is_refused(self) -> None:
        """Separators alone fail the input check with a message of their own.

        The concurrency group tests the raw issues input and selection
        the split one, so a list that parses to nothing would take a
        per-run lock and then name nothing. The check reads both,
        before the selection and the mint.
        """
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(env["ISSUES_RAW"], "inputs.issues")
        script = squash(str(budget["run"]))
        self.assertIn("::error::issues was given but names no issue", script)
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
            "MAX_ROUNDS": "10",
            "STALE_MINUTES": "8",
            "MAX_AGENTS": "1",
            "MODEL": "auto",
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
            ("", "0", SCAN_REFUSED),
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
    def test_a_run_naming_nothing_is_refused(self) -> None:
        """No list and no revise_only would scan for issues: the check refuses it."""
        budget = self.step("select", "budget")
        env = {
            "PATH": os.environ["PATH"],
            "MODE": "pull-requests",
            "MAX_PRS": "1",
            "MAX_RUNTIME": "60",
            "MAX_ROUNDS": "10",
            "STALE_MINUTES": "8",
            "MAX_AGENTS": "1",
            "MODEL": "auto",
            "ISSUE_REPOSITORIES": "",
            "PULL_COUNT": "0",
            "PULL_REQUESTS_RAW": "",
            "REVISE": "true",
            "ORG": "o",
            "GUIDANCE_REPOSITORY": "o/.github",
        }
        cases = (
            ("", "0", "false", SCAN_REFUSED),
            ("", "", "false", SCAN_REFUSED),
            (" , ", "0", "false", "issues was given but names no issue"),
            (",", "0", "false", "issues was given but names no issue"),
            (" ", "0", "false", "issues was given but names no issue"),
            ("repo#1", "1", "false", None),
            ("", "0", "true", None),
        )
        for issues, issue_count, revise_only, error in cases:
            with (
                self.subTest(issues=issues, count=issue_count, revise_only=revise_only),
                tempfile.TemporaryDirectory() as holder,
            ):
                output = Path(holder) / "output"
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        **env,
                        "ISSUES_RAW": issues,
                        "ISSUE_COUNT": issue_count,
                        "REVISE_ONLY": revise_only,
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

    def test_the_scan_refusal_precedes_selection_and_the_mint(self) -> None:
        """The refusal sits in the input check, before any token or read."""
        script = squash(str(self.step("select", "budget")["run"]))
        self.assertIn("::error::scanning for new issues is off;", script)
        check = self.position("select", "budget")
        self.assertLess(check, self.position("select", "app-token"))
        self.assertLess(check, self.position("select", "Select issues"))

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
        self.assertNotIn("REPOSITORIES", env)
        script = squash(str(named["run"]))
        self.assertIn("HTTP 404", script)
        # Readable is not public: the native token reads the private
        # caller repository too, and the App may not be installed there.
        self.assertIn("--jq .private", script)
        self.assertIn('[ "$output" = false ]', script)
        self.assertIn("::error::none of the named issues' repositories exist", script)
        self.assertLess(
            self.position("select", "issues"), self.position("select", "named")
        )
        self.assertLess(
            self.position("select", "named"), self.position("select", "budget")
        )

    def test_read_mint_scope_derives_from_the_split_list(self) -> None:
        """The scope is the resolved names plus the guidance repository."""
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assertNotIn("REPOSITORIES", env)
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

        The input check alone also sees the raw issues and pull
        requests, under names of their own, to tell an absent list from
        one that parsed to nothing.
        """
        env = cast(dict[str, Any], self.step("select", "Select issues")["env"])
        self.assertNotIn("REPOSITORIES", env)
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
                                if k not in ("ISSUES_RAW", "PULL_REQUESTS_RAW")
                            },
                        }
                    text = json.dumps(checked)
                    for name in self.LISTS:
                        self.assertNotRegex(text, rf"inputs\.{name}\b")

    def test_scan_only_inputs_are_gone(self) -> None:
        """No input names repositories to scan, categories or assignees (#196)."""
        inputs = cast(dict[str, Any], self.triggers["workflow_call"]["inputs"])
        for name in ("repositories", "categories", "include_assigned"):
            with self.subTest(input=name):
                self.assertNotIn(name, inputs)
        text = self.WORKFLOW.read_text(encoding="utf-8")
        for flag in ("--repositories", "--categories", "--include-assigned"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, text)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class NamedRepositoryContracts(ReusableWorkflowCase):
    """The repository check reads gh's answer from stdout alone."""

    STUB = (
        "#!/bin/sh\n"
        "printf '%b' \"$GH_STDOUT\"\n"
        "printf '%b' \"$GH_STDERR\" >&2\n"
        'exit "$GH_STATUS"\n'
    )

    def resolve(self, stdout: str, stderr: str, status: int) -> tuple[int, str]:
        """Run the step on one named issue's repository against a stub gh."""
        script = str(self.step("select", "named")["run"])
        with tempfile.TemporaryDirectory() as holder:
            stub = Path(holder) / "gh"
            stub.write_text(self.STUB, encoding="utf-8")
            stub.chmod(0o755)
            proc = subprocess.run(
                ["bash", "-eo", "pipefail", "-c", script],
                env={
                    "PATH": f"{holder}{os.pathsep}{os.environ['PATH']}",
                    "ORG": "o",
                    "ISSUE_REPOSITORIES": "repo",
                    "PULL_REPOSITORIES": "",
                    "GITHUB_OUTPUT": str(Path(holder) / "output"),
                    "GH_STDOUT": stdout,
                    "GH_STDERR": stderr,
                    "GH_STATUS": str(status),
                },
                capture_output=True,
                text=True,
                check=False,
            )
        return proc.returncode, proc.stdout

    def test_a_warning_on_stderr_does_not_reject_a_public_repository(self) -> None:
        """An update notice beside a successful answer is not part of it."""
        status, output = self.resolve("false\\n", "A new release of gh\\n", 0)
        self.assertEqual(status, 0, output)

    def test_failures_are_told_apart_by_stderr(self) -> None:
        """Private and absent drop the name; anything else is an error."""
        rejected = "::error::none of the named issues' repositories exist"
        failed = "::error::Could not read o/repo: gh: Server Error (HTTP 502)"
        cases = (
            ("true\\n", "", 0, 1, rejected),
            ("", "gh: Not Found (HTTP 404)\\n", 1, 1, rejected),
            ("", "gh: Server Error (HTTP 502)\\n", 1, 2, failed),
        )
        for stdout, stderr, code, expected, message in cases:
            with self.subTest(stdout=stdout, stderr=stderr):
                status, output = self.resolve(stdout, stderr, code)
                self.assertEqual(status, expected, output)
                self.assertIn(message, output)


class ReviseOnlyContracts(ReusableWorkflowCase):
    """A revise-only run revises open bot pull requests and works no issue (#113)."""

    BUDGET_ENV: ClassVar[dict[str, str]] = {
        "MODE": "pull-requests",
        "MAX_PRS": "10",
        "MAX_RUNTIME": "60",
        "MAX_ROUNDS": "10",
        "STALE_MINUTES": "8",
        "MAX_AGENTS": "1",
        "MODEL": "auto",
        "ISSUE_REPOSITORIES": "",
        "ISSUES_RAW": "",
        "ISSUE_COUNT": "0",
        "PULL_REQUESTS_RAW": "",
        "PULL_COUNT": "0",
        "REVISE": "true",
        "REVISE_ONLY": "true",
        "OUTSTANDING_ONLY": "false",
        "ORG": "o",
        "GUIDANCE_REPOSITORY": "o/.github",
    }

    def test_input_is_declared_off_and_reaches_the_selector(self) -> None:
        """A boolean, off by default, passed to select_issues.py as a flag."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["revise_only"]["type"], "boolean")
        self.assertIs(inputs["revise_only"]["default"], False)
        select = self.step("select", "Select issues")
        env = cast(dict[str, Any], select["env"])
        self.assert_expression(env["REVISE_ONLY"], "inputs.revise_only")
        run = flatten(select["run"])
        self.assertIn('if [ "$REVISE_ONLY" = true ]; then', run)
        self.assertIn("extra+=(--revise-only)", run)
        budget = cast(dict[str, Any], self.step("select", "budget")["env"])
        self.assert_expression(budget["REVISE_ONLY"], "inputs.revise_only")
        self.assert_expression(budget["REVISE"], "inputs.revise")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_check_inputs_refuses_targets_and_other_modes(self) -> None:
        """Only a pull-requests scan with revisions on may be revise-only."""
        budget = self.step("select", "budget")
        cases: tuple[tuple[dict[str, str], str | None], ...] = (
            ({}, None),
            ({"REVISE_ONLY": "false", "ISSUES_RAW": "a#1", "ISSUE_COUNT": "1"}, None),
            ({"PULL_REQUESTS_RAW": "a#2", "PULL_COUNT": "1"}, None),
            (
                {"ISSUES_RAW": "a#1", "ISSUE_COUNT": "1"},
                "revise_only cannot be given with issues",
            ),
            ({"MODE": "branches"}, "revise_only needs pull-requests mode"),
            ({"MODE": "select"}, "revise_only needs pull-requests mode"),
            ({"REVISE": "false"}, "revise_only needs revise"),
        )
        for overrides, error in cases:
            with (
                self.subTest(**overrides),
                tempfile.TemporaryDirectory() as holder,
            ):
                output = Path(holder) / "output"
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        "PATH": os.environ["PATH"],
                        **self.BUDGET_ENV,
                        **overrides,
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


class ReviseOnlyCallerContracts(WorkflowCase):
    """The caller's revise-only form input and lock (#113, #196)."""

    WORKFLOW = CRON

    def test_the_form_alone_sets_revise_only(self) -> None:
        """No schedule sets it: an operator's dispatch is the only source."""
        self.assertNotIn("schedule", self.triggers)
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(with_["revise_only"], "inputs.revise_only")

    def test_dispatch_form_offers_revise_only(self) -> None:
        """A boolean, off by default; the form stays inside GitHub's 25."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["revise_only"]["type"], "boolean")
        self.assertIs(inputs["revise_only"]["default"], False)
        self.assertEqual(len(inputs), 24)

    def test_revise_only_runs_share_the_scan_group(self) -> None:
        """Two live revise-only runs queue one behind the other."""
        group = unwrap(str(cast(dict[str, Any], self.workflow["concurrency"])["group"]))
        self.assertNotIn("revise_only", group)
        self.assertNotIn("schedule", group)
        self.assertTrue(group.endswith("|| 'code-monkey'"))


ANTAGONISTIC_TOKEN = "secrets.antagonistic_dispatch_token"
ANTAGONISTIC_WORKFLOW = "code-review-antagonistic.yaml"


class Dispatch(NamedTuple):
    """What one run of the dispatch step did."""

    status: int
    output: str
    calls: list[str]
    stdout: str
    summary: str
    sleeps: list[str]


def published_head(pull: str) -> str:
    """A distinct published head for a nominee, as the report names it."""
    return f"{sum(map(ord, pull)):040x}"


def step_outputs(text: str) -> dict[str, str]:
    """Read a ``GITHUB_OUTPUT`` file of single-line ``key=value`` entries."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class AntagonisticRequestContracts(ReusableWorkflowCase):
    """The request for an antagonistic review and its one credential (#138)."""

    def test_token_reaches_one_step_of_one_job(self) -> None:
        """The dispatch token appears in the dispatch step alone."""
        holders = [
            (job, step.get("name"))
            for job in self.jobs
            for step in self.steps(job)
            if "antagonistic_dispatch_token" in dumped(step)
        ]
        self.assertEqual(holders, [("antagonistic", "Dispatch antagonistic reviews")])
        for job, body in self.jobs.items():
            outside = {k: v for k, v in body.items() if k != "steps"}
            with self.subTest(job=job):
                self.assertNotIn("antagonistic_dispatch_token", dumped(outside))
        step = self.step("antagonistic", "dispatch")
        self.assert_expression(step["env"]["GH_TOKEN"], ANTAGONISTIC_TOKEN)

    def test_job_holds_no_permission_and_follows_the_report(self) -> None:
        """No grant, after the report, skipped in dry runs and when off."""
        job = self.jobs["antagonistic"]
        self.assertEqual(job["name"], "Request antagonistic review")
        self.assertEqual(job["permissions"], {})
        self.assertEqual(job["needs"], ["select", "report"])
        self.assert_expression(
            job["if"],
            "!cancelled() && needs.report.result == 'success' &&"
            " !inputs.dry_run && inputs.mode == 'pull-requests' &&"
            " needs.select.outputs.request_antagonistic_review == 'true' &&"
            " inputs.antagonistic_repository != '' &&"
            " needs.report.outputs.antagonistic_pull_requests != '' &&"
            " needs.report.outputs.antagonistic_pull_requests != '[]'",
        )
        self.assertEqual(self.actions("antagonistic", CHECKOUT), [])
        self.assertEqual(self.actions("antagonistic", DOWNLOAD), [])

    def test_publish_job_holds_no_new_credential(self) -> None:
        """The publish job's record step is offline and sees no token."""
        self.assertNotIn("antagonistic_dispatch_token", dumped(self.jobs["publish"]))
        self.assertNotIn("antagonistic_dispatch_token", dumped(self.jobs["report"]))
        step = self.step("publish", "antagonistic")
        self.assertNotIn("GH_TOKEN", step.get("env", {}))
        self.assertLess(
            self.position("publish", "merge-probe"),
            self.position("publish", "antagonistic"),
        )
        self.assertLess(
            self.position("publish", "antagonistic"),
            self.position("publish", "comment-token"),
        )
        self.assert_expression(
            step["env"]["SWITCH"], "needs.select.outputs.request_antagonistic_review"
        )
        self.assertIn("scripts/antagonistic_request.py", flatten(str(step["run"])))

    def test_switch_defaults_on_in_both_workflows(self) -> None:
        """The workflow_call input and the dispatch form default to true."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["request_antagonistic_review"]["type"], "boolean")
        self.assertIs(inputs["request_antagonistic_review"]["default"], True)
        self.assertEqual(
            inputs["antagonistic_repository"]["default"],
            "lfreleng-actions/github-code-review-bot",
        )
        cron = cast(
            dict[str | bool, Any], yaml.safe_load(CRON.read_text(encoding="utf-8"))
        )
        form = cast(
            dict[str, dict[str, Any]], cron[TRIGGERS]["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(form["request_antagonistic_review"]["type"], "boolean")
        self.assertIs(form["request_antagonistic_review"]["default"], True)
        caller = cron["jobs"]["code-monkey"]
        self.assert_expression(
            caller["with"]["request_antagonistic_review"],
            "inputs.request_antagonistic_review",
        )
        self.assert_expression(
            caller["secrets"]["antagonistic_dispatch_token"],
            "secrets.CODE_REVIEW_DISPATCH_TOKEN",
        )

    def dispatch(
        self,
        nominees: object,
        token: str = "t",
        fail: str = "",
        failures: int = 99,
        delays: str = "0 0 0",
        lost: str = "",
        lost_answer: str = "connection reset by peer",
        heads: object = None,
    ) -> Dispatch:
        """Run the dispatch step with a fake gh and sleep; return what it did.

        A call naming ``fail`` is refused, as a rate limit is, its first
        ``failures`` times, then goes. A call naming ``lost`` fails with
        ``lost_answer``, as a dispatch GitHub may have accepted does when
        its response is lost. The fake sleep records each wait instead
        of taking it. ``heads`` defaults to a published head for each
        nominee, as the report outputs them.
        """
        if heads is None:
            named = cast(list[object], nominees if isinstance(nominees, list) else [])
            heads = {
                pull: published_head(pull) for pull in named if isinstance(pull, str)
            }
        step = self.step("antagonistic", "dispatch")
        with tempfile.TemporaryDirectory() as holder:
            work = Path(holder)
            fake = work / "bin"
            fake.mkdir()
            calls = work / "calls"
            sleeps = work / "sleeps"
            count = work / "count"
            calls.touch()
            sleeps.touch()
            count.write_text("0\n", encoding="utf-8")
            (fake / "gh").write_text(
                "#!/bin/bash\n"
                f'printf "%s\\n" "$*" >> "{calls}"\n'
                f'case "$*" in *"{fail or "never"}"*)\n'
                f'  n=$(( $(cat "{count}") + 1 )); echo "$n" > "{count}"\n'
                f'  [ "$n" -gt {failures} ] && exit 0\n'
                "  echo 'HTTP 403: API rate limit exceeded' >&2; exit 1 ;;\n"
                f'  *"{lost or "never"}"*) echo "{lost_answer}" >&2; exit 1 ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            (fake / "sleep").write_text(
                f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{sleeps}"\n', encoding="utf-8"
            )
            (fake / "gh").chmod(0o755)
            (fake / "sleep").chmod(0o755)
            output = work / "output"
            summary = work / "summary"
            output.touch()
            summary.touch()
            env = {
                "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
                "GH_TOKEN": token,
                "REPOSITORY": "lfreleng-actions/github-code-review-bot",
                "ORG": "lfreleng-actions",
                "NOMINEES": nominees
                if isinstance(nominees, str)
                else json.dumps(nominees),
                "HEADS": heads if isinstance(heads, str) else json.dumps(heads),
                "RETRY_DELAYS": delays,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            run = subprocess.run(
                ["bash", "-c", str(step["run"])],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            return Dispatch(
                status=run.returncode,
                output=output.read_text(encoding="utf-8"),
                calls=calls.read_text(encoding="utf-8").splitlines(),
                stdout=run.stdout,
                summary=summary.read_text(encoding="utf-8"),
                sleeps=sleeps.read_text(encoding="utf-8").splitlines(),
            )

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_one_nominee_dispatches_once_live(self) -> None:
        """One dispatch per pull request, with dry_run=false."""
        run = self.dispatch(["lfreleng-actions/a#1"])
        self.assertEqual(run.status, 0)
        self.assertEqual(
            run.calls,
            [
                f"workflow run {ANTAGONISTIC_WORKFLOW} --repo"
                " lfreleng-actions/github-code-review-bot -f dry_run=false"
                " -f pull_requests=lfreleng-actions/a#1"
            ],
        )
        self.assertIn('requested=["lfreleng-actions/a#1"]', run.output)
        self.assertIn("requested: lfreleng-actions/a#1", run.summary)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_empty_token_warns_and_dispatches_nothing(self) -> None:
        """The default stays harmless until the secret is configured."""
        run = self.dispatch(["lfreleng-actions/a#1"], token="")
        self.assertEqual(run.status, 0)
        self.assertEqual(run.calls, [])
        self.assertIn("::warning::", run.stdout)
        self.assertIn("requested=[]", run.output)

    def test_retry_delays_are_the_steps_own(self) -> None:
        """The waits before passes two to four live in the step's env."""
        step = self.step("antagonistic", "dispatch")
        self.assertEqual(step["env"]["RETRY_DELAYS"], "15 60 240")

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_a_dispatch_that_fails_once_goes_on_the_next_pass(self) -> None:
        """A failure is a notice; the second pass requests it and the step passes."""
        run = self.dispatch(
            ["lfreleng-actions/a#1"], fail="a#1", failures=1, delays="15 60 240"
        )
        self.assertEqual(run.status, 0)
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(run.sleeps, ["15"])
        outputs = step_outputs(run.output)
        self.assertEqual(outputs["requested"], '["lfreleng-actions/a#1"]')
        self.assertEqual(outputs["failed_count"], "0")
        self.assertEqual(json.loads(outputs["failed"]), {"include": []})
        self.assertIn("::notice::", run.stdout)
        self.assertIn("lfreleng-actions/a#1", run.stdout)
        self.assertIn("pass 1", run.stdout)
        self.assertNotIn("::error::", run.stdout)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_a_nominee_that_never_goes_fails_the_step(self) -> None:
        """Four passes, three waits, then an error, every output and a red step."""
        run = self.dispatch(["lfreleng-actions/a#1"], fail="a#1", delays="15 60 240")
        self.assertNotEqual(run.status, 0)
        self.assertEqual(len(run.calls), 4)
        self.assertEqual(run.sleeps, ["15", "60", "240"])
        outputs = step_outputs(run.output)
        self.assertEqual(outputs["requested"], "[]")
        self.assertEqual(outputs["failed_count"], "1")
        self.assertEqual(
            json.loads(outputs["failed"]),
            {
                "include": [
                    {
                        "pull": "lfreleng-actions/a#1",
                        "repository": "lfreleng-actions/a",
                        "repo_name": "a",
                        "number": 1,
                        "kind": "failed",
                        "head": published_head("lfreleng-actions/a#1"),
                    }
                ]
            },
        )
        self.assertEqual(run.stdout.count("::notice::"), 3)
        self.assertEqual(run.stdout.count("::error::"), 1)
        self.assertIn("failed: lfreleng-actions/a#1", run.summary)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_one_failing_nominee_of_three(self) -> None:
        """Two requested on the first pass; the third alone is retried, then failed."""
        pulls = [
            "lfreleng-actions/a#1",
            "lfreleng-actions/b.c#2",
            "lfreleng-actions/d#3",
        ]
        run = self.dispatch(pulls, fail="b.c#2")
        self.assertNotEqual(run.status, 0)
        self.assertEqual(len(run.calls), 6)
        self.assertEqual(run.sleeps, ["0", "0", "0"])
        outputs = step_outputs(run.output)
        self.assertEqual(
            outputs["requested"], '["lfreleng-actions/a#1","lfreleng-actions/d#3"]'
        )
        self.assertEqual(outputs["failed_count"], "1")
        failed = json.loads(outputs["failed"])["include"]
        self.assertEqual(
            failed,
            [
                {
                    "pull": "lfreleng-actions/b.c#2",
                    "repository": "lfreleng-actions/b.c",
                    "repo_name": "b.c",
                    "number": 2,
                    "kind": "failed",
                    "head": published_head("lfreleng-actions/b.c#2"),
                }
            ],
        )
        self.assertIn("requested: lfreleng-actions/a#1", run.summary)
        self.assertIn("failed: lfreleng-actions/b.c#2", run.summary)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_the_waits_come_from_retry_delays(self) -> None:
        """Each later pass waits the next value, once, whatever the nominees."""
        pulls = ["lfreleng-actions/a#1", "lfreleng-actions/a#2"]
        run = self.dispatch(pulls, fail="lfreleng-actions/a#", delays="1 2 3")
        self.assertEqual(run.sleeps, ["1", "2", "3"])
        self.assertEqual(len(run.calls), 8)
        self.assertEqual(step_outputs(run.output)["failed_count"], "2")
        for bad in ("1 2", "1 2 3 4", "1 x 3", "-1 2 3"):
            with self.subTest(delays=bad):
                run = self.dispatch(["lfreleng-actions/a#1"], delays=bad)
                self.assertNotEqual(run.status, 0)
                self.assertEqual(run.calls, [])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_the_loop_ends_when_nothing_is_left(self) -> None:
        """A first pass that requests everything waits for nothing."""
        pulls = ["lfreleng-actions/a#1", "lfreleng-actions/b#2"]
        run = self.dispatch(pulls, delays="15 60 240")
        self.assertEqual(run.status, 0)
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(run.sleeps, [])
        self.assertEqual(step_outputs(run.output)["failed_count"], "0")

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_a_failure_github_did_not_refuse_is_never_sent_again(self) -> None:
        """A lost response or a server error may have started a run.

        It is sent once, is an error and an ``uncertain`` summary line,
        in ``failed`` as kind ``uncertain`` for its own notice, and
        fails the step; the others still go.
        """
        pulls = ["lfreleng-actions/a#1", "lfreleng-actions/b#2"]
        for answer in (
            "connection reset by peer",
            "could not create workflow dispatch event: HTTP 502: Bad Gateway",
        ):
            with self.subTest(answer=answer):
                run = self.dispatch(pulls, lost="b#2", lost_answer=answer)
                self.assertNotEqual(run.status, 0)
                self.assertEqual(len(run.calls), 2)
                self.assertEqual(run.sleeps, [])
                outputs = step_outputs(run.output)
                self.assertEqual(outputs["requested"], '["lfreleng-actions/a#1"]')
                self.assertEqual(outputs["failed_count"], "1")
                self.assertEqual(
                    json.loads(outputs["failed"]),
                    {
                        "include": [
                            {
                                "pull": "lfreleng-actions/b#2",
                                "repository": "lfreleng-actions/b",
                                "repo_name": "b",
                                "number": 2,
                                "kind": "uncertain",
                                "head": published_head("lfreleng-actions/b#2"),
                            }
                        ]
                    },
                )
                self.assertEqual(run.stdout.count("::error::"), 1)
                self.assertNotIn("::notice::", run.stdout)
                self.assertIn("uncertain: lfreleng-actions/b#2", run.summary)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_both_kinds_are_in_failed_with_their_heads(self) -> None:
        """A refused nominee and an uncertain one each get a notice entry."""
        pulls = ["lfreleng-actions/a#1", "lfreleng-actions/b#2", "lfreleng-actions/c#3"]
        run = self.dispatch(pulls, fail="a#1", lost="c#3")
        self.assertNotEqual(run.status, 0)
        outputs = step_outputs(run.output)
        self.assertEqual(outputs["requested"], '["lfreleng-actions/b#2"]')
        self.assertEqual(outputs["failed_count"], "2")
        entries = json.loads(outputs["failed"])["include"]
        self.assertEqual(
            [(e["pull"], e["kind"], e["head"]) for e in entries],
            [
                ("lfreleng-actions/a#1", "failed", published_head(pulls[0])),
                ("lfreleng-actions/c#3", "uncertain", published_head(pulls[2])),
            ],
        )

    def test_heads_come_from_the_report(self) -> None:
        """The report names each nominee's published head for the step."""
        self.assert_expression(
            str(self.jobs["report"]["outputs"]["antagonistic_heads"]),
            "steps.report.outputs.antagonistic_heads",
        )
        step = self.step("antagonistic", "dispatch")
        self.assert_expression(
            step["env"]["HEADS"], "needs.report.outputs.antagonistic_heads"
        )

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_a_nominee_without_a_published_head_is_refused(self) -> None:
        """A head map that is not an object, or lacks a nominee's SHA."""
        pull = "lfreleng-actions/a#1"
        for name, heads in (
            ("not json", "nope"),
            ("list", [published_head(pull)]),
            ("missing", {}),
            ("short", {pull: "a" * 12}),
            ("upper", {pull: "A" * 40}),
            ("number", {pull: 1}),
        ):
            with self.subTest(name):
                run = self.dispatch([pull], heads=heads)
                self.assertNotEqual(run.status, 0)
                self.assertEqual(run.calls, [])
                self.assertIn("::error::", run.stdout)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_malformed_nominees_are_refused_before_any_dispatch(self) -> None:
        """Another owner, bad syntax or a newline refuses the whole list."""
        good = "lfreleng-actions/a#1"
        for bad in (
            "elsewhere/a#2",
            "lfreleng-actions/a#0",
            "lfreleng-actions/a",
            "lfreleng-actions/a#2\nlfreleng-actions/b#3",
            "lfreleng-actions/a b#2",
            "{}",
        ):
            with self.subTest(nominee=bad):
                run = self.dispatch([good, bad])
                self.assertNotEqual(run.status, 0)
                self.assertEqual(run.calls, [])
        run = self.dispatch("not json")
        self.assertNotEqual(run.status, 0)
        self.assertEqual(run.calls, [])
        run = self.dispatch([good, 3])
        self.assertNotEqual(run.status, 0)
        self.assertEqual(run.calls, [])


NOTICE_STEPS = [
    "Load egress allow-list",
    "Harden runner",
    "nominee",
    "Checkout trusted assets",
    "notice-token",
    "Pre-flight: notice token identity",
    "notice",
]


class AntagonisticNoticeContracts(ReusableWorkflowCase):
    """The notice on each pull request whose review request failed (#228)."""

    JOB = "antagonistic-notice"

    def step_keys(self) -> list[str]:
        """Each notice job step by its id, or its name where it has none."""
        return [str(s.get("id") or s.get("name")) for s in self.steps(self.JOB)]

    def test_job_follows_a_failed_request(self) -> None:
        """Named, after select and the request, only when a nominee failed."""
        job = self.jobs[self.JOB]
        self.assertEqual(job["name"], "Report failed review requests")
        self.assertEqual(job["needs"], ["select", "antagonistic"])
        self.assert_expression(
            job["if"],
            "!cancelled() && needs.antagonistic.outputs.failed_count != '' &&"
            " needs.antagonistic.outputs.failed_count != '0'",
        )
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(job["timeout-minutes"], 10)
        strategy = cast(dict[str, Any], job["strategy"])
        self.assertIs(strategy["fail-fast"], False)
        self.assert_expression(
            strategy["matrix"], "fromJSON(needs.antagonistic.outputs.failed)"
        )
        outputs = cast(dict[str, str], self.jobs["antagonistic"]["outputs"])
        self.assert_expression(outputs["failed"], "steps.dispatch.outputs.failed")
        self.assert_expression(
            outputs["failed_count"], "steps.dispatch.outputs.failed_count"
        )

    def test_steps_run_in_order_behind_harden_runner(self) -> None:
        """Loader, harden-runner, check, assets, mint, identity, notice."""
        self.assertEqual(self.step_keys(), NOTICE_STEPS)
        steps = self.steps(self.JOB)
        self.assertTrue(is_action(steps[0], BLOCK_ACTION))
        self.assertTrue(is_action(steps[1], HARDEN_RUNNER))
        self.assertTrue(is_action(steps[3], CHECKOUT))
        self.assertTrue(is_action(steps[4], APP_TOKEN))

    def test_notices_serialise_per_pull_request(self) -> None:
        """One notice job at a time per pull request, across runs.

        Two named runs, or a scan and a named run, hold different
        workflow locks, so both can reach this job for one pull
        request. The script reads the comments before it posts; a later
        job must wait for the earlier one's comment and find it, never
        be cancelled.
        """
        concurrency = cast(dict[str, Any], self.jobs[self.JOB]["concurrency"])
        self.assert_expression(
            concurrency["group"],
            "format('code-monkey-notice-{0}-pull-{1}',"
            " matrix.repository, matrix.number)",
        )
        self.assertIs(concurrency["cancel-in-progress"], False)
        self.assertEqual(concurrency["queue"], "max")

    def test_offline_check_reads_the_selection_before_the_mint(self) -> None:
        """The check holds no token and reads the select job's matrix."""
        check = self.step(self.JOB, "nominee")
        env = cast(dict[str, str], check["env"])
        self.assertNotIn("GH_TOKEN", env)
        self.assert_expression(env["SELECTED"], "needs.select.outputs.matrix")
        self.assert_expression(env["ORG"], "inputs.org")
        self.assert_expression(env["REPOSITORY"], "matrix.repository")
        self.assert_expression(env["REPO_NAME"], "matrix.repo_name")
        self.assert_expression(env["NUMBER"], "matrix.number")
        self.assert_expression(env["KIND"], "matrix.kind")
        self.assert_expression(env["HEAD"], "matrix.head")
        self.assertLess(
            self.position(self.JOB, "nominee"),
            self.position(self.JOB, "notice-token"),
        )
        assets = self.step(self.JOB, "Checkout trusted assets")
        self.assert_expression(assets["with"]["ref"], "needs.select.outputs.assets_sha")
        self.assert_expression(assets["with"]["repository"], "inputs.assets_repository")
        self.assertIs(assets["with"]["persist-credentials"], False)

    def check_nominee(self, **changes: str) -> tuple[int, str, dict[str, str]]:
        """Run the offline check on a nominee; return status, stdout, outputs.

        The selection revises sbom-action#41 on issue 40's branch and
        works issue 9 in another repository, which opens a new pull
        request this run.
        """
        selected = {
            "include": [
                {
                    "repository": "lfreleng-actions/other",
                    "number": 9,
                    "pull_number": 0,
                    "branch": "code-monkey/issue-9",
                },
                {
                    "repository": "lfreleng-actions/sbom-action",
                    "number": 40,
                    "pull_number": 41,
                    "branch": "code-monkey/issue-40",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as holder:
            output = Path(holder) / "output"
            output.touch()
            env = {
                "PATH": os.environ["PATH"],
                "ORG": "lfreleng-actions",
                "REPOSITORY": "lfreleng-actions/sbom-action",
                "REPO_NAME": "sbom-action",
                "NUMBER": "41",
                "KIND": "failed",
                "HEAD": "a" * 40,
                "SELECTED": json.dumps(selected),
                "GITHUB_OUTPUT": str(output),
                **changes,
            }
            run = subprocess.run(
                ["bash", "-c", str(self.step(self.JOB, "nominee")["run"])],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            return run.returncode, run.stdout, step_outputs(output.read_text())

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_offline_check_binds_the_selected_pull_request(self) -> None:
        """A revision binds its number; a new pull request, its issue branch."""
        status, _, outputs = self.check_nominee()
        self.assertEqual(status, 0)
        self.assertEqual(outputs["branches"], '["code-monkey/issue-40"]')
        self.assertEqual(self.check_nominee(KIND="uncertain")[0], 0)
        status, _, outputs = self.check_nominee(
            REPOSITORY="lfreleng-actions/other", REPO_NAME="other", NUMBER="77"
        )
        self.assertEqual(status, 0)
        self.assertEqual(outputs["branches"], '["code-monkey/issue-9"]')
        status, stdout, outputs = self.check_nominee(NUMBER="42")
        self.assertNotEqual(status, 0)
        self.assertIn("::error::", stdout)
        self.assertNotIn("branches", outputs)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_offline_check_refuses_what_the_selection_does_not_name(self) -> None:
        """Another owner, a bad name or number, or an unselected repository."""
        self.assertEqual(self.check_nominee()[0], 0)
        revision = '{"repository": "lfreleng-actions/sbom-action", "pull_number": 41'
        for name, changes in (
            ("owner", {"REPOSITORY": "elsewhere/sbom-action"}),
            ("name", {"REPO_NAME": "sbom action"}),
            ("dots", {"REPOSITORY": "lfreleng-actions/..", "REPO_NAME": ".."}),
            ("mismatch", {"REPO_NAME": "other"}),
            ("zero", {"NUMBER": "0"}),
            ("number", {"NUMBER": "4x"}),
            ("kind", {"KIND": "refused"}),
            ("short head", {"HEAD": "a" * 12}),
            ("head", {"HEAD": "g" * 40}),
            ("unselected", {"SELECTED": '{"include": []}'}),
            ("unselected pull", {"NUMBER": "42"}),
            (
                "bad branch",
                {"SELECTED": '{"include": [' + revision + ', "branch": "main"}]}'},
            ),
            ("no branch", {"SELECTED": '{"include": [' + revision + "}]}"}),
            ("not json", {"SELECTED": "nope"}),
            (
                "newline",
                {
                    "REPOSITORY": "lfreleng-actions/a\n::warning::x",
                    "REPO_NAME": "a\n::warning::x",
                },
            ),
        ):
            with self.subTest(name):
                status, stdout, _ = self.check_nominee(**changes)
                self.assertNotEqual(status, 0)
                self.assertIn("::error::", stdout)
                self.assertNotIn("::warning::", stdout)

    def test_mint_is_scoped_to_the_checked_repository(self) -> None:
        """Pull requests: write and metadata on the one repository, no more."""
        mint = self.step(self.JOB, "notice-token")
        with_ = cast(dict[str, Any], mint["with"])
        self.assert_expression(with_["owner"], "inputs.org")
        self.assert_expression(with_["repositories"], "matrix.repo_name")
        self.assertEqual(
            {k: v for k, v in with_.items() if k.startswith("permission-")},
            {"permission-pull-requests": "write", "permission-metadata": "read"},
        )
        identity = self.step(self.JOB, "Pre-flight: notice token identity")
        self.assert_expression(
            identity["env"]["MINTED_SLUG"], "steps.notice-token.outputs.app-slug"
        )
        self.assertIn(
            "preflight.py identity --config monkey-assets/config/bot.json",
            flatten(identity["run"]),
        )

    def test_token_reaches_the_notice_step_alone(self) -> None:
        """The notice step holds the token; no step sees the dispatch token."""
        job = self.jobs[self.JOB]
        self.assertNotIn("antagonistic_dispatch_token", dumped(job))
        holders = [
            str(step.get("id") or step.get("name"))
            for step in self.steps(self.JOB)
            if "notice-token.outputs.token" in dumped(step)
        ]
        self.assertEqual(holders, ["notice"])
        key = [
            str(step.get("id") or step.get("name"))
            for step in self.steps(self.JOB)
            if "github_app_private_key" in dumped(step)
        ]
        self.assertEqual(key, ["notice-token"])
        step = self.step(self.JOB, "notice")
        env = cast(dict[str, str], step["env"])
        self.assert_expression(env["GH_TOKEN"], "steps.notice-token.outputs.token")
        self.assert_expression(env["BOT_SLUG"], "steps.notice-token.outputs.app-slug")
        self.assert_expression(
            env["REVIEW_REPOSITORY"], "inputs.antagonistic_repository"
        )
        self.assert_expression(env["BRANCHES"], "steps.nominee.outputs.branches")
        self.assert_expression(env["KIND"], "matrix.kind")
        self.assert_expression(env["HEAD"], "matrix.head")
        run = flatten(step["run"])
        self.assertIn("python3 monkey-assets/scripts/request_notice.py", run)
        for flag in (
            '--repository "$REPOSITORY"',
            '--number "$NUMBER"',
            '--kind "$KIND"',
            '--head "$HEAD"',
            '--bot-slug "$BOT_SLUG"',
            '--run-id "$RUN_ID"',
            '--run-url "$RUN_URL"',
            '--review-repository "$REVIEW_REPOSITORY"',
            '--branches "$BRANCHES"',
            '--summary "$GITHUB_STEP_SUMMARY"',
        ):
            self.assertIn(flag, run)


class OutstandingOnlyContracts(ReusableWorkflowCase):
    """A continuation names pull requests and asks for the scan's test (#167)."""

    def test_input_is_declared_off_and_reaches_the_selector(self) -> None:
        """A boolean, off by default, passed to select_issues.py as a flag."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["outstanding_only"]["type"], "boolean")
        self.assertIs(inputs["outstanding_only"]["default"], False)
        select = self.step("select", "Select issues")
        env = cast(dict[str, Any], select["env"])
        self.assert_expression(env["OUTSTANDING_ONLY"], "inputs.outstanding_only")
        run = flatten(select["run"])
        self.assertIn('if [ "$OUTSTANDING_ONLY" = true ]; then', run)
        self.assertIn("extra+=(--outstanding-only)", run)
        budget = cast(dict[str, Any], self.step("select", "budget")["env"])
        self.assert_expression(budget["OUTSTANDING_ONLY"], "inputs.outstanding_only")

    def test_continues_run_reaches_the_selector_as_a_run_url(self) -> None:
        """A string, empty by default, passed as the footer's run URL (#179)."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["continues_run"]["type"], "string")
        self.assertEqual(inputs["continues_run"]["default"], "")
        select = self.step("select", "Select issues")
        env = cast(dict[str, Any], select["env"])
        self.assert_expression(env["CONTINUES_RUN"], "inputs.continues_run")
        self.assert_expression(env["RESULTS_REPOSITORY"], "github.repository")
        run = flatten(select["run"])
        self.assertIn(
            'run_url="$GITHUB_SERVER_URL/$RESULTS_REPOSITORY/actions/runs"', run
        )
        self.assertIn('extra+=(--continues-run "$run_url/$CONTINUES_RUN")', run)
        # The footer the publisher posts names the run by the same URL.
        publish = flatten(dumped(self.jobs["publish"]))
        self.assertIn(
            "${{ github.server_url }}/${{ github.repository }}"
            "/actions/runs/${{ github.run_id }}",
            publish,
        )
        budget = cast(dict[str, Any], self.step("select", "budget")["env"])
        self.assert_expression(budget["CONTINUES_RUN"], "inputs.continues_run")

    def test_comment_learns_whether_the_caller_continues(self) -> None:
        """The outcome comment says who answers a late comment (#252)."""
        comment = self.step("publish", "Comment on the issue")
        env = cast(dict[str, Any], comment["env"])
        self.assert_expression(env["CONTINUE"], "inputs.continue_rounds")
        self.assertIn('--continue-rounds "$CONTINUE"', flatten(comment["run"]))

    def test_only_a_live_pull_requests_run_names_a_continuation(self) -> None:
        """The report's list is [] in any other run, whatever the results say (#179)."""
        output = str(self.jobs["report"]["outputs"]["continue_pull_requests"])
        forged = '["lfreleng-actions/a#1"]'
        for mode, dry_run, expected in (
            ("pull-requests", False, forged),
            ("branches", False, "[]"),
            ("select", False, "[]"),
            ("pull-requests", True, "[]"),
        ):
            with self.subTest(mode=mode, dry_run=dry_run):
                context = {
                    "inputs.mode": mode,
                    "inputs.dry_run": dry_run,
                    "steps.report.outputs.continue_pull_requests": forged,
                }
                self.assertEqual(evaluate(output, context), expected)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_check_inputs_needs_pull_requests_alone(self) -> None:
        """Accepted beside pull_requests alone; refused without them or with targets."""
        budget = self.step("select", "budget")
        base = {
            **ReviseOnlyContracts.BUDGET_ENV,
            "REVISE_ONLY": "false",
            "OUTSTANDING_ONLY": "true",
        }
        pulls = {"PULL_REQUESTS_RAW": "a#2", "PULL_COUNT": "1"}
        error = "outstanding_only needs pull_requests alone"
        cases: tuple[tuple[dict[str, str], str | None], ...] = (
            (pulls, None),
            ({**pulls, "OUTSTANDING_ONLY": "false"}, None),
            # Naming nothing is refused before outstanding_only is read.
            ({}, SCAN_REFUSED),
            ({"PULL_COUNT": ""}, SCAN_REFUSED),
            ({**pulls, "ISSUES_RAW": "a#1", "ISSUE_COUNT": "1"}, error),
            ({"ISSUES_RAW": "a#1", "ISSUE_COUNT": "1"}, error),
            ({**pulls, "CONTINUES_RUN": "42"}, None),
            (
                {**pulls, "CONTINUES_RUN": "42", "OUTSTANDING_ONLY": "false"},
                "continues_run needs outstanding_only",
            ),
            ({**pulls, "CONTINUES_RUN": "4 2"}, "continues_run must be a run ID"),
            ({**pulls, "CONTINUES_RUN": "0"}, "continues_run must be a run ID"),
        )
        for overrides, expected in cases:
            with (
                self.subTest(**overrides),
                tempfile.TemporaryDirectory() as holder,
            ):
                output = Path(holder) / "output"
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        "PATH": os.environ["PATH"],
                        **base,
                        **overrides,
                        "GITHUB_OUTPUT": str(output),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if expected is None:
                    self.assertEqual(proc.returncode, 0, proc.stdout)
                else:
                    self.assertEqual(proc.returncode, 1, proc.stdout)
                    self.assertIn(f"::error::{expected}", proc.stdout)
                    self.assertFalse(output.exists())


def evaluate(expression: str, context: Mapping[str, object]) -> object:
    """Evaluate a GitHub expression of names, literals, !, ==, !=, && and ||.

    Enough for a job condition: each dotted name is read from
    ``context``, ``cancelled()`` is false, and the operators keep
    GitHub's value semantics, which Python's ``and`` and ``or`` share.
    """
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2]
    names: list[str] = []

    def name(match: re.Match[str]) -> str:
        names.append(match.group(0))
        return f"_[{len(names) - 1}]"

    body = re.sub(
        r"'[^']*'|[A-Za-z_][\w-]*(?:\.[\w-]+)+",
        lambda m: m.group(0) if m.group(0).startswith("'") else name(m),
        body,
    )
    body = body.replace("cancelled()", "False")
    body = re.sub(r"!(?!=)", " not ", body)
    body = body.replace("&&", " and ").replace("||", " or ")
    values = [context[n] for n in names]
    return eval(body, {"__builtins__": {}}, {"_": values})  # noqa: S307


CONTINUE_DISPATCH = (
    'gh workflow run code-monkey-cron.yaml --repo "$GITHUB_REPOSITORY"'
    " -f dry_run=false -f mode=pull-requests -f max_pull_requests=0"
    ' -f pull_requests="$pull" -f outstanding_only=true'
    ' -f continues_run="$PARENT_RUN"'
    ' -f model="$MODEL" -f fusion_auto_model_select="$FUSION"'
    ' -f fusion_tier="$FUSION_TIER" -f max_runtime_minutes="$RUNTIME"'
    ' -f max_rounds="$ROUNDS" -f stale_status_minutes="$STALE"'
    ' -f allow_subagents="$SUBAGENTS"'
    ' -f request_review="$REVIEW"'
    ' -f request_antagonistic_review="$ANTAGONISTIC"'
    ' -f load_repo_skills="$SKILLS" -f include_dotgithub="$DOTGITHUB"'
    ' "${inherit[@]}"'
)


class ContinueJobContracts(WorkflowCase):
    """The caller dispatches the next round of a pull request left with work (#167)."""

    WORKFLOW = CRON

    def test_form_offers_outstanding_only_and_forwards_it(self) -> None:
        """A boolean, off by default, reaching the reusable workflow."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["outstanding_only"]["type"], "boolean")
        self.assertIs(inputs["outstanding_only"]["default"], False)
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(
            str(with_["outstanding_only"]), "inputs.outstanding_only"
        )

    def test_form_offers_continues_run_and_forwards_it(self) -> None:
        """A string, empty by default, reaching the reusable workflow (#179)."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["continues_run"]["type"], "string")
        self.assertEqual(inputs["continues_run"]["default"], "")
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(str(with_["continues_run"]), "inputs.continues_run")
        env = cast(dict[str, Any], self.step("continue", "dispatch")["env"])
        self.assert_expression(env["PARENT_RUN"], "github.run_id")

    def test_continue_rounds_follows_the_variable(self) -> None:
        """The variable that stops the continue job tells the comment too (#252)."""
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(
            str(with_["continue_rounds"]), "vars.CODE_MONKEY_CONTINUE != 'false'"
        )
        self.assertIn(
            "vars.CODE_MONKEY_CONTINUE != 'false'", str(self.jobs["continue"]["if"])
        )

    def test_job_holds_actions_write_alone(self) -> None:
        """One grant, no checkout, no artifact and no stored secret."""
        job = self.jobs["continue"]
        self.assertEqual(job["needs"], ["code-monkey"])
        self.assertEqual(job["permissions"], {"actions": "write"})
        self.assertEqual(self.actions("continue", CHECKOUT), [])
        self.assertEqual(self.actions("continue", DOWNLOAD), [])
        self.assertEqual(SECRET_REFERENCE.findall(dumped(job)), [])
        step = self.step("continue", "dispatch")
        self.assert_expression(step["env"]["GH_TOKEN"], "github.token")
        self.assert_expression(
            step["env"]["PULLS"], "needs.code-monkey.outputs.continue_pull_requests"
        )

    def test_job_is_gated_on_the_output_and_the_switch(self) -> None:
        """Not on the call's result; never on a dry run; a variable stops it."""
        self.assert_expression(
            self.jobs["continue"]["if"],
            "!cancelled()"
            " && needs.code-monkey.outputs.continue_pull_requests != ''"
            " && needs.code-monkey.outputs.continue_pull_requests != '[]'"
            " && !inputs.dry_run"
            " && inputs.mode == 'pull-requests'"
            " && vars.CODE_MONKEY_CONTINUE != 'false'",
        )

    def test_only_a_pull_requests_run_continues(self) -> None:
        """A forged output of a branches or select run starts no round (#179).

        Every continuation is dispatched in pull-requests mode, so the
        parent's own mode, trusted input, gates it.
        """
        condition = str(self.jobs["continue"]["if"])
        forged = '["lfreleng-actions/a#1"]'
        cases = (
            ("pull-requests", forged, True),
            ("branches", forged, False),
            ("select", forged, False),
            ("pull-requests", "[]", False),
            ("pull-requests", "", False),
        )
        for mode, pulls, expected in cases:
            with self.subTest(mode=mode, pulls=pulls):
                context = {
                    "inputs.mode": mode,
                    "inputs.dry_run": False,
                    "needs.code-monkey.outputs.continue_pull_requests": pulls,
                    "vars.CODE_MONKEY_CONTINUE": "",
                }
                self.assertIs(bool(evaluate(condition, context)), expected)
        dry = {
            "inputs.mode": "pull-requests",
            "inputs.dry_run": True,
            "needs.code-monkey.outputs.continue_pull_requests": '["o/r#1"]',
            "vars.CODE_MONKEY_CONTINUE": "",
        }
        self.assertFalse(evaluate(condition, dry))

    def test_dispatch_runs_after_harden_runner_in_block_mode(self) -> None:
        """The allow-list loads, then the runner is hardened, then the dispatch."""
        steps = self.steps("continue")
        self.assertTrue(is_action(steps[0], BLOCK_ACTION))
        self.assertTrue(is_action(steps[1], HARDEN_RUNNER))
        self.assertEqual(steps[1]["with"]["egress-policy"], "block")
        forwarded = self.jobs["code-monkey"]["with"]["harden_runner_allowlist"]
        self.assertEqual(steps[0]["with"]["config"], forwarded)
        self.assertEqual(self.position("continue", "dispatch"), 2)
        self.assertEqual(len(steps), 3)
        self.assertIn(CONTINUE_DISPATCH, flatten(str(steps[2]["run"])))

    def test_options_are_forwarded_with_the_form_defaults(self) -> None:
        """The loop keeps the operator's model, runtime, rounds and switches."""
        env = cast(dict[str, Any], self.step("continue", "dispatch")["env"])
        self.assert_expression(env["MODEL"], "inputs.model")
        self.assert_expression(env["RUNTIME"], "inputs.max_runtime_minutes || '180'")
        self.assert_expression(env["ROUNDS"], "inputs.max_rounds || '10'")
        self.assert_expression(env["STALE"], "inputs.stale_status_minutes || '8'")
        self.assert_expression(env["SUBAGENTS"], "inputs.allow_subagents")
        self.assert_expression(env["REVIEW"], "inputs.request_review")
        self.assert_expression(
            env["ANTAGONISTIC"], "inputs.request_antagonistic_review"
        )
        self.assert_expression(env["SKILLS"], "inputs.load_repo_skills")
        self.assert_expression(env["DOTGITHUB"], "inputs.include_dotgithub")
        self.assert_expression(env["FUSION"], "inputs.fusion_auto_model_select")
        self.assert_expression(env["FUSION_TIER"], "inputs.fusion_tier || 'standard'")
        self.assertNotIn("github.event", dumped(self.jobs["continue"]))

    def dispatch(
        self, pulls: object, fail: str = "", **overrides: str
    ) -> tuple[int, list[str], str, str]:
        """Run the dispatch step with a fake gh; return status, calls, stdout, summary."""
        step = self.step("continue", "dispatch")
        with tempfile.TemporaryDirectory() as holder:
            work = Path(holder)
            fake = work / "bin"
            fake.mkdir()
            calls = work / "calls"
            calls.touch()
            (fake / "gh").write_text(
                "#!/bin/bash\n"
                f'printf "%s\\n" "$*" >> "{calls}"\n'
                f'case "$*" in *"{fail or "never"}"*) exit 1 ;; esac\n',
                encoding="utf-8",
            )
            (fake / "gh").chmod(0o755)
            summary = work / "summary"
            summary.touch()
            env = {
                "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
                "GH_TOKEN": "t",
                "GITHUB_REPOSITORY": "lfreleng-actions/github-code-monkey-bot",
                "ORG": "lfreleng-actions",
                "PULLS": pulls if isinstance(pulls, str) else json.dumps(pulls),
                "MODEL": "Claude Opus 5.5",
                "RUNTIME": "180",
                "ROUNDS": "10",
                "STALE": "8",
                "PARENT_RUN": "42",
                "SUBAGENTS": "true",
                "REVIEW": "true",
                "ANTAGONISTIC": "true",
                "SKILLS": "false",
                "DOTGITHUB": "false",
                "FUSION": "false",
                "FUSION_TIER": "standard",
                "INHERIT": "false",
                "EFFECTIVE": "",
                "GITHUB_STEP_SUMMARY": str(summary),
                **overrides,
            }
            run = subprocess.run(
                ["bash", "-c", str(step["run"])],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            return (
                run.returncode,
                calls.read_text(encoding="utf-8").splitlines(),
                run.stdout,
                summary.read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_one_dispatch_per_pull_request(self) -> None:
        """Each pull request is its own live, outstanding-only named run."""
        pulls = ["lfreleng-actions/a#1", "LFReleng-Actions/b#2"]
        status, calls, _, summary = self.dispatch(pulls)
        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [
                "workflow run code-monkey-cron.yaml --repo"
                " lfreleng-actions/github-code-monkey-bot -f dry_run=false"
                " -f mode=pull-requests -f max_pull_requests=0"
                f" -f pull_requests={pull} -f outstanding_only=true"
                " -f continues_run=42"
                " -f model=Claude Opus 5.5 -f fusion_auto_model_select=false"
                " -f fusion_tier=standard -f max_runtime_minutes=180"
                " -f max_rounds=10 -f stale_status_minutes=8"
                " -f allow_subagents=true"
                " -f request_review=true -f request_antagonistic_review=true"
                " -f load_repo_skills=false -f include_dotgithub=false"
                for pull in pulls
            ],
        )
        self.assertIn("dispatched: lfreleng-actions/a#1", summary)
        self.assertIn("dispatched: LFReleng-Actions/b#2", summary)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_disabled_review_switches_stay_off(self) -> None:
        """A run that turned a review off dispatches its next round with it off."""
        status, calls, _, _ = self.dispatch(
            ["lfreleng-actions/a#1"], REVIEW="false", ANTAGONISTIC="false"
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            calls[0].endswith(
                " -f request_review=false -f request_antagonistic_review=false"
                " -f load_repo_skills=false -f include_dotgithub=false"
            ),
            calls[0],
        )

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_enabled_skills_and_dotgithub_stay_on(self) -> None:
        """A run that trusted checkout skills or .github keeps them for the loop."""
        status, calls, _, _ = self.dispatch(
            ["lfreleng-actions/a#1"], SKILLS="true", DOTGITHUB="true"
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            calls[0].endswith(" -f load_repo_skills=true -f include_dotgithub=true"),
            calls[0],
        )

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_a_failed_dispatch_is_a_warning(self) -> None:
        """Three pull requests, one failing: all tried, one warned, success."""
        pulls = ["lfreleng-actions/a#1", "lfreleng-actions/b#2", "lfreleng-actions/c#3"]
        status, calls, stdout, summary = self.dispatch(pulls, fail="b#2")
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(stdout.count("::warning::"), 1)
        self.assertIn("lfreleng-actions/b#2", stdout)
        self.assertIn("failed: lfreleng-actions/b#2", summary)
        self.assertIn("dispatched: lfreleng-actions/c#3", summary)

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_malformed_list_is_refused_before_any_dispatch(self) -> None:
        """Another owner, bad syntax or a newline refuses the whole list."""
        good = "lfreleng-actions/a#1"
        for bad in (
            "elsewhere/a#2",
            "lfreleng-actions/a#0",
            "lfreleng-actions/a",
            "lfreleng-actions/a#2\nlfreleng-actions/b#3",
            "lfreleng-actions/a b#2",
            "{}",
        ):
            with self.subTest(entry=bad):
                status, calls, _, _ = self.dispatch([good, bad])
                self.assertNotEqual(status, 0)
                self.assertEqual(calls, [])
        for pulls in ("not json", json.dumps({"a": 1}), json.dumps([good, 3])):
            with self.subTest(pulls=pulls):
                status, calls, _, _ = self.dispatch(pulls)
                self.assertNotEqual(status, 0)
                self.assertEqual(calls, [])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_forwarded_options_are_validated(self) -> None:
        """An unknown model or a malformed number refuses before any dispatch."""
        for overrides in (
            {"MODEL": "gpt; rm -rf"},
            {"RUNTIME": "1 -f x=y"},
            {"ROUNDS": "0"},
            {"STALE": ""},
            {"STALE": "-1"},
            {"STALE": "8 -f x=y"},
            {"PARENT_RUN": ""},
            {"PARENT_RUN": "42 -f x=y"},
            {"SUBAGENTS": "yes"},
            {"REVIEW": "true -f x=y"},
            {"ANTAGONISTIC": ""},
            {"SKILLS": "1"},
            {"DOTGITHUB": "true -f x=y"},
            {"FUSION": "yes"},
            {"FUSION_TIER": "ultra"},
            {"FUSION_TIER": "high -f x=y"},
        ):
            with self.subTest(**overrides):
                status, calls, _, _ = self.dispatch(
                    ["lfreleng-actions/a#1"], **overrides
                )
                self.assertNotEqual(status, 0)
                self.assertEqual(calls, [])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_zero_stale_minutes_is_forwarded(self) -> None:
        """``0``, which turns the stale rule off, passes the check."""
        status, calls, _, _ = self.dispatch(["lfreleng-actions/a#1"], STALE="0")
        self.assertEqual(status, 0)
        self.assertIn(" -f stale_status_minutes=0 ", calls[0])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_stale_minutes_take_the_select_jobs_syntax(self) -> None:
        """A value the select job accepts, ``08`` or ``00``, is forwarded."""
        for value in ("08", "00"):
            with self.subTest(value=value):
                status, calls, _, _ = self.dispatch(
                    ["lfreleng-actions/a#1"], STALE=value
                )
                self.assertEqual(status, 0)
                self.assertIn(f" -f stale_status_minutes={value} ", calls[0])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_fusion_stays_on_at_its_tier(self) -> None:
        """A Copilot Fusion run dispatches its next round on Fusion (#240)."""
        status, calls, _, _ = self.dispatch(
            ["lfreleng-actions/a#1"], FUSION="true", FUSION_TIER="max"
        )
        self.assertEqual(status, 0)
        self.assertIn(" -f fusion_auto_model_select=true -f fusion_tier=max ", calls[0])


class StaleStatusContracts(ReusableWorkflowCase):
    """stale_status_minutes reaches the scan and the review wait (#168)."""

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_check_inputs_takes_a_non_negative_integer(self) -> None:
        """``0`` and up pass; anything else fails the select job."""
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(env["STALE_MINUTES"], "inputs.stale_status_minutes")
        base = {
            **ReviseOnlyContracts.BUDGET_ENV,
            "REVISE_ONLY": "false",
            "PULL_REQUESTS_RAW": "a#2",
            "PULL_COUNT": "1",
        }
        for value, passes in (
            ("0", True),
            ("8", True),
            ("30", True),
            ("08", True),
            ("", False),
            ("-1", False),
            ("1.5", False),
            ("8 x", False),
        ):
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as holder,
            ):
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        **base,
                        "PATH": os.environ["PATH"],
                        "STALE_MINUTES": value,
                        "GITHUB_OUTPUT": str(Path(holder) / "output"),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode == 0, passes, proc.stdout)
                if not passes:
                    self.assertIn(
                        "::error::stale_status_minutes must be a non-negative",
                        proc.stdout,
                    )

    def test_the_scan_and_the_review_wait_read_the_list(self) -> None:
        """Both steps pass the context list and the minutes."""
        for job, step, source in (
            ("select", "Select issues", "inputs.stale_status_minutes"),
            # The effective value, which a hand-back may inherit (#241).
            ("publish", "review", "needs.select.outputs.stale_status_minutes"),
        ):
            with self.subTest(step=step):
                found = self.step(job, step)
                env = cast(dict[str, Any], found["env"])
                self.assert_expression(env["STALE_MINUTES"], source)
                run = flatten(found["run"])
                self.assertIn(
                    "--stale-statuses monkey-assets/config/stale-statuses.txt", run
                )
                self.assertIn('--stale-status-minutes "$STALE_MINUTES"', run)


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
                "fusion_tier",
                "max_pull_requests",
                "issues",
                "pull_requests",
                "revise",
                "revise_only",
                "outstanding_only",
                "continues_run",
                "inherit_run_parameters",
                "continue_rounds",
                "max_runtime_minutes",
                "max_rounds",
                "stale_status_minutes",
                "max_concurrent_agents",
                "allow_subagents",
                "include_dotgithub",
                "load_repo_skills",
                "allow_workflow_edits",
                "allow_precommit_config_edits",
                "request_review",
                "request_antagonistic_review",
                "antagonistic_repository",
                "exclude_repos",
                "only_known_authors",
                "issue_authors",
                "fork_pool_token_max_days",
                "fork_pool_token_expires",
                "guidance_repository",
                "guidance_ref",
                "harden_runner_egress",
                "harden_runner_allowlist",
                "harden_runner_supplemental_allowlist",
                "harden_runner_supplemental_unpinned",
                "harden_runner_extra_endpoints",
                "github_app_client_id",
                "assets_repository",
                "assets_ref",
            },
        )
        secrets = cast(dict[str, dict[str, Any]], call["secrets"])
        self.assertEqual(
            set(secrets),
            {
                "copilot_token",
                "github_app_private_key",
                "copilot_review_token",
                "antagonistic_dispatch_token",
                "fork_pool_token",
            },
        )
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
            "max_runtime_minutes": "180",
            "max_rounds": "10",
            "stale_status_minutes": "8",
            "max_concurrent_agents": "10",
            "allow_subagents": True,
            "include_dotgithub": False,
            "load_repo_skills": False,
            "allow_workflow_edits": False,
            "allow_precommit_config_edits": False,
            "request_review": True,
            "request_antagonistic_review": True,
            "antagonistic_repository": "lfreleng-actions/github-code-review-bot",
            "revise": True,
            "revise_only": False,
            "outstanding_only": False,
            "continues_run": "",
            # A caller that does not continue says an operator must.
            "continue_rounds": False,
            "pull_requests": "",
            "fork_pool_token_max_days": "",
            "fork_pool_token_expires": "",
            # A caller omitting assets_repository checks out these
            # assets; the stale pre-rename name broke such callers.
            "assets_repository": "lfreleng-actions/github-code-monkey-bot",
        }
        for name, default in expected.items():
            with self.subTest(input=name):
                actual: object = inputs[name]["default"]
                self.assertIs(type(actual), type(default))
                self.assertEqual(actual, default)


class SudoAndContainerContracts(ReusableWorkflowCase):
    """The author job runs without sudo and containers unless opted in (#189)."""

    MARKER = ".enable-sudo-and-containers"
    GUARD = "Require sudo and containers off"

    def test_first_step_disables_both_unless_the_entry_opted_in(self) -> None:
        """Harden-runner leads the job and turns both off by default."""
        first = self.steps("author")[0]
        self.assertTrue(is_action(first, HARDEN_RUNNER))
        with_ = cast(dict[str, Any], first["with"])
        self.assertEqual(with_["egress-policy"], "audit")
        self.assert_expression(
            with_["disable-sudo-and-containers"],
            "matrix.sudo_and_containers != true",
        )

    def test_second_step_refuses_both_left_usable(self) -> None:
        """A hardener that skipped its setup cannot hand a session root or Docker."""
        guard = self.step("author", self.GUARD)
        self.assertEqual(self.position("author", self.GUARD), 1)
        self.assert_expression(guard["if"], "matrix.sudo_and_containers != true")
        lines = command_lines(str(guard["run"]))
        self.assertIn("if sudo -n true >/dev/null 2>&1; then", lines)
        self.assertIn("if timeout 30 docker info >/dev/null 2>&1; then", lines)
        self.assertEqual(lines.count("exit 1"), 2)

    def test_author_steps_need_neither(self) -> None:
        """No step the job runs itself calls sudo or docker, bar the probe."""
        command = re.compile(r"(^|[;&|(]\s*)(sudo|docker|podman)(\s|$)")
        for step in self.steps("author"):
            if step.get("name") == self.GUARD:
                continue
            with self.subTest(step=step.get("name")):
                for line in command_lines(str(step.get("run", ""))):
                    self.assertIsNone(command.search(line), line)

    def test_runtime_context_states_the_switch(self) -> None:
        """Every session learns whether sudo and containers are available."""
        step = self.step("author", "Assemble prompt")
        env = cast(dict[str, Any], step["env"])
        self.assert_expression(env["SUDO_AND_CONTAINERS"], "matrix.sudo_and_containers")
        script = flatten(step["run"])
        self.assertIn('if [ "$SUDO_AND_CONTAINERS" = true ]; then', script)
        self.assertIn("sudo and containers: available", script)
        self.assertIn("sudo and containers: not available", script)

    def test_prompts_protect_and_name_the_marker(self) -> None:
        """Both prompts protect the marker and name it as the maintainer's switch."""
        for path in (PROMPT, REVISE_PROMPT):
            text = squash(path.read_text(encoding="utf-8"))
            with self.subTest(prompt=path.name):
                self.assertIn(
                    f"`SECURITY.md`, `.zizmor.yml`, `.zizmor.yaml` and `{self.MARKER}`",
                    text,
                )
                self.assertIn("sudo and containers: not available", text)
                self.assertIn("do not work around", text)

    def test_prompts_name_the_zizmor_configuration(self) -> None:
        """Both prompts protect the files zizmor reads by their names (#207)."""
        for path in (PROMPT, REVISE_PROMPT):
            text = squash(path.read_text(encoding="utf-8"))
            with self.subTest(prompt=path.name):
                self.assertIn(
                    "the zizmor configuration (`zizmor.yml` or `zizmor.yaml`,"
                    " at the root or directly under `.github/`)",
                    text,
                )


class WorkflowEditSwitchContracts(ReusableWorkflowCase):
    """``allow_workflow_edits`` reaches selection, prompt and publisher (#143)."""

    def test_input_is_an_off_by_default_boolean(self) -> None:
        """A caller that says nothing gets no workflow edits."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        switch = cast(dict[str, Any], call["inputs"]["allow_workflow_edits"])
        self.assertEqual(switch["type"], "boolean")
        self.assertIs(switch["default"], False)

    def test_selection_records_the_switch(self) -> None:
        """The trusted selection carries it, for the publisher to read."""
        step = self.step("select", "Select issues")
        env = cast(dict[str, Any], step["env"])
        self.assert_expression(
            env["ALLOW_WORKFLOW_EDITS"], "inputs.allow_workflow_edits"
        )
        self.assertIn(
            'if [ "$ALLOW_WORKFLOW_EDITS" = true ]; then'
            " extra+=(--allow-workflow-edits) fi",
            flatten(step["run"]),
        )

    def test_runtime_context_states_the_switch(self) -> None:
        """An issue session learns whether it may change workflow files."""
        step = self.step("author", "Assemble prompt")
        script = flatten(step["run"])
        self.assertIn("workflow edits: allowed", script)
        self.assertIn("workflow edits: not allowed", script)

    def test_runtime_context_states_the_entry_grant(self) -> None:
        """The session reads its entry's grant, which a label may widen (#223)."""
        step = self.step("author", "Assemble prompt")
        env = cast(dict[str, Any], step["env"])
        self.assert_expression(
            env["ALLOW_WORKFLOW_EDITS"], "matrix.allow_workflow_edits"
        )
        self.assertIn(
            'elif [ "$ALLOW_WORKFLOW_EDITS" = true ]; then'
            " printf -- '- workflow edits: allowed\\n' fi",
            flatten(step["run"]),
        )

    def test_prompts_state_the_workflow_rules(self) -> None:
        """Authoring follows the switch; a round stays within its pull request."""
        author = squash(PROMPT.read_text(encoding="utf-8"))
        self.assertIn("workflow edits: not allowed", author)
        revise = squash(REVISE_PROMPT.read_text(encoding="utf-8"))
        self.assertIn("workflow files the pull request already changes", revise)

    def test_callers_forward_the_switch(self) -> None:
        """Both dispatch forms offer it, off; every call passes it on."""
        for path, job, expected in (
            (CRON, "code-monkey", "inputs.allow_workflow_edits"),
            (TESTING, "dry-run", "inputs.allow_workflow_edits || false"),
        ):
            with self.subTest(workflow=path.name):
                loaded = cast(
                    dict[Any, Any], yaml.safe_load(path.read_text(encoding="utf-8"))
                )
                inputs = loaded[TRIGGERS]["workflow_dispatch"]["inputs"]
                self.assertEqual(inputs["allow_workflow_edits"]["type"], "boolean")
                self.assertIs(inputs["allow_workflow_edits"]["default"], False)
                with_ = loaded["jobs"][job]["with"]
                self.assert_expression(with_["allow_workflow_edits"], expected)
        loaded = cast(
            dict[Any, Any], yaml.safe_load(TESTING.read_text(encoding="utf-8"))
        )
        self.assertIs(loaded["jobs"]["plumbing"]["with"]["allow_workflow_edits"], False)


class PrecommitConfigSwitchContracts(ReusableWorkflowCase):
    """``allow_precommit_config_edits`` reaches selection and prompt (#227)."""

    def test_input_is_an_off_by_default_boolean(self) -> None:
        """A caller that says nothing gets no pre-commit config edits."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        switch = cast(dict[str, Any], call["inputs"]["allow_precommit_config_edits"])
        self.assertEqual(switch["type"], "boolean")
        self.assertIs(switch["default"], False)

    def test_selection_records_the_switch(self) -> None:
        """The trusted selection carries it, for the publisher to read."""
        step = self.step("select", "Select issues")
        env = cast(dict[str, Any], step["env"])
        self.assert_expression(
            env["ALLOW_PRECOMMIT_CONFIG_EDITS"],
            "inputs.allow_precommit_config_edits",
        )
        self.assertIn(
            'if [ "$ALLOW_PRECOMMIT_CONFIG_EDITS" = true ]; then'
            " extra+=(--allow-precommit-config-edits) fi",
            flatten(step["run"]),
        )

    def test_runtime_context_states_the_switch(self) -> None:
        """An issue session learns whether it may change the lint gate."""
        script = flatten(self.step("author", "Assemble prompt")["run"])
        self.assertIn("pre-commit config edits: allowed", script)
        self.assertIn("pre-commit config edits: not allowed", script)

    def test_runtime_context_states_the_entry_grant(self) -> None:
        """The session reads its entry's grant, which a label may widen (#231)."""
        step = self.step("author", "Assemble prompt")
        env = cast(dict[str, Any], step["env"])
        self.assert_expression(
            env["ALLOW_PRECOMMIT_CONFIG_EDITS"],
            "matrix.allow_precommit_config_edits",
        )
        self.assertIn(
            'if [ "$TASK" != issue ] && [ "$ALLOW_PRECOMMIT_CONFIG_EDITS" = true ];'
            " then printf -- '- pre-commit config edits: allowed\\n' fi",
            flatten(step["run"]),
        )

    def test_prompts_state_the_precommit_config_rules(self) -> None:
        """Authoring follows the switch; a round stays within its pull request."""
        author = squash(PROMPT.read_text(encoding="utf-8"))
        self.assertIn("pre-commit config edits: not allowed", author)
        revise = squash(REVISE_PROMPT.read_text(encoding="utf-8"))
        self.assertIn(".pre-commit-config.yaml", revise)
        self.assertIn("pull request already changes it", revise)

    def test_callers_forward_the_switch(self) -> None:
        """Both dispatch forms offer it, off; every call passes it on."""
        for path, job, expected in (
            (CRON, "code-monkey", "inputs.allow_precommit_config_edits"),
            (TESTING, "dry-run", "inputs.allow_precommit_config_edits || false"),
        ):
            with self.subTest(workflow=path.name):
                loaded = cast(
                    dict[Any, Any], yaml.safe_load(path.read_text(encoding="utf-8"))
                )
                inputs = loaded[TRIGGERS]["workflow_dispatch"]["inputs"]
                switch = inputs["allow_precommit_config_edits"]
                self.assertEqual(switch["type"], "boolean")
                self.assertIs(switch["default"], False)
                with_ = loaded["jobs"][job]["with"]
                self.assert_expression(with_["allow_precommit_config_edits"], expected)
        loaded = cast(
            dict[Any, Any], yaml.safe_load(TESTING.read_text(encoding="utf-8"))
        )
        plumbing = loaded["jobs"]["plumbing"]["with"]
        self.assertIs(plumbing["allow_precommit_config_edits"], False)

    def test_continue_job_forwards_neither_switch(self) -> None:
        """An unattended continuation cannot widen a pull request's gated paths."""
        loaded = cast(dict[Any, Any], yaml.safe_load(CRON.read_text(encoding="utf-8")))
        job = dumped(loaded["jobs"]["continue"])
        for switch in ("allow_precommit_config_edits", "allow_workflow_edits"):
            with self.subTest(switch=switch):
                self.assertNotIn(switch, job)


class CronCallerContracts(WorkflowCase):
    """The dispatch-only caller: triggers, dispatch form and what it forwards."""

    WORKFLOW = CRON

    def test_no_schedule_and_dispatch_concurrency(self) -> None:
        """A dispatch is the only trigger; revise-only dispatches share a group."""
        self.assertEqual(list(self.triggers), ["workflow_dispatch"])
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        self.assert_expression(
            str(concurrency["group"]),
            "inputs.dry_run"
            " && format('code-monkey-dry-run-{0}', github.run_id)"
            " || (inputs.issues != '' || inputs.pull_requests != '')"
            " && format('code-monkey-named-{0}', github.run_id)"
            " || 'code-monkey'",
        )
        self.assertIs(concurrency["cancel-in-progress"], False)
        self.assertEqual(concurrency["queue"], "max")

    def test_issue_authors_come_from_the_variable_alone(self) -> None:
        """No dispatch field can widen the list of trusted issue authors."""
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(
            str(with_["issue_authors"]), "vars.CODE_MONKEY_ISSUE_AUTHORS || ''"
        )
        inputs = cast(dict[str, Any], self.triggers["workflow_dispatch"]["inputs"])
        self.assertNotIn("issue_authors", inputs)

    def test_fork_pool_token_variables_come_from_the_variables_alone(self) -> None:
        """No dispatch field, and no continuation, can widen the token's limit."""
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        inputs = cast(dict[str, Any], self.triggers["workflow_dispatch"]["inputs"])
        dispatch = dumped(self.jobs["continue"]).lower()
        for name, variable in (
            ("fork_pool_token_max_days", "FORK_POOL_TOKEN_MAX_DAYS"),
            ("fork_pool_token_expires", "FORK_POOL_TOKEN_EXPIRES"),
        ):
            with self.subTest(name=name):
                self.assert_expression(str(with_[name]), f"vars.{variable} || ''")
                self.assertNotIn(name, inputs)
                self.assertNotIn(name, dispatch)

    def test_only_known_authors_comes_from_the_variable_alone(self) -> None:
        """No dispatch field, and no continuation, can switch the list off."""
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(
            str(with_["only_known_authors"]), "vars.ONLY_KNOWN_AUTHORS || ''"
        )
        inputs = cast(dict[str, Any], self.triggers["workflow_dispatch"]["inputs"])
        self.assertNotIn("only_known_authors", inputs)
        dispatch = dumped(self.jobs["continue"]).lower()
        for name in ("only_known_authors", "issue_authors"):
            with self.subTest(name=name):
                self.assertNotIn(name, dispatch)

    def test_no_schedule_only_expression_remains(self) -> None:
        """Nothing tests the event or a cron, and nothing posts to Slack (#196)."""
        text = self.WORKFLOW.read_text(encoding="utf-8")
        for fragment in ("github.event_name", "github.event.schedule", "SLACK_"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, text)
        self.assertEqual(set(self.jobs), {"options", "code-monkey", "continue"})

    def test_run_name_says_what_the_run_works(self) -> None:
        """The Actions list tells each dispatch apart.

        The name keeps the workflow's own as its prefix so the sidebar
        still groups by workflow. A dispatch names its pull requests,
        else its issues, else a revise-only scan; one naming none of
        them fails the input check, and a dry run says so.
        """
        name = str(self.workflow["name"])
        run_name = unwrap(str(self.workflow["run-name"]))
        self.assertTrue(run_name.startswith(f"format('{name}: "), run_name)
        self.assert_expression(
            run_name,
            f"format('{name}: {{0}}{{1}}{{2}}',"
            " inputs.pull_requests != ''"
            " && format('revise {0}', inputs.pull_requests)"
            " || inputs.issues != '' && format('work {0}', inputs.issues)"
            " || inputs.revise_only && 'revise-only scan'"
            " || 'nothing named',"
            " inputs.fusion_auto_model_select"
            " && format(' (Fusion {0})', inputs.fusion_tier) || '',"
            " inputs.dry_run && ' (dry run)' || '')",
        )

    def test_dispatch_form(self) -> None:
        """The manual form mirrors DESIGN.md section 9.2; a dispatch is live."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["dry_run"]["type"], "boolean")
        self.assertIs(inputs["dry_run"]["default"], False)

        model = inputs["model"]
        self.assertEqual(model["type"], "choice")
        self.assertEqual(
            model["options"],
            ["Claude Opus 5.5", "Claude Fable 5.2", "Claude Sonnet 5.5", "GPT-6 Astra"],
        )
        self.assertEqual(model["default"], "Claude Opus 5.5")

        mode = inputs["mode"]
        self.assertEqual(mode["type"], "choice")
        self.assertEqual(mode["options"], ["select", "branches", "pull-requests"])
        self.assertEqual(mode["default"], "pull-requests")

        defaults: dict[str, object] = {
            "max_pull_requests": "10",
            "max_runtime_minutes": "180",
            "max_rounds": "10",
            "stale_status_minutes": "8",
            "max_concurrent_agents": "10",
            "allow_subagents": True,
            "include_dotgithub": False,
            "load_repo_skills": False,
            "allow_workflow_edits": False,
            "allow_precommit_config_edits": False,
            "issues": "",
            "revise_only": False,
            "outstanding_only": False,
            "continues_run": "",
            "fusion_auto_model_select": False,
            "fusion_tier": "standard",
        }
        for name, default in defaults.items():
            with self.subTest(input=name):
                actual: object = inputs[name]["default"]
                self.assertIs(type(actual), type(default))
                self.assertEqual(actual, default)
        for name in ("repositories", "categories", "include_assigned"):
            with self.subTest(gone=name):
                self.assertNotIn(name, inputs)
        # GitHub caps a dispatch form at 25 inputs.
        self.assertLessEqual(len(inputs), 25)

    def test_targets_are_forwarded_as_typed(self) -> None:
        """The target lists reach the reusable workflow; no scan input does."""
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(with_["issues"], "inputs.issues")
        for name in ("repositories", "categories", "include_assigned"):
            with self.subTest(gone=name):
                self.assertNotIn(name, with_)
        self.assert_expression(
            with_["stale_status_minutes"], "inputs.stale_status_minutes || '8'"
        )
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
                "Claude Fable 5.2": "claude-fable-5.2",
                "Claude Sonnet 5.5": "claude-sonnet-5.5",
                "GPT-6 Astra": "gpt-6-astra",
            },
        )
        self.assertRegex(script, r"\*\)[^\n]*exit 1")
        # The continue job validates the same display names before it
        # forwards one, so its case must accept exactly the form's choices.
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["model"]["options"], list(arms))
        guard = str(self.step("continue", "dispatch")["run"])
        block = re.search(r'case "\$MODEL" in(.*?)\*\)', guard, re.S)
        assert block is not None
        self.assertEqual(sorted(re.findall(r"'([^']+)'", block.group(1))), sorted(arms))

    def test_caller_forwards_credentials_and_pins_assets(self) -> None:
        """A dispatch is live unless dry; secrets and App id come from org config."""
        job = self.jobs["code-monkey"]
        self.assertEqual(job["uses"], REUSABLE_CALL)
        with_ = cast(dict[str, Any], job["with"])
        self.assert_expression(with_["model"], "needs.options.outputs.model")
        self.assert_expression(with_["dry_run"], "inputs.dry_run")
        self.assertIn(
            "vars.BOT_APP_CLIENT_ID", unwrap(str(with_["github_app_client_id"]))
        )
        self.assert_expression(with_["assets_ref"], "github.sha")
        # Trusted jobs hold App tokens: block mode with the pinned
        # organisation allow-list, never audit, on the live caller.
        self.assertEqual(with_["harden_runner_egress"], "block")
        self.assertRegex(str(with_["harden_runner_allowlist"]), r"^@[0-9a-f]{40}$")
        secrets = cast(dict[str, Any], job["secrets"])
        self.assertEqual(
            set(secrets),
            {
                "copilot_token",
                "github_app_private_key",
                "copilot_review_token",
                "antagonistic_dispatch_token",
                "fork_pool_token",
            },
        )
        self.assert_expression(secrets["copilot_token"], "secrets.COPILOT_CLI_TOKEN")
        self.assert_expression(
            secrets["github_app_private_key"], "secrets.BOT_APP_PRIVATE_KEY"
        )

    def test_caller_forwards_the_fork_pool_token(self) -> None:
        """The machine account's token arrives under its repository secret name."""
        secrets = cast(dict[str, Any], self.jobs["code-monkey"]["secrets"])
        self.assert_expression(secrets["fork_pool_token"], "secrets.FORK_POOL_TOKEN")

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
        forwarded = self.jobs["code-monkey"]["with"]["harden_runner_allowlist"]
        self.assertEqual(loader["with"]["config"], forwarded)
        self.assertEqual(loader["with"]["allow_list_summary"], "false")
        harden = cast(dict[str, Any], steps[1]["with"])
        self.assertEqual(harden["egress-policy"], "block")
        self.assert_expression(harden["allowed-endpoints"], "env.CONNECTION_ALLOW_LIST")

    def test_caller_steps_are_pinned(self) -> None:
        """The model job's actions are commit-pinned like the reusable ones."""
        self.assert_pinned_steps()


# Free-length dispatch inputs. GitHub refuses a concurrency group name
# of 400 characters or more before any job starts, so no group may
# embed one of these. A test for emptiness yields a boolean that picks
# a branch and never reaches the name, so it is allowed.
UNBOUNDED_INPUTS = ("inputs.issues", "inputs.pull_requests")
EMPTINESS_TEST = re.compile(r"\binputs\.[a-z_]+\s*!=\s*''")


class ConcurrencyGroupLengthContracts(unittest.TestCase):
    """No concurrency group name can grow with a dispatch's target lists."""

    def test_no_group_embeds_a_target_list(self) -> None:
        """Workflow- and job-level groups in both workflows stay bounded."""
        for path in (REUSABLE, CRON):
            loaded = cast(
                dict[str | bool, Any],
                yaml.safe_load(path.read_text(encoding="utf-8")),
            )
            jobs = cast(dict[str, dict[str, Any]], loaded["jobs"])
            scopes: list[tuple[str, object]] = [("workflow", loaded["concurrency"])]
            scopes += [
                (job, body["concurrency"])
                for job, body in jobs.items()
                if "concurrency" in body
            ]
            for scope, concurrency in scopes:
                group = (
                    cast(dict[str, Any], concurrency)["group"]
                    if isinstance(concurrency, dict)
                    else concurrency
                )
                named = EMPTINESS_TEST.sub("", str(group))
                for name in UNBOUNDED_INPUTS:
                    with self.subTest(workflow=path.name, scope=scope, input=name):
                        self.assertNotIn(name, named)


class UnattendedRunContracts(unittest.TestCase):
    """No schedule starts an agent session (#196)."""

    # What a workflow that starts or feeds an agent session names.
    AGENT_MARKERS = ("code-monkey.yaml", "code-monkey-cron.yaml", "COPILOT")

    def test_no_scheduled_workflow_starts_an_agent(self) -> None:
        """A workflow on a schedule names neither the pipeline nor its token."""
        paths = sorted(WORKFLOWS.glob("*.y*ml"))
        self.assertIn(CRON, paths)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            loaded = cast(dict[str | bool, Any], yaml.safe_load(text))
            triggers: Any = loaded.get(TRIGGERS) or {}
            if isinstance(triggers, str):
                triggers = [triggers]
            if "schedule" not in triggers:
                continue
            for marker in self.AGENT_MARKERS:
                with self.subTest(workflow=path.name, marker=marker):
                    self.assertNotIn(marker, text)

    def test_every_pipeline_caller_is_dispatched_or_tested(self) -> None:
        """Only a dispatch or the pull request plumbing calls the pipeline."""
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            text = path.read_text(encoding="utf-8")
            if path == REUSABLE or REUSABLE_CALL not in text:
                continue
            loaded = cast(dict[str | bool, Any], yaml.safe_load(text))
            triggers = cast(dict[str, Any], loaded[TRIGGERS])
            with self.subTest(workflow=path.name):
                self.assertLessEqual(
                    set(triggers), {"workflow_dispatch", "pull_request"}
                )


# Includes the DNS bypass fix.
MIN_HARDEN_VERSION = (2, 22, 3)
# Supports the allow-list loader wiring used by these jobs.
MIN_BLOCK_VERSION = (0, 16, 4)
# Baseline for the expiry alert's Slack API posting steps.
MIN_SLACK_VERSION = (4, 0, 0)
# Baseline for the endpoints these jobs need in block mode.
MIN_ALLOW_LIST_VERSION = (0, 21, 0)

ALLOW_LIST = "allow-list config"
PIN_FLOORS = {
    HARDEN_RUNNER: MIN_HARDEN_VERSION,
    BLOCK_ACTION: MIN_BLOCK_VERSION,
    SLACK_ACTION: MIN_SLACK_VERSION,
    ALLOW_LIST: MIN_ALLOW_LIST_VERSION,
}
PIN_RELEASE = re.compile(r"[ \t]+# v([0-9]+)\.([0-9]+)\.([0-9]+)[ \t]*")


def yaml_fields(node: yaml.Node | None) -> dict[str, yaml.Node]:
    """Read the fields of a YAML mapping without losing source marks."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {
        key.value: value
        for key, value in node.value
        if isinstance(key, yaml.ScalarNode)
    }


def pin_fields(workflow: str, text: str) -> Iterator[tuple[str, yaml.Node]]:
    """Yield action pins and literal allow-list coordinates, not script text."""
    root = yaml_fields(yaml.BaseLoader(text).get_single_node())
    for job in yaml_fields(root.get("jobs")).values():
        fields = yaml_fields(job)
        caller_pin = yaml_fields(fields.get("with")).get("harden_runner_allowlist")
        if caller_pin is not None:
            yield ALLOW_LIST, caller_pin
        steps = fields.get("steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for step in steps.value:
            fields = yaml_fields(step)
            uses = fields.get("uses")
            if not isinstance(uses, yaml.ScalarNode):
                continue
            action = uses.value.partition("@")[0]
            if action not in PIN_FLOORS or action == ALLOW_LIST:
                continue
            yield action, uses
            if action == BLOCK_ACTION:
                config = yaml_fields(fields.get("with")).get("config")
                # The reusable workflow forwards its caller's coordinate.
                if config is not None and not (
                    workflow == REUSABLE.name
                    and isinstance(config, yaml.ScalarNode)
                    and re.fullmatch(
                        r"\$\{\{\s*inputs\.harden_runner_allowlist\s*\}\}", config.value
                    )
                ):
                    yield ALLOW_LIST, config


class PinSite(NamedTuple):
    """The source line and value of one workflow pin."""

    workflow: str
    line: int
    column: int
    source: str
    ref: str
    release: tuple[int, ...]

    @property
    def location(self) -> str:
        """Report a one-based line number for a maintainer."""
        return f"{self.workflow}:{self.line + 1}"


def workflow_pin_sites(sources: Mapping[str, str]) -> dict[str, list[PinSite]]:
    """Collect every call site, including pins with malformed values."""
    sites: dict[str, list[PinSite]] = {name: [] for name in PIN_FLOORS}
    for workflow, text in sources.items():
        lines = text.splitlines()
        for name, node in pin_fields(workflow, text):
            start, end = node.start_mark, node.end_mark
            assert start is not None and end is not None
            line = lines[start.line]
            value = node.value if isinstance(node, yaml.ScalarNode) else ""
            prefix, _, ref = value.partition("@")
            if name == ALLOW_LIST and prefix:
                ref = ""
            comment = (
                PIN_RELEASE.fullmatch(line[end.column :])
                if start.line == end.line
                else None
            )
            sites[name].append(
                PinSite(
                    workflow,
                    start.line,
                    start.column,
                    line,
                    ref,
                    tuple(map(int, comment.groups())) if comment else (),
                )
            )
    return sites


def pin_errors(sites: Mapping[str, list[PinSite]]) -> dict[str, str]:
    """Explain a rejected pin group with all of its call sites."""
    errors: dict[str, str] = {}
    for name, calls in sites.items():
        floor = PIN_FLOORS[name]
        if not calls:
            problem = "no call sites found"
        elif any(
            not re.fullmatch(r"[0-9a-f]{40}", s.ref) or not s.release for s in calls
        ):
            problem = (
                "each pin needs a 40-character lowercase SHA and a # vX.Y.Z comment"
            )
        elif any(s.release < floor for s in calls):
            problem = f"release is below v{'.'.join(map(str, floor))}"
        elif len({(s.ref, s.release) for s in calls}) != 1:
            problem = "call sites disagree"
        else:
            continue
        listing = "".join(f"\n  {s.location}: {s.source.strip()}" for s in calls)
        errors[name] = f"{name}: {problem}{listing}"
    return errors


def rewrite_pin_fixture(
    sources: Mapping[str, str], edits: Iterable[tuple[PinSite, str]]
) -> dict[str, str]:
    """Rewrite real workflow sources in memory, leaving the checkout alone."""
    lines = {name: text.splitlines() for name, text in sources.items()}
    for site, replacement in edits:
        line = lines[site.workflow][site.line]
        lines[site.workflow][site.line] = line[: site.column] + replacement
    return {name: "\n".join(rows) for name, rows in lines.items()}


def fixture_pin(name: str, ref: str, version: str) -> str:
    """Make a quoted YAML pin, optionally with its release comment."""
    prefix = "" if name == ALLOW_LIST else name
    comment = f"  # v{version}" if version else ""
    return f"'{prefix}@{ref}'{comment}"


class ActionPinContracts(unittest.TestCase):
    """A coordinated release bump needs no edits to the wiring contracts."""

    @cached_property
    def sources(self) -> dict[str, str]:
        """Read the same set of workflows as the repository-wide contracts."""
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(WORKFLOWS.glob("*.y*ml"))
        }

    def test_pins_agree_and_meet_the_release_floors(self) -> None:
        """Current workflows agree on each commit and its release comment."""
        errors = pin_errors(workflow_pin_sites(self.sources))
        if errors:
            self.fail("\n".join(errors.values()))

    def test_coordinated_pin_bump_passes(self) -> None:
        """A Dependabot-style update of every call passes without test edits."""
        sites = workflow_pin_sites(self.sources)
        major = max(s.release[0] for calls in sites.values() for s in calls)
        # Pick an unused synthetic SHA, not a copy of any real pin.
        used = {s.ref for calls in sites.values() for s in calls}
        ref = next(
            candidate
            for i in range(len(used) + 1)
            if (candidate := f"{i:040x}") not in used
        )
        bumped = rewrite_pin_fixture(
            self.sources,
            (
                (site, fixture_pin(name, ref, f"{major + 1}.0.0"))
                for name, calls in sites.items()
                for site in calls
            ),
        )
        errors = pin_errors(workflow_pin_sites(bumped))
        if errors:
            self.fail("\n".join(errors.values()))

    def test_bad_pins_name_the_call_sites(self) -> None:
        """Drift and malformed or too-old pins fail against real workflows."""
        sites = workflow_pin_sites(self.sources)
        for name, calls in sites.items():
            first = calls[0]
            ref = first.ref
            version = ".".join(map(str, first.release))
            different = ("1" if ref[0] == "0" else "0") + ref[1:]
            # Use synthetic refs and a synthetic release below every floor.
            for case, new_ref, release, problem in (
                ("different SHA", different, version, "disagree"),
                ("tag", "v99.0.0", version, "lowercase SHA"),
                ("branch", "main", version, "lowercase SHA"),
                ("short SHA", ref[:12], version, "lowercase SHA"),
                ("missing comment", ref, "", "# vX.Y.Z comment"),
                ("old release", ref, "0.0.0", "release is below"),
            ):
                with self.subTest(pin=name, case=case):
                    mutated = rewrite_pin_fixture(
                        self.sources, [(first, fixture_pin(name, new_ref, release))]
                    )
                    error = pin_errors(workflow_pin_sites(mutated))[name]
                    self.assertIn(problem, error)
                    for site in calls:
                        self.assertIn(f"{site.location}:", error)


SLACK_TOKEN = "secrets.SLACK_BOT_TOKEN"


class ExpiryAlertWorkflowContracts(unittest.TestCase):
    """The scheduled fork pool token expiry check and its Slack alert (#244).

    It is no bot run: it holds no App key and no copy of the token,
    reads one variable, and only its posting steps see the Slack token.
    """

    # Load on first use so a missing file fails the test, not setUp.
    @cached_property
    def text(self) -> str:
        """The workflow source."""
        return EXPIRY.read_text(encoding="utf-8")

    @cached_property
    def workflow(self) -> dict[str | bool, Any]:
        """The parsed workflow."""
        return cast(dict[str | bool, Any], yaml.safe_load(self.text))

    @cached_property
    def triggers(self) -> dict[str, Any]:
        """The workflow's triggers."""
        return cast(dict[str, Any], self.workflow[TRIGGERS])

    @cached_property
    def jobs(self) -> dict[str, dict[str, Any]]:
        """The workflow's jobs."""
        return cast(dict[str, dict[str, Any]], self.workflow["jobs"])

    def steps(self, job: str) -> list[dict[str, Any]]:
        """The steps of one job."""
        return cast(list[dict[str, Any]], self.jobs[job]["steps"])

    def test_daily_schedule_and_test_dispatch(self) -> None:
        """A fixed daily time, and a manual test that posts whatever the date."""
        self.assertEqual(set(self.triggers), {"schedule", "workflow_dispatch"})
        schedule = cast(list[dict[str, str]], self.triggers["schedule"])
        self.assertEqual(len(schedule), 1)
        self.assertRegex(schedule[0]["cron"], r"^[0-9]+ [0-9]+ \* \* \*$")
        inputs = cast(dict[str, Any], self.triggers["workflow_dispatch"]["inputs"])
        self.assertEqual(set(inputs), {"test_notification"})
        self.assertEqual(inputs["test_notification"]["type"], "boolean")
        self.assertIs(inputs["test_notification"]["default"], False)

    def test_permissions_concurrency_and_timeouts(self) -> None:
        """No default grant, contents read to check out alone, bounded jobs."""
        self.assertEqual(self.workflow["permissions"], {})
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        self.assertIs(concurrency["cancel-in-progress"], False)
        for job, body in self.jobs.items():
            with self.subTest(job=job):
                self.assertIsInstance(body["timeout-minutes"], int)
                checkouts = [s for s in self.steps(job) if is_action(s, CHECKOUT)]
                if checkouts:
                    self.assertEqual(body["permissions"], {"contents": "read"})
                    self.assertIs(checkouts[0]["with"]["persist-credentials"], False)
                else:
                    self.assertEqual(body["permissions"], {})
        self.assertEqual(
            [job for job in self.jobs if "contents" in self.jobs[job]["permissions"]],
            ["check"],
        )

    def test_every_job_blocks_egress_with_the_pinned_list(self) -> None:
        """Each job loads the organisation allow-list, then blocks against it."""
        for job in self.jobs:
            steps = self.steps(job)
            with self.subTest(job=job):
                self.assertTrue(is_action(steps[0], BLOCK_ACTION))
                self.assertRegex(steps[0]["with"]["config"], r"^@[0-9a-f]{40}$")
                self.assertTrue(is_action(steps[1], HARDEN_RUNNER))
                self.assertEqual(steps[1]["with"]["egress-policy"], "block")
                self.assertEqual(
                    unwrap(steps[1]["with"]["allowed-endpoints"]),
                    "env.CONNECTION_ALLOW_LIST",
                )
                for step in steps:
                    if "uses" in step:
                        self.assertRegex(uses_of(step), COMMIT_PIN)

    def test_evaluation_runs_in_python_without_a_secret(self) -> None:
        """The check job reads the variable through env and holds no secret."""
        check = self.jobs["check"]
        self.assertNotIn("environment", check)
        self.assertEqual(SECRET_REFERENCE.findall(dumped(check)), [])
        evaluate = next(s for s in self.steps("check") if s.get("id") == "evaluate")
        env = cast(dict[str, Any], evaluate["env"])
        self.assertEqual(
            unwrap(env["FORK_POOL_TOKEN_EXPIRES"]), "vars.FORK_POOL_TOKEN_EXPIRES || ''"
        )
        run = str(evaluate["run"])
        self.assertIn("python3 scripts/pool_token.py expiry-check", flatten(run))
        self.assertNotIn("${{", run)

    def test_slack_token_reaches_the_posting_steps_alone(self) -> None:
        """Posting jobs run in production; only the post holds the token's value.

        The configuration check sees whether the secret is set, never
        what it is.
        """
        posters = 0
        for job, body in self.jobs.items():
            for step in self.steps(job):
                found = SECRET_REFERENCE.findall(dumped(step))
                with self.subTest(job=job, step=step.get("name")):
                    self.assertLessEqual(set(found), {SLACK_TOKEN})
                    if is_action(step, SLACK_ACTION):
                        posters += 1
                        with_ = cast(dict[str, Any], step["with"])
                        self.assertEqual(with_["method"], "chat.postMessage")
                        self.assertEqual(with_["errors"], "true")
                        self.assertEqual(unwrap(with_["token"]), SLACK_TOKEN)
                        self.assertEqual(body["environment"], "production")
                    elif found:
                        self.assertEqual(step["name"], "Check Slack configuration")
                        self.assertEqual(
                            unwrap(step["env"]["HAS_TOKEN"]), f"{SLACK_TOKEN} != ''"
                        )
            outside = {k: v for k, v in body.items() if k != "steps"}
            with self.subTest(job=job):
                self.assertEqual(SECRET_REFERENCE.findall(dumped(outside)), [])
        self.assertGreater(posters, 0)
        self.assert_slack_check_precedes_each_post()

    def assert_slack_check_precedes_each_post(self) -> None:
        """A missing Slack setting skips the post with a warning."""
        for job in self.jobs:
            steps = self.steps(job)
            posts = [i for i, s in enumerate(steps) if is_action(s, SLACK_ACTION)]
            for index in posts:
                check = steps[index - 1]
                with self.subTest(job=job):
                    self.assertEqual(check["name"], "Check Slack configuration")
                    self.assertIn("::warning", str(check["run"]))
                    self.assertEqual(
                        unwrap(steps[index]["if"]),
                        f"steps.{check['id']}.outputs.configured == 'true'",
                    )

    def test_alert_posts_only_when_the_check_says_so(self) -> None:
        """The alert job follows the evaluation's notify output."""
        self.assertEqual(
            unwrap(self.jobs["notify"]["if"]), "needs.check.outputs.notify == 'true'"
        )
        self.assertEqual(self.jobs["notify"]["needs"], "check")

    def test_no_pool_token_app_key_or_dispatch(self) -> None:
        """It reads its two variables and touches nothing of the pipeline."""
        for fragment in (
            "FORK_POOL_TOKEN }}",
            "secrets.FORK_POOL_TOKEN",
            "fork_pool_token",
            "PRIVATE_KEY",
            "APP_CLIENT_ID",
            APP_TOKEN,
            "gh workflow run",
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, self.text)
        self.assertEqual(
            set(re.findall(r"vars\.([A-Za-z0-9_]+)", self.text)),
            {"FORK_POOL_TOKEN_EXPIRES", "SLACK_CHANNEL_ID"},
        )
        for job, body in self.jobs.items():
            with self.subTest(job=job):
                self.assertNotIn("uses", body)


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

    def test_plumbing_is_secretless_selection_of_the_merge_ref(self) -> None:
        """PR runs select only, dry, with no secrets, from the merge commit.

        The local ``uses:`` reads the workflow from ``github.sha``; the
        assets come from the same commit, so a branch behind main across
        a workflow/script interface change still runs one consistent
        pair rather than the merge-ref workflow with head-ref scripts.
        """
        job = self.jobs["plumbing"]
        self.assertEqual(job["uses"], REUSABLE_CALL)
        self.assertNotIn("secrets", job)
        with_ = cast(dict[str, Any], job["with"])
        self.assertEqual(with_["mode"], "select")
        self.assertIs(with_["dry_run"], True)
        # No run scans for issues, so plumbing names one of its own.
        self.assert_expression(with_["issues"], "format('{0}#1', github.repository)")
        # The list check must pass, so plumbing names a login.
        self.assert_expression(with_["issue_authors"], "github.repository_owner")
        self.assert_expression(with_["assets_repository"], "github.repository")
        self.assert_expression(with_["assets_ref"], "github.sha")

    def test_plumbing_callers_pass_the_flag_explicitly(self) -> None:
        """Pull request plumbing keeps the list on; the dry-run reads the variable."""
        plumbing = cast(dict[str, Any], self.jobs["plumbing"]["with"])
        self.assert_expression(plumbing["only_known_authors"], "'true'")
        dry_run = cast(dict[str, Any], self.jobs["dry-run"]["with"])
        self.assert_expression(
            dry_run["only_known_authors"], "vars.ONLY_KNOWN_AUTHORS || ''"
        )

    def test_manual_dry_run_takes_the_target_lists(self) -> None:
        """A dispatch names the issues to work; no input names a scan."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["issues"]["default"], "")
        self.assertNotIn("repositories", inputs)
        with_ = cast(dict[str, Any], self.jobs["dry-run"]["with"])
        self.assertNotIn("repositories", with_)
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
            flatten(str(step["run"])),
            '"$UV" run --locked --python "$PYTHON"'
            " python -B -m unittest discover -s tests -v",
        )

    def test_caller_steps_are_pinned(self) -> None:
        """The regression job's actions are commit-pinned."""
        self.assert_pinned_steps()

    def test_actionlint_job_runs_the_hook_pre_commit_ci_skips(self) -> None:
        """actionlint-shellcheck outruns pre-commit.ci, so Actions runs it.

        pre-commit.ci stops a run at 180 seconds; skipping the hook there
        keeps its coverage only while this job runs the same hook, at the
        rev and arguments the configuration pins, over every file.
        """
        config = cast(
            dict[str, Any],
            yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8")),
        )
        hooks = [
            hook["id"]
            for repo in cast(list[dict[str, Any]], config["repos"])
            for hook in cast(list[dict[str, Any]], repo["hooks"])
        ]
        self.assertIn("actionlint-shellcheck", hooks)
        self.assertIn("actionlint-shellcheck", config["ci"]["skip"])
        job = self.jobs["actionlint"]
        self.assertNotIn("if", job)
        self.assertEqual(job["permissions"], {"contents": "read"})
        install = self.step("actionlint", "Install prek")
        self.assertRegex(
            flatten(str(install["run"])),
            r"^\"\$UV\" tool install --python \"\$PYTHON\" 'prek==\d+\.\d+\.\d+'$",
        )
        lint = self.step("actionlint", "Run actionlint with ShellCheck")
        self.assertEqual(
            flatten(str(lint["run"])),
            '"$RUNNER_TEMP/uv-tool-bin/prek" run actionlint-shellcheck --all-files',
        )
        self.assertEqual(
            self.position("actionlint", "Run actionlint with ShellCheck"),
            len(self.steps("actionlint")) - 1,
        )

    def test_uv_workdir_job_runs_the_select_uv_steps(self) -> None:
        """Plants go in first, the select job's uv steps run verbatim, the check last."""
        select = load_jobs(REUSABLE)["select"]["steps"]
        for name in UV_STEPS:
            with self.subTest(step=name):
                (original,) = [s for s in select if s.get("name") == name]
                self.assertEqual(
                    runnable(self.step("uv-workdir", name)), runnable(original)
                )
        plant = "Plant Python and uv settings"
        order = [
            self.position("uv-workdir", "Checkout assets"),
            self.position("uv-workdir", plant),
            *(self.position("uv-workdir", name) for name in UV_STEPS),
            self.position("uv-workdir", "Check outputs and marker"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order[-1], len(self.steps("uv-workdir")) - 1)
        checkout = self.step("uv-workdir", "Checkout assets")
        self.assertEqual(checkout["with"]["path"], "monkey-assets")
        self.assertIs(checkout["with"]["persist-credentials"], False)
        planted = str(self.step("uv-workdir", plant)["run"])
        self.assertIn('plant "$GITHUB_WORKSPACE"\n', planted)
        self.assertIn('plant "$GITHUB_WORKSPACE/monkey-assets"\n', planted)
        check = str(self.step("uv-workdir", "Check outputs and marker")["run"])
        self.assertIn('"$RUNNER_TEMP/preflight-venv/bin/python" -I -c', check)
        self.assertEqual(self.jobs["uv-workdir"]["permissions"], {"contents": "read"})

    def test_uv_workdir_plants_the_job_environment(self) -> None:
        """Bait reaches the uv steps through GITHUB_PATH and GITHUB_ENV.

        One leg names it in UV_PYTHON and UV_PYTHON_SEARCH_PATH, the
        other in VIRTUAL_ENV, CONDA_PREFIX and the parent interpreter;
        a control proves it live before setup-uv, and the check proves
        setup-uv's own lookup was refused.
        """
        job = self.jobs["uv-workdir"]
        strategy = cast(dict[str, Any], job["strategy"])
        self.assertIs(strategy["fail-fast"], False)
        self.assertEqual(strategy["matrix"], {"leg": ["uv-python", "venv"]})
        plant = self.step("uv-workdir", "Plant Python and uv settings")
        self.assert_expression(plant["env"]["LEG"], "matrix.leg")
        planted = str(plant["run"])
        for fragment in (
            'exe "$bait/python3.13"',
            'exe "$bait/uv"',
            '"$1/.venv/conda-meta"',
            'echo "$bait" >> "$GITHUB_PATH"',
            '} >> "$GITHUB_ENV"',
            "echo 'UV_NO_CACHE=1'",
            'echo "UV_PYTHON_ARCH=',
            'echo "UV_PYTHON=$bait/python3"',
            'echo "UV_PYTHON_SEARCH_PATH=$bait"',
            'echo "VIRTUAL_ENV=$GITHUB_WORKSPACE/.venv"',
            'echo "CONDA_PREFIX=$GITHUB_WORKSPACE/.venv"',
            'echo "UV_INTERNAL__PARENT_INTERPRETER=$bait/python3"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, planted)
        control = "Control: the bait is live"
        self.assertEqual(
            self.position("uv-workdir", control),
            self.position("uv-workdir", "Plant Python and uv settings") + 1,
        )
        self.assertEqual(
            self.position("uv-workdir", "Set up uv"),
            self.position("uv-workdir", control) + 1,
        )
        script = str(self.step("uv-workdir", control)["run"])
        self.assertIn("/usr/bin/python3 -I -", script)
        self.assertIn('rm "$RUNNER_TEMP/planted-ran"', script)
        check = self.step("uv-workdir", "Check outputs and marker")
        self.assertIn('[ "$PYTHON_VERSION" = unknown ]', str(check["run"]))
        self.assertEqual(check["env"]["PYTHON"], TRUSTED_PYTHON)

    def test_uv_workdir_plants_a_python_install_dir(self) -> None:
        """Both legs name a workspace install directory holding a bait uv.

        setup-uv puts its Python install directory on PATH ahead of its
        own uv, so an inherited one would run the bait. The control
        starts that bait and the workspace .venv interpreter, which the
        'Install Python' step must never reach from its working directory.
        """
        planted = str(self.step("uv-workdir", "Plant Python and uv settings")["run"])
        for fragment in (
            'installs="$GITHUB_WORKSPACE/planted-uv-python"',
            'exe "$installs/uv"',
            'echo "UV_PYTHON_INSTALL_DIR=$installs"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, planted)
        # Written before the leg's case, so both legs carry it.
        self.assertLess(
            planted.index('echo "UV_PYTHON_INSTALL_DIR=$installs"'),
            planted.index('case "$LEG" in'),
        )
        control = str(self.step("uv-workdir", "Control: the bait is live")["run"])
        self.assertIn('("UV_PYTHON_INSTALL_DIR", ("uv",)),', control)
        self.assertIn('os.path.join(workspace, ".venv", "bin", "python"),', control)
        self.assertIn('if [ "$want" -lt 6 ]', control)


class LintingCallerContracts(unittest.TestCase):
    """The hooks pre-commit.ci skips run in Actions, from a pinned reusable.

    pre-commit.ci has no network and stops the whole run at 3 minutes,
    so ``ci.skip`` lists hooks it cannot run. The linting caller runs
    that set, less the hooks ``testing.yaml`` already runs, so each one
    is enforced somewhere.
    """

    REUSABLE_LINTING = (
        "lfreleng-actions/generic-workflows/.github/workflows/linting.yaml"
    )
    # Hooks only the linting caller runs; testing.yaml runs the rest.
    ENFORCED = ("gha-workflow-linter", "aislop")

    def load(self) -> tuple[dict[str | bool, Any], dict[str, Any]]:
        """Return the linting caller and the job that calls the reusable."""
        workflow = cast(
            dict[str | bool, Any],
            yaml.safe_load(LINTING.read_text(encoding="utf-8")),
        )
        jobs = cast(dict[str, dict[str, Any]], workflow["jobs"])
        self.assertEqual(list(jobs), ["linting"])
        return workflow, jobs["linting"]

    @staticmethod
    def ci_skip() -> list[str]:
        """Return the hook ids pre-commit.ci is told to skip."""
        config = cast(
            dict[str, Any],
            yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8")),
        )
        return cast(list[str], config["ci"]["skip"])

    def test_reusable_is_pinned_to_a_commit(self) -> None:
        """A tag or branch would let the reusable change under this repository."""
        _, job = self.load()
        self.assertRegex(str(job["uses"]), COMMIT_PIN)
        self.assertTrue(str(job["uses"]).startswith(self.REUSABLE_LINTING + "@"))

    def test_holds_no_write_credential(self) -> None:
        """Read-only token, no secrets, and never pull_request_target."""
        workflow, job = self.load()
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertNotIn("secrets", job)
        triggers = cast(dict[str, Any], workflow[TRIGGERS])
        self.assertNotIn("pull_request_target", triggers)
        self.assertIn("pull_request", triggers)

    def test_runs_the_ci_skip_set_less_hooks_testing_runs(self) -> None:
        """Each hook named here must stay in ci.skip for the caller to run it."""
        _, job = self.load()
        with_ = cast(dict[str, Any], job["with"])
        self.assertIs(with_["ci_skipped"], True)
        self.assertEqual(with_["harden_runner_egress"], "block")
        skip = self.ci_skip()
        excluded = str(with_["skip_hooks"]).split()
        self.assertEqual(excluded, ["monkey-tests", "actionlint-shellcheck"])
        for hook in (*self.ENFORCED, *excluded):
            with self.subTest(hook=hook):
                self.assertIn(hook, skip)
        for hook in self.ENFORCED:
            with self.subTest(hook=hook):
                self.assertNotIn(hook, excluded)


class SetupUvContracts(unittest.TestCase):
    """setup-uv works outside the workspace in every workflow."""

    def test_every_setup_uv_step_works_in_runner_temp(self) -> None:
        """setup-uv starts the interpreter it finds in its working directory.

        It also reads uv settings there with its own code, so the env
        alone would not do; python-version would export UV_PYTHON to
        every later step, so it is not used instead. The author job's
        workspace holds the target repository, so this must not rest
        on where that checkout lands.
        """
        found = 0
        for path in sorted(WORKFLOWS.glob("*.yaml")):
            for job, body in load_jobs(path).items():
                for step in cast(list[dict[str, Any]], body.get("steps", [])):
                    if not is_action(step, SETUP_UV):
                        continue
                    found += 1
                    with self.subTest(workflow=path.name, job=job):
                        self.assertEqual(
                            step["with"]["working-directory"], SETUP_UV_WORKDIR
                        )
                        self.assertEqual(step["env"], SETUP_UV_ENV)
                        self.assertNotIn("python-version", step["with"])
        self.assertGreater(found, 0)

    def test_every_setup_uv_step_disables_its_python_lookup(self) -> None:
        """setup-uv runs the interpreter 'uv python find' reports.

        The job's environment steers that lookup, and an earlier step
        can set it through GITHUB_ENV or GITHUB_PATH. The guarded env
        makes uv refuse the lookup; the cache stays off because its
        post-step prune would hit the same refusal. activate-environment
        would put a venv on PATH for every later step.
        """
        jobs = setup_uv_jobs()
        self.assertGreater(len(jobs), 0)
        for workflow, job, steps in jobs:
            with self.subTest(workflow=workflow, job=job):
                (step,) = [s for s in steps if is_action(s, SETUP_UV)]
                self.assertEqual(step.get("id"), "setup-uv")
                self.assertEqual(step["with"], SETUP_UV_WITH)
                env = cast(dict[str, str], step["env"])
                self.assertEqual(env["UV_PYTHON_PREFERENCE"], "lookup-disabled")
                self.assertEqual(env["UV_PYTHON_ARCH"], "")
                self.assertNotIn("UV_SYSTEM_PYTHON", env)
                self.assertNotIn("UV_NO_MODIFY_PATH", env)

    def test_every_setup_uv_step_names_its_python_install_dir(self) -> None:
        """setup-uv puts its Python install directory ahead of its own uv.

        Its 'uv python find' is a bare 'uv', so an install directory an
        earlier step exported through GITHUB_ENV would run any uv held
        there before the refused preference applies. The step names the
        directory 'Install Python' installs into instead.
        """
        jobs = setup_uv_jobs()
        self.assertGreater(len(jobs), 0)
        for workflow, job, steps in jobs:
            with self.subTest(workflow=workflow, job=job):
                (index,) = [i for i, s in enumerate(steps) if is_action(s, SETUP_UV)]
                env = cast(dict[str, str], steps[index]["env"])
                self.assertEqual(env.get("UV_PYTHON_INSTALL_DIR"), PYTHON_INSTALL_DIR)
                install = cast(dict[str, str], steps[index + 1]["env"])
                self.assertEqual(install["INSTALL_DIR"], PYTHON_INSTALL_DIR)

    def test_every_install_python_step_works_in_runner_temp(self) -> None:
        """uv's interpreter queries start a .venv in their working directory.

        Even under 'env -i', uv starts .venv/bin/python to inspect it
        before rejecting it as unmanaged, and the workspace is where an
        earlier step or the checkout can leave one.
        """
        jobs = setup_uv_jobs()
        self.assertGreater(len(jobs), 0)
        for workflow, job, steps in jobs:
            with self.subTest(workflow=workflow, job=job):
                (step,) = [s for s in steps if s.get("name") == INSTALL_PYTHON]
                self.assertEqual(step.get("working-directory"), SETUP_UV_WORKDIR)

    def test_every_uv_job_installs_its_own_python(self) -> None:
        """uv installs and finds a Python under an emptied environment.

        Each copy is identical, and comes straight after setup-uv, so no
        step between them can run uv first.
        """
        scripts: set[str] = set()
        for workflow, job, steps in setup_uv_jobs():
            with self.subTest(workflow=workflow, job=job):
                (index,) = [i for i, s in enumerate(steps) if is_action(s, SETUP_UV)]
                step = steps[index + 1]
                self.assertEqual(step.get("name"), INSTALL_PYTHON)
                self.assertEqual(step.get("id"), "python")
                self.assertEqual(step.get("shell"), "bash")
                self.assertEqual(
                    step["env"],
                    {"UV": UV_PATH, "INSTALL_DIR": "${{ runner.temp }}/uv-python"},
                )
                run = flatten(str(step["run"]))
                for fragment in (
                    'env -i "${clean[@]}" "$UV" python install --quiet --no-bin 3.13',
                    'listed=$(env -i "${clean[@]}" "$UV" python list --only-installed'
                    " --managed-python --output-format json 3.13)",
                    'if [[ "$listed" =~ $first ]]; then',
                    'py="${BASH_REMATCH[2]}"',
                    'case "$py" in /*) ;; ?*) py="$PWD/$py" ;; esac',
                    'case "$py/" in */../*) py= ;; esac',
                    'case "$py" in "$INSTALL_DIR"/*) ;;',
                    'echo "path=$py" >> "$GITHUB_OUTPUT"',
                ):
                    self.assertIn(fragment, run)
                scripts.add(str(step["run"]))
        self.assertEqual(len(scripts), 1)

    def test_install_python_resolves_the_listed_path(self) -> None:
        """The step exports an absolute path below its install directory.

        uv lists an install below the working directory relative to it,
        and the step runs in runner.temp, the install directory's parent.
        The run script goes through bash against a stand-in uv that lists
        the given JSON, so relative and absolute listings take one path.
        """
        _, _, steps = setup_uv_jobs()[0]
        (step,) = [s for s in steps if s.get("name") == INSTALL_PYTHON]
        with tempfile.TemporaryDirectory() as raw:
            temp = os.path.realpath(raw)
            install = os.path.join(temp, "uv-python")
            inside = "uv-python/cpython-3.13-linux-x86_64-gnu/bin/python3.13"
            resolved = os.path.join(temp, inside)
            nested = '"version_parts":{"major":3,"minor":13}'
            cases = {
                "relative": (f'[{{{nested},"path":"{inside}"}}]', resolved),
                "absolute": (f'[{{{nested},"path":"{resolved}"}}]', resolved),
                "outside": ('[{"path":"/usr/bin/python3.13"}]', None),
                "relative-dotdot": ('[{"path":"uv-python/../bait/python3"}]', None),
                "absolute-dotdot": (f'[{{"path":"{install}/../bait/python3"}}]', None),
                "second-entry": (
                    f'[{{"path":"/usr/bin/python3"}},{{"path":"{inside}"}}]',
                    None,
                ),
                "null-first": (f'[{{"path":null}},{{"path":"{inside}"}}]', None),
                "nested-path": (f'[{{"x":{{"path":"{inside}"}}}}]', None),
                "empty": ("[]", None),
            }
            for case, (listed, want) in cases.items():
                with self.subTest(case=case):
                    uv = os.path.join(temp, f"uv-{case}")
                    with open(uv, "w", encoding="utf-8") as handle:
                        handle.write(
                            "#!/bin/sh\n"
                            'if [ "$2" = list ]; then\n'
                            f"  printf '%s\\n' '{listed}'\n"
                            "fi\n"
                        )
                    os.chmod(uv, 0o755)
                    output = os.path.join(temp, f"out-{case}")
                    result = subprocess.run(
                        [
                            "bash",
                            "--noprofile",
                            "--norc",
                            "-eo",
                            "pipefail",
                            "-c",
                            str(step["run"]),
                        ],
                        cwd=temp,
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "PWD": temp,
                            "UV": uv,
                            "INSTALL_DIR": install,
                            "RUNNER_TEMP": temp,
                            "GITHUB_OUTPUT": output,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if want is None:
                        self.assertEqual(result.returncode, 1, result.stdout)
                        self.assertIn("outside its install directory", result.stdout)
                        self.assertFalse(os.path.exists(output))
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout)
                        with open(output, encoding="utf-8") as handle:
                            self.assertEqual(handle.read(), f"path={want}\n")

    def test_every_uv_call_takes_the_trusted_python(self) -> None:
        """uv runs by its setup-uv path and every call names the Python.

        Without an absolute --python, uv run, venv, pip and tool take
        their interpreter from PATH, UV_PYTHON, VIRTUAL_ENV or
        CONDA_PREFIX, and uv python find --system from PATH.
        """
        calls = 0
        for path in sorted(WORKFLOWS.glob("*.yaml")):
            for job, body in load_jobs(path).items():
                for step in cast(list[dict[str, Any]], body.get("steps", [])):
                    lines = command_lines(str(step.get("run", "")))
                    env = cast(dict[str, str], step.get("env") or {})
                    for line in lines:
                        with self.subTest(workflow=path.name, job=job, line=line):
                            self.assertIsNone(BARE_UV.search(line))
                            self.assertNotIn("python find --system", line)
                            if not UV_CALL.search(line):
                                continue
                            calls += 1
                            self.assertRegex(line, r'--python "\$[A-Za-z_{]')
                            self.assertEqual(env.get("UV"), UV_PATH)
                            self.assertEqual(env.get("PYTHON"), TRUSTED_PYTHON)
        self.assertGreater(calls, 0)


class ToolchainPinContracts(unittest.TestCase):
    """The author toolchain is pinned by a committed npm lockfile."""

    def test_pin_directory_holds_manifest_lockfile_and_npmrc(self) -> None:
        """npm ci needs all three; the workflow copies exactly these."""
        for name in ("package.json", "package-lock.json", ".npmrc"):
            with self.subTest(name=name):
                self.assertTrue((TOOLCHAIN_PIN / name).is_file())

    def test_manifest_pins_exact_versions_the_lockfile_records(self) -> None:
        """No range in package.json, and the lockfile matches it."""
        manifest = json.loads((TOOLCHAIN_PIN / "package.json").read_text())
        dependencies = cast(dict[str, str], manifest["dependencies"])
        self.assertEqual(set(dependencies), {"@github/copilot", "aislop"})
        for name, version in dependencies.items():
            with self.subTest(name=name):
                self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        lock = json.loads((TOOLCHAIN_PIN / "package-lock.json").read_text())
        self.assertEqual(lock["packages"][""]["dependencies"], dependencies)


class FusionContracts(ReusableWorkflowCase):
    """Copilot Fusion: the tier input, its check and the CLI flag (#240)."""

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_check_inputs_pairs_fusion_with_a_tier(self) -> None:
        """Fusion needs a known tier; a tier needs Fusion; no tier is a fixed model."""
        budget = self.step("select", "budget")
        env = cast(dict[str, Any], budget["env"])
        self.assert_expression(env["FUSION_TIER"], "inputs.fusion_tier")
        base = {**ReviseOnlyContracts.BUDGET_ENV, "MODEL": "claude-opus-5.5"}
        tier_refused = "model fusion needs fusion_tier standard, high or max"
        cases: tuple[tuple[dict[str, str], str | None], ...] = (
            ({}, None),
            ({"FUSION_TIER": ""}, None),
            *(({"MODEL": "fusion", "FUSION_TIER": t}, None) for t in FUSION_TIERS),
            ({"MODEL": "fusion"}, tier_refused),
            ({"MODEL": "fusion", "FUSION_TIER": "ultra"}, tier_refused),
            ({"MODEL": "fusion", "FUSION_TIER": "high -x"}, tier_refused),
            ({"FUSION_TIER": "high"}, "fusion_tier needs model fusion"),
        )
        for overrides, error in cases:
            with (
                self.subTest(**overrides),
                tempfile.TemporaryDirectory() as holder,
            ):
                output = Path(holder) / "output"
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        "PATH": os.environ["PATH"],
                        **base,
                        **overrides,
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

    def test_input_is_declared_empty_and_reaches_the_selector(self) -> None:
        """A string, empty by default, recorded in the selection."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["fusion_tier"]["type"], "string")
        self.assertEqual(inputs["fusion_tier"]["default"], "")
        select = self.step("select", "Select issues")
        env = cast(dict[str, Any], select["env"])
        self.assert_expression(env["FUSION_TIER"], "inputs.fusion_tier")
        self.assertIn('--fusion-tier="$FUSION_TIER"', flatten(select["run"]))

    def invoke(self, model: str, tier: str) -> list[str]:
        """Run the agent step with a fake copilot; return the argv it got."""
        step = self.step("author", "agent")
        with tempfile.TemporaryDirectory() as holder:
            work = Path(holder)
            fake = work / "bin"
            fake.mkdir()
            argv = work / "argv"
            (fake / "copilot").write_text(
                f'#!/bin/bash\nprintf "%s\\n" "$@" > "{argv}"\n',
                encoding="utf-8",
            )
            (fake / "copilot").chmod(0o755)
            (work / "artefacts").mkdir()
            (work / "artefacts" / "prompt.md").write_text("p", encoding="utf-8")
            (work / "workspace").mkdir()
            subprocess.run(
                ["bash", "-e", "-c", str(step["run"])],
                cwd=work,
                env={
                    "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
                    "COPILOT_GITHUB_TOKEN": "github_pat_x",
                    "MODEL": model,
                    "FUSION_TIER": tier,
                    "ALLOW_SUBAGENTS": "true",
                    "LOAD_REPO_SKILLS": "false",
                },
                check=True,
            )
            return argv.read_text(encoding="utf-8").splitlines()

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_a_fixed_model_takes_no_tier(self) -> None:
        """With the switch off the invocation names the model alone."""
        env = cast(dict[str, Any], self.step("author", "agent")["env"])
        self.assert_expression(env["FUSION_TIER"], "needs.select.outputs.fusion_tier")
        args = self.invoke("claude-opus-5.5", "")
        self.assertEqual(args[:2], ["--prompt=p", "--model=claude-opus-5.5"])
        self.assertEqual(args[2], "--mode")
        self.assertFalse([a for a in args if a.startswith("--fusion-tier")])

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_fusion_passes_its_tier(self) -> None:
        """Each tier reaches the CLI beside ``--model=fusion``."""
        for tier in FUSION_TIERS:
            with self.subTest(tier=tier):
                args = self.invoke("fusion", tier)
                self.assertEqual(args[1:3], ["--model=fusion", f"--fusion-tier={tier}"])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_fallback_result_keeps_the_models_used(self) -> None:
        """A failure after the check still reports the models the session ran."""
        ensure = self.step("publish", "Ensure a result exists")
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
            "AUTHOR_SESSION": "77",
        }
        for used, expected in (
            (["claude-opus-5.5", "gpt-6-astra"], ["claude-opus-5.5", "gpt-6-astra"]),
            ("claude-opus-5.5", []),
            (None, []),
        ):
            with (
                self.subTest(used=used),
                tempfile.TemporaryDirectory() as holder,
            ):
                work = Path(holder)
                (work / "artefacts").mkdir()
                (work / "artefacts" / "check.json").write_text(
                    json.dumps({"models_used": used}), encoding="utf-8"
                )
                subprocess.run(
                    ["bash", "-e", "-c", str(ensure["run"])],
                    cwd=work,
                    env=env,
                    check=True,
                )
                result = json.loads(
                    (work / "artefacts" / "result.json").read_text(encoding="utf-8")
                )
                self.assertEqual(result["models_used"], expected)


class FusionCallerContracts(WorkflowCase):
    """The dispatch form's Fusion switch and tier, off by default (#240)."""

    WORKFLOW = CRON

    def test_form_offers_the_switch_off(self) -> None:
        """A boolean off by default, and a tier choice that starts at standard."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        switch = inputs["fusion_auto_model_select"]
        self.assertEqual(switch["type"], "boolean")
        self.assertIs(switch["default"], False)
        tier = inputs["fusion_tier"]
        self.assertEqual(tier["type"], "choice")
        self.assertEqual(tier["options"], list(FUSION_TIERS))
        self.assertEqual(tier["default"], "standard")
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(
            with_["fusion_tier"], "needs.options.outputs.fusion_tier"
        )
        outputs = cast(dict[str, Any], self.jobs["options"]["outputs"])
        self.assert_expression(outputs["fusion_tier"], "steps.map.outputs.fusion_tier")

    def map_outputs(self, model: str, fusion: str, tier: str) -> tuple[int, str]:
        """Run the options map step; return its status and GITHUB_OUTPUT."""
        step = self.step("options", "map")
        with tempfile.TemporaryDirectory() as holder:
            output = Path(holder) / "output"
            output.touch()
            proc = subprocess.run(
                ["bash", "-e", "-c", str(step["run"])],
                env={
                    "PATH": os.environ["PATH"],
                    "MODEL": model,
                    "FUSION": fusion,
                    "FUSION_TIER": tier,
                    "GITHUB_OUTPUT": str(output),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            return proc.returncode, output.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_switch_off_keeps_the_chosen_model(self) -> None:
        """Off, the tier is ignored and the reusable workflow gets no tier."""
        env = cast(dict[str, Any], self.step("options", "map")["env"])
        self.assert_expression(env["FUSION"], "inputs.fusion_auto_model_select")
        self.assert_expression(env["FUSION_TIER"], "inputs.fusion_tier")
        status, output = self.map_outputs("Claude Opus 5.5", "false", "max")
        self.assertEqual(status, 0)
        self.assertEqual(output, "id=claude-opus-5.5\nfusion_tier=\n")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_switch_on_overrides_the_model(self) -> None:
        """On, every tier routes through ``fusion``; an unknown tier fails."""
        for tier in FUSION_TIERS:
            with self.subTest(tier=tier):
                status, output = self.map_outputs("GPT-6 Astra", "true", tier)
                self.assertEqual(status, 0)
                self.assertEqual(output, f"id=fusion\nfusion_tier={tier}\n")
        status, output = self.map_outputs("GPT-6 Astra", "true", "ultra")
        self.assertEqual(status, 1)
        self.assertEqual(output, "")

    def test_run_name_marks_a_fusion_run(self) -> None:
        """A Fusion dispatch says so, with its tier, in the Actions list."""
        run_name = unwrap(str(self.workflow["run-name"]))
        self.assertIn(
            "inputs.fusion_auto_model_select"
            " && format(' (Fusion {0})', inputs.fusion_tier) || ''",
            " ".join(run_name.split()),
        )


def run_parameters_module() -> Any:
    """The inherited set's one definition, imported on use (#241)."""
    scripts = str(WORKFLOWS.parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return import_module("run_parameters")


# The continue job's per-run inputs: never inherited (#241).
PER_RUN_DISPATCH = {
    "dry_run",
    "mode",
    "max_pull_requests",
    "pull_requests",
    "outstanding_only",
    "continues_run",
    "inherit_run_parameters",
}


class InheritedRunParameterContracts(ReusableWorkflowCase):
    """A hand-back inherits its pull request's run parameters (#241)."""

    def test_input_is_declared_off_and_reaches_the_selector(self) -> None:
        """A boolean, off by default, passed to select_issues.py as a flag."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_call"]["inputs"]
        )
        self.assertEqual(inputs["inherit_run_parameters"]["type"], "boolean")
        self.assertIs(inputs["inherit_run_parameters"]["default"], False)
        select = self.step("select", "Select issues")
        self.assert_expression(
            select["env"]["INHERIT"], "inputs.inherit_run_parameters"
        )
        self.assertIn("extra+=(--inherit-run-parameters)", flatten(select["run"]))

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_check_inputs_needs_one_pull_request_alone(self) -> None:
        """Zero or several pull requests, issues, or another mode are refused."""
        budget = self.step("select", "budget")
        self.assert_expression(
            budget["env"]["INHERIT"], "inputs.inherit_run_parameters"
        )
        one = {
            "INHERIT": "true",
            "REVISE_ONLY": "false",
            "PULL_REQUESTS_RAW": "o/a#2",
            "PULL_COUNT": "1",
        }
        cases: tuple[tuple[dict[str, str], bool], ...] = (
            ({}, True),
            ({"PULL_REQUESTS_RAW": "o/a#2 o/a#3", "PULL_COUNT": "2"}, False),
            (
                {"PULL_REQUESTS_RAW": "", "PULL_COUNT": "0", "REVISE_ONLY": "true"},
                False,
            ),
            ({"ISSUES_RAW": "o/a#1", "ISSUE_COUNT": "1"}, False),
            ({"MODE": "branches"}, False),
        )
        for overrides, passes in cases:
            with self.subTest(**overrides), tempfile.TemporaryDirectory() as holder:
                proc = subprocess.run(
                    ["bash", "-e", "-c", str(budget["run"])],
                    env={
                        "PATH": os.environ["PATH"],
                        **ReviseOnlyContracts.BUDGET_ENV,
                        **one,
                        **overrides,
                        "GITHUB_OUTPUT": str(Path(holder) / "output"),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode == 0, passes, proc.stdout)
                if not passes:
                    self.assertIn("::error::inherit_run_parameters needs", proc.stdout)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_inherited_runtime_sets_the_author_budget(self) -> None:
        """The settle step runs after selection and reads what it inherited."""
        self.assertLess(
            self.position("select", "Select issues"), self.position("select", "params")
        )
        outputs = cast(dict[str, Any], self.jobs["select"]["outputs"])
        for name in ("author_timeout", "agent_timeout", "model", "params"):
            with self.subTest(output=name):
                self.assert_expression(outputs[name], f"steps.params.outputs.{name}")
        step = self.step("select", "params")
        env = {
            "PATH": os.environ["PATH"],
            "MODEL": "claude-opus-5.5",
            "FUSION_TIER": "",
            "MAX_RUNTIME": "180",
            "MAX_ROUNDS": "10",
            "STALE_MINUTES": "8",
            "ALLOW_SUBAGENTS": "true",
            "REQUEST_REVIEW": "true",
            "REQUEST_ANTAGONISTIC_REVIEW": "true",
            "LOAD_REPO_SKILLS": "false",
            "INCLUDE_DOTGITHUB": "false",
        }
        inherited = {
            "model": "gpt-6-astra",
            "fusion_auto_model_select": "false",
            "fusion_tier": "",
            "max_runtime_minutes": "45",
            "max_rounds": "3",
            "stale_status_minutes": "8",
            "allow_subagents": "false",
            "request_review": "false",
            "request_antagonistic_review": "false",
            "load_repo_skills": "false",
            "include_dotgithub": "false",
        }
        with tempfile.TemporaryDirectory() as holder:
            work = Path(holder)
            (work / "monkey-assets").symlink_to(WORKFLOWS.parents[1])
            (work / "artefacts").mkdir()
            (work / "artefacts" / "selection.json").write_text(
                json.dumps({"inherited_parameters": inherited}), encoding="utf-8"
            )
            output = work / "output"
            subprocess.run(
                ["bash", "-e", "-c", str(step["run"])],
                cwd=work,
                env={**env, "GITHUB_OUTPUT": str(output)},
                check=True,
            )
            lines = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
        self.assertEqual(
            (lines["agent_timeout"], lines["author_timeout"]), ("45", "65")
        )
        self.assertEqual(lines["model"], "gpt-6-astra")
        self.assertEqual(lines["request_review"], "false")

    def test_later_jobs_read_the_effective_values(self) -> None:
        """--model, the deny-list switches, the reviews and the footer token."""
        agent = cast(dict[str, Any], self.step("author", "agent")["env"])
        for variable, output in (
            ("MODEL", "model"),
            ("FUSION_TIER", "fusion_tier"),
            ("ALLOW_SUBAGENTS", "allow_subagents"),
            ("LOAD_REPO_SKILLS", "load_repo_skills"),
        ):
            with self.subTest(variable=variable):
                self.assert_expression(
                    agent[variable], f"needs.select.outputs.{output}"
                )
        comment = self.step("publish", "Comment on the issue")
        self.assert_expression(comment["env"]["PARAMS"], "needs.select.outputs.params")
        self.assertIn('--run-parameters "$PARAMS"', flatten(comment["run"]))
        self.assertNotIn("inputs.request_review", dumped(self.jobs["publish"]))
        self.assertNotIn(
            "inputs.request_antagonistic_review", dumped(self.jobs["antagonistic"])
        )
        declared = self.triggers["workflow_call"]["outputs"]
        self.assert_expression(
            declared["run_parameters"]["value"], "jobs.select.outputs.run_parameters"
        )


class InheritedContinueContracts(WorkflowCase):
    """The continue job forwards the inherited set, as the run used it (#241)."""

    WORKFLOW = CRON

    def test_form_offers_the_input_and_forwards_it(self) -> None:
        """A boolean, off by default, the form's 24th input."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["inherit_run_parameters"]["type"], "boolean")
        self.assertIs(inputs["inherit_run_parameters"]["default"], False)
        with_ = cast(dict[str, Any], self.jobs["code-monkey"]["with"])
        self.assert_expression(
            str(with_["inherit_run_parameters"]), "inputs.inherit_run_parameters"
        )

    def test_dispatch_forwards_exactly_the_inherited_set(self) -> None:
        """The forwarded inputs, less the per-run ones, are run_parameters.INHERITED."""
        script = flatten(str(self.step("continue", "dispatch")["run"]))
        forwarded = set(re.findall(r"-f ([a-z_]+)=", script))
        self.assertEqual(
            forwarded - PER_RUN_DISPATCH, set(run_parameters_module().INHERITED)
        )
        env = self.step("continue", "dispatch")["env"]
        self.assert_expression(
            env["EFFECTIVE"], "needs.code-monkey.outputs.run_parameters"
        )

    def dispatch(self, **overrides: str) -> tuple[int, list[str]]:
        """Run the dispatch step for one pull request with a fake gh."""
        cases = ContinueJobContracts()
        cases.setUp()
        status, calls, _, _ = cases.dispatch(["lfreleng-actions/a#1"], **overrides)
        return status, calls

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_effective_values_replace_the_form(self) -> None:
        """An inherited Fusion run continues on Fusion, inheriting again."""
        effective = {
            "model": "fusion",
            "fusion_tier": "high",
            "max_runtime_minutes": "45",
            "max_rounds": "3",
            "stale_status_minutes": "0",
            "allow_subagents": "false",
            "request_review": "false",
            "request_antagonistic_review": "false",
            "load_repo_skills": "true",
            "include_dotgithub": "true",
        }
        status, calls = self.dispatch(EFFECTIVE=json.dumps(effective), INHERIT="true")
        self.assertEqual(status, 0)
        self.assertTrue(
            calls[0].endswith(
                " -f model=Claude Opus 5.5 -f fusion_auto_model_select=true"
                " -f fusion_tier=high -f max_runtime_minutes=45"
                " -f max_rounds=3 -f stale_status_minutes=0"
                " -f allow_subagents=false"
                " -f request_review=false -f request_antagonistic_review=false"
                " -f load_repo_skills=true -f include_dotgithub=true"
                " -f inherit_run_parameters=true"
            ),
            calls[0],
        )
        status, calls = self.dispatch(
            EFFECTIVE=json.dumps({**effective, "model": "gpt-6-astra"})
        )
        self.assertEqual(status, 0)
        self.assertIn(
            " -f model=GPT-6 Astra -f fusion_auto_model_select=false ", calls[0]
        )
        self.assertNotIn("inherit_run_parameters", calls[0])

    @unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs jq")
    def test_bad_effective_values_are_refused(self) -> None:
        """A model the form cannot name, or a malformed value, dispatches nothing."""
        good = {
            "model": "claude-opus-5.5",
            "fusion_tier": "",
            "max_runtime_minutes": "45",
            "max_rounds": "3",
            "stale_status_minutes": "0",
            "allow_subagents": "false",
            "request_review": "false",
            "request_antagonistic_review": "false",
            "load_repo_skills": "true",
            "include_dotgithub": "true",
        }
        self.assertEqual(self.dispatch(EFFECTIVE=json.dumps(good))[0], 0)
        for bad in (
            {**good, "model": "other-model"},
            {**good, "max_runtime_minutes": "45 -f x=y"},
            {**good, "request_review": "yes"},
            {**good, "model": "fusion", "fusion_tier": ""},
        ):
            with self.subTest(**bad):
                status, calls = self.dispatch(EFFECTIVE=json.dumps(bad))
                self.assertNotEqual(status, 0)
                self.assertEqual(calls, [])
        status, calls = self.dispatch(EFFECTIVE="[]")
        self.assertNotEqual(status, 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
