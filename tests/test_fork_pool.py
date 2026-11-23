# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The forks job: proving the matrix, checking the token, readying forks."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
policy = import_module("proposal_policy")
pool = import_module("fork_pool")

ORG = "owner"
TARGET = f"{ORG}/repo"
FORK_ORG = "lfreleng-bot-forks"
FORK = f"{FORK_ORG}/repo"
CONFIG: dict[str, Any] = {"default": FORK_ORG, "organisations": {}}
ACCOUNT = "pool-bot"


def expiring(days: float) -> str:
    """The expiry header of a token that lapses ``days`` from now."""
    when = datetime.now(UTC) + timedelta(days=days)
    return f"GitHub-Authentication-Token-Expiration: {when:%Y-%m-%d %H:%M:%S} UTC"


def reply(
    scopes: str = "public_repo",
    expiry: str | None = None,
    body: str = f'{{"login": "{ACCOUNT}"}}',
) -> str:
    """A ``gh api --include user`` reply; a 30-day expiry unless given."""
    expiry = expiring(30) if expiry is None else expiry
    headers = ["HTTP/2.0 200 OK", f"X-Oauth-Scopes: {scopes}"]
    if expiry:
        headers.append(expiry)
    return "\n".join([*headers, "", body])


SCOPED = reply()
OLD = f"{FORK_ORG}/old-repo"


def repo_object(**overrides: Any) -> dict[str, Any]:
    """A repository API object describing a fork of the target."""
    data: dict[str, Any] = {
        "full_name": FORK,
        "fork": True,
        "parent": {"full_name": TARGET},
    }
    data.update(overrides)
    return data


def absent() -> Exception:
    """The error gh raises for a missing resource."""
    return github.GitHubError("gh: Not Found (HTTP 404)")


def past_deadline() -> list[float]:
    """Clock readings that end a fork poll on its first miss."""
    return [0.0, pool.FORK_WAIT_SECONDS + 1.0]


def entry(**overrides: Any) -> dict[str, Any]:
    """One select-job matrix entry for the target."""
    data: dict[str, Any] = {
        "key": "repo-7",
        "repository": TARGET,
        "repo_name": "repo",
        "fork_org": FORK_ORG,
        "fork_repository": FORK,
    }
    data.update(overrides)
    return data


def matrix(*entries: dict[str, Any]) -> str:
    """The select job's matrix output holding ``entries``."""
    return json.dumps({"include": list(entries)})


TARGET_ENTRY = pool.Target(TARGET, FORK_ORG, "repo")


def prepare(target: Any) -> str:
    """Start one target and, when it is pending, wait for it as the command does."""
    state = pool.start(target, ACCOUNT)
    if isinstance(state, pool.Pending):
        return pool.finish(state, pool.time.monotonic() + pool.FORK_WAIT_SECONDS)
    return state


class NoNetworkCase(unittest.TestCase):
    """Base class failing any test that reaches a real subprocess."""

    def setUp(self) -> None:
        """Forbid ``gh``."""
        guard = patch.object(
            github.subprocess, "run", side_effect=AssertionError("unexpected gh")
        )
        guard.start()
        self.addCleanup(guard.stop)


class TargetsTest(unittest.TestCase):
    """``targets`` proves every matrix entry against the committed mapping."""

    def test_entries_reduce_to_one_target_per_fork(self) -> None:
        """An issue and a revision in one repository need one fork."""
        found = pool.targets(
            matrix(entry(), entry(key="repo-9"), entry(repository="Owner/Repo")),
            CONFIG,
            ORG,
        )
        self.assertEqual(found, [TARGET_ENTRY])
        self.assertEqual(found[0].fork, FORK)

    def test_empty_matrix_has_no_targets(self) -> None:
        """Nothing selected, nothing to ready."""
        self.assertEqual(pool.targets(matrix(), CONFIG, ORG), [])

    def test_mapping_decides_the_fork_organisation(self) -> None:
        """A dedicated pool in the mapping wins over the default."""
        config = {"default": FORK_ORG, "organisations": {ORG: "lfreleng-bot-forks-x"}}
        found = pool.targets(
            matrix(
                entry(
                    fork_org="lfreleng-bot-forks-x",
                    fork_repository="lfreleng-bot-forks-x/repo",
                )
            ),
            config,
            ORG,
        )
        self.assertEqual(found[0].fork, "lfreleng-bot-forks-x/repo")

    def test_disagreeing_entries_are_refused(self) -> None:
        """An entry naming another target, pool or fork never reaches a write."""
        for bad, message in (
            (entry(repository="other/repo"), "is not repo in owner"),
            (entry(fork_org="lfreleng-bot-forks-x"), "another fork organisation"),
            (entry(fork_repository=f"{FORK_ORG}/else"), "another fork repository"),
            (entry(repo_name=""), "no repo_name"),
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(pool.PoolError, message),
            ):
                pool.targets(matrix(bad), CONFIG, ORG)

    def test_malformed_matrix_is_refused(self) -> None:
        """Invalid JSON, no include list or a non-object entry is an error."""
        for raw in ("{", "[]", '{"include": {}}', '{"include": [1]}'):
            with (
                self.subTest(raw=raw),
                self.assertRaises(pool.PoolError),
            ):
                pool.targets(raw, CONFIG, ORG)

    def test_target_owner_cannot_be_its_own_pool(self) -> None:
        """A run against a fork pool itself has nowhere to fork to."""
        with self.assertRaisesRegex(pool.PoolError, "itself"):
            pool.targets(matrix(), CONFIG, FORK_ORG)


class TokenCheckTest(NoNetworkCase):
    """The pool token must be classic and scoped to public_repo alone."""

    def check(self, reply: str) -> str:
        """Run the check against one ``gh api --include user`` reply."""
        with patch.object(github, "run_gh", return_value=reply) as run:
            login = pool.check_token()
        run.assert_called_once_with(["api", "--include", "user"])
        return login

    def test_public_repo_alone_passes(self) -> None:
        """The scope header is matched without regard to its case."""
        self.assertEqual(self.check(SCOPED), ACCOUNT)
        lower = SCOPED.replace("X-Oauth-Scopes", "x-oauth-scopes")
        self.assertEqual(self.check(lower.replace("\n", "\r\n")), ACCOUNT)

    def test_account_without_login_is_refused(self) -> None:
        """A failure the account must fix names it, so its login is required."""
        for body in ("{}", "[]", "not json", '{"login": ""}'):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(pool.PoolError, "reports no login"),
            ):
                self.check(reply(body=body))

    def test_broader_scopes_are_refused(self) -> None:
        """An operator's token, or one that can do more, never writes.

        A well-formed expiry does not excuse a scope beyond public_repo.
        """
        for scopes in ("repo", "public_repo, workflow", "public_repo, admin:org", ""):
            with (
                self.subTest(scopes=scopes),
                self.assertRaisesRegex(pool.PoolError, "must carry public_repo alone"),
            ):
                self.check(reply(scopes=scopes, body="{}"))

    def test_token_without_scopes_is_refused(self) -> None:
        """Fine-grained and App tokens report no scopes header."""
        with self.assertRaisesRegex(pool.PoolError, "reports no scopes"):
            self.check("HTTP/2.0 200 OK\nContent-Type: application/json\n\n{}")

    def test_a_header_in_the_body_does_not_count(self) -> None:
        """Only the header block before the first blank line is read."""
        with self.assertRaisesRegex(pool.PoolError, "reports no scopes"):
            self.check('HTTP/2.0 200 OK\n\n{"x":"X-Oauth-Scopes: public_repo"}')

    def test_token_that_never_expires_is_refused(self) -> None:
        """A classic token may be made without expiry; this one may not."""
        with self.assertRaisesRegex(
            pool.PoolError, "never expires; create it with an expiry"
        ):
            self.check(reply(expiry=""))

    def test_unparsable_expiry_is_refused(self) -> None:
        """An expiry the job cannot read fails, as a missing scope header does."""
        for value in ("soon", "2027-13-40 10:39:33 UTC", "2027-01-18"):
            header = f"GitHub-Authentication-Token-Expiration: {value}"
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(pool.PoolError, "expiry .* cannot be read"),
            ):
                self.check(reply(expiry=header))

    def test_expiry_beyond_ninety_days_is_refused(self) -> None:
        """A token must be rotated at least every 90 days."""
        header = expiring(91)
        date = header.partition(": ")[2]
        with self.assertRaisesRegex(pool.PoolError, f"expires {date}.*90 days"):
            self.check(reply(expiry=header))

    def test_expiry_within_fourteen_days_warns_and_passes(self) -> None:
        """A rotation falling due is announced on every run, without failing."""
        header = expiring(13)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.check(reply(expiry=header)), ACCOUNT)
        self.assertIn("::warning", output.getvalue())
        self.assertIn(header.partition(": ")[2], output.getvalue())

    def test_expiry_in_between_passes_silently(self) -> None:
        """A token with weeks to run neither warns nor fails."""
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.check(reply(expiry=expiring(60))), ACCOUNT)
        self.assertEqual(output.getvalue(), "")

    def test_unreadable_token_is_refused(self) -> None:
        """A rejected token fails before any fork is touched."""
        with (
            patch.object(
                github, "run_gh", side_effect=github.GitHubError("Bad credentials")
            ),
            self.assertRaisesRegex(pool.PoolError, "could not read"),
        ):
            pool.check_token()


class PrepareTest(NoNetworkCase):
    """``start`` finds, renames or requests the fork; ``finish`` waits for it."""

    def test_existing_fork_is_used(self) -> None:
        """A repository that is a fork of the target needs nothing, not even a permission read."""
        with (
            patch.object(github, "api_object", return_value=repo_object()) as read,
            patch.object(github, "api_list") as listing,
            patch.object(github, "api_write") as write,
        ):
            self.assertEqual(prepare(TARGET_ENTRY), "present")
        read.assert_called_once_with(f"repos/{FORK}")
        listing.assert_not_called()
        write.assert_not_called()

    def test_case_differences_in_the_parent_are_tolerated(self) -> None:
        """GitHub reports owner names in their own case; the match folds it."""
        parent = {"full_name": "Owner/Repo"}
        with patch.object(
            github, "api_object", return_value=repo_object(parent=parent)
        ):
            self.assertEqual(prepare(TARGET_ENTRY), "present")

    def test_unrelated_repository_is_refused(self) -> None:
        """A same-named repository that is not the target's fork is an error."""
        for data in (
            repo_object(fork=False, parent=None),
            repo_object(parent={"full_name": "someone/else"}),
            repo_object(parent=None),
        ):
            with (
                self.subTest(data=data),
                patch.object(github, "api_object", return_value=data),
                patch.object(github, "api_write") as write,
                self.assertRaisesRegex(policy.PublishError, "not a fork of"),
            ):
                prepare(TARGET_ENTRY)
            write.assert_not_called()

    def test_read_failure_is_operational(self) -> None:
        """A 500 on the fork read never leads to a fork request."""
        with (
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 500)")
            ),
            patch.object(github, "api_write") as write,
            self.assertRaisesRegex(policy.PublishError, "could not read fork"),
        ):
            prepare(TARGET_ENTRY)
        write.assert_not_called()

    def test_missing_fork_is_created_then_awaited(self) -> None:
        """A 404 with no listed fork creates one and polls until it appears."""
        with (
            patch.object(
                github,
                "api_object",
                side_effect=[absent(), absent(), repo_object()],
            ) as read,
            patch.object(github, "api_list", return_value=[]) as listing,
            patch.object(github, "api_write", return_value=repo_object()) as write,
            patch.object(pool.time, "sleep") as sleep,
            patch.object(pool.time, "monotonic", side_effect=[0.0, 1.0, 2.0]),
        ):
            self.assertEqual(prepare(TARGET_ENTRY), "created")
        listing.assert_called_once_with(f"repos/{TARGET}/forks?sort=newest")
        write.assert_called_once_with(
            "POST",
            f"repos/{TARGET}/forks",
            {"organization": FORK_ORG, "default_branch_only": True},
        )
        self.assertEqual(read.call_count, 3)
        sleep.assert_called_once_with(pool.FORK_POLL_SECONDS)

    def test_create_reply_under_an_old_name_is_renamed(self) -> None:
        """The create reply, not the target's name, says where the fork is."""
        old = f"{FORK_ORG}/old-repo"
        renamed = repo_object(full_name=FORK)
        with (
            patch.object(github, "api_object", side_effect=[absent(), renamed]) as read,
            patch.object(github, "api_list", return_value=[]),
            patch.object(
                github,
                "api_write",
                side_effect=[repo_object(full_name=old), renamed],
            ) as write,
            patch.object(pool.time, "sleep"),
            patch.object(pool.time, "monotonic", return_value=0.0),
        ):
            self.assertEqual(prepare(TARGET_ENTRY), f"created as {old}, then renamed")
        write.assert_called_with("PATCH", f"repos/{old}", {"name": "repo"})
        # The account's own new fork needs no permission read before its rename.
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args_list[-1].args, (f"repos/{FORK}",))

    def test_create_reply_outside_the_pool_is_refused(self) -> None:
        """A create reply naming no fork in the pool is never trusted."""
        for reply in ({}, repo_object(full_name="someone/repo")):
            with (
                self.subTest(reply=reply),
                patch.object(github, "api_object", side_effect=absent()),
                patch.object(github, "api_list", return_value=[]),
                patch.object(github, "api_write", return_value=reply),
                patch.object(pool.time, "sleep") as sleep,
                patch.object(pool.time, "monotonic", side_effect=past_deadline()),
                self.assertRaisesRegex(policy.PublishError, "named no fork"),
            ):
                prepare(TARGET_ENTRY)
            sleep.assert_not_called()

    def test_fork_under_an_old_name_is_renamed(self) -> None:
        """A fork kept from before a target rename, which the account administers, is renamed."""
        old = OLD
        listed = [
            {"full_name": "someone/repo", "owner": {"login": "someone"}},
            {"full_name": old, "owner": {"login": FORK_ORG.upper()}},
        ]
        admin = repo_object(full_name=old, permissions={"admin": True})
        with (
            patch.object(
                github, "api_object", side_effect=[absent(), admin, repo_object()]
            ) as read,
            patch.object(github, "api_list", return_value=listed),
            patch.object(github, "api_write", return_value={}) as write,
            patch.object(pool.time, "sleep"),
            patch.object(pool.time, "monotonic", return_value=0.0),
        ):
            self.assertEqual(prepare(TARGET_ENTRY), f"renamed from {old}")
        write.assert_called_once_with("PATCH", f"repos/{old}", {"name": "repo"})
        self.assertEqual(read.call_args_list[1].args, (f"repos/{old}",))
        self.assertEqual(read.call_args_list[-1].args, (f"repos/{FORK}",))

    def test_renamed_fork_still_absent_at_the_deadline_fails(self) -> None:
        """A rename with no fork request behind it never warns as not yet visible."""
        listed = [{"full_name": OLD, "owner": {"login": FORK_ORG}}]
        admin = repo_object(full_name=OLD, permissions={"admin": True})
        with (
            patch.object(github, "api_object", side_effect=[absent(), admin, absent()]),
            patch.object(github, "api_list", return_value=listed),
            patch.object(github, "api_write", return_value={}) as write,
            patch.object(pool.time, "sleep"),
            patch.object(pool.time, "monotonic", side_effect=past_deadline()),
            self.assertRaisesRegex(
                policy.PublishError, f"did not appear .* renamed from {OLD}"
            ) as raised,
        ):
            prepare(TARGET_ENTRY)
        self.assertNotIsInstance(raised.exception, pool.NotYetVisible)
        write.assert_called_once_with("PATCH", f"repos/{OLD}", {"name": "repo"})

    def test_listed_fork_without_admin_is_not_renamed(self) -> None:
        """A pool fork the account cannot administer fails, naming the right it lacks."""
        listed = [{"full_name": OLD, "owner": {"login": FORK_ORG}}]
        reason = (
            f"{ACCOUNT} cannot rename {OLD}: it needs admin on the fork, "
            f"or owner of {FORK_ORG}"
        )
        for permissions in ({"admin": False, "push": True}, None, "admin"):
            data = repo_object(full_name=OLD, permissions=permissions)
            with (
                self.subTest(permissions=permissions),
                patch.object(github, "api_object", side_effect=[absent(), data]),
                patch.object(github, "api_list", return_value=listed),
                patch.object(github, "api_write") as write,
                self.assertRaises(policy.PublishError) as raised,
            ):
                prepare(TARGET_ENTRY)
            self.assertEqual(str(raised.exception), reason)
            write.assert_not_called()

    def test_unreadable_listed_fork_is_not_renamed(self) -> None:
        """A failed permission read makes no write."""
        listed = [{"full_name": OLD, "owner": {"login": FORK_ORG}}]
        with (
            patch.object(
                github,
                "api_object",
                side_effect=[absent(), github.GitHubError("x (HTTP 500)")],
            ),
            patch.object(github, "api_list", return_value=listed),
            patch.object(github, "api_write") as write,
            self.assertRaisesRegex(policy.PublishError, f"could not read fork {OLD}"),
        ):
            prepare(TARGET_ENTRY)
        write.assert_not_called()

    def test_rename_refusal_is_operational(self) -> None:
        """A 422 because the target's name is taken fails this target."""
        listed = [{"full_name": OLD, "owner": {"login": FORK_ORG}}]
        admin = repo_object(full_name=OLD, permissions={"admin": True})
        with (
            patch.object(github, "api_object", side_effect=[absent(), admin]),
            patch.object(github, "api_list", return_value=listed),
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Validation Failed (HTTP 422)"),
            ),
            patch.object(pool.time, "sleep") as sleep,
            self.assertRaisesRegex(policy.PublishError, "could not rename fork"),
        ):
            prepare(TARGET_ENTRY)
        sleep.assert_not_called()

    def test_fork_list_failure_is_operational(self) -> None:
        """A failed fork listing never leads to a fork request."""
        with (
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(
                github, "api_list", side_effect=github.GitHubError("x (HTTP 500)")
            ),
            patch.object(github, "api_write") as write,
            self.assertRaisesRegex(policy.PublishError, "could not list forks"),
        ):
            prepare(TARGET_ENTRY)
        write.assert_not_called()

    def test_fork_request_failure_is_operational(self) -> None:
        """A refused fork request is reported without polling."""
        with (
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(github, "api_list", return_value=[]),
            patch.object(
                github,
                "api_write",
                side_effect=github.GitHubError("gh: Forbidden (HTTP 403)"),
            ),
            patch.object(pool.time, "sleep") as sleep,
            self.assertRaisesRegex(policy.PublishError, "could not fork"),
        ):
            prepare(TARGET_ENTRY)
        sleep.assert_not_called()

    def test_poll_timeout_is_not_yet_visible(self) -> None:
        """A requested fork still absent at the deadline is told apart from a failure."""
        with (
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_write", return_value=repo_object()),
            patch.object(pool.time, "sleep"),
            patch.object(
                pool.time,
                "monotonic",
                side_effect=[0.0, 1.0, pool.FORK_WAIT_SECONDS + 1.0],
            ),
            self.assertRaisesRegex(pool.NotYetVisible, "not yet visible"),
        ):
            prepare(TARGET_ENTRY)

    def test_poll_read_failure_is_operational(self) -> None:
        """A 500 or a 410 while polling is a failure, not a fork still being made."""
        for status in (500, 410):
            failure = github.GitHubError(f"x (HTTP {status})")
            with (
                self.subTest(status=status),
                patch.object(github, "api_object", side_effect=[absent(), failure]),
                patch.object(github, "api_list", return_value=[]),
                patch.object(github, "api_write", return_value=repo_object()),
                patch.object(pool.time, "sleep"),
                patch.object(pool.time, "monotonic", return_value=0.0),
                self.assertRaisesRegex(policy.PublishError, "did not appear"),
            ):
                prepare(TARGET_ENTRY)

    def test_created_repository_must_still_be_a_fork(self) -> None:
        """What appears after the request is checked like a found one."""
        with (
            patch.object(
                github, "api_object", side_effect=[absent(), repo_object(fork=False)]
            ),
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_write", return_value=repo_object()),
            patch.object(pool.time, "sleep"),
            patch.object(pool.time, "monotonic", return_value=0.0),
            self.assertRaisesRegex(policy.PublishError, "not a fork of"),
        ):
            prepare(TARGET_ENTRY)


class MainTest(NoNetworkCase):
    """The command: every target attempted, the token checked once, exit codes."""

    def setUp(self) -> None:
        """A config file on disk and a summary file to append to."""
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        self.config = root / "fork-orgs.json"
        self.config.write_text(json.dumps(CONFIG), encoding="utf-8")
        self.summary = root / "summary.md"

    def run_main(self, env: dict[str, str]) -> int:
        """Run ``prepare`` under ``env`` and return its exit status."""
        argv = [
            "prepare",
            "--config",
            str(self.config),
            "--org",
            ORG,
            "--summary",
            str(self.summary),
        ]
        with (
            patch.dict(pool.os.environ, env, clear=True),
            self.assertRaises(SystemExit) as exited,
        ):
            pool.main(argv)
        return int(exited.exception.code or 0)

    def test_no_token_warns_and_writes_nothing(self) -> None:
        """Without the secret nothing is read or written; publish reports gaps."""
        with patch.object(github, "run_gh") as run:
            self.assertEqual(self.run_main({"MATRIX": matrix(entry())}), 0)
        run.assert_not_called()
        self.assertFalse(self.summary.exists())

    def test_ready_forks_pass_and_are_summarised(self) -> None:
        """The token is checked once, then each fork is reported."""
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry(), entry(key="repo-9"))}
        with (
            patch.object(github, "run_gh", return_value=SCOPED) as run,
            patch.object(github, "api_object", return_value=repo_object()) as read,
        ):
            self.assertEqual(self.run_main(env), 0)
        run.assert_called_once_with(["api", "--include", "user"])
        read.assert_called_once_with(f"repos/{FORK}")
        self.assertIn(f"| `{FORK}` | present |", self.summary.read_text())

    def test_one_failure_fails_the_job_after_every_target(self) -> None:
        """A fork that cannot be made fails the job; the others are still tried."""
        other = entry(
            key="other-1",
            repository=f"{ORG}/other",
            repo_name="other",
            fork_repository=f"{FORK_ORG}/other",
        )
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry(), other)}
        ready = repo_object(
            full_name=f"{FORK_ORG}/other", parent={"full_name": f"{ORG}/other"}
        )
        with (
            patch.object(github, "run_gh", return_value=SCOPED),
            patch.object(
                github, "api_object", side_effect=[repo_object(fork=False), ready]
            ) as read,
        ):
            self.assertEqual(self.run_main(env), 1)
        self.assertEqual(read.call_count, 2)
        summary = self.summary.read_text()
        self.assertIn(f"| `{FORK}` | failed: ", summary)
        self.assertIn(f"| `{FORK_ORG}/other` | present |", summary)

    def test_a_fork_not_yet_visible_warns_and_passes(self) -> None:
        """GitHub can take over a minute to fork; the publish job reads it later."""
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry())}
        out = io.StringIO()
        with (
            patch.object(github, "run_gh", return_value=SCOPED),
            patch.object(github, "api_object", side_effect=absent()),
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_write", return_value=repo_object()),
            patch.object(pool.time, "sleep"),
            patch.object(
                pool.time, "monotonic", side_effect=[0.0, pool.FORK_WAIT_SECONDS + 1.0]
            ),
            redirect_stdout(out),
        ):
            self.assertEqual(self.run_main(env), 0)
        self.assertIn(f"::warning title=Fork not yet visible::{FORK}", out.getvalue())
        self.assertNotIn("::error", out.getvalue())
        self.assertIn(
            f"| `{FORK}` | requested; not yet visible |", self.summary.read_text()
        )

    def test_a_refused_or_wrong_fork_still_fails_the_job(self) -> None:
        """Only a slow fork is spared: a refusal or a non-fork still exits 1."""
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry())}
        refused = github.GitHubError("gh: Forbidden (HTTP 403)")
        cases = {
            "refused": ([absent()], refused),
            "not a fork": ([absent(), repo_object(fork=False)], repo_object()),
        }
        for name, (reads, created) in cases.items():
            self.summary.unlink(missing_ok=True)
            with (
                self.subTest(name),
                patch.object(github, "run_gh", return_value=SCOPED),
                patch.object(github, "api_object", side_effect=reads),
                patch.object(github, "api_list", return_value=[]),
                patch.object(github, "api_write", side_effect=[created]),
                patch.object(pool.time, "sleep"),
                patch.object(pool.time, "monotonic", return_value=0.0),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(self.run_main(env), 1)
            self.assertIn(f"| `{FORK}` | failed: ", self.summary.read_text())

    def test_a_wrong_token_stops_before_any_fork(self) -> None:
        """A token with broader scopes exits 1 before reading a fork."""
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry())}
        broad = reply(scopes="repo", body="{}")
        with (
            patch.object(github, "run_gh", return_value=broad),
            patch.object(github, "api_object") as read,
        ):
            self.assertEqual(self.run_main(env), 1)
        read.assert_not_called()

    def test_a_bad_expiry_stops_before_any_fork(self) -> None:
        """No expiry, an unreadable one or a distant one exits 1 before reading."""
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry())}
        for expiry in (
            "",
            "GitHub-Authentication-Token-Expiration: soon",
            expiring(91),
        ):
            with (
                self.subTest(expiry=expiry),
                patch.object(github, "run_gh", return_value=reply(expiry=expiry)),
                patch.object(github, "api_object") as read,
            ):
                self.assertEqual(self.run_main(env), 1)
            read.assert_not_called()

    def test_an_empty_selection_needs_no_token_check(self) -> None:
        """Nothing selected means no API call at all."""
        with patch.object(github, "run_gh") as run:
            self.assertEqual(self.run_main({"GH_TOKEN": "t", "MATRIX": matrix()}), 0)
        run.assert_not_called()

    def test_every_fork_is_requested_before_any_wait(self) -> None:
        """Forking is asynchronous, so the waits overlap instead of adding up.

        One deadline covers every new fork: a matrix of hundreds of
        targets must not spend a poll interval on each in turn.
        """
        other = entry(
            key="other-1",
            repository=f"{ORG}/other",
            repo_name="other",
            fork_repository=f"{FORK_ORG}/other",
        )
        made = {
            FORK: repo_object(),
            f"{FORK_ORG}/other": repo_object(
                full_name=f"{FORK_ORG}/other", parent={"full_name": f"{ORG}/other"}
            ),
        }
        events: list[str] = []
        requested: set[str] = set()

        def read(endpoint: str) -> dict[str, Any]:
            name = endpoint.removeprefix("repos/")
            events.append(f"read {name}")
            if name not in requested:
                raise absent()
            return made[name]

        def write(method: str, endpoint: str, payload: Any) -> dict[str, Any]:
            target = endpoint.removeprefix("repos/").removesuffix("/forks")
            name = f"{FORK_ORG}/{target.partition('/')[2]}"
            requested.add(name)
            events.append(f"{method} {name}")
            return made[name]

        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry(), other)}
        with (
            patch.object(github, "run_gh", return_value=SCOPED),
            patch.object(github, "api_object", side_effect=read),
            patch.object(github, "api_list", return_value=[]),
            patch.object(github, "api_write", side_effect=write),
            patch.object(pool.time, "monotonic", return_value=0.0) as clock,
            patch.object(pool.time, "sleep") as sleep,
        ):
            self.assertEqual(self.run_main(env), 0)
        self.assertEqual(
            events,
            [
                f"read {FORK}",
                f"POST {FORK}",
                f"read {FORK_ORG}/other",
                f"POST {FORK_ORG}/other",
                f"read {FORK}",
                f"read {FORK_ORG}/other",
            ],
        )
        sleep.assert_not_called()
        # One deadline for the whole wait, not one per fork.
        self.assertEqual(clock.call_count, 1)
        summary = self.summary.read_text()
        self.assertIn(f"| `{FORK}` | created |", summary)
        self.assertIn(f"| `{FORK_ORG}/other` | created |", summary)

    def test_summary_rows_survive_a_job_cut_short(self) -> None:
        """Each row is written as its outcome is final, not after the loop."""
        other = entry(
            key="other-1",
            repository=f"{ORG}/other",
            repo_name="other",
            fork_repository=f"{FORK_ORG}/other",
        )
        env = {"GH_TOKEN": "t", "MATRIX": matrix(entry(), other)}
        with (
            patch.object(github, "run_gh", return_value=SCOPED),
            patch.object(
                github,
                "api_object",
                side_effect=[repo_object(), KeyboardInterrupt()],
            ),
            patch.dict(pool.os.environ, env, clear=True),
            self.assertRaises(KeyboardInterrupt),
        ):
            pool.main(
                [
                    "prepare",
                    "--config",
                    str(self.config),
                    "--org",
                    ORG,
                    "--summary",
                    str(self.summary),
                ]
            )
        self.assertIn(f"| `{FORK}` | present |", self.summary.read_text())


if __name__ == "__main__":
    unittest.main()
