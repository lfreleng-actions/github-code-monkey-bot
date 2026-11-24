# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Resolving the base tree's protected symlinks, without a clone (#216)."""

from __future__ import annotations

import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
protected_links = import_module("protected_links")


def reader(links: dict[str, str]) -> Any:
    """A ``read_target`` over a base tree's symlinks, path to target."""
    return links.get


class ResolveLinkTest(unittest.TestCase):
    """``resolve_link`` follows a base tree's symlinks the way git stores them."""

    def test_relative_to_the_link_directory(self) -> None:
        """A target is read from the link's own directory."""
        links = {".github/zizmor.yml": "../docs/zizmor.yml"}
        resolved, followed = protected_links.resolve_link(
            ".github/zizmor.yml", reader(links)
        )
        self.assertEqual(resolved, "docs/zizmor.yml")
        self.assertEqual(followed, [".github/zizmor.yml"])

    def test_chain_is_followed(self) -> None:
        """Each link on the way is followed and recorded."""
        links = {"AGENTS.md": "docs/a.md", "docs/a.md": "../text/a.md"}
        resolved, followed = protected_links.resolve_link("AGENTS.md", reader(links))
        self.assertEqual(resolved, "text/a.md")
        self.assertEqual(followed, ["AGENTS.md", "docs/a.md"])

    def test_linked_directory_on_the_way(self) -> None:
        """A directory link inside a target resolves before the rest."""
        links = {".claude": "tooling/claude", "tooling": "vendor/tools"}
        resolved, followed = protected_links.resolve_link(".claude", reader(links))
        self.assertEqual(resolved, "vendor/tools/claude")
        self.assertEqual(followed, [".claude", "tooling"])

    def test_unresolvable_links(self) -> None:
        """Absolute, escaping, looping and over-long chains are refused."""
        chain = {
            f"l{i}": f"l{i + 1}" for i in range(protected_links.MAX_LINK_CHAIN + 1)
        }
        cases = {
            "absolute": {"AGENTS.md": "/etc/agents.md"},
            "empty": {"AGENTS.md": ""},
            "escape": {"AGENTS.md": "../agents.md"},
            "nested escape": {"docs/AGENTS.md": "../x/../../agents.md"},
            "loop": {"AGENTS.md": "a.md", "a.md": "AGENTS.md"},
            "too long": chain,
        }
        for name, links in cases.items():
            with self.subTest(name), self.assertRaises(protected_links.LinkError):
                protected_links.resolve_link(next(iter(links)), reader(links))

    def test_link_past_the_bound_is_followed(self) -> None:
        """The link that passes the bound is reported with the rest."""
        bound = protected_links.MAX_LINK_CHAIN
        links = {"AGENTS.md": "a1", **{f"a{i}": f"a{i + 1}" for i in range(1, bound)}}
        links[f"a{bound}"] = "target.md"
        with self.assertRaises(protected_links.LinkError) as caught:
            protected_links.resolve_link("AGENTS.md", reader(links))
        self.assertEqual(caught.exception.followed[-1], f"a{bound}")
        self.assertEqual(caught.exception.resolved, "target.md")

    def test_loop_resolves_to_nothing(self) -> None:
        """A loop ends where a link repeats with nothing consumed between."""
        cases = {
            "growing": {"AGENTS.md": "a.md", "a.md": "a.md/x"},
            "pair": {"AGENTS.md": "a.md", "a.md": "b.md", "b.md": "a.md"},
            "through a directory": {".claude": "d/x", "d": "e", "e": "d"},
        }
        for name, links in cases.items():
            with self.subTest(name):
                with self.assertRaises(protected_links.LinkError) as caught:
                    protected_links.resolve_link(next(iter(links)), reader(links))
                self.assertIsNone(caught.exception.resolved)
                self.assertIn("loops", str(caught.exception))

    def test_repeated_link_is_not_a_loop(self) -> None:
        """A link reached again after the walk moved on is a finite hop."""
        links = {".claude": "a/a/settings", "a": "."}
        resolved, followed = protected_links.resolve_link(".claude", reader(links))
        self.assertEqual(resolved, "settings")
        self.assertEqual(followed, [".claude", "a", "a"])

    def test_repeated_link_past_the_bound_keeps_its_target(self) -> None:
        """A finite chain past the bound through one link reports its end."""
        bound = protected_links.MAX_LINK_CHAIN
        links = {".claude": "a/" * (bound + 1) + "settings", "a": "."}
        with self.assertRaises(protected_links.LinkError) as caught:
            protected_links.resolve_link(".claude", reader(links))
        self.assertEqual(caught.exception.resolved, "settings")

    def test_chain_past_the_context_cap_is_refused(self) -> None:
        """A chain of distinct links is followed only so far."""
        cap = protected_links.MAX_LINK_CONTEXTS
        links = {f"l{i}": f"l{i + 1}" for i in range(cap + 1)}
        with self.assertRaises(protected_links.policy.Rejection):
            protected_links.resolve_link("l0", reader(links))

    def test_directories_climbed_out_of(self) -> None:
        """Each directory a .. steps out of is recorded, the link's own too."""
        climbed: list[str] = []
        links = {"docs/AGENTS.md": "../a/b/../../c.md"}
        protected_links.resolve_link("docs/AGENTS.md", reader(links), climbed)
        self.assertEqual(climbed, ["docs", "a/b", "a"])
        self.assertEqual(
            protected_links.needed_directories(climbed, ["docs/AGENTS.md"]),
            ["a", "a/b"],
        )

    def test_failure_carries_the_links_followed(self) -> None:
        """A failed resolution reports every link it reached."""
        links = {"AGENTS.md": "a.md", "a.md": "b.md", "b.md": "../x.md"}
        with self.assertRaises(protected_links.LinkError) as caught:
            protected_links.resolve_link("AGENTS.md", reader(links))
        self.assertEqual(caught.exception.followed, ["AGENTS.md", "a.md", "b.md"])


class GuardsProtectedTest(unittest.TestCase):
    """``guards_protected`` picks the links whose meaning is protected."""

    def test_protected_files_and_directories(self) -> None:
        """A protected file, or a directory a protected path lies under."""
        for path in (
            "AGENTS.md",
            "docs/CLAUDE.md",
            ".pre-commit-config.yaml",
            ".claude",
            "pkg/.cursor",
            "LICENSES",
            ".github",
            ".github/prompts",
        ):
            with self.subTest(path=path):
                self.assertTrue(protected_links.guards_protected(path))

    def test_ordinary_paths(self) -> None:
        """A link standing for an ordinary file or directory guards nothing."""
        for path in ("docs/link.md", "docs", "src/.github", "tooling"):
            with self.subTest(path=path):
                self.assertFalse(protected_links.guards_protected(path))


class LinkGuardTest(unittest.TestCase):
    """``link_guard`` names the link that makes a changed path protected."""

    def guard(self, links: dict[str, str]) -> Any:
        """The guard for a base tree holding ``links``."""
        return protected_links.link_guard(links, reader(links))

    def test_targets_of_protected_links(self) -> None:
        """Each example in #216 names its link; neighbours stay editable."""
        guard = self.guard(
            {
                ".github/zizmor.yml": "../docs/zizmor.yml",
                "AGENTS.md": "docs/guide.md",
                ".pre-commit-config.yaml": "config/pre-commit.yaml",
                ".claude": "tooling/claude",
            }
        )
        expected = {
            "docs/zizmor.yml": ".github/zizmor.yml",
            "docs/guide.md": "AGENTS.md",
            "config/pre-commit.yaml": ".pre-commit-config.yaml",
            "tooling/claude/settings.json": ".claude",
            "tooling/claude": ".claude",
        }
        for path, link in expected.items():
            with self.subTest(path=path):
                self.assertEqual(guard.cause(path), f"target of {link}")
        for path in ("docs/other.md", "tooling/other.json", "tooling/claude.md"):
            with self.subTest(path=path):
                self.assertIsNone(guard.cause(path))

    def test_links_on_a_chain(self) -> None:
        """Every link a chain passes through is pinned with its target."""
        guard = self.guard({"AGENTS.md": "a.md", "a.md": "b.md"})
        self.assertEqual(guard.cause("a.md"), "target of AGENTS.md")
        self.assertEqual(guard.cause("b.md"), "target of AGENTS.md")

    def test_a_linked_directory_itself_is_protected(self) -> None:
        """Deleting the link to a protected directory changes its meaning."""
        guard = self.guard({".claude": "tooling/claude"})
        self.assertEqual(guard.cause(".claude"), ".claude is a protected link")

    def test_link_inside_a_linked_directory(self) -> None:
        """A link in a linked directory's target protects its own target."""
        guard = self.guard(
            {
                ".claude": "tooling/claude",
                "tooling/claude/settings.json": "../../x.json",
                "tooling/notes.md": "../README.md",
            }
        )
        for path in ("x.json", "tooling/claude/settings.json"):
            with self.subTest(path=path):
                self.assertEqual(guard.cause(path), "target of .claude")
        self.assertIsNone(guard.cause("README.md"))

    def test_unprotected_link_adds_nothing(self) -> None:
        """A link whose own path is not protected protects no target."""
        guard = self.guard({"docs/link.md": "../README.md", "docs": "site"})
        for path in ("README.md", "site/guide.md", "docs/link.md", "docs"):
            with self.subTest(path=path):
                self.assertIsNone(guard.cause(path))

    def test_unresolvable_link_pins_its_first_path(self) -> None:
        """A loop names the link and its first path, and refuses both."""
        guard = self.guard({"AGENTS.md": "a.md", "a.md": "AGENTS.md"})
        for path in ("a.md", "AGENTS.md"):
            with self.subTest(path=path):
                cause = guard.cause(path)
                self.assertIsNotNone(cause)
                self.assertIn("AGENTS.md -> a.md", cause or "")

    def test_unresolvable_link_pins_every_hop(self) -> None:
        """Each link reached before a loop is pinned, not only the first."""
        cases = {
            "AGENTS.md": {"AGENTS.md": "a.md", "a.md": "b.md", "b.md": "a.md"},
            ".claude": {".claude": "x", "x": "y", "y": "x"},
        }
        for link, links in cases.items():
            guard = self.guard(links)
            for path in links:
                with self.subTest(link=link, path=path):
                    self.assertIn(f"{link} -> ", guard.cause(path) or "")

    def test_link_past_the_bound_and_its_target_are_pinned(self) -> None:
        """Neither the last link of an over-long chain nor its file can change."""
        bound = protected_links.MAX_LINK_CHAIN
        links = {"AGENTS.md": "a1", **{f"a{i}": f"a{i + 1}" for i in range(1, bound)}}
        links[f"a{bound}"] = "target.md"
        guard = self.guard(links)
        for path in (*links, "target.md"):
            with self.subTest(path=path):
                self.assertIn("AGENTS.md -> a1", guard.cause(path) or "")

    def test_link_reached_under_two_names_counts_under_both(self) -> None:
        """A protected link inside a linked directory keeps that name too."""
        guard = self.guard({".claude": "config", "config/AGENTS.md": "../settings"})
        self.assertEqual(guard.cause("settings/options.json"), "target of .claude")
        self.assertIsNotNone(guard.cause("settings"))

    def test_directory_alias_cycles_end(self) -> None:
        """A link to its own ancestor is resolved a bounded number of times."""
        for links in (
            {".claude": "."},
            {"AGENTS.md": "."},
            {"docs/AGENTS.md": "..", "docs/CLAUDE.md": "../docs"},
        ):
            with self.subTest(links=links):
                guard = self.guard(links)
                for path in links:
                    self.assertIsNotNone(guard.cause(path))

    def test_too_many_aliases_are_refused(self) -> None:
        """Links that alias each other without bound refuse the proposal."""
        links = {f"a/{name}": ".." for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")}
        with self.assertRaises(protected_links.policy.Rejection):
            self.guard(links)

    def test_alias_cap_bounds_pending_work(self) -> None:
        """The cap counts names as they are queued, not as they resolve."""
        links = {f"d{i}/AGENTS.md": ".." for i in range(64)}
        calls: list[str] = []

        def read(path: str) -> str | None:
            calls.append(path)
            return links.get(path)

        with (
            mock.patch.object(protected_links, "MAX_LINK_CONTEXTS", 100),
            self.assertRaises(protected_links.policy.Rejection),
        ):
            protected_links.link_guard(links, read)
        self.assertLess(len(calls), 10)

    def test_directory_climbed_out_of_is_protected(self) -> None:
        """A directory a link steps out of with .. cannot change."""
        guard = self.guard(
            {"AGENTS.md": "scratch/../instructions.txt", "docs/CLAUDE.md": "../x.md"}
        )
        for path in ("scratch", "scratch/keep.txt", "scratch/sub/a.txt"):
            with self.subTest(path=path):
                self.assertEqual(guard.cause(path), "AGENTS.md passes through scratch")
        for path in ("scratchpad/a.txt", "docs/other.md"):
            with self.subTest(path=path):
                self.assertIsNone(guard.cause(path))

    def test_escaping_link_pins_itself(self) -> None:
        """A link out of the repository has no first path inside it."""
        guard = self.guard({".claude": "../claude"})
        self.assertIn(".claude -> ../claude", guard.cause(".claude") or "")
        self.assertIsNone(guard.cause("claude/settings.json"))

    def test_path_holding_a_protected_one_is_protected(self) -> None:
        """A submodule holding a target or a pinned path cannot change."""
        guard = self.guard(
            {
                "AGENTS.md": "vendor/rules/instructions.md",
                "CLAUDE.md": "ext/a.md",
                "ext/a.md": "../CLAUDE.md",
            }
        )
        expected = {
            "vendor": "holds vendor/rules/instructions.md, target of AGENTS.md",
            "vendor/rules": "holds vendor/rules/instructions.md, target of AGENTS.md",
            "ext": "holds ext/a.md, CLAUDE.md -> ext/a.md cannot be resolved: it loops",
        }
        for path, cause in expected.items():
            with self.subTest(path=path):
                self.assertEqual(guard.cause(path), cause)
        for path in ("vendor/other", "vendor/rules2", "extra"):
            with self.subTest(path=path):
                self.assertIsNone(guard.cause(path))

    def test_path_holding_protected_ones_under_a_linked_directory(self) -> None:
        """Under a linked .github, what holds a protected directory counts."""
        guard = self.guard({".github": "config"})
        self.assertEqual(guard.cause("config/instructions"), "target of .github")
        self.assertIsNone(guard.cause("config/workflows"))


if __name__ == "__main__":
    unittest.main()
