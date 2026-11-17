# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/issue_recheck.py: the issue re-read before a publish."""

from __future__ import annotations

import subprocess
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
github = import_module("bot_github")
recheck = import_module("issue_recheck")

REPOSITORY = "owner/repo"
FORK = "lfreleng-bot-forks/repo"
OLD_FORK = "lfreleng-bot-forks/old-repo"
BRANCH = "code-monkey/issue-7"
BOT = "code-monkey[bot]"
BASE = "a" * 40


def check(**overrides: Any) -> dict[str, Any]:
    """The check.json fields the re-read uses."""
    data: dict[str, Any] = {
        "repository": REPOSITORY,
        "fork_repository": FORK,
        "fork_org": "lfreleng-bot-forks",
        "issue": 7,
        "branch": BRANCH,
        "base_sha": BASE,
        "bot_login": BOT,
        "commits": [{"sha": "c" * 40}],
        "issue_authors": ["alice"],
    }
    data.update(overrides)
    return data


def issue(
    state: str = "OPEN",
    labels: tuple[str, ...] = (),
    linked: tuple[tuple[int, str], ...] = (),
    closer: dict[str, Any] | None = None,
    author: Any = None,
    edited_at: str | None = None,
    editor: Any = None,
) -> dict[str, Any]:
    """The GraphQL reply for the issue, by a listed author unless told otherwise."""
    events = [{"closer": closer}] if closer is not None else []
    return {
        "repository": {
            "issue": {
                "state": state,
                "labels": {"nodes": [{"name": name} for name in labels]},
                "closedByPullRequestsReferences": {
                    "nodes": [{"number": n, "url": u} for n, u in linked]
                },
                "timelineItems": {"nodes": events},
                "author": author or {"login": "alice", "__typename": "User"},
                "lastEditedAt": edited_at,
                "editor": editor,
            }
        }
    }


def pull(
    number: int,
    state: str = "open",
    *,
    merged: bool = False,
    login: str = BOT,
    repo: str = FORK,
) -> dict[str, Any]:
    """One pull request from the branch, as the REST listing gives it."""
    return {
        "number": number,
        "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "state": state,
        "merged_at": "2026-11-03T20:43:00Z" if merged else None,
        "user": {"login": login},
        "head": {"repo": {"full_name": repo}},
    }


class StaleSelectionTest(unittest.TestCase):
    """``stale_selection`` refuses on every fact that changed since selection."""

    def reads(
        self,
        reply: dict[str, Any],
        pulls: list[dict[str, Any]] | None = None,
        fork: dict[str, Any] | None = None,
    ) -> None:
        """Answer the issue query, the fork read and the branch's pull requests."""
        query = patch.object(github, "graphql", return_value=reply)
        self.graphql = query.start()
        self.addCleanup(query.stop)
        found = fork or {"fork": True, "parent": {"full_name": REPOSITORY}}
        repo = patch.object(github, "api_object", return_value=found)
        self.api_object = repo.start()
        self.addCleanup(repo.stop)
        listing = patch.object(github, "api_list", return_value=pulls or [])
        self.api_list = listing.start()
        self.addCleanup(listing.stop)

    def test_open_issue_with_nothing_new_stands(self) -> None:
        """No closer, no label, no pull request: the selection holds."""
        self.reads(issue())
        self.assertIsNone(recheck.stale_selection(check()))
        self.assertTrue(self.graphql.call_args.kwargs["read"])
        self.assertEqual(
            self.graphql.call_args.args[1],
            {"owner": "owner", "name": "repo", "number": 7},
        )
        endpoint = self.api_list.call_args.args[0]
        self.assertIn("state=all", endpoint)
        self.assertIn("head=lfreleng-bot-forks%3Acode-monkey%2Fissue-7", endpoint)

    def test_closed_issue_names_its_closer(self) -> None:
        """A closed issue refuses, naming the pull request or commit that closed it."""
        self.reads(
            issue(
                "CLOSED",
                closer={"number": 304, "url": "https://github.com/owner/repo/pull/304"},
            )
        )
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertTrue(refusal.issue_closed)
        self.assertIn("closed by pull request #304", refusal.reason)
        self.api_list.assert_not_called()
        self.reads(issue("CLOSED", closer={"oid": "f" * 40}))
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertIn("closed by commit fffffff", refusal.reason)
        self.reads(issue("CLOSED"))
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertIn("issue #7 was closed after", refusal.reason)

    def test_no_agent_label_refuses(self) -> None:
        """A ``no-agent`` label added since selection refuses."""
        self.reads(issue(labels=("bug", "no-agent")))
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertFalse(refusal.issue_closed)
        self.assertFalse(refusal.unlisted)
        self.assertIn("no-agent", refusal.reason)

    def test_author_and_editor_are_rechecked(self) -> None:
        """An author or last editor the recorded list does not admit refuses."""
        mallory = {"login": "mallory", "__typename": "User"}
        for reply, record, reason in (
            (issue(author=mallory), ["alice"], "author_not_listed"),
            (
                issue(author={"login": "alice", "__typename": "Bot"}),
                ["alice"],
                "author_not_listed",
            ),
            (issue(edited_at="t", editor=mallory), ["alice"], "editor_not_listed"),
            (issue(edited_at="t", editor=None), ["alice"], "editor_not_listed"),
            (issue(), None, "no CODE_MONKEY_ISSUE_AUTHORS list"),
        ):
            self.reads(reply)
            with self.subTest(reply=reply, record=record):
                refusal = recheck.stale_selection(check(issue_authors=record))
                assert refusal is not None
                self.assertIn(reason, refusal.reason)
                self.assertFalse(refusal.issue_closed)
                self.assertTrue(refusal.unlisted)
                self.api_list.assert_not_called()
        self.reads(
            issue(edited_at="t", editor={"login": "Alice", "__typename": "User"})
        )
        self.assertIsNone(recheck.stale_selection(check()))

    def test_rule_off_rechecks_no_author(self) -> None:
        """A check that recorded the rule off admits any author, with no list."""
        mallory = {"login": "mallory", "__typename": "User"}
        off = check(only_known_authors=False, issue_authors=[])
        self.reads(issue(author=mallory, edited_at="t", editor=mallory))
        self.assertIsNone(recheck.stale_selection(off))
        self.graphql.reset_mock()
        # The publisher's later re-checks read nothing at all.
        self.assertIsNone(recheck.listed_refusal(off))
        self.graphql.assert_not_called()
        result: dict[str, Any] = {}
        self.assertTrue(recheck.listed_issue(off, result))
        self.assertNotIn(recheck.AUTHOR_UNLISTED, result)
        # Anything but a JSON false keeps the rule on.
        refusal = recheck.listed_refusal(check(only_known_authors="false"))
        assert refusal is not None
        self.assertIn("author_not_listed", refusal.reason)

    def test_open_pull_request_from_the_branch_refuses(self) -> None:
        """Another run's open pull request from the branch refuses, by name."""
        self.reads(issue(), [pull(304)])
        with patch.object(recheck.refs, "branch_matches", return_value=None):
            refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertIn("pull request #304", refusal.reason)
        self.assertIn("is open", refusal.reason)

    def test_foreign_open_pull_request_is_never_adopted(self) -> None:
        """An open pull request the bot did not open refuses without a chain read."""
        self.reads(issue(), [pull(304, login="someone")])
        with patch.object(recheck.refs, "branch_matches") as matches:
            refusal = recheck.stale_selection(check())
        matches.assert_not_called()
        assert refusal is not None
        self.assertIn("#304", refusal.reason)

    def test_own_open_pull_request_on_a_rerun_stands(self) -> None:
        """The bot's open pull request carrying this proposal's chain is a rerun's own."""
        own = f"https://github.com/{REPOSITORY}/pull/304"
        self.reads(issue(linked=((304, own),)), [pull(304), pull(200, "closed")])
        with patch.object(
            recheck.refs, "branch_matches", return_value=["c" * 40]
        ) as matches:
            self.assertIsNone(recheck.stale_selection(check()))
        matches.assert_called_once_with(FORK, BRANCH, BASE, [{"sha": "c" * 40}], BOT)

    def test_merged_pull_request_refuses(self) -> None:
        """A merged pull request from the branch refuses, by name."""
        self.reads(issue(), [pull(304, "closed", merged=True)])
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertIn("pull request #304", refusal.reason)
        self.assertIn("merged", refusal.reason)

    def test_closed_pull_request_refuses_unless_retried(self) -> None:
        """A closed unmerged pull request refuses unless ``retry_of`` names the newest."""
        self.reads(issue(), [pull(170, "closed"), pull(120, "closed")])
        for retry_of in (None, 120, "170"):
            with self.subTest(retry_of=retry_of):
                refusal = recheck.stale_selection(check(retry_of=retry_of))
                assert refusal is not None
                self.assertIn("pull request #170", refusal.reason)
                self.assertIn("not a retry", refusal.reason)
        self.assertIsNone(recheck.stale_selection(check(retry_of=170)))

    def test_other_forks_pull_requests_do_not_count(self) -> None:
        """A pull request from a same-named branch elsewhere is not the bot's."""
        self.reads(issue(), [pull(304, repo="someone/repo")])
        self.assertIsNone(recheck.stale_selection(check()))

    def test_renamed_targets_fork_counts_under_its_old_name(self) -> None:
        """A fork not yet renamed after its target is found by parent, read-only.

        Its merged pull request refuses, and a rerun's own open one is
        checked against the fork under the name it carries now.
        """
        owner = {"login": "lfreleng-bot-forks"}
        forks = [{"owner": owner, "full_name": OLD_FORK}]

        def listing(endpoint: str) -> list[dict[str, Any]]:
            if endpoint.startswith(f"repos/{REPOSITORY}/forks"):
                return forks
            return self.pulls

        for absent in (True, False):
            with self.subTest(absent=absent):
                self.pulls = [pull(304, "closed", merged=True, repo=OLD_FORK)]
                self.reads(issue(), fork={"fork": False})
                if absent:
                    self.api_object.side_effect = github.GitHubError("gone", 404)
                self.api_list.side_effect = listing
                refusal = recheck.stale_selection(check())
                assert refusal is not None
                self.assertIn("pull request #304", refusal.reason)
                self.assertIn("merged", refusal.reason)
                self.api_object.assert_called_once_with(f"repos/{FORK}")
        self.pulls = [pull(304, repo=OLD_FORK)]
        with patch.object(
            recheck.refs, "branch_matches", return_value=["c" * 40]
        ) as matches:
            self.assertIsNone(recheck.stale_selection(check()))
        matches.assert_called_once_with(
            OLD_FORK, BRANCH, BASE, [{"sha": "c" * 40}], BOT
        )

    def test_redirected_fork_counts_under_its_current_name(self) -> None:
        """A read by name that a rename redirects keeps the name the reply carries.

        A closed prior pull request from the renamed fork still refuses
        a run that is not a retry of it, and a rerun's chain is checked
        under the current name.
        """
        fork: dict[str, Any] = {
            "fork": True,
            "full_name": OLD_FORK,
            "parent": {"full_name": REPOSITORY},
        }
        self.reads(issue(), [pull(304, "closed", repo=OLD_FORK)], fork=fork)
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertIn("pull request #304", refusal.reason)
        self.assertIn("not a retry", refusal.reason)
        self.api_object.assert_called_once_with(f"repos/{FORK}")
        self.assertFalse(
            any("/forks" in call.args[0] for call in self.api_list.call_args_list)
        )
        self.reads(issue(), [pull(305, repo=OLD_FORK)], fork=fork)
        with patch.object(
            recheck.refs, "branch_matches", return_value=["c" * 40]
        ) as matches:
            self.assertIsNone(recheck.stale_selection(check()))
        matches.assert_called_once_with(
            OLD_FORK, BRANCH, BASE, [{"sha": "c" * 40}], BOT
        )

    def test_linked_open_pull_request_refuses(self) -> None:
        """Another open pull request linked to close the issue refuses."""
        url = f"https://github.com/{REPOSITORY}/pull/310"
        self.reads(issue(linked=((310, url),)))
        refusal = recheck.stale_selection(check())
        assert refusal is not None
        self.assertIn(f"pull request #310 ({url}) is linked", refusal.reason)

    def test_failed_reads_raise(self) -> None:
        """A read that fails or answers no issue raises for the caller to fail."""
        with (
            patch.object(github, "graphql", side_effect=github.GitHubError("boom")),
            self.assertRaises(github.GitHubError),
        ):
            recheck.stale_selection(check())
        with (
            patch.object(
                github, "graphql", return_value={"repository": {"issue": None}}
            ),
            self.assertRaisesRegex(github.GitHubError, "could not read issue"),
        ):
            recheck.stale_selection(check())
        self.reads(issue())
        self.api_list.side_effect = github.GitHubError("boom (HTTP 502)")
        with self.assertRaises(github.GitHubError):
            recheck.stale_selection(check())
        self.reads(issue())
        self.api_object.side_effect = github.GitHubError("boom", 502)
        with self.assertRaises(github.GitHubError):
            recheck.stale_selection(check())
        self.api_object.side_effect = github.GitHubError("gone", 404)
        self.api_list.side_effect = github.GitHubError("boom", 502)
        with self.assertRaisesRegex(recheck.PublishError, "could not list forks"):
            recheck.stale_selection(check())


class ImportOrderTest(unittest.TestCase):
    """issue_recheck sits on an import cycle through branch_refs."""

    def test_every_module_imports_first(self) -> None:
        # Each module is imported first in a clean module table, so a
        # cycle that only loads from one end fails here.
        code = (
            "import importlib, sys\n"
            "names = sys.argv[1:]\n"
            "for name in names:\n"
            "    for loaded in names:\n"
            "        sys.modules.pop(loaded, None)\n"
            "    importlib.import_module(name)\n"
        )
        names = sorted(path.stem for path in SCRIPTS.glob("*.py"))
        done = subprocess.run(
            [sys.executable, "-B", "-c", code, *names],
            cwd=SCRIPTS,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
