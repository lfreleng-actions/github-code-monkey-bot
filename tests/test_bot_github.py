# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The gh wrapper: pinned API version, bounded retries, strict replies."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
import unittest.mock
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

github = import_module("bot_github")


def completed(stdout: str = "", stderr: str = "", code: int = 0) -> Any:
    """A finished gh process."""
    return subprocess.CompletedProcess(["gh"], code, stdout=stdout, stderr=stderr)


class RunGhTest(unittest.TestCase):
    """``run_gh`` pins the REST version and retries reads alone."""

    def test_rest_calls_carry_the_api_version_header(self) -> None:
        """REST gets the pinned version; GraphQL has no such header."""
        with patch.object(
            github.subprocess, "run", return_value=completed("{}")
        ) as run:
            github.run_gh(["api", "repos/o/r"])
            github.run_gh(["api", "graphql", "--input", "-"], input="{}")
        rest, graphql = (call.args[0] for call in run.call_args_list)
        self.assertEqual(
            rest[-2:], ["--header", f"X-GitHub-Api-Version: {github.API_VERSION}"]
        )
        self.assertNotIn("--header", graphql)

    def test_read_retries_transient_failures(self) -> None:
        """A 502 then a timeout then success is one successful read."""
        replies = [
            completed(stderr="gh: Bad Gateway (HTTP 502)", code=1),
            subprocess.TimeoutExpired(["gh"], github.TIMEOUT_SECONDS),
            completed("ok"),
        ]
        with (
            patch.object(github.subprocess, "run", side_effect=replies),
            patch.object(github.time, "sleep") as sleep,
        ):
            self.assertEqual(github.run_gh(["api", "repos/o/r"]), "ok")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [github.RETRY_DELAY_SECONDS, github.RETRY_DELAY_SECONDS * 2],
        )

    def test_read_gives_up_after_the_attempt_budget(self) -> None:
        """Persistent 503s surface after READ_ATTEMPTS tries."""
        reply = completed(stderr="gh: Unavailable (HTTP 503)", code=1)
        with (
            patch.object(github.subprocess, "run", return_value=reply) as run,
            patch.object(github.time, "sleep"),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.run_gh(["api", "repos/o/r"])
        self.assertEqual(run.call_count, github.READ_ATTEMPTS)
        self.assertEqual(caught.exception.status, 503)

    def test_non_transient_read_failure_is_not_retried(self) -> None:
        """A 404 is an answer, not a blip."""
        reply = completed(stderr="gh: Not Found (HTTP 404)", code=1)
        with (
            patch.object(github.subprocess, "run", return_value=reply) as run,
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.run_gh(["api", "repos/o/r"])
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertTrue(github.is_absent(caught.exception))

    def test_read_with_a_status_ignores_timeout_wording(self) -> None:
        """A 422 read whose reply mentions a timeout is not retried."""
        reply = completed(
            stderr="gh: Validation Failed: query timed out (HTTP 422)", code=1
        )
        with (
            patch.object(github.subprocess, "run", return_value=reply) as run,
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.run_gh(["api", "repos/o/r"])
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(caught.exception.status, 422)

    def test_bare_server_error_is_retried(self) -> None:
        """A 502 without a JSON message is as transient as one with it."""
        replies = [completed(stderr="gh: HTTP 502", code=1), completed("ok")]
        with (
            patch.object(github.subprocess, "run", side_effect=replies) as run,
            patch.object(github.time, "sleep"),
        ):
            self.assertEqual(github.run_gh(["api", "repos/o/r"]), "ok")
        self.assertEqual(run.call_count, 2)

    def test_a_reply_cut_short_is_retried(self) -> None:
        """gh's wording for an unreadable error body carries no status."""
        replies = [
            completed(stderr="unexpected end of JSON input", code=1),
            completed("ok"),
        ]
        with (
            patch.object(github.subprocess, "run", side_effect=replies) as run,
            patch.object(github.time, "sleep"),
        ):
            self.assertEqual(github.run_gh(["api", "repos/o/r"]), "ok")
        self.assertEqual(run.call_count, 2)

    def test_writes_run_once(self) -> None:
        """A write that hits a 502 is not repeated: it may have landed."""
        reply = completed(stderr="gh: Bad Gateway (HTTP 502)", code=1)
        with (
            patch.object(github.subprocess, "run", return_value=reply) as run,
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError),
        ):
            github.run_gh(["api", "--method", "POST", "repos/o/r/x"], input="{}")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_graphql_is_a_write_unless_declared_a_read(self) -> None:
        """GraphQL runs once by default and retries only with ``read=True``."""
        reply = completed(stderr="gh: Bad Gateway (HTTP 502)", code=1)
        with (
            patch.object(github.subprocess, "run", return_value=reply) as run,
            patch.object(github.time, "sleep"),
        ):
            with self.assertRaises(github.GitHubError):
                github.run_gh(["api", "graphql"], input="{}")
            self.assertEqual(run.call_count, 1)
            with self.assertRaises(github.GitHubError):
                github.run_gh(["api", "graphql"], input="{}", read=True)
            self.assertEqual(run.call_count, 1 + github.READ_ATTEMPTS)

    def test_missing_gh_is_an_operational_failure(self) -> None:
        """An OSError launching gh becomes a GitHubError, not a crash."""
        with (
            patch.object(github.subprocess, "run", side_effect=FileNotFoundError("gh")),
            self.assertRaisesRegex(github.GitHubError, "could not run gh"),
        ):
            github.run_gh(["api", "repos/o/r"])


class WriteWithRecoveryTest(unittest.TestCase):
    """A write is retried only after a look shows it did not land."""

    BAD_GATEWAY = "gh: Bad Gateway (HTTP 502)"

    def fake(self, outcomes: list[Any]) -> tuple[Any, list[int]]:
        """A write that raises or returns each outcome in turn, counting calls."""
        calls: list[int] = []

        def write() -> Any:
            calls.append(1)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return write, calls

    def test_success_needs_no_look(self) -> None:
        """A write that answers is returned; landed is never consulted."""
        write, calls = self.fake([{"oid": "a"}])
        landed = unittest.mock.Mock(return_value=None)
        with patch.object(github.time, "sleep") as sleep:
            self.assertEqual(
                github.write_with_recovery(write, landed, repeat_safe=True),
                {"oid": "a"},
            )
        self.assertEqual(len(calls), 1)
        landed.assert_not_called()
        sleep.assert_not_called()

    def test_landed_write_is_not_repeated(self) -> None:
        """A 502 on a write that took effect returns the read-back result."""
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY)])
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            got = github.write_with_recovery(
                write, lambda: {"oid": "b"}, label="commit", repeat_safe=True
            )
        self.assertEqual(got, {"oid": "b"})
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        self.assertIn("commit: landed despite", err.getvalue())

    def test_write_that_did_not_land_is_retried(self) -> None:
        """Not landed: wait, write again, and return its answer."""
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY), "ok"])
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(
                github.write_with_recovery(write, lambda: None, repeat_safe=True), "ok"
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [github.WRITE_RETRY_DELAYS_SECONDS[0]],
        )
        self.assertIn("attempt 1 of 4 did not land", err.getvalue())

    def test_last_failure_is_checked_then_raised(self) -> None:
        """Every attempt fails and none landed: the last error surfaces."""
        failures = [
            github.GitHubError(f"gh: Unavailable {n} (HTTP 503)")
            for n in range(github.WRITE_ATTEMPTS)
        ]
        write, calls = self.fake(failures)
        landed = unittest.mock.Mock(return_value=None)
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=True)
        self.assertIs(caught.exception, failures[-1])
        self.assertEqual(len(calls), github.WRITE_ATTEMPTS)
        self.assertEqual(landed.call_count, github.WRITE_ATTEMPTS)
        self.assertEqual(
            tuple(call.args[0] for call in sleep.call_args_list),
            github.WRITE_RETRY_DELAYS_SECONDS,
        )

    def test_last_attempt_that_landed_is_returned(self) -> None:
        """The final failure may have landed too, so it is looked at."""
        failures = [github.GitHubError(self.BAD_GATEWAY)] * github.WRITE_ATTEMPTS
        write, _ = self.fake(failures)
        looks = iter([None] * (github.WRITE_ATTEMPTS - 1) + ["landed"])
        with (
            patch.object(github.time, "sleep"),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(
                github.write_with_recovery(
                    write, lambda: next(looks), repeat_safe=True
                ),
                "landed",
            )

    def test_only_none_means_not_landed(self) -> None:
        """An empty read-back result is a result, not a miss."""

        def empty() -> dict[str, Any]:
            return {}

        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY)])
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(
                github.write_with_recovery(write, empty, repeat_safe=True), {}
            )
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_failed_look_is_never_read_as_not_landed(self) -> None:
        """When landed cannot tell, the write fails with both errors."""
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY)])

        def landed() -> Any:
            raise github.GitHubError("gh: Server Error (HTTP 500)")

        with (
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, label="review", repeat_safe=True)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        message = str(caught.exception)
        self.assertIn("could not tell whether the review landed", message)
        self.assertIn("HTTP 502", message)
        self.assertIn("HTTP 500", message)
        self.assertEqual(caught.exception.status, 502)

    def test_timeout_keeps_no_status_when_the_look_fails(self) -> None:
        """A look's 404 must not make a timed-out write read as absent."""
        timeout = github.GitHubError(
            f"gh timed out after {github.TIMEOUT_SECONDS} seconds"
        )
        write, _ = self.fake([timeout])

        def landed() -> Any:
            raise github.GitHubError("gh: Not Found (HTTP 404)")

        with (
            patch.object(github.time, "sleep"),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=True)
        self.assertIsNone(caught.exception.status)
        self.assertFalse(github.is_absent(caught.exception))
        self.assertIn("HTTP 404", str(caught.exception))

    def test_non_transient_failure_raises_without_a_look(self) -> None:
        """A 422 is an answer: no look, no retry."""
        failure = github.GitHubError("gh: Validation Failed (HTTP 422)")
        write, calls = self.fake([failure])
        landed = unittest.mock.Mock(return_value="x")
        with (
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=True)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 1)
        landed.assert_not_called()
        sleep.assert_not_called()

    def test_rejected_retry_after_a_late_landing_is_recovered(self) -> None:
        """A timed-out first copy can land during the backoff.

        A compare-and-swap retry is then refused, since the head moved
        to the first copy's commit. That refusal is looked at, and the
        write that landed is returned; nothing is sent a third time.
        """
        refused = github.GitHubError("gh: Expected head mismatch (HTTP 422)")
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY), refused])
        looks = iter([None, {"oid": "late"}])
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            got = github.write_with_recovery(
                write, lambda: next(looks), label="commit", repeat_safe=True
            )
        self.assertEqual(got, {"oid": "late"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("commit: landed despite", err.getvalue())
        self.assertIn("HTTP 422", err.getvalue())

    def test_rejected_retry_that_did_not_land_is_not_sent_again(self) -> None:
        """A refused retry with nothing landed raises the refusal at once."""
        refused = github.GitHubError("gh: Expected head mismatch (HTTP 422)")
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY), refused])
        landed = unittest.mock.Mock(return_value=None)
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=True)
        self.assertIs(caught.exception, refused)
        self.assertEqual(len(calls), 2)
        self.assertEqual(landed.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_rejected_retry_whose_look_fails_keeps_both_errors(self) -> None:
        """A refused retry that cannot be checked fails with both errors."""
        refused = github.GitHubError("gh: Expected head mismatch (HTTP 422)")
        write, _ = self.fake([github.GitHubError(self.BAD_GATEWAY), refused])
        looks = iter([None])

        def landed() -> Any:
            for outcome in looks:
                return outcome
            raise github.GitHubError("gh: Server Error (HTTP 500)")

        with (
            patch.object(github.time, "sleep"),
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=True)
        message = str(caught.exception)
        self.assertIn("HTTP 422", message)
        self.assertIn("HTTP 500", message)
        self.assertEqual(caught.exception.status, 422)

    def test_status_outranks_timeout_wording(self) -> None:
        """A 422 whose detail says "timed out" is rejected, not retried."""
        rejected = github.GitHubError(
            "gh: Validation Failed: the check timed out (HTTP 422)"
        )
        write, calls = self.fake([rejected])
        landed = unittest.mock.Mock(return_value="x")
        with (
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=True)
        self.assertIs(caught.exception, rejected)
        self.assertEqual(len(calls), 1)
        landed.assert_not_called()
        sleep.assert_not_called()

    def test_timeout_counts_as_transient(self) -> None:
        """A gh timeout is as ambiguous as a 5xx."""
        timeout = github.GitHubError(
            f"gh timed out after {github.TIMEOUT_SECONDS} seconds"
        )
        write, calls = self.fake([timeout, "ok"])
        with (
            patch.object(github.time, "sleep"),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(
                github.write_with_recovery(write, lambda: None, repeat_safe=True), "ok"
            )
        self.assertEqual(len(calls), 2)

    def test_a_reply_cut_short_is_looked_at(self) -> None:
        """An error body gh could not read may hide a write that landed."""
        cut = github.GitHubError("unexpected end of JSON input")
        write, calls = self.fake([cut])
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(
                github.write_with_recovery(write, lambda: "x", repeat_safe=False), "x"
            )
        self.assertEqual(len(calls), 1)

    def test_graphql_internal_error_is_looked_at_and_retried(self) -> None:
        """GitHub's "Something went wrong" is a 5xx in all but its status."""
        failed = github.GitHubError(
            'GraphQL errors: [{"message": "Something went wrong while executing'
            ' your query. Please include `D443:353A81` when reporting this issue."}]'
        )
        write, calls = self.fake([failed, "ok"])
        with (
            patch.object(github.time, "sleep"),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(
                github.write_with_recovery(write, lambda: None, repeat_safe=True), "ok"
            )
        self.assertEqual(len(calls), 2)

    def test_attempts_outside_the_budget_are_refused(self) -> None:
        """Zero tries, or more than the delays allow, is a caller error."""
        for attempts in (0, github.WRITE_ATTEMPTS + 1):
            with self.assertRaises(ValueError):
                github.write_with_recovery(
                    lambda: "x", lambda: None, attempts=attempts, repeat_safe=True
                )

    def test_fewer_attempts_shorten_the_retries(self) -> None:
        """attempts=1 checks once and does not write again."""
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY)])
        with (
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError),
        ):
            github.write_with_recovery(
                write, lambda: None, attempts=1, repeat_safe=True
            )
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_repeat_safe_is_required(self) -> None:
        """Every caller states whether its write may be sent again."""
        call: Any = github.write_with_recovery
        with self.assertRaises(TypeError):
            call(lambda: "x", lambda: None)

    def test_marker_only_write_is_looked_at_but_not_repeated(self) -> None:
        """Not repeat-safe: a 502 that landed is recovered by the look."""
        write, calls = self.fake([github.GitHubError(self.BAD_GATEWAY)])
        with (
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            got = github.write_with_recovery(write, lambda: "review", repeat_safe=False)
        self.assertEqual(got, "review")
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_marker_only_write_that_did_not_land_is_not_sent_again(self) -> None:
        """Not repeat-safe and not found: fail, since it may land late."""
        failure = github.GitHubError(
            f"gh timed out after {github.TIMEOUT_SECONDS} seconds"
        )
        write, calls = self.fake([failure, "never"])
        landed = unittest.mock.Mock(return_value=None)
        with (
            patch.object(github.time, "sleep") as sleep,
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.write_with_recovery(write, landed, repeat_safe=False)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 1)
        landed.assert_called_once()
        sleep.assert_not_called()

    def test_marker_only_write_refuses_more_attempts(self) -> None:
        """Asking to repeat a write that is not repeat-safe is a caller error."""
        with self.assertRaises(ValueError):
            github.write_with_recovery(
                lambda: "x", lambda: None, repeat_safe=False, attempts=2
            )

    def test_log_cannot_carry_a_workflow_command(self) -> None:
        """GitHub's error text is escaped before it reaches the log."""
        write, _ = self.fake(
            [github.GitHubError("::error::forged\ngh: Bad Gateway (HTTP 502)")]
        )
        with (
            patch.object(github.time, "sleep"),
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            github.write_with_recovery(write, lambda: "ok", repeat_safe=True)
        self.assertNotIn("::error::", err.getvalue())

    def test_api_write_is_still_sent_once_per_attempt(self) -> None:
        """Through the real wrapper: run_gh adds no retries of its own."""
        replies = [completed(stderr=self.BAD_GATEWAY, code=1), completed('{"id": 7}')]
        with (
            patch.object(github.subprocess, "run", side_effect=replies) as run,
            patch.object(github.time, "sleep") as sleep,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            got = github.write_with_recovery(
                lambda: github.api_write("POST", "repos/o/r/git/refs", {"ref": "x"}),
                lambda: None,
                repeat_safe=True,
            )
        self.assertEqual(got, {"id": 7})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [github.WRITE_RETRY_DELAYS_SECONDS[0]],
        )


class TransientWordingTest(unittest.TestCase):
    """``is_transient`` on a failure that carries no HTTP status."""

    # What gh prints, with no status, when a reply or the connection
    # under it fails: an error body it could not read, a connection
    # the server closed, a transport timeout, and GitHub's own
    # internal error on GraphQL, which arrives as a 200.
    CUT_SHORT = (
        "unexpected end of JSON input",
        'Post "https://api.github.com/graphql": EOF',
        "unexpected EOF",
        "read tcp 10.1.0.4:53112->140.82.112.6:443: read: connection reset by peer",
        "dial tcp 140.82.112.6:443: i/o timeout",
        "net/http: TLS handshake timeout",
        "net/http: request canceled (Client.Timeout exceeded while awaiting headers)",
        "context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
        'GraphQL errors: [{"message": "Something went wrong while executing your'
        ' query. Please include `D443:353A81` when reporting this issue."}]',
    )

    def test_each_wording_without_a_status_is_transient(self) -> None:
        """A reply gh could not read is as ambiguous as a 5xx."""
        for message in self.CUT_SHORT:
            with self.subTest(message=message):
                failure = github.GitHubError(message)
                self.assertIsNone(failure.status)
                self.assertTrue(github.is_transient(failure))

    def test_a_status_beside_the_wording_decides_alone(self) -> None:
        """A 422 whose reply ends mid-sentence is still a 422."""
        for message in (
            "gh: unexpected end of JSON input (HTTP 422)",
            "gh: EOF (HTTP 404)",
            "gh: connection reset by peer (HTTP 403)",
        ):
            with self.subTest(message=message):
                self.assertFalse(github.is_transient(github.GitHubError(message)))

    def test_a_wrong_or_closed_endpoint_stays_fatal(self) -> None:
        """A host nobody resolves, or a port nobody listens on, repeats."""
        for message in (
            "dial tcp: lookup api.github.invalid: no such host",
            "dial tcp 127.0.0.1:443: connect: connection refused",
            "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN"
            " environment variable.",
            "could not run gh: [Errno 2] No such file or directory: 'gh'",
        ):
            with self.subTest(message=message):
                self.assertFalse(github.is_transient(github.GitHubError(message)))

    def test_a_wording_is_matched_as_whole_words(self) -> None:
        """Letters on either side make another word, not a match."""
        for message in (
            "gh: the node THEREOF is not valid",
            "gh: unexpected end of JSON inputs",
        ):
            with self.subTest(message=message):
                self.assertFalse(github.is_transient(github.GitHubError(message)))


class GitHubErrorTest(unittest.TestCase):
    """``GitHubError`` reads the status gh prints."""

    def test_status_parsed_from_message(self) -> None:
        """The three digits in ``(HTTP nnn)`` become the status."""
        self.assertEqual(github.GitHubError("gh: Not Found (HTTP 404)").status, 404)
        self.assertIsNone(github.GitHubError("gh timed out").status)
        self.assertIsNone(github.GitHubError("(HTTP 40)").status)

    def test_bare_status_parsed(self) -> None:
        """A reply without a JSON message leaves gh printing ``gh: HTTP nnn``."""
        self.assertEqual(github.GitHubError("gh: HTTP 403").status, 403)
        self.assertEqual(github.GitHubError("gh: HTTP 404\ngh: hint").status, 404)
        self.assertIsNone(github.GitHubError("gh: HTTP 40").status)
        self.assertIsNone(github.GitHubError("see HTTP 404 in the docs").status)
        for hybrid in ("gh: Bad (HTTP 404", "gh: HTTP 404)"):
            with self.subTest(hybrid=hybrid):
                self.assertIsNone(github.GitHubError(hybrid).status)

    def test_only_the_status_gh_appends_counts(self) -> None:
        """A status quoted before the one gh appends is not the status."""
        self.assertIsNone(github.GitHubError("see (HTTP 502) in the docs").status)
        quoted = "gh: upstream said (HTTP 502)\nso this is final (HTTP 404)"
        self.assertEqual(github.GitHubError(quoted).status, 404)

    def test_explicit_status_wins(self) -> None:
        """A caller that knows the status passes it rather than a token."""
        failure = github.GitHubError("gh exit 1 (HTTP 503), then gave up", 503)
        self.assertEqual(failure.status, 503)


VALIDATION_REPLY = json.dumps(
    {
        "message": "Validation Failed",
        "errors": [
            {"resource": "PullRequest", "field": "head", "code": "invalid"},
            {"message": "No commits between main and topic"},
        ],
        "documentation_url": "https://docs.github.com/rest",
        "status": "422",
    }
)


class ErrorDetailTest(unittest.TestCase):
    """A failed call keeps the detail GitHub returned with it."""

    def test_validation_errors_reach_the_message(self) -> None:
        """The ``errors`` entries follow gh's line; the status survives."""
        reply = completed(
            stdout=VALIDATION_REPLY,
            stderr="gh: Validation Failed (HTTP 422)",
            code=1,
        )
        with (
            patch.object(github.subprocess, "run", return_value=reply),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.run_gh(["api", "--method", "POST", "repos/o/r/pulls"], input="{}")
        message = str(caught.exception)
        self.assertTrue(message.startswith("gh: Validation Failed (HTTP 422)"))
        self.assertIn("PullRequest.head invalid", message)
        self.assertIn("No commits between main and topic", message)
        self.assertEqual(caught.exception.status, 422)

    def test_detail_is_one_bounded_line(self) -> None:
        """Many or long entries collapse to one line within the cap."""
        noisy = json.dumps(
            {"errors": [{"message": "x\n" * 400}, *([{"code": "y"}] * 50)]}
        )
        detail = github.error_detail(noisy)
        self.assertNotIn("\n", detail)
        self.assertLessEqual(len(detail), github.MAX_ERROR_DETAIL)

    def test_reply_text_cannot_supply_the_status(self) -> None:
        """A status inside the reply never overrides the one gh printed."""
        forged = json.dumps({"errors": [{"message": "gone (HTTP 404)"}]})
        reply = completed(stdout=forged, stderr="gh: Bad Gateway (HTTP 502)", code=1)
        with (
            patch.object(github.subprocess, "run", return_value=reply),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.run_gh(["api", "--method", "POST", "repos/o/r/x"], input="{}")
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("gone", str(caught.exception))

    def test_replies_without_entries_add_nothing(self) -> None:
        """No ``errors`` array, or a body that is not JSON, adds no detail."""
        for body in ("", "<xml/>", '{"message": "Not Found"}', '{"errors": "x"}'):
            with self.subTest(body=body):
                self.assertEqual(github.error_detail(body), "")
        reply = completed(
            stdout='{"message": "Not Found"}', stderr="gh: Not Found (HTTP 404)", code=1
        )
        with (
            patch.object(github.subprocess, "run", return_value=reply),
            self.assertRaises(github.GitHubError) as caught,
        ):
            github.run_gh(["api", "repos/o/r"])
        self.assertEqual(str(caught.exception), "gh: Not Found (HTTP 404)")
        self.assertEqual(caught.exception.status, 404)


class RepliesTest(unittest.TestCase):
    """The typed readers refuse replies of the wrong shape."""

    def test_api_object_rejects_non_objects(self) -> None:
        """A list or scalar where an object was expected is a failure."""
        with (
            patch.object(github, "run_gh", return_value="[1]"),
            self.assertRaisesRegex(github.GitHubError, "expected an object"),
        ):
            github.api_object("repos/o/r")

    def test_api_list_flattens_pages_and_rejects_non_list_pages(self) -> None:
        """Slurped pages are concatenated; a non-array page is refused."""
        pages = json.dumps([[{"a": 1}], [{"b": 2}]])
        with patch.object(github, "run_gh", return_value=pages):
            self.assertEqual(github.api_list("repos/o/r/x"), [{"a": 1}, {"b": 2}])
        for raw in ('[{"not": "a page"}]', "[[1]]", "{}"):
            with (
                self.subTest(raw=raw),
                patch.object(github, "run_gh", return_value=raw),
                self.assertRaises(github.GitHubError),
            ):
                github.api_list("repos/o/r/x")

    def test_api_write_sends_json_and_tolerates_empty_reply(self) -> None:
        """The payload goes on stdin; a 204-style empty body is ``{}``."""
        with patch.object(github, "run_gh", return_value="") as run:
            self.assertEqual(github.api_write("PUT", "repos/o/r/x", {"k": 1}), {})
        self.assertEqual(run.call_args.kwargs["input"], '{"k": 1}')
        self.assertEqual(run.call_args.args[0][:3], ["api", "--method", "PUT"])

    def test_invalid_json_is_a_github_error(self) -> None:
        """Malformed bodies stay on the operational-failure path."""
        with (
            patch.object(github, "run_gh", return_value="{nope"),
            self.assertRaisesRegex(github.GitHubError, "invalid JSON"),
        ):
            github.api_object("repos/o/r")


class GraphqlTest(unittest.TestCase):
    """``graphql`` never hands back a partial payload."""

    def test_errors_entry_raises(self) -> None:
        """A reply carrying ``errors`` fails even with data alongside."""
        reply = json.dumps({"data": {"x": 1}, "errors": [{"message": "nope"}]})
        with (
            patch.object(github, "run_gh", return_value=reply),
            self.assertRaisesRegex(github.GitHubError, "GraphQL errors"),
        ):
            github.graphql("query {}", {})

    def test_missing_data_raises(self) -> None:
        """A reply without a data object is a failure."""
        for reply in ("{}", '{"data": null}', "[]"):
            with (
                self.subTest(reply=reply),
                patch.object(github, "run_gh", return_value=reply),
                self.assertRaises(github.GitHubError),
            ):
                github.graphql("query {}", {})

    def test_data_returned_and_read_flag_forwarded(self) -> None:
        """The data object comes back; ``read`` reaches ``run_gh``."""
        reply = json.dumps({"data": {"viewer": {"login": "x"}}})
        with patch.object(github, "run_gh", return_value=reply) as run:
            data = github.graphql("query {}", {"a": 1}, read=True)
        self.assertEqual(data, {"viewer": {"login": "x"}})
        self.assertIs(run.call_args.kwargs["read"], True)
        self.assertEqual(
            json.loads(run.call_args.kwargs["input"])["variables"], {"a": 1}
        )


class RequireTest(unittest.TestCase):
    """The ``require_*`` helpers fail the operation on a bad field."""

    def test_require_sha(self) -> None:
        """Only a lowercase 40-hex string is a commit SHA."""
        self.assertEqual(github.require_sha({"s": "a" * 40}, "s", "c"), "a" * 40)
        for value in ("A" * 40, "a" * 39, "", None, 7):
            with self.subTest(value=value), self.assertRaises(github.GitHubError):
                github.require_sha({"s": value}, "s", "c")

    def test_require_int_and_str(self) -> None:
        """Booleans are not integers; empty strings are not strings."""
        self.assertEqual(github.require_int({"n": 3}, "n", "c"), 3)
        for value in (0, -1, True, "3", None):
            with self.subTest(value=value), self.assertRaises(github.GitHubError):
                github.require_int({"n": value}, "n", "c")
        with self.assertRaisesRegex(github.GitHubError, "missing or invalid 'k'"):
            github.require_str({"k": ""}, "k", "c")


class SafeMessageTest(unittest.TestCase):
    """``safe_message`` renders an error for a log parsed for commands."""

    def test_escapes_commands_and_non_ascii(self) -> None:
        """Workflow command syntax is broken up; control bytes are escaped."""
        rendered = github.safe_message(ValueError("::warning::x ##[group]\n\u2028é"))
        self.assertNotIn("::", rendered)
        self.assertNotIn("##[", rendered)
        self.assertNotIn("\n", rendered)
        self.assertTrue(rendered.isascii())
        self.assertIn("\\n", rendered)


if __name__ == "__main__":
    unittest.main()
