# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The notice on a pull request whose antagonistic review request did not go (#228).

A fake GitHub stands in for the reads and the one write: it holds the
pull request and its comments, applies each comment the script posts,
and can lose the reply to one after applying it.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
notice = import_module("request_notice")
round_reads = import_module("round_reads")

REPOSITORY = "lfreleng-actions/sbom-action"
NUMBER = 41
SLUG = "lf-releng-code-monkey-bot"
BOT = f"{SLUG}[bot]"
HEAD = "a" * 40
BRANCH = "code-monkey/issue-7"
OLDER = "b" * 40
RUN_ID = "44741309912"
RUN_URL = f"https://github.com/lfreleng-actions/caller/actions/runs/{RUN_ID}"
REVIEWER = "lfreleng-actions/github-code-review-bot"
ENDPOINT = f"repos/{REPOSITORY}/issues/{NUMBER}/comments"
PAGE = re.compile(r"[?&]page=(\d+)")


def bot_user(login: str = BOT) -> dict[str, str]:
    """A user GitHub types as an App."""
    return {"login": login, "type": "Bot"}


class FakeGitHub:
    """The pull request, its comments, and every write the script sends."""

    def __init__(self, **pull: Any) -> None:
        """Open, by this App, ``HEAD`` on ``BRANCH``, unless ``pull`` says otherwise."""
        self.pull: dict[str, Any] = {
            "state": "open",
            "user": bot_user(),
            "head": {"sha": HEAD, "ref": BRANCH},
            "title": "Fix: Something <!-- injected -->",
            **pull,
        }
        self.comments: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.lose_reply = False

    def comment(self, body: str, user: dict[str, str] | None = None) -> None:
        """Add a comment someone already posted."""
        number = len(self.comments) + 1
        self.comments.append(
            {
                "id": number,
                "body": body,
                "user": user or bot_user(),
                "html_url": f"https://github.com/{REPOSITORY}/pull/{NUMBER}#c{number}",
            }
        )

    def api_object(self, endpoint: str) -> dict[str, Any]:
        """Answer the pull request read."""
        if endpoint != f"repos/{REPOSITORY}/pulls/{NUMBER}":
            raise AssertionError(f"unexpected read {endpoint}")
        return self.pull

    def api_page(self, endpoint: str) -> list[dict[str, Any]]:
        """Answer one page of the comment list."""
        if not endpoint.startswith(ENDPOINT + "?"):
            raise AssertionError(f"unexpected read {endpoint}")
        found = PAGE.search(endpoint)
        page = int(found.group(1)) if found else 1
        return self.comments[(page - 1) * 100 : page * 100]

    def api_write(
        self, method: str, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply the comment; lose the reply once when told to."""
        self.writes.append((method, endpoint, payload))
        self.comment(str(payload["body"]))
        if self.lose_reply:
            self.lose_reply = False
            raise github.GitHubError("gh: Bad Gateway (HTTP 502)")
        return {"html_url": self.comments[-1]["html_url"]}


class RequestNoticeTests(unittest.TestCase):
    """Posts once per head, on the App's own open pull request alone."""

    def run_notice(self, fake: FakeGitHub, *extra: str) -> tuple[int, str, str]:
        """Run main against ``fake``; return status, stdout and the summary."""
        with tempfile.TemporaryDirectory() as holder:
            summary = Path(holder) / "summary.md"
            out = io.StringIO()
            with (
                patch.object(github, "api_object", fake.api_object),
                patch.object(github, "api_write", fake.api_write),
                patch.object(reads, "api_page", fake.api_page),
                redirect_stdout(out),
                redirect_stderr(io.StringIO()),
            ):
                status = notice.main(
                    [
                        "--repository", REPOSITORY,
                        "--number", str(NUMBER),
                        "--kind", "failed",
                        "--head", HEAD,
                        "--bot-slug", SLUG,
                        "--run-id", RUN_ID,
                        "--run-url", RUN_URL,
                        "--review-repository", REVIEWER,
                        "--branches", json.dumps(["code-monkey/issue-9", BRANCH]),
                        "--summary", str(summary),
                        *extra,
                    ]
                )  # fmt: skip
            written = summary.read_text(encoding="utf-8") if summary.exists() else ""
        for method, endpoint, _ in fake.writes:
            self.assertEqual((method, endpoint), ("POST", ENDPOINT))
        return status, out.getvalue(), written

    def posted_body(self, head: str = HEAD, kind: str = "failed") -> str:
        """The ``kind`` notice for ``head`` as the script writes it."""
        return notice.notice_body(kind, head, RUN_ID, RUN_URL, REVIEWER)

    def test_posts_one_notice_on_the_apps_open_pull_request(self) -> None:
        """The fixed text, the short and full head, the run, nothing else."""
        fake = FakeGitHub()
        status, out, summary = self.run_notice(fake)
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.writes), 1)
        body = str(fake.writes[0][2]["body"])
        self.assertEqual(
            body,
            "🐒 Notice from the Code Monkey workflow: the request for an"
            f" antagonistic review of head `{HEAD[:12]}` did not reach the review"
            f" bot. Every dispatch failed in [this run]({RUN_URL}), retries"
            " included.\n\nTo ask again, dispatch `code-review-antagonistic.yaml`"
            f" in `{REVIEWER}` naming this pull request.\n\n"
            "<!-- code-monkey-notice antagonistic-request kind=failed"
            f" head={HEAD} run={RUN_ID} -->",
        )
        self.assertNotIn("injected", body)
        self.assertIsNone(round_reads.FOOTER.search(body))
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("posted a failed notice on"))
        self.assertIn("posted a failed notice on", summary)

    def test_an_uncertain_dispatch_says_it_may_not_have_gone(self) -> None:
        """The uncertain notice says what is known and how to ask again."""
        fake = FakeGitHub()
        status, out, _ = self.run_notice(fake, "--kind", "uncertain")
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(
            str(fake.writes[0][2]["body"]),
            "🐒 Notice from the Code Monkey workflow: the request for an"
            f" antagonistic review of head `{HEAD[:12]}` may not have reached the"
            f" review bot. Its dispatch in [this run]({RUN_URL}) ended in an"
            " error that does not say whether GitHub accepted it.\n\nIf no"
            " review arrives, dispatch `code-review-antagonistic.yaml` in"
            f" `{REVIEWER}` naming this pull request.\n\n"
            "<!-- code-monkey-notice antagonistic-request kind=uncertain"
            f" head={HEAD} run={RUN_ID} -->",
        )
        self.assertTrue(out.startswith("posted an uncertain notice on"))

    def test_one_notice_per_head_and_kind(self) -> None:
        """A notice of one kind for the head does not cover the other."""
        for posted, asked, writes in (
            ("failed", "uncertain", 1),
            ("uncertain", "failed", 1),
            ("uncertain", "uncertain", 0),
        ):
            with self.subTest(posted=posted, asked=asked):
                fake = FakeGitHub()
                fake.comment(self.posted_body(kind=posted))
                status, _, _ = self.run_notice(fake, "--kind", asked)
                self.assertEqual(status, 0)
                self.assertEqual(len(fake.writes), writes)

    def test_a_head_that_moved_after_the_request_gets_no_notice(self) -> None:
        """Another run published a newer head before the notice read it.

        The request was for the older head; a notice naming the newer
        one would say a review nobody asked for was not requested, and
        its marker would hide a later run's real notice for that head.
        """
        fake = FakeGitHub()
        status, out, summary = self.run_notice(fake, "--head", OLDER)
        self.assertEqual(status, 0)
        self.assertEqual(fake.writes, [])
        reason = f"its head moved from {OLDER[:12]} to {HEAD[:12]}"
        self.assertIn(reason, out)
        self.assertIn(reason, summary)
        status, _, _ = self.run_notice(fake)
        self.assertEqual(len(fake.writes), 1)

    def test_each_condition_alone_stops_the_write(self) -> None:
        """Closed, another author, an unselected branch or a notice: no write."""
        closed = FakeGitHub(state="closed")
        human = FakeGitHub(user={"login": BOT, "type": "User"})
        other = FakeGitHub(user=bot_user("other-app[bot]"))
        # Another App pull request in the same repository, which a
        # forged result could name in place of the selected one.
        unselected = FakeGitHub(head={"sha": HEAD, "ref": "code-monkey/issue-8"})
        no_branch = FakeGitHub(head={"sha": HEAD})
        done = FakeGitHub()
        done.comment(self.posted_body())
        for name, fake, reason in (
            ("closed", closed, "the pull request is not open"),
            ("user", human, "this App did not open the pull request"),
            ("other app", other, "this App did not open the pull request"),
            ("unselected", unselected, "branch is not one this run selected"),
            ("no branch", no_branch, "branch is not one this run selected"),
            ("noticed", done, "this App already posted a failed notice for head"),
        ):
            with self.subTest(name):
                status, out, summary = self.run_notice(fake)
                self.assertEqual(status, 0)
                self.assertEqual(fake.writes, [])
                self.assertTrue(out.startswith("not posted"))
                self.assertIn(reason, out)
                self.assertIn(reason, summary)

    def test_a_new_head_posts_again(self) -> None:
        """A notice for an earlier head does not cover the current one."""
        fake = FakeGitHub()
        fake.comment(self.posted_body(OLDER))
        status, out, _ = self.run_notice(fake)
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.writes), 1)
        self.assertIn(f"head={HEAD}", str(fake.writes[0][2]["body"]))
        self.assertTrue(out.startswith("posted a failed notice on"))

    def test_a_marker_from_anyone_else_suppresses_nothing(self) -> None:
        """A user, another App, or this App quoting it mid-comment: still posts."""
        for name, user, body in (
            ("user", {"login": BOT, "type": "User"}, self.posted_body()),
            ("other app", bot_user("other-app[bot]"), self.posted_body()),
            ("quoted", bot_user(), self.posted_body() + "\n\nand more"),
        ):
            with self.subTest(name):
                fake = FakeGitHub()
                fake.comment(body, user)
                status, _, _ = self.run_notice(fake)
                self.assertEqual(status, 0)
                self.assertEqual(len(fake.writes), 1)

    def test_a_lost_reply_whose_notice_reads_back_is_posted_once(self) -> None:
        """A transient failure after the comment landed is never sent again."""
        fake = FakeGitHub()
        fake.lose_reply = True
        status, out, _ = self.run_notice(fake)
        self.assertEqual(status, 0)
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(len(fake.comments), 1)
        self.assertTrue(out.startswith("posted a failed notice on"))
        self.assertIn(str(fake.comments[0]["html_url"]), out)

    def test_a_github_failure_exits_one(self) -> None:
        """A read that fails posts nothing and fails the step."""
        fake = FakeGitHub()

        def broken(endpoint: str) -> dict[str, Any]:
            raise github.GitHubError(f"gh: Not Found (HTTP 404) {endpoint}")

        fake.api_object = broken  # type: ignore[method-assign]
        status, out, summary = self.run_notice(fake)
        self.assertEqual(status, 1)
        self.assertEqual(fake.writes, [])
        self.assertIn("::error::", out)
        self.assertIn("failed on", summary)

    def test_unchecked_arguments_are_refused(self) -> None:
        """A run URL for another run, or a bad slug, never reaches GitHub."""
        for flag, value in (
            ("--run-url", RUN_URL.replace(RUN_ID, "1")),
            ("--run-url", "https://example.com/actions/runs/" + RUN_ID),
            ("--bot-slug", "x[bot]"),
            ("--review-repository", "not a repo"),
            ("--branches", "[]"),
            ("--branches", "nope"),
            ("--branches", '["main"]'),
            ("--branches", '"code-monkey/issue-7"'),
            ("--head", HEAD[:12]),
            ("--head", HEAD.upper()),
            ("--kind", "refused"),
        ):
            with self.subTest(flag=flag, value=value):
                fake = FakeGitHub()
                with self.assertRaises(SystemExit):
                    self.run_notice(fake, flag, value)
                self.assertEqual(fake.writes, [])


if __name__ == "__main__":
    unittest.main()
