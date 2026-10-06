# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fork-organisation mapping: loading, resolving and the CLI."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
forks = import_module("fork_orgs")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "fork-orgs.json"


def mapping(**overrides: Any) -> dict[str, Any]:
    """A valid mapping document."""
    data: dict[str, Any] = {
        "description": "ignored",
        "default": "lfreleng-bot-forks",
        "organisations": {"onap": "lfreleng-bot-forks-onap"},
    }
    data.update(overrides)
    return data


class MappingCase(unittest.TestCase):
    """Writes mapping files into a temporary directory."""

    def setUp(self) -> None:
        """Create the directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def write(self, data: Any) -> Path:
        """Write ``data`` as JSON and return the path."""
        path = self.root / "fork-orgs.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class LoadTest(MappingCase):
    """``load`` accepts the documented shape and nothing else."""

    def test_repository_config_loads(self) -> None:
        """The shipped file is valid and every value is a bot pool."""
        config = forks.load(CONFIG_PATH)
        self.assertTrue(forks.ALLOWED_RE.fullmatch(config["default"]))
        for owner, pool in config["organisations"].items():
            self.assertTrue(forks.ALLOWED_RE.fullmatch(pool), owner)

    def test_description_is_dropped(self) -> None:
        """Only the default and the mapping survive loading."""
        config = forks.load(self.write(mapping()))
        self.assertEqual(
            config,
            {
                "default": "lfreleng-bot-forks",
                "organisations": {"onap": "lfreleng-bot-forks-onap"},
            },
        )

    def test_allow_pattern(self) -> None:
        """Values must match the fixed pattern, whatever the file says."""
        for pool in ("lfreleng-bot-forks", "lfreleng-bot-forks-oransc"):
            with self.subTest(pool=pool):
                forks.load(self.write(mapping(default=pool)))
        for bad in (
            "evil-org",
            "lfreleng-bot-forks-",
            "lfreleng-bot-forks-ONAP",
            "lfreleng-bot-forks-a-b",
            "xlfreleng-bot-forks",
            "lfreleng-bot-forks/x",
            "",
            7,
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(forks.ForkOrgError, "fork organisation"),
            ):
                forks.load(self.write(mapping(default=bad)))
            with (
                self.subTest(bad=bad, where="organisations"),
                self.assertRaisesRegex(forks.ForkOrgError, "fork organisation"),
            ):
                forks.load(self.write(mapping(organisations={"x": bad})))

    def test_shape_is_validated(self) -> None:
        """A non-object, a missing default or a non-object mapping is refused."""
        cases: list[Any] = [
            [],
            "x",
            {"default": "lfreleng-bot-forks"},
            mapping(organisations=[]),
        ]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(forks.ForkOrgError):
                forks.load(self.write(bad))

    def test_keys_are_lower_case_owners(self) -> None:
        """Keys must be lower-case owner names."""
        for bad in ("ONAP", "a/b", "-x", "", "a b"):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(forks.ForkOrgError, "lower-case owner"),
            ):
                forks.load(
                    self.write(mapping(organisations={bad: "lfreleng-bot-forks"}))
                )

    def test_self_mapping_is_refused(self) -> None:
        """A pool cannot be its own fork organisation."""
        bad = mapping(organisations={"lfreleng-bot-forks": "lfreleng-bot-forks"})
        with self.assertRaisesRegex(forks.ForkOrgError, "own fork organisation"):
            forks.load(self.write(bad))

    def test_unreadable_and_malformed_files(self) -> None:
        """A missing path, invalid JSON and an oversized file are errors."""
        with self.assertRaisesRegex(forks.ForkOrgError, "cannot read"):
            forks.load(self.root / "absent.json")
        broken = self.root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(forks.ForkOrgError, "not valid JSON"):
            forks.load(broken)
        huge = self.root / "huge.json"
        huge.write_bytes(b" " * (forks.MAX_BYTES + 1))
        with self.assertRaisesRegex(forks.ForkOrgError, "exceeds"):
            forks.load(huge)


class ResolveTest(MappingCase):
    """``resolve`` maps an owner to its pool, never to itself."""

    def test_mapped_owner_is_case_insensitive(self) -> None:
        """Owners fold to lower case before the lookup."""
        config = forks.load(self.write(mapping()))
        for owner in ("onap", "ONAP", "Onap"):
            with self.subTest(owner=owner):
                self.assertEqual(
                    forks.resolve(config, owner), "lfreleng-bot-forks-onap"
                )

    def test_unmapped_owner_uses_the_default(self) -> None:
        """Any other owner falls back to the default pool."""
        config = forks.load(self.write(mapping()))
        self.assertEqual(
            forks.resolve(config, "lfreleng-actions"), "lfreleng-bot-forks"
        )

    def test_pool_as_target_is_refused(self) -> None:
        """A target inside the pool would fork into itself."""
        config = forks.load(self.write(mapping()))
        for owner in ("lfreleng-bot-forks", "LFreleng-Bot-Forks"):
            with (
                self.subTest(owner=owner),
                self.assertRaisesRegex(forks.ForkOrgError, "resolves to itself"),
            ):
                forks.resolve(config, owner)


class CliTest(MappingCase):
    """``main`` prints the output-file line, or exits 1 on a bad file."""

    def test_resolve_prints_output_line(self) -> None:
        """``resolve`` prints ``fork_org=<pool>`` for ``$GITHUB_OUTPUT``."""
        path = self.write(mapping())
        with redirect_stdout(io.StringIO()) as stdout:
            forks.main(["resolve", "--config", str(path), "--owner", "ONAP"])
        self.assertEqual(stdout.getvalue(), "fork_org=lfreleng-bot-forks-onap\n")
        with redirect_stdout(io.StringIO()) as stdout:
            forks.main(["resolve", "--config", str(path), "--owner", "other"])
        self.assertEqual(stdout.getvalue(), "fork_org=lfreleng-bot-forks\n")

    def test_check_prints_the_mapping(self) -> None:
        """``check`` lists the default and every mapped owner."""
        with redirect_stdout(io.StringIO()) as stdout:
            forks.main(["check", "--config", str(self.write(mapping()))])
        self.assertEqual(
            stdout.getvalue(),
            "default=lfreleng-bot-forks\nonap=lfreleng-bot-forks-onap\n",
        )

    def test_bad_config_exits_one(self) -> None:
        """A mapping defect exits 1 with the ``fork orgs:`` prefix."""
        path = self.write(mapping(default="evil-org"))
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            forks.main(["check", "--config", str(path)])
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(stderr.getvalue().startswith("fork orgs: "))
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            forks.main(["resolve", "--config", str(path), "--owner", "x"])
        self.assertEqual(caught.exception.code, 1)

    def test_no_command_is_usage_error(self) -> None:
        """A missing subcommand is a usage error."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            forks.main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
