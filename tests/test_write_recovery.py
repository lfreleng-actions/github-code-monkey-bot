# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Publisher writes that ride out a transient 5xx (issue #100).

A fake GitHub stands in for ``gh``: it holds the fork branch and the
commits on it, applies or refuses each write as a test scripts it,
and answers the reads the publisher makes to look a write up.
"""

from __future__ import annotations

import argparse
import io
import json
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
bounded = import_module("git_bounded")
checks = import_module("proposal_check")
policy = import_module("proposal_policy")
publish = import_module("publish")
refs = import_module("branch_refs")
revision = import_module("revision_publish")
comments = import_module("issue_comment")
review = import_module("copilot_review")

BASE = "a" * 40
BLOB = "1" * 40
REPOSITORY = "owner/repo"
FORK_ORG = "lfreleng-bot-forks"
FORK = f"{FORK_ORG}/repo"
BRANCH = "code-monkey/issue-7"
BOT = "code-monkey[bot]"
UNRELATED = "9" * 40


def bad_gateway() -> Exception:
    """The error gh raises for GitHub's intermittent 502."""
    return github.GitHubError("gh: Bad Gateway (HTTP 502)")


def proposal(count: int = 3) -> dict[str, Any]:
    """A proposed check.json of ``count`` commits, each with its own tree."""
    return {
        "schema": 1,
        "key": "repo-7",
        "repository": REPOSITORY,
        "fork_org": FORK_ORG,
        "fork_repository": FORK,
        "issue": 7,
        "branch": BRANCH,
        "base_sha": BASE,
        "default_branch": "main",
        "verdict": "proposed",
        "reasons": [],
        "commits": [
            {
                "sha": f"{n:x}" * 40,
                "tree": f"{n + 4:x}" * 40,
                "headline": f"Fix: Part {n}",
                "body": f"Body {n}\n\nSigned-off-by: b <b@x>",
                "additions": [{"path": f"f{n}.txt", "blob": BLOB, "size": 3}],
                "deletions": [],
            }
            for n in range(1, count + 1)
        ],
        "pr_title": "Fix: All parts",
        "pr_body": "Body\n",
        "bot_login": BOT,
    }


class FakeGitHub:
    """The fork as GitHub holds it, with scripted faults on its writes.

    ``faults`` maps the number of a ``createCommitOnBranch`` call,
    from 1, to how it fails: ``refused`` answers 502 and applies
    nothing, ``lost`` applies the commit and answers 502, ``moved``
    puts an unrelated commit on the branch and answers 502.
    ``sync_errors`` are the answers merge-upstream gives before it
    succeeds. ``broken_reads`` makes every branch and commit read
    answer 502 once the first commit fault has fired. ``create_lost``
    applies the branch create and answers 502; ``create_refused``
    answers 502 and applies nothing.
    """

    def __init__(self) -> None:
        """Start with the fork synced and no bot branch."""
        self.head: str | None = None
        self.objects: dict[str, dict[str, Any]] = {}
        self.faults: dict[int, str] = {}
        self.sync_errors: list[Exception] = []
        self.create_lost = False
        self.create_refused = False
        self.broken_reads = False
        self.faulted = False
        self.mutations = 0
        self.syncs = 0
        self.creates = 0
        self.deletes = 0
        self.expected_heads: list[str] = []

    def api_object(self, endpoint: str) -> dict[str, Any]:
        """Serve the fork, the base commit, the branch ref and its commits."""
        if endpoint == f"repos/{FORK}":
            return {
                "full_name": FORK,
                "fork": True,
                "parent": {"full_name": REPOSITORY},
            }
        if endpoint == f"repos/{FORK}/commits/{BASE}":
            return {"sha": BASE}
        if self.broken_reads and self.faulted:
            raise bad_gateway()
        if endpoint == f"repos/{FORK}/git/ref/heads/{BRANCH}":
            if self.head is None:
                raise github.GitHubError("gh: Not Found (HTTP 404)")
            return {"object": {"sha": self.head, "type": "commit"}}
        prefix = f"repos/{FORK}/git/commits/"
        if endpoint.startswith(prefix) and endpoint[len(prefix) :] in self.objects:
            return self.objects[endpoint[len(prefix) :]]
        raise AssertionError(f"unexpected read {endpoint}")

    def api_write(
        self, method: str, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Sync the fork or create the branch."""
        if (method, endpoint) == ("POST", f"repos/{FORK}/merge-upstream"):
            self.syncs += 1
            if self.sync_errors:
                raise self.sync_errors.pop(0)
            return {}
        if (method, endpoint) == ("POST", f"repos/{FORK}/git/refs"):
            self.creates += 1
            if self.create_refused:
                self.create_refused = False
                raise bad_gateway()
            if self.head is not None:
                raise github.GitHubError("gh: Reference already exists (HTTP 422)")
            self.head = str(payload["sha"])
            if self.create_lost:
                self.create_lost = False
                raise bad_gateway()
            return {}
        raise AssertionError(f"unexpected write {method} {endpoint}")

    def run_gh(self, args: list[str], **_: Any) -> str:
        """Delete the branch."""
        if args[:3] == ["api", "--method", "DELETE"]:
            self.deletes += 1
            if self.head is None:
                raise github.GitHubError("gh: Reference does not exist (HTTP 422)")
            self.head = None
            return ""
        raise AssertionError(f"unexpected gh {args}")

    def add(self, parent: str, tree: str, message: str) -> str:
        """Put a commit on the branch; return its oid."""
        oid = f"{len(self.objects) + 1:040x}"
        self.objects[oid] = {
            "sha": oid,
            "tree": {"sha": tree},
            "parents": [{"sha": parent}],
            "message": message,
        }
        self.head = oid
        return oid

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Apply ``createCommitOnBranch`` as a compare-and-swap, with faults."""
        self.assertion(query)
        data = variables["input"]
        self.mutations += 1
        expected = str(data["expectedHeadOid"])
        self.expected_heads.append(expected)
        fault = self.faults.get(self.mutations)
        if fault == "refused":
            self.faulted = True
            raise bad_gateway()
        if self.head != expected:
            raise github.GitHubError("GraphQL errors: expected head moved")
        message = data["message"]
        number = int(str(message["headline"]).rsplit(" ", 1)[1])
        tree = f"{number + 4:x}" * 40
        if fault == "moved":
            self.faulted = True
            self.add(expected, "0" * 40, "Someone else's work")
            raise bad_gateway()
        oid = self.add(expected, tree, f"{message['headline']}\n\n{message['body']}")
        if fault == "lost":
            self.faulted = True
            raise bad_gateway()
        return {"createCommitOnBranch": {"commit": {"oid": oid, "url": "u"}}}

    @staticmethod
    def assertion(query: str) -> None:
        """Only the commit mutation reaches this fake."""
        if "createCommitOnBranch" not in query:
            raise AssertionError("unexpected GraphQL operation")


class RecoveryCase(unittest.TestCase):
    """Runs the publisher against a ``FakeGitHub`` with no subprocess and no sleep."""

    def setUp(self) -> None:
        """Route every GitHub call to the fake; forbid real processes."""
        for target, name in ((github.subprocess, "run"), (bounded.subprocess, "Popen")):
            guard = patch.object(
                target, name, side_effect=AssertionError("unexpected subprocess")
            )
            guard.start()
            self.addCleanup(guard.stop)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.fake = FakeGitHub()
        self.sleeps: list[float] = []
        for name in ("api_object", "api_write", "graphql", "run_gh"):
            stub = patch.object(github, name, side_effect=getattr(self.fake, name))
            stub.start()
            self.addCleanup(stub.stop)
        for clock in (github.time, refs.time):
            nap = patch.object(clock, "sleep", side_effect=self.sleeps.append)
            nap.start()
            self.addCleanup(nap.stop)
        blobs = patch.object(checks, "git", return_value=b"one")
        blobs.start()
        self.addCleanup(blobs.stop)

    def push(self, check: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run ``publish.py push`` in pull-requests mode; return the result."""
        path = self.root / "check.json"
        path.write_text(json.dumps(check or proposal()), encoding="utf-8")
        args = argparse.Namespace(
            check=path,
            workdir=self.root / "work",
            mode="pull-requests",
            dry_run=False,
            run_attempt=1,
            author_session="",
            output=self.root / "result.json",
        )
        return publish.run_push(args)

    def failed_push(self) -> dict[str, Any]:
        """Run a push that must fail; return the result it wrote."""
        with self.assertRaises(policy.PublishError):
            self.push()
        return json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def chain(self) -> list[str]:
        """The branch's commits from the base up, by parent."""
        found: list[str] = []
        head = self.fake.head
        while head is not None and head != BASE:
            found.append(head)
            head = self.fake.objects[head]["parents"][0]["sha"]
        return found[::-1]


class ReplayRecoveryTest(RecoveryCase):
    """``createCommitOnBranch`` is a compare-and-swap, retried or read back."""

    def test_a_502_that_did_not_land_is_sent_again(self) -> None:
        """The head still at expectedHeadOid: the same mutation goes again."""
        self.fake.faults = {2: "refused"}
        result = self.push()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["commits"], self.chain())
        self.assertEqual(len(result["commits"]), 3)
        self.assertEqual(self.fake.mutations, 4)
        first = result["commits"][0]
        self.assertEqual(
            self.fake.expected_heads, [BASE, first, first, result["commits"][1]]
        )
        self.assertTrue(any("commit 2 of 3" in w for w in result["warnings"]))

    def test_a_502_that_landed_is_adopted_without_a_duplicate(self) -> None:
        """The head moved to this very commit: its oid is taken, nothing resent."""
        self.fake.faults = {2: "lost"}
        result = self.push()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(self.fake.mutations, 3)
        self.assertEqual(result["commits"], self.chain())
        self.assertEqual(len(self.fake.objects), 3)
        self.assertTrue(any("commit 2 of 3" in w for w in result["warnings"]))

    def test_a_head_moved_to_an_unrelated_commit_fails(self) -> None:
        """A head that is not this commit is no landing: fail and roll back."""
        self.fake.faults = {2: "moved"}
        written = self.failed_push()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertEqual(self.fake.mutations, 2)
        self.assertIn("branch removed", written["reasons"][-1])
        self.assertIsNone(self.fake.head)

    def test_every_read_back_failing_fails_and_rolls_back(self) -> None:
        """A look that cannot answer is never read as "not landed"."""
        self.fake.faults = {2: "refused"}
        self.fake.broken_reads = True
        written = self.failed_push()
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertEqual(self.fake.mutations, 2)
        self.assertIn("createCommitOnBranch failed", written["reasons"][-1])
        self.assertIn("branch removed", written["reasons"][-1])
        self.assertIsNone(self.fake.head)


class RefRecoveryTest(RecoveryCase):
    """The fork sync, the branch create and its delete ride out a 502."""

    def test_a_502_on_merge_upstream_is_retried(self) -> None:
        """The sync is idempotent: a 502 is retried with the sync's backoff."""
        self.fake.sync_errors = [bad_gateway()]
        result = self.push()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(self.fake.syncs, 2)
        self.assertEqual(self.sleeps[:1], [refs.SYNC_BACKOFF_SECONDS[0]])
        self.assertTrue(any("fork sync" in w for w in result["warnings"]))

    def test_a_502_on_every_sync_fails(self) -> None:
        """A sync that never gets through still fails the publication."""
        self.fake.sync_errors = [bad_gateway()] * refs.SYNC_ATTEMPTS
        written = self.failed_push()
        self.assertIn("could not sync fork", written["reasons"][-1])
        self.assertEqual(self.fake.creates, 0)

    def test_a_502_then_a_tolerated_answer_records_the_incident(self) -> None:
        """A 502 the sync rode out is recorded, though it ended on a 409."""
        self.fake.sync_errors = [
            bad_gateway(),
            github.GitHubError("gh: Conflict (HTTP 409)"),
        ]
        result = self.push()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(self.fake.syncs, 2)
        self.assertTrue(
            any("fork sync" in w and "409" in w for w in result["warnings"])
        )

    def test_a_branch_found_at_the_base_is_not_deleted_as_ours(self) -> None:
        """A create read back, not answered, cannot prove the branch is this run's."""
        self.fake.head = BASE
        self.fake.create_refused = True
        self.fake.faults = {1: "moved"}
        written = self.failed_push()
        self.assertEqual(self.fake.creates, 1)
        self.assertEqual(self.fake.deletes, 0)
        self.assertIsNotNone(self.fake.head)
        self.assertIn("any branch there is kept", written["reasons"][-1])

    def test_a_lost_create_is_read_back(self) -> None:
        """A branch at the base after a 502 on its create is this run's."""
        self.fake.create_lost = True
        result = self.push()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(self.fake.creates, 1)
        self.assertEqual(len(result["commits"]), 3)
        self.assertTrue(any("branch creation" in w for w in result["warnings"]))

    def test_a_lost_delete_is_read_back(self) -> None:
        """A branch gone after a 502 on its delete was deleted."""
        self.fake.head = BASE

        def lost(args: list[str], **_: Any) -> str:
            self.fake.head = None
            raise bad_gateway()

        with patch.object(github, "run_gh", side_effect=lost) as delete:
            self.assertIsNone(refs.delete_branch(FORK, BRANCH))
        delete.assert_called_once()

    def test_a_delete_that_did_not_land_is_sent_again(self) -> None:
        """A DELETE is idempotent: a 502 with the branch still there goes again."""
        self.fake.head = BASE
        replies: list[Any] = [bad_gateway()]

        def flaky(args: list[str], **kwargs: Any) -> str:
            if replies:
                raise replies.pop(0)
            return self.fake.run_gh(args, **kwargs)

        with patch.object(github, "run_gh", side_effect=flaky):
            self.assertIsNone(refs.delete_branch(FORK, BRANCH))
        self.assertIsNone(self.fake.head)
        self.assertEqual(self.fake.deletes, 1)


REVISION_MARKER = "<!-- code-monkey reply round=2 session=1 -->"


def revision_check() -> dict[str, Any]:
    """A proposed revision's check.json, answering one thread."""
    data = proposal(1)
    data.update(
        task="revise",
        head_sha="e" * 40,
        pull_number=41,
        pull_request_url=f"https://github.com/{REPOSITORY}/pull/41",
        round=2,
        session="1",
        pr_title="Fix: New title",
        pr_body="New body",
        responses=[
            {
                "thread_id": "PRRT_1",
                "comment_id": 100,
                "reply": f"Fixed.\n\n{REVISION_MARKER}",
                "resolve": False,
            }
        ],
    )
    return data


def no_replies(_: str) -> list[dict[str, Any]]:
    """A thread holding no reply of the bot's."""
    return []


def answered_thread(_: str) -> list[dict[str, Any]]:
    """A thread the bot already answered this round."""
    return [{"author": {"login": "code-monkey"}, "body": REVISION_MARKER}]


class TargetRecoveryTest(unittest.TestCase):
    """Replies, the description and the issue comment look before failing."""

    def setUp(self) -> None:
        """Forbid real processes and real sleeps."""
        guard = patch.object(
            github.subprocess, "run", side_effect=AssertionError("unexpected gh")
        )
        guard.start()
        self.addCleanup(guard.stop)
        nap = patch.object(github.time, "sleep")
        nap.start()
        self.addCleanup(nap.stop)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def respond(
        self, write: Any, thread: Any, pull: dict[str, Any]
    ) -> tuple[dict[str, Any], Any]:
        """Run ``respond`` for a published revision; return the result and writes."""
        check = self.root / "check.json"
        check.write_text(json.dumps(revision_check()), encoding="utf-8")
        result = self.root / "result.json"
        tip = "f" * 40
        result.write_text(
            json.dumps(
                {
                    "key": "repo-7",
                    "verdict": "proposed",
                    "commits": [tip],
                    "warnings": [],
                    "reasons": [],
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(review, "pull_head", return_value=tip),
            patch.object(revision.threads_of, "read_thread", side_effect=thread),
            patch.object(github, "api_write", side_effect=write) as writes,
            patch.object(github, "api_object", return_value=pull),
            redirect_stdout(io.StringIO()),
        ):
            revision.run_respond(
                argparse.Namespace(result=result, check=check, output=result)
            )
        return json.loads(result.read_text(encoding="utf-8")), writes

    def test_a_reply_that_landed_is_not_posted_again(self) -> None:
        """The marker on the thread after a 502 proves the reply landed."""
        posted: list[str] = []

        def write(method: str, endpoint: str, payload: dict[str, Any]) -> Any:
            if endpoint.endswith("/replies"):
                posted.append(str(payload["body"]))
                raise bad_gateway()
            return {}

        def thread(_: str) -> list[dict[str, Any]]:
            return [
                {"author": {"login": "code-monkey"}, "body": body} for body in posted
            ]

        stored, _ = self.respond(
            write, thread, {"title": "Fix: New title", "body": "New body"}
        )
        self.assertEqual(len(posted), 1)
        self.assertEqual(stored["responded"]["replied"], 1)
        self.assertEqual(stored["responded"]["failed"], 0)
        self.assertTrue(any("thread reply" in w for w in stored["warnings"]))

    def test_a_reply_that_did_not_land_is_never_resent(self) -> None:
        """A marker's absence proves nothing: one POST, then a warning."""
        posted: list[str] = []

        def write(method: str, endpoint: str, payload: dict[str, Any]) -> Any:
            if endpoint.endswith("/replies"):
                posted.append(str(payload["body"]))
                raise bad_gateway()
            return {}

        stored, _ = self.respond(
            write, no_replies, {"title": "Fix: New title", "body": "New body"}
        )
        self.assertEqual(len(posted), 1)
        self.assertEqual(stored["responded"]["failed"], 1)

    def description(self, pull: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Respond with a description PATCH answering 502, ``pull`` read back.

        Return the result and the number of PATCHes sent.
        """

        def write(method: str, endpoint: str, payload: dict[str, Any]) -> Any:
            if method == "PATCH":
                raise bad_gateway()
            return {}

        stored, writes = self.respond(write, answered_thread, pull)
        return stored, sum(1 for c in writes.call_args_list if c.args[0] == "PATCH")

    def test_a_description_edit_that_landed_is_recorded(self) -> None:
        """The title and body in place after a 502: written, no second PATCH."""
        stored, patches = self.description(
            {"title": "Fix: New title", "body": "New body"}
        )
        self.assertEqual(patches, 1)
        self.assertTrue(
            any("pull request description landed" in w for w in stored["warnings"])
        )

    def test_a_description_edit_not_landed_is_never_resent(self) -> None:
        """A newer description may arrive meanwhile: one PATCH, then a warning."""
        stored, patches = self.description({"title": "Fix: Newer", "body": "Newer"})
        self.assertEqual(patches, 1)
        self.assertTrue(
            any("could not update the description" in w for w in stored["warnings"])
        )

    def restore(
        self, reads: list[dict[str, Any]], replies: list[Any], *, fails: bool = False
    ) -> tuple[Any, str]:
        """Run ``restore-title`` with a PATCH answering ``replies``.

        ``reads`` are the pull request reads in turn; ``fails`` says
        the step must exit non-zero. Return the PATCH mock and stderr.
        """
        record = self.root / "retitle.json"
        record.write_text(
            json.dumps(
                {
                    "repository": REPOSITORY,
                    "pull_number": 41,
                    "head_sha": "e" * 40,
                    "previous": "Fix: Old",
                    "title": "Fix: New title",
                }
            ),
            encoding="utf-8",
        )

        def pull_read(_: str) -> dict[str, Any]:
            return reads.pop(0)

        with (
            patch.object(review, "pull_head", return_value="e" * 40),
            patch.object(github, "api_object", side_effect=pull_read),
            patch.object(github, "api_write", side_effect=replies) as write,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as err,
        ):
            argv = [
                "restore-title",
                "--record",
                str(record),
                "--result",
                str(self.root / "absent.json"),
            ]
            if fails:
                with self.assertRaises(SystemExit):
                    publish.main(argv)
            else:
                publish.main(argv)
        self.assertEqual(reads, [])
        return write, err.getvalue()

    def test_a_lost_title_restore_is_read_back(self) -> None:
        """A restore that landed despite a 502 succeeds, with no second PATCH."""
        write, err = self.restore(
            [{"title": "Fix: New title"}, {"title": "Fix: Old"}], [bad_gateway()]
        )
        write.assert_called_once()
        self.assertIn("landed despite", err)

    def test_a_title_restore_not_landed_is_never_resent(self) -> None:
        """A maintainer may retitle meanwhile: one PATCH, and the step fails."""
        unchanged = {"title": "Fix: New title"}
        write, err = self.restore(
            [unchanged, unchanged], [bad_gateway(), {}], fails=True
        )
        write.assert_called_once()
        self.assertIn("502", err)

    def comment(self, pages: Any) -> tuple[Path, Any]:
        """Run ``comment`` for a proposal whose POST answers 502."""
        path = self.root / "result.json"
        path.write_text(
            json.dumps(
                {
                    "key": "repo-7",
                    "repository": REPOSITORY,
                    "issue": 7,
                    "verdict": "proposed",
                    "reasons": [],
                    "warnings": [],
                    "dry_run": False,
                    "mode": "pull-requests",
                    "pull_request_url": f"https://github.com/{REPOSITORY}/pull/8",
                    "bot_login": BOT,
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(comments.reads, "api_page", side_effect=pages),
            patch.object(github, "api_write", side_effect=bad_gateway()) as write,
            redirect_stdout(io.StringIO()),
        ):
            try:
                comments.run_comment(
                    argparse.Namespace(result=path, run_url="https://run", since="t")
                )
            except github.GitHubError:
                pass
        return path, write

    def test_an_issue_comment_that_landed_is_recorded(self) -> None:
        """The run's marker on the bot's comment after a 502 proves it landed."""
        landed = {
            "id": 5,
            "user": {"login": BOT},
            "body": "text\n\n<!-- code-monkey https://run proposed -->",
            "html_url": "https://x/c/5",
        }
        path, write = self.comment([[], [landed]])
        write.assert_called_once()
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["comment_url"], "https://x/c/5")
        self.assertTrue(any("issue comment" in w for w in stored["warnings"]))

    def test_an_issue_comment_that_did_not_land_is_never_resent(self) -> None:
        """No marker after the 502: one POST, and the step fails."""
        path, write = self.comment([[], []])
        write.assert_called_once()
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(stored.get("comment_url"))


if __name__ == "__main__":
    unittest.main()
