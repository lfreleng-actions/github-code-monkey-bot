# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Selection policy: parsing, filtering, ranking, choosing and outputs."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("issue_reads")
repository_reads = import_module("repository_reads")
select = import_module("select_issues")
explicit_targets = import_module("explicit_issues")
inputs = import_module("selection_inputs")
revision_select = import_module("revision_select")
categories = import_module("issue_categories")

ALL = frozenset(categories.ALL_CATEGORIES)
outputs = import_module("selection_outputs")

SHA_A = "a" * 40
SHA_B = "b" * 40
FORK_ORGS: dict[str, Any] = {
    "default": "lfreleng-bot-forks",
    "organisations": {"onap": "lfreleng-bot-forks-onap"},
}
FORK_ORGS_PATH = Path(__file__).resolve().parents[1] / "config" / "fork-orgs.json"


def repo_meta(name: str, **overrides: Any) -> dict[str, Any]:
    """Repository metadata as ``list_repositories`` would report it."""
    meta: dict[str, Any] = {
        "name": name,
        "archived": False,
        "template": False,
        "fork": False,
        "public": True,
        "default_branch": "main",
    }
    meta.update(overrides)
    return meta


def search_issue(
    repo: str, number: int, *, labels: list[str] | None = None, **overrides: Any
) -> dict[str, Any]:
    """One entry as ``gh search issues --json`` would produce it."""
    issue: dict[str, Any] = {
        "repository": {"name": repo, "nameWithOwner": f"org/{repo}"},
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://github.com/org/{repo}/issues/{number}",
        "labels": [{"name": name} for name in labels or []],
        "assignees": [],
        "authorAssociation": "MEMBER",
        "createdAt": f"2026-01-{number:02d}T00:00:00Z",
        "isPullRequest": False,
        "state": "open",
    }
    issue.update(overrides)
    return issue


def candidate(
    repo: str, number: int, *, priority: str | None = None, created_at: str = ""
) -> dict[str, Any]:
    """An enriched candidate ready for ranking and choosing."""
    return {
        "repository": f"org/{repo}",
        "repo_name": repo,
        "number": number,
        "url": f"https://github.com/org/{repo}/issues/{number}",
        "created_at": created_at or f"2026-01-{number:02d}T00:00:00Z",
        "author_association": "MEMBER",
        "default_branch": "main",
        "title": f"Issue {number}",
        "body": "",
        "labels": [],
        "type": "Bug",
        "assignees": [],
        "priority": priority,
    }


def attempted_issue_one(_repo: str, _fork: str, branch: str) -> str | None:
    """A ``prior_attempt`` double that reports a pull request for issue 1."""
    return "pull_request" if branch.endswith("-1") else None


def orphan_issue_one(_repo: str, _fork: str, branch: str) -> str | None:
    """A ``prior_attempt`` double that reports an orphan fork branch for issue 1."""
    return "branch" if branch.endswith("-1") else None


def linked_issue_two(_repo: str, number: int) -> bool:
    """A ``has_open_linked_pr`` double that reports issue 2 as linked."""
    return number == 2


def fresh_skipped() -> dict[str, int]:
    """The zeroed skip counters ``build_selection`` starts with."""
    return dict.fromkeys(
        (
            "pull_request",
            "repository",
            "label",
            "no_type",
            "closed",
            "category",
            "assigned",
            "attempted",
            "linked_pr",
            "one_per_repo",
            "cap",
        ),
        0,
    )


class NoSubprocessCase(unittest.TestCase):
    """Base class that fails any test reaching a real ``gh`` subprocess."""

    def setUp(self) -> None:
        """Forbid subprocess use for the duration of each test."""
        guard = patch.object(
            github.subprocess,
            "run",
            side_effect=AssertionError("unexpected subprocess"),
        )
        guard.start()
        self.addCleanup(guard.stop)


class ParseRepositoriesTest(unittest.TestCase):
    """``parse_repositories`` splits, folds, dedups and rejects."""

    def test_mixed_separators(self) -> None:
        """Commas and whitespace both separate; empties vanish."""
        self.assertEqual(inputs.parse_repositories("a, b c,,d"), ["a", "b", "c", "d"])

    def test_case_folding_and_dedup(self) -> None:
        """Names fold to lower case and repeats collapse, keeping order."""
        self.assertEqual(inputs.parse_repositories("Foo bar FOO Bar"), ["foo", "bar"])

    def test_empty_text(self) -> None:
        """Whitespace only yields no names."""
        self.assertEqual(inputs.parse_repositories("  \n "), [])

    def test_rejects_bad_names(self) -> None:
        """Traversal, slashes and punctuation are refused."""
        for bad in ("..", ".", "a/b", "x y!", "a,b/c"):
            with self.subTest(bad=bad), self.assertRaises(reads.SelectionError):
                inputs.parse_repositories(bad)


class ParseIssuesTest(unittest.TestCase):
    """``explicit_issues.parse_issues`` reads the splitter's canonical ``owner/repo#N`` form."""

    def test_order_kept_and_repeats_dropped(self) -> None:
        """Items keep the given order; case differences are repeats."""
        self.assertEqual(
            explicit_targets.parse_issues(
                "org/beta#2, Org/Alpha#1 org/alpha#1\norg/beta#3", "org"
            ),
            [("beta", 2), ("alpha", 1), ("beta", 3)],
        )
        self.assertEqual(explicit_targets.parse_issues("  ", "org"), [])

    def test_rejects_other_owners_and_bad_forms(self) -> None:
        """A stranger's issue, a bad number or an unexpected spelling fails."""
        for text in (
            "other/alpha#1",
            "org/alpha#0",
            "alpha#1",
            "org/alpha",
            "org/a b#1",
        ):
            with self.subTest(text=text), self.assertRaises(reads.SelectionError):
                explicit_targets.parse_issues(text, "org")


class ExplicitIssuesTest(NoSubprocessCase):
    """Named issues skip the scan and the triage filters, not the safety ones."""

    def setUp(self) -> None:
        """Patch the reads an explicit selection performs."""
        super().setUp()
        repositories = {
            "alpha": repo_meta("alpha"),
            "beta": repo_meta("beta", archived=True),
            "delta": repo_meta("delta"),
        }
        self.details: dict[tuple[str, int], dict[str, Any]] = {
            ("org/alpha", 1): self.detail(
                type=None, assignees=["someone"], labels=["chore"]
            ),
            ("org/alpha", 3): self.detail(),
            ("org/delta", 5): self.detail(state="closed"),
            ("org/delta", 6): self.detail(labels=["no-agent"]),
        }
        self.repositories = repositories
        patch.object(
            repository_reads, "list_repositories", return_value=repositories
        ).start()
        patch.object(repository_reads, "repository_meta", side_effect=self.meta).start()
        self.search = patch.object(reads, "search_open_issues").start()
        patch.object(reads, "issue_details", side_effect=self.lookup).start()
        patch.object(reads, "prior_attempt", return_value=None).start()
        patch.object(reads, "has_open_linked_pr", return_value=False).start()
        patch.object(reads, "branch_head", return_value=SHA_A).start()
        patch.object(reads, "filtered_comments", return_value=([], 0, False)).start()
        patch.object(reads, "fetch_guidance", return_value=(b"g\n", SHA_B)).start()
        patch.object(
            reads, "bot_identity", return_value={"login": "b[bot]", "email": "e"}
        ).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def detail(**overrides: Any) -> dict[str, Any]:
        """An ``issue_details`` reply, with the fields a direct read adds."""
        data: dict[str, Any] = {
            "state": "open",
            "title": "Title",
            "body": "Body",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": "Low",
            "url": "https://github.com/org/x/issues/1",
            "created_at": "2026-01-01T00:00:00Z",
            "author_association": "MEMBER",
        }
        data.update(overrides)
        return data

    def meta(self, _org: str, name: str) -> dict[str, Any] | None:
        """Serve the fixture repository metadata by lower-cased name."""
        return self.repositories.get(name)

    def lookup(self, repo: str, number: int) -> dict[str, Any]:
        """Serve the fixture details for a known issue."""
        return self.details[(repo, number)]

    def args(self, issues: str, repositories: str = "") -> Any:
        """The parsed arguments of an explicit run."""
        return select.build_parser().parse_args(
            [
                "--org",
                "org",
                "--output-dir",
                "out",
                "--mode",
                "pull-requests",
                "--model",
                "m",
                "--guidance-repository",
                "org/.github",
                "--fork-orgs",
                str(FORK_ORGS_PATH),
                "--categories",
                "docs",
                "--issues",
                issues,
                "--repositories",
                repositories,
            ]
        )

    def test_named_issues_are_worked_in_order_without_triage_filters(self) -> None:
        """No scan, no Type, label, assignee, category or one-per-repository rule."""
        selection, _ = select.build_selection(
            self.args(
                "org/alpha#3,org/alpha#1,org/beta#2,org/gamma#4,org/delta#5,org/delta#6"
            )
        )
        self.search.assert_not_called()
        self.assertEqual(
            [i["key"] for i in selection["issues"]], ["alpha-3", "alpha-1"]
        )
        self.assertEqual(selection["issues"][1]["type"], None)
        self.assertEqual(selection["candidates_seen"], 6)
        self.assertEqual(selection["skipped"]["one_per_repo"], 0)
        self.assertEqual(selection["skipped"]["category"], 0)
        self.assertEqual(selection["skipped"]["repository"], 2)
        self.assertEqual(selection["skipped"]["closed"], 1)
        self.assertEqual(selection["skipped"]["label"], 1)
        self.assertEqual(
            selection["explicit_issues"],
            [
                "org/alpha#3",
                "org/alpha#1",
                "org/beta#2",
                "org/gamma#4",
                "org/delta#5",
                "org/delta#6",
            ],
        )
        reasons = {
            entry["issue"]: entry["reason"] for entry in selection["skipped_issues"]
        }
        self.assertEqual(
            set(reasons), {"org/beta#2", "org/gamma#4", "org/delta#5", "org/delta#6"}
        )
        self.assertIn("archived", reasons["org/beta#2"])
        self.assertIn("not found", reasons["org/gamma#4"])
        self.assertIn("closed", reasons["org/delta#5"])
        self.assertIn("no-agent", reasons["org/delta#6"])

    def test_named_repositories_are_read_one_by_one(self) -> None:
        """No organisation listing: a named run cannot hit the listing ceiling."""
        listing = patch.object(
            repository_reads, "list_repositories", side_effect=AssertionError("listed")
        ).start()
        self.repositories = {"alpha": repo_meta("alpha")}
        selection, _ = select.build_selection(self.args("org/alpha#3,org/gamma#4"))
        listing.assert_not_called()
        self.assertEqual([i["key"] for i in selection["issues"]], ["alpha-3"])
        (entry,) = selection["skipped_issues"]
        self.assertIn("not found", entry["reason"])

    def test_missing_issue_and_pull_request_number_are_skipped(self) -> None:
        """An absent issue, or a pull request's number, skips that item alone."""

        def lookup(repo: str, number: int) -> dict[str, Any]:
            if number == 9:
                raise github.GitHubError("gh: Not Found (HTTP 404)")
            if number == 8:
                raise reads.NotAnIssue(f"{repo}#{number} is a pull request")
            return self.details[(repo, number)]

        patch.object(reads, "issue_details", side_effect=lookup).start()
        selection, _ = select.build_selection(
            self.args("org/alpha#9,org/alpha#8,org/alpha#3")
        )
        self.assertEqual([i["key"] for i in selection["issues"]], ["alpha-3"])
        reasons = {e["issue"]: e["reason"] for e in selection["skipped_issues"]}
        self.assertIn("not found", reasons["org/alpha#9"])
        self.assertIn("pull request", reasons["org/alpha#8"])

    def test_other_read_failures_still_abort(self) -> None:
        """A server or permission failure is not a skip."""
        patch.object(
            reads,
            "issue_details",
            side_effect=github.GitHubError("gh: Server Error (HTTP 502)"),
        ).start()
        with self.assertRaises(github.GitHubError):
            select.build_selection(self.args("org/alpha#3"))

    def test_empty_repository_is_a_named_skip(self) -> None:
        """An issue in a repository with no commits skips alone; the rest proceed."""
        self.repositories["empty"] = repo_meta("empty", default_branch=None)
        selection, _ = select.build_selection(self.args("org/empty#1,org/alpha#3"))
        self.assertEqual([i["key"] for i in selection["issues"]], ["alpha-3"])
        (entry,) = selection["skipped_issues"]
        self.assertEqual(entry["issue"], "org/empty#1")
        self.assertIn("empty", entry["reason"])

    def test_prior_attempt_is_named(self) -> None:
        """An issue with an open bot pull request is skipped with a reason."""
        patch.object(reads, "prior_attempt", return_value="pull_request").start()
        selection, _ = select.build_selection(self.args("org/alpha#3"))
        self.assertEqual(selection["issues"], [])
        (entry,) = selection["skipped_issues"]
        self.assertIn("pull request", entry["reason"])

    def test_issues_and_repositories_exclude_each_other(self) -> None:
        """Naming issues and repositories at once is refused."""
        with self.assertRaises(reads.SelectionError):
            select.build_selection(self.args("org/alpha#3", repositories="alpha"))

    def test_summary_lists_skipped_issues(self) -> None:
        """The step summary names each skipped issue and its reason."""
        selection, _ = select.build_selection(self.args("org/gamma#4"))
        text = outputs.summary_markdown(selection)
        self.assertIn("org/gamma#4", text)
        self.assertIn("not found", text)


class RevisionIntegrationTest(NoSubprocessCase):
    """Named pull requests, named issues with a bot PR, and the scan's revisions."""

    def setUp(self) -> None:
        """Patch the issue path to select nothing and the revision path to answer."""
        super().setUp()
        self.entries: dict[tuple[str, int], dict[str, Any]] = {}
        patch.object(
            repository_reads,
            "list_repositories",
            return_value={"alpha": repo_meta("alpha")},
        ).start()
        patch.object(reads, "search_open_issues", return_value=[]).start()
        patch.object(reads, "fetch_guidance", return_value=(b"g\n", SHA_B)).start()
        patch.object(
            reads, "bot_identity", return_value={"login": "b[bot]", "email": "e"}
        ).start()
        patch.object(revision_select, "build_entry", side_effect=self.entry).start()
        patch.object(revision_select, "scan", return_value=[]).start()
        self.addCleanup(patch.stopall)

    def entry(
        self, org: str, repo_name: str, pull_number: int, **kwargs: Any
    ) -> dict[str, Any]:
        """A revision entry for a known pull request, or a Skip."""
        found = self.entries.get((repo_name, pull_number))
        if found is None:
            raise revision_select.Skip("not an open pull request the bot opened")
        return {**found, "explicit": kwargs["explicit"]}

    @staticmethod
    def revision(repo: str, pull_number: int, issue: int) -> dict[str, Any]:
        """A minimal revision entry as ``revision_select.build_entry`` returns it."""
        return {
            "key": f"{repo}-{issue}",
            "task": "revise",
            "repository": f"org/{repo}",
            "repo_name": repo,
            "number": issue,
            "title": "T",
            "priority": None,
            "type": "Bug",
            "url": f"https://github.com/org/{repo}/issues/{issue}",
            "base_sha": SHA_A,
            "rebase_sha": SHA_B,
            "checkout_sha": SHA_B,
            "branch": f"code-monkey/issue-{issue}",
            "fork_org": "lfreleng-bot-forks",
            "fork_repository": f"lfreleng-bot-forks/{repo}",
            "pull_request": {"number": pull_number, "head_sha": SHA_A},
            "round": 1,
        }

    def args(self, *extra: str) -> Any:
        """Parsed arguments for a pull-requests run."""
        return select.build_parser().parse_args(
            [
                "--org",
                "org",
                "--output-dir",
                "out",
                "--mode",
                "pull-requests",
                "--model",
                "m",
                "--guidance-repository",
                "org/.github",
                "--fork-orgs",
                str(FORK_ORGS_PATH),
                *extra,
            ]
        )

    @unittest.expectedFailure
    def test_named_pull_requests_become_revision_entries(self) -> None:
        """Each named pull request is built explicitly; a miss is a named skip."""
        self.entries[("alpha", 41)] = self.revision("alpha", 41, 7)
        selection, _ = select.build_selection(
            self.args("--pull-requests", "org/alpha#41,org/alpha#99")
        )
        self.assertEqual([e["key"] for e in selection["issues"]], ["alpha-7"])
        self.assertIs(selection["issues"][0]["explicit"], True)
        self.assertEqual(
            selection["explicit_pull_requests"], ["org/alpha#41", "org/alpha#99"]
        )
        (skip,) = selection["skipped_issues"]
        self.assertEqual(skip["issue"], "org/alpha#99")
        self.assertEqual(selection["skipped"]["revision"], 1)
        self.assertIs(selection["revise"], True)

    @unittest.expectedFailure
    def test_pull_requests_and_repositories_exclude_each_other(self) -> None:
        """A targeted run and a scan cannot mix."""
        with self.assertRaises(reads.SelectionError):
            select.build_selection(
                self.args("--pull-requests", "org/alpha#41", "--repositories", "alpha")
            )

    @unittest.expectedFailure
    def test_named_issue_with_a_bot_pull_request_is_revised(self) -> None:
        """An issue whose earlier attempt is open becomes a round, not a skip."""
        self.entries[("alpha", 41)] = self.revision("alpha", 41, 7)
        patch.object(
            repository_reads, "repository_meta", return_value=repo_meta("alpha")
        ).start()
        patch.object(
            reads,
            "issue_details",
            return_value={
                "state": "open",
                "title": "T",
                "body": "",
                "labels": [],
                "type": "Bug",
                "assignees": [],
                "priority": None,
                "url": "u",
                "created_at": "t",
                "author_association": "MEMBER",
            },
        ).start()
        patch.object(reads, "prior_attempt", return_value="pull_request").start()
        patch.object(
            revision_select, "pull_request_for_branch", return_value=41
        ).start()
        selection, _ = select.build_selection(self.args("--issues", "org/alpha#7"))
        (entry,) = selection["issues"]
        self.assertEqual(entry["task"], "revise")
        self.assertIs(entry["explicit"], True)
        self.assertEqual(selection["skipped"]["attempted"], 0)
        # With revisions off the old behaviour stands.
        selection, _ = select.build_selection(
            self.args("--issues", "org/alpha#7", "--no-revise")
        )
        self.assertEqual(selection["issues"], [])
        self.assertEqual(selection["skipped"]["attempted"], 1)

    @unittest.expectedFailure
    def test_scan_puts_revisions_first_under_the_cap(self) -> None:
        """Revisions come before new issues and share the cap and the repository rule."""
        patch.object(
            revision_select,
            "scan",
            return_value=[self.revision("alpha", 41, 7), self.revision("beta", 9, 2)],
        ).start()
        selection, _ = select.build_selection(self.args("--max-pull-requests", "1"))
        self.assertEqual([e["key"] for e in selection["issues"]], ["alpha-7"])
        self.assertEqual(selection["skipped"]["cap"], 1)
        selection, _ = select.build_selection(self.args("--no-revise"))
        self.assertEqual(selection["issues"], [])


class ParseMaxPullRequestsTest(unittest.TestCase):
    """``parse_max_pull_requests`` accepts non-negative integers only."""

    def test_zero_lifts_cap(self) -> None:
        """Zero is valid and means unbounded."""
        self.assertEqual(inputs.parse_max_pull_requests("0"), 0)

    def test_positive(self) -> None:
        """A positive integer, with surrounding whitespace, parses."""
        self.assertEqual(inputs.parse_max_pull_requests(" 10 "), 10)

    def test_rejects_invalid(self) -> None:
        """Negatives, words and empty strings are refused."""
        for bad in ("-1", "ten", "", "1.5"):
            with self.subTest(bad=bad), self.assertRaises(reads.SelectionError):
                inputs.parse_max_pull_requests(bad)


class LoadExclusionsTest(unittest.TestCase):
    """``load_exclusions`` prefers the override and parses the file."""

    def test_override_beats_file(self) -> None:
        """A non-blank override wins even when the file would differ."""
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "ex.txt"
            file.write_text("from-file\n", encoding="utf-8")
            self.assertEqual(
                inputs.load_exclusions(file, "Over, ride"), ["over", "ride"]
            )

    def test_file_comments_and_blanks(self) -> None:
        """Comments (whole and trailing) and blank lines are ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "ex.txt"
            file.write_text(
                "# heading\n\nAlpha  # trailing\n  \nbeta, Gamma\n", encoding="utf-8"
            )
            self.assertEqual(
                inputs.load_exclusions(file, ""), ["alpha", "beta", "gamma"]
            )

    def test_no_file_no_override(self) -> None:
        """Nothing configured means nothing excluded."""
        self.assertEqual(inputs.load_exclusions(None, "  "), [])

    def test_non_file_raises(self) -> None:
        """A missing path or a directory is an operational failure."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(reads.SelectionError):
                inputs.load_exclusions(Path(tmp) / "absent", "")
            with self.assertRaises(reads.SelectionError):
                inputs.load_exclusions(Path(tmp), "")


class RankKeyTest(unittest.TestCase):
    """``rank_key`` orders by priority, then age, then name."""

    def test_priority_order(self) -> None:
        """Urgent first, unset last."""
        order = ["Low", None, "Urgent", "Medium", "High", "Nonsense"]
        ranked = sorted(
            (candidate("r", i + 1, priority=p) for i, p in enumerate(order)),
            key=select.rank_key,
        )
        self.assertEqual(
            [c["priority"] for c in ranked],
            ["Urgent", "High", "Medium", "Low", None, "Nonsense"],
        )

    def test_older_first_within_priority(self) -> None:
        """Within a priority the older issue ranks first."""
        newer = candidate("r", 1, priority="High", created_at="2026-05-01T00:00:00Z")
        older = candidate("r", 2, priority="High", created_at="2026-01-01T00:00:00Z")
        self.assertEqual(sorted([newer, older], key=select.rank_key), [older, newer])

    def test_name_breaks_ties(self) -> None:
        """Equal priority and age fall back to repository then number."""
        same = "2026-01-01T00:00:00Z"
        b2 = candidate("b", 2, created_at=same)
        a9 = candidate("a", 9, created_at=same)
        a3 = candidate("a", 3, created_at=same)
        self.assertEqual(sorted([b2, a9, a3], key=select.rank_key), [a3, a9, b2])


class CheapFilterTest(unittest.TestCase):
    """``cheap_filter`` applies everything the search payload can answer."""

    def setUp(self) -> None:
        """Common repository metadata."""
        self.repositories: dict[str, dict[str, Any]] = {
            "alpha": repo_meta("alpha"),
            "beta": repo_meta("beta"),
            ".github": repo_meta(".github"),
            "archived": repo_meta("archived", archived=True),
            "template": repo_meta("template", template=True),
            "fork": repo_meta("fork", fork=True),
            "private": repo_meta("private", public=False),
            "headless": repo_meta("headless", default_branch=None),
        }

    def run_filter(
        self,
        issues: list[dict[str, Any]],
        *,
        exclusions: set[str] | None = None,
        explicit: list[str] | None = None,
        include_dotgithub: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Run the filter with fresh counters and return both."""
        skipped = fresh_skipped()
        kept = select.cheap_filter(
            issues,
            self.repositories,
            exclusions=exclusions or set(),
            explicit=explicit or [],
            include_dotgithub=include_dotgithub,
            skipped=skipped,
        )
        return kept, skipped

    def test_drops_pull_requests(self) -> None:
        """Pull requests are dropped and counted."""
        kept, skipped = self.run_filter([search_issue("alpha", 1, isPullRequest=True)])
        self.assertEqual(kept, [])
        self.assertEqual(skipped["pull_request"], 1)
        self.assertEqual(sum(skipped.values()), 1)

    def test_repository_rules(self) -> None:
        """Dot-github, archived, template, fork, private, unknown, excluded skip."""
        issues = [
            search_issue(".github", 1),
            search_issue("archived", 2),
            search_issue("template", 3),
            search_issue("fork", 4),
            search_issue("unknown", 5),
            search_issue("beta", 6),
            search_issue("headless", 7),
            search_issue("alpha", 8),
            search_issue("private", 9),
        ]
        kept, skipped = self.run_filter(issues, exclusions={"beta"})
        self.assertEqual([c["number"] for c in kept], [8])
        self.assertEqual(skipped["repository"], 8)
        self.assertEqual(kept[0]["repository"], "org/alpha")
        self.assertEqual(kept[0]["repo_name"], "alpha")
        self.assertEqual(kept[0]["default_branch"], "main")

    def test_include_dotgithub(self) -> None:
        """The flag lets ``.github`` through."""
        kept, _ = self.run_filter([search_issue(".github", 1)], include_dotgithub=True)
        self.assertEqual(len(kept), 1)

    def test_skip_labels(self) -> None:
        """Each opt-out label drops the issue and counts it."""
        issues = [
            search_issue("alpha", i + 1, labels=[label, "bug"])
            for i, label in enumerate(sorted(select.SKIP_LABELS))
        ]
        issues.append(search_issue("alpha", 9, labels=["bug"]))
        kept, skipped = self.run_filter(issues)
        self.assertEqual([c["number"] for c in kept], [9])
        self.assertEqual(skipped["label"], len(select.SKIP_LABELS))

    def test_explicit_overrides_exclusions_and_dotgithub_only(self) -> None:
        """Naming a repository lifts the exclusion list and .github rule, no more."""
        issues = [
            search_issue("beta", 1),
            search_issue(".github", 2),
            search_issue("archived", 3),
            search_issue("template", 4),
            search_issue("fork", 5),
            search_issue("private", 6),
        ]
        kept, skipped = self.run_filter(
            issues,
            exclusions={"beta"},
            explicit=["beta", ".github", "archived", "template", "fork", "private"],
        )
        self.assertEqual([c["number"] for c in kept], [1, 2])
        self.assertEqual(skipped["repository"], 4)

    def test_explicit_still_needs_default_branch(self) -> None:
        """Even an explicit repository needs a resolvable default branch."""
        kept, skipped = self.run_filter(
            [search_issue("headless", 1)], explicit=["headless"]
        )
        self.assertEqual(kept, [])
        self.assertEqual(skipped["repository"], 1)

    def test_malformed_entry_raises(self) -> None:
        """A search entry without a repository object is an API failure."""
        with self.assertRaises(github.GitHubError):
            self.run_filter([search_issue("alpha", 1, repository="alpha")])


class EnrichTest(NoSubprocessCase):
    """``enrich`` adds details and drops untriaged or claimed issues."""

    def test_drops_no_type_and_assigned(self) -> None:
        """Issues without a Type and, by default, assigned issues are skipped."""
        details: dict[int, dict[str, Any]] = {
            1: {
                "state": "open",
                "title": "t",
                "body": "",
                "labels": [],
                "type": None,
                "assignees": [],
                "priority": None,
            },
            2: {
                "state": "open",
                "title": "t",
                "body": "",
                "labels": [],
                "type": "Bug",
                "assignees": ["someone"],
                "priority": "High",
            },
            3: {
                "state": "open",
                "title": "t",
                "body": "",
                "labels": ["chore"],
                "type": "Bug",
                "assignees": [],
                "priority": None,
            },
            4: {
                "state": "open",
                "title": "kept",
                "body": "b",
                "labels": ["bug"],
                "type": "Bug",
                "assignees": [],
                "priority": "Low",
            },
        }
        base = [
            {k: v for k, v in candidate("r", n).items() if k not in details[n]}
            for n in details
        ]
        skipped = fresh_skipped()

        def lookup(_repo: str, number: int) -> dict[str, Any]:
            """Serve the canned details for an issue number."""
            return details[number]

        with patch.object(reads, "issue_details", side_effect=lookup) as read:
            enriched = select.enrich(
                base, include_assigned=False, enabled=ALL, skipped=skipped
            )
        self.assertEqual(read.call_count, 4)
        self.assertEqual([c["number"] for c in enriched], [4])
        self.assertEqual(enriched[0]["priority"], "Low")
        self.assertEqual(enriched[0]["title"], "kept")
        self.assertEqual(skipped["no_type"], 1)
        self.assertEqual(skipped["assigned"], 1)
        self.assertEqual(skipped["label"], 1)

        skipped = fresh_skipped()
        with patch.object(reads, "issue_details", side_effect=lookup):
            enriched = select.enrich(
                base, include_assigned=True, enabled=ALL, skipped=skipped
            )
        self.assertEqual([c["number"] for c in enriched], [2, 4])
        self.assertEqual(skipped["assigned"], 0)


class ChooseTest(NoSubprocessCase):
    """``choose`` keeps one per repository, runs the checks and caps."""

    def setUp(self) -> None:
        """Patch every read ``choose`` performs with benign defaults."""
        super().setUp()
        self.prior = patch.object(reads, "prior_attempt", return_value=None).start()
        self.linked = patch.object(
            reads, "has_open_linked_pr", return_value=False
        ).start()
        self.head = patch.object(reads, "branch_head", return_value=SHA_A).start()
        self.comments = patch.object(
            reads, "filtered_comments", return_value=([], 0, False)
        ).start()
        self.addCleanup(patch.stopall)

    def choose(
        self,
        ranked: list[dict[str, Any]],
        *,
        max_pull_requests: int,
        skipped: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Run ``choose`` with the fixture fork mapping."""
        return select.choose(
            ranked,
            fork_orgs=FORK_ORGS,
            max_pull_requests=max_pull_requests,
            skipped=skipped,
        )

    def test_one_per_repository_and_shape(self) -> None:
        """The second issue of a repository is dropped; the chosen entry is shaped."""
        ranked = [candidate("alpha", 3), candidate("alpha", 4), candidate("beta", 5)]
        skipped = fresh_skipped()
        chosen = self.choose(ranked, max_pull_requests=10, skipped=skipped)
        self.assertEqual([c["number"] for c in chosen], [3, 5])
        self.assertEqual(skipped["one_per_repo"], 1)
        first = chosen[0]
        self.assertEqual(first["key"], "alpha-3")
        self.assertEqual(first["branch"], "code-monkey/issue-3")
        self.assertEqual(first["base_sha"], SHA_A)
        self.assertEqual(first["fork_org"], "lfreleng-bot-forks")
        self.assertEqual(first["fork_repository"], "lfreleng-bot-forks/alpha")
        self.assertEqual(first["comments"], [])
        self.assertEqual(first["comments_dropped"], 0)
        self.assertNotIn("assignees", first)
        self.head.assert_any_call("org/alpha", "main")
        self.prior.assert_any_call(
            "org/alpha", "lfreleng-bot-forks/alpha", "code-monkey/issue-3"
        )

    def test_fork_org_follows_the_target_owner(self) -> None:
        """A mapped owner gets its own pool; the owner itself is refused."""
        onap = candidate("alpha", 3)
        onap["repository"] = "ONAP/alpha"
        chosen = self.choose([onap], max_pull_requests=0, skipped=fresh_skipped())
        self.assertEqual(chosen[0]["fork_org"], "lfreleng-bot-forks-onap")
        self.assertEqual(chosen[0]["fork_repository"], "lfreleng-bot-forks-onap/alpha")
        own = candidate("alpha", 4)
        own["repository"] = "lfreleng-bot-forks/alpha"
        with self.assertRaises(reads.SelectionError):
            self.choose([own], max_pull_requests=0, skipped=fresh_skipped())

    def test_cap(self) -> None:
        """Survivors beyond the cap count as ``cap``; zero lifts the cap."""
        ranked = [candidate(f"r{i}", i) for i in range(1, 6)]
        skipped = fresh_skipped()
        chosen = self.choose(ranked, max_pull_requests=2, skipped=skipped)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(skipped["cap"], 3)

        skipped = fresh_skipped()
        chosen = self.choose(ranked, max_pull_requests=0, skipped=skipped)
        self.assertEqual(len(chosen), 5)
        self.assertEqual(skipped["cap"], 0)

    def test_matrix_limit_bounds_even_unbounded_runs(self) -> None:
        """Zero and oversized caps both stop at the Actions matrix limit."""
        ranked = [candidate(f"r{i}", i) for i in range(1, select.MATRIX_LIMIT + 4)]
        for requested in (0, select.MATRIX_LIMIT + 50):
            with self.subTest(max_pull_requests=requested):
                skipped = fresh_skipped()
                chosen = self.choose(
                    ranked, max_pull_requests=requested, skipped=skipped
                )
                self.assertEqual(len(chosen), select.MATRIX_LIMIT)
                self.assertEqual(skipped["cap"], 3)

    def test_selection_byte_budget(self) -> None:
        """The cumulative serialised size stops selection under the verifier cap."""
        big = candidate("big", 1)
        big["body"] = "x" * 1000
        small = candidate("small", 2)
        ranked = [big, small, candidate("third", 3)]
        skipped = fresh_skipped()
        with patch.object(select, "MAX_SELECTION_BYTES", 1600):
            chosen = self.choose(ranked, max_pull_requests=0, skipped=skipped)
        # The first entry fits alone; the second would push past the budget
        # and the third is refused without further reads.
        self.assertEqual([c["number"] for c in chosen], [1])
        self.assertEqual(skipped["cap"], 2)

    def test_prior_attempt_and_linked_pr(self) -> None:
        """A prior bot branch or an open linked PR skips the issue."""
        self.prior.side_effect = attempted_issue_one
        self.linked.side_effect = linked_issue_two
        ranked = [candidate("a", 1), candidate("b", 2), candidate("c", 3)]
        skipped = fresh_skipped()
        chosen = self.choose(ranked, max_pull_requests=0, skipped=skipped)
        self.assertEqual([c["number"] for c in chosen], [3])
        self.assertEqual(skipped["attempted"], 1)
        self.assertEqual(skipped["linked_pr"], 1)

    def test_orphan_fork_branch_is_selected_for_resume(self) -> None:
        """A fork branch with no pull request keeps the issue, flagged to resume."""
        self.prior.side_effect = orphan_issue_one
        ranked = [candidate("a", 1), candidate("b", 2)]
        skipped = fresh_skipped()
        chosen = self.choose(ranked, max_pull_requests=0, skipped=skipped)
        self.assertEqual(
            [(c["number"], c["resume"]) for c in chosen], [(1, True), (2, False)]
        )
        self.assertEqual(skipped["attempted"], 0)

    def test_skipped_repository_stays_available(self) -> None:
        """A skipped attempt does not claim the repository slot."""
        self.prior.side_effect = attempted_issue_one
        ranked = [candidate("a", 1), candidate("a", 2)]
        chosen = self.choose(ranked, max_pull_requests=0, skipped=fresh_skipped())
        self.assertEqual([c["number"] for c in chosen], [2])


def sample_selection(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """A minimal selection document around the given issues."""
    return {
        "schema": 1,
        "org": "org",
        "generated_at": "2026-09-18T00:00:00Z",
        "mode": "pull-requests",
        "dry_run": True,
        "model": "claude-opus-5.5",
        "categories": sorted(categories.ALL_CATEGORIES),
        "max_pull_requests": 10,
        "bot": {"login": "code-monkey[bot]", "email": "x@example", "placeholder": True},
        "guidance": {},
        "exclusions": ["beta", "gamma"],
        "explicit_repositories": [],
        "candidates_seen": 3,
        "skipped": fresh_skipped(),
        "issues": issues,
    }


class CategoryFilterTest(NoSubprocessCase):
    """``enrich`` keeps an issue when any of its categories is enabled."""

    def run_enrich(
        self, labels: list[str], issue_type: str | None, enabled: frozenset[str]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Enrich one candidate carrying the given labels and Type."""
        details: dict[str, Any] = {
            "state": "open",
            "title": "t",
            "body": "",
            "labels": labels,
            "type": issue_type,
            "assignees": [],
            "priority": None,
        }
        base = [{k: v for k, v in candidate("r", 1).items() if k not in details}]
        skipped = fresh_skipped()
        with patch.object(reads, "issue_details", return_value=details):
            kept = select.enrich(
                base, include_assigned=False, enabled=enabled, skipped=skipped
            )
        return kept, skipped

    def test_docs_only(self) -> None:
        """Only documentation issues pass a docs-only run; others count as skipped."""
        docs = frozenset({"docs"})
        kept, _ = self.run_enrich(["documentation"], "Task", docs)
        self.assertEqual(kept[0]["categories"], ["docs"])
        kept, skipped = self.run_enrich(["bug"], "Bug", docs)
        self.assertEqual(kept, [])
        self.assertEqual(skipped["category"], 1)

    def test_type_places_bugs_and_features(self) -> None:
        """A Bug Type counts as a bug even without the label."""
        kept, _ = self.run_enrich([], "Bug", frozenset({"bugs"}))
        self.assertEqual(kept[0]["categories"], ["bugs"])

    def test_any_enabled_category_is_enough(self) -> None:
        """An issue in two categories passes when either is enabled."""
        kept, _ = self.run_enrich(["CI", "code-quality"], "Task", frozenset({"ci"}))
        self.assertEqual(kept[0]["categories"], ["ci", "code_quality"])

    def test_uncategorised_issues_are_other(self) -> None:
        """No category label and no bug/feature Type means ``other``."""
        kept, _ = self.run_enrich([], "Task", frozenset({"other"}))
        self.assertEqual(kept[0]["categories"], ["other"])
        kept, _ = self.run_enrich([], "Task", frozenset({"bugs"}))
        self.assertEqual(kept, [])


class ClosedSinceSearchTest(NoSubprocessCase):
    """An issue closed between the search and the per-issue read is skipped."""

    def test_closed_issue_is_skipped_and_counted(self) -> None:
        """The fresh read wins over the stale search result."""
        details: dict[str, Any] = {
            "state": "closed",
            "title": "t",
            "body": "",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": "High",
        }
        base = [{k: v for k, v in candidate("r", 1).items() if k not in details}]
        skipped = fresh_skipped()
        with patch.object(reads, "issue_details", return_value=details):
            kept = select.enrich(
                base, include_assigned=False, enabled=ALL, skipped=skipped
            )
        self.assertEqual(kept, [])
        self.assertEqual(skipped["closed"], 1)


class ParseCategoriesTest(unittest.TestCase):
    """``parse_categories`` reads the list the caller passes."""

    def test_all_and_lists(self) -> None:
        """``all`` expands; commas and spaces both separate names."""
        self.assertEqual(categories.parse_categories("all"), ALL)
        self.assertEqual(
            categories.parse_categories("docs, ci code_quality"),
            frozenset({"docs", "ci", "code_quality"}),
        )

    def test_rejects_empty_and_unknown(self) -> None:
        """Nothing selected, or an unknown name, is an error."""
        for bad in ("", " , ", "docs,security"):
            with (
                self.subTest(bad=bad),
                self.assertRaises(categories.CategoryError),
            ):
                categories.parse_categories(bad)


class WriteOutputsTest(unittest.TestCase):
    """``write_outputs`` and ``summary_markdown`` produce the consumed files."""

    def chosen_issue(self) -> dict[str, Any]:
        """A chosen issue with a pipe in the title."""
        return {
            **candidate("alpha", 3, priority="High"),
            "title": "Fix a | thing",
            "key": "alpha-3",
            "base_sha": SHA_A,
            "branch": "code-monkey/issue-3",
            "fork_org": "lfreleng-bot-forks",
            "fork_repository": "lfreleng-bot-forks/alpha",
            "comments": [],
            "comments_dropped": 0,
        }

    @unittest.expectedFailure
    def test_files_written(self) -> None:
        """Every output file exists with the documented content."""
        selection = sample_selection([self.chosen_issue()])
        guidance = b"# Guidance\n\xe2\x9c\x93\n"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "out"
            outputs.write_outputs(out, selection, guidance)
            written = json.loads((out / "selection.json").read_text(encoding="utf-8"))
            self.assertEqual(written, selection)
            matrix = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
            self.assertEqual(
                matrix,
                {
                    "include": [
                        {
                            "key": "alpha-3",
                            "repository": "org/alpha",
                            "repo_name": "alpha",
                            "number": 3,
                            "base_sha": SHA_A,
                            "branch": "code-monkey/issue-3",
                            "fork_org": "lfreleng-bot-forks",
                            "fork_repository": "lfreleng-bot-forks/alpha",
                            "task": "issue",
                            "checkout_sha": SHA_A,
                            "head_sha": "",
                            "pull_number": 0,
                        }
                    ]
                },
            )
            self.assertEqual((out / "agents.md").read_bytes(), guidance)
            self.assertEqual(
                (out / "excluded-repos.txt").read_text(encoding="utf-8"),
                "beta\ngamma\n",
            )
            summary = (out / "selection-summary.md").read_text(encoding="utf-8")
        self.assertIn("## Issue selection", summary)
        self.assertIn(
            "| [alpha#3](https://github.com/org/alpha/issues/3) | High | Bug "
            "| Fix a \\| thing |",
            summary,
        )
        self.assertIn("| cap | 0 |", summary)
        self.assertNotIn("nothing to work", summary)

    def test_title_cannot_inject_rows(self) -> None:
        """Line breaks in a public title stay inside its table cell."""
        issue = self.chosen_issue()
        issue["title"] = "Real\n| evil | row | x | y |\n## Heading"
        summary = outputs.summary_markdown(sample_selection([issue]))
        rows = [line for line in summary.splitlines() if "alpha#3" in line]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("\n## Heading", summary)
        self.assertIn("Real \\| evil \\| row", rows[0])

    def test_empty_summary(self) -> None:
        """No issues renders the placeholder row and an em dash for no priority."""
        summary = outputs.summary_markdown(sample_selection([]))
        self.assertIn("| — | — | — | nothing to work |", summary)
        self.assertIn("selected: 0.", summary)
        issue = {**self.chosen_issue(), "priority": None}
        summary = outputs.summary_markdown(sample_selection([issue]))
        self.assertIn("| — | Bug |", summary)


class MainTest(NoSubprocessCase):
    """End-to-end ``main`` with every read patched."""

    def patch_reads(self) -> None:
        """Install the read doubles a successful run needs."""
        repositories = {"alpha": repo_meta("alpha"), "beta": repo_meta("beta")}
        issues = [
            search_issue("alpha", 1),
            search_issue("beta", 2, labels=["chore"]),
            search_issue("alpha", 3),
        ]
        details: dict[str, Any] = {
            "state": "open",
            "title": "Title",
            "body": "Body",
            "labels": ["bug"],
            "type": "Bug",
            "assignees": [],
            "priority": "Urgent",
        }
        patch.object(
            repository_reads, "list_repositories", return_value=repositories
        ).start()
        patch.object(reads, "search_open_issues", return_value=issues).start()
        patch.object(reads, "issue_details", return_value=details).start()
        patch.object(reads, "prior_attempt", return_value=None).start()
        patch.object(reads, "has_open_linked_pr", return_value=False).start()
        patch.object(reads, "branch_head", return_value=SHA_B).start()
        patch.object(
            reads, "filtered_comments", return_value=([{"body": "hi"}], 2, False)
        ).start()
        patch.object(
            reads, "fetch_guidance", return_value=(b"guidance\n", SHA_A)
        ).start()
        patch.object(
            reads,
            "bot_identity",
            return_value={
                "login": "b[bot]",
                "email": "1+b[bot]@x",
                "placeholder": False,
            },
        ).start()
        self.addCleanup(patch.stopall)

    def test_end_to_end(self) -> None:
        """The pipeline writes a selection reflecting the patched reads."""
        self.patch_reads()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                select.main(
                    [
                        "--org",
                        "org",
                        "--output-dir",
                        str(out),
                        "--mode",
                        "pull-requests",
                        "--model",
                        "claude-opus-5.5",
                        "--dry-run",
                        "--guidance-repository",
                        "org/.github",
                        "--fork-orgs",
                        str(FORK_ORGS_PATH),
                        "--max-pull-requests",
                        "5",
                        "--exclude-repos",
                        "gamma",
                        "--bot-slug",
                        "b",
                    ]
                )
            selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
            self.assertEqual((out / "agents.md").read_bytes(), b"guidance\n")
            self.assertEqual(
                (out / "excluded-repos.txt").read_text(encoding="utf-8"), "gamma\n"
            )
        self.assertIn("Selected 1 issue(s) from 3 open", stderr.getvalue())
        self.assertEqual(selection["schema"], select.SCHEMA)
        self.assertTrue(selection["dry_run"])
        self.assertEqual(selection["bot"]["login"], "b[bot]")
        self.assertEqual(selection["guidance"]["commit"], SHA_A)
        self.assertEqual(
            selection["guidance"]["sha256"],
            hashlib.sha256(b"guidance\n").hexdigest(),
        )
        self.assertEqual(selection["exclusions"], ["gamma"])
        self.assertEqual(selection["candidates_seen"], 3)
        self.assertEqual(selection["skipped"]["label"], 1)
        self.assertEqual(selection["skipped"]["one_per_repo"], 1)
        (issue,) = selection["issues"]
        self.assertEqual(issue["key"], "alpha-1")
        self.assertEqual(issue["fork_org"], "lfreleng-bot-forks")
        self.assertEqual(issue["fork_repository"], "lfreleng-bot-forks/alpha")
        self.assertEqual(issue["base_sha"], SHA_B)
        self.assertEqual(issue["comments_dropped"], 2)
        self.assertEqual(issue["priority"], "Urgent")

    def test_selection_error_exits_one(self) -> None:
        """A ``SelectionError`` becomes exit status 1 with a prefixed message."""
        self.patch_reads()
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                select.main(
                    [
                        "--org",
                        "org",
                        "--output-dir",
                        tmp,
                        "--mode",
                        "select",
                        "--model",
                        "m",
                        "--guidance-repository",
                        "org/.github",
                        "--fork-orgs",
                        str(FORK_ORGS_PATH),
                        "--repositories",
                        "missing",
                    ]
                )
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("select issues: ", stderr.getvalue())
            self.assertIn("org/missing not found", stderr.getvalue())
            self.assertFalse((Path(tmp) / "selection.json").exists())

    def test_github_error_exits_one(self) -> None:
        """A ``GitHubError`` from a read takes the same failure path."""
        self.patch_reads()
        patch.object(
            repository_reads,
            "list_repositories",
            side_effect=github.GitHubError("boom (HTTP 500)"),
        ).start()
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                select.main(
                    [
                        "--org",
                        "org",
                        "--output-dir",
                        tmp,
                        "--mode",
                        "select",
                        "--model",
                        "m",
                        "--guidance-repository",
                        "org/.github",
                        "--fork-orgs",
                        str(FORK_ORGS_PATH),
                    ]
                )
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("select issues: 'boom (HTTP 500)'", stderr.getvalue())

    def test_bad_fork_orgs_file_exits_one(self) -> None:
        """A malformed fork mapping stops the run before any read."""
        self.patch_reads()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "forks.json"
            bad.write_text('{"default": "evil-org", "organisations": {}}')
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                select.main(
                    [
                        "--org",
                        "org",
                        "--output-dir",
                        tmp,
                        "--mode",
                        "select",
                        "--model",
                        "m",
                        "--guidance-repository",
                        "org/.github",
                        "--fork-orgs",
                        str(bad),
                    ]
                )
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("fork organisation", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
