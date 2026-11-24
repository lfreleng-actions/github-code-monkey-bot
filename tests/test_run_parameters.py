# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A hand-back round inherits its pull request's run parameters (issue #241).

The publisher records the run's parameters in every round footer as
one ``params=`` token; with ``inherit_run_parameters`` the select job
reads the latest one back for the one named pull request, and keeps
the form's values on any doubt.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
reads = import_module("issue_reads")
round_reads = import_module("round_reads")

REPO = "org/alpha"
BOT = "lf-code-monkey[bot]"
FORK_ORGS: dict[str, Any] = {"default": "org-forks", "organisations": {}}
FORM = {
    "model": "claude-opus-5.5",
    "fusion_tier": "",
    "max_runtime_minutes": "180",
    "max_rounds": "10",
    "stale_status_minutes": "8",
    "allow_subagents": "true",
    "request_review": "true",
    "request_antagonistic_review": "true",
    "load_repo_skills": "false",
    "include_dotgithub": "false",
}
TOKEN = (
    "v1:model:claude-opus-5.5,runtime:180,rounds:10,stale:8,subagents:1,"
    "skills:0,dotgithub:0,review:1,antagonistic:1"
)
FUSION_TOKEN = (
    "v1:model:fusion,tier:max,runtime:60,rounds:4,stale:0,subagents:0,"
    "skills:1,dotgithub:1,review:0,antagonistic:0"
)


def parameters() -> ModuleType:
    """The module under test, imported on use."""
    return import_module("run_parameters")


def footer(run: str, extra: str = "") -> str:
    """A round footer for pull request 41."""
    return f"<!-- code-monkey {run} proposed round=1 pr=41{extra} -->"


def history(bodies: list[tuple[str, str]], pull: int | None = 41) -> Any:
    """The round history the comments, by author and body, give in order."""
    entries = [
        {
            "user": {"login": login},
            "body": f"r{n}\n\n{body}",
            "created_at": f"2026-11-01T{n:02d}:00:00Z",
        }
        for n, (login, body) in enumerate(bodies)
    ]
    with patch.object(reads, "api_page", return_value=entries):
        return round_reads.round_history(REPO, 7, BOT, None, pull)


class CodecTest(unittest.TestCase):
    """The ``params=`` token: compact, versioned, checked field by field."""

    @unittest.expectedFailure
    def test_round_trip(self) -> None:
        """The form's values encode to the token and decode back."""
        module = parameters()
        self.assertEqual(module.encode(FORM), TOKEN)
        decoded = module.decode(TOKEN)
        self.assertEqual(decoded, {**FORM, "fusion_auto_model_select": "false"})
        fusion = module.decode(FUSION_TOKEN)
        self.assertEqual(fusion["model"], "fusion")
        self.assertEqual(fusion["fusion_tier"], "max")
        self.assertEqual(fusion["fusion_auto_model_select"], "true")
        self.assertEqual(module.encode(fusion), FUSION_TOKEN)
        self.assertEqual(list(decoded), list(module.INHERITED))

    @unittest.expectedFailure
    def test_each_invalid_field_is_named(self) -> None:
        """A value the input check would refuse fails, naming its field."""
        module = parameters()
        cases = {
            "model:claude-opus-5.5": ("model:Claude Opus", "model"),
            "runtime:180": ("runtime:331", "max_runtime_minutes"),
            "rounds:10": ("rounds:0", "max_rounds"),
            "stale:8": ("stale:-1", "stale_status_minutes"),
            "subagents:1": ("subagents:yes", "allow_subagents"),
            "skills:0": ("skills:", "load_repo_skills"),
            "dotgithub:0": ("dotgithub:2", "include_dotgithub"),
            "review:1": ("review:true", "request_review"),
            "antagonistic:1": ("antagonistic:x", "request_antagonistic_review"),
        }
        for good, (bad, field) in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(module.ParameterError) as raised:
                    module.decode(TOKEN.replace(good, bad))
                self.assertEqual(raised.exception.field, field)
        for token, field in (
            (TOKEN.replace("runtime:180", "tier:max,runtime:180"), "fusion_tier"),
            (FUSION_TOKEN.replace("tier:max,", ""), "fusion_tier"),
            (FUSION_TOKEN.replace("tier:max", "tier:ultra"), "fusion_tier"),
            (TOKEN.replace(",rounds:10", ""), "max_rounds"),
            (TOKEN + ",rounds:10", "max_rounds"),
        ):
            with self.subTest(token=token):
                with self.assertRaises(module.ParameterError) as raised:
                    module.decode(token)
                self.assertEqual(raised.exception.field, field)

    @unittest.expectedFailure
    def test_malformed_or_over_long_tokens_fail(self) -> None:
        """Another version, an unknown field, non-ASCII or a long token fails."""
        module = parameters()
        for token in (
            TOKEN.replace("v1:", "v2:"),
            TOKEN.replace("v1:", ""),
            TOKEN + ",colour:red",
            TOKEN.replace("claude", "cl\u00e4ude"),
            TOKEN + "," + "x" * module.MAX_TOKEN,
            "",
        ):
            with self.subTest(token=token[:40]):
                with self.assertRaises(module.ParameterError):
                    module.decode(token)

    @unittest.expectedFailure
    def test_the_token_spells_no_footer_field(self) -> None:
        """A reader scanning the footer for ``name=`` finds only the footer's own.

        The review bot reads ``antagonistic=`` from the footer with a
        looser pattern than ``round_reads.FOOTER``; a token spelling
        ``antagonistic=1`` would read as answered review 1.
        """
        module = parameters()
        for values in (FORM, module.decode(FUSION_TOKEN)):
            token = module.encode(values)
            with self.subTest(token=token):
                self.assertNotIn("=", token)
                marker = footer("https://r/1", f" antagonistic=6 params={token}")
                self.assertEqual(re.findall(r"antagonistic=(\d+)", marker), ["6"])
                self.assertEqual(re.findall(r"\breview=", marker), [])
                self.assertEqual(re.findall(r"\bstale=", marker), [])
        with self.assertRaises(module.ParameterError):
            module.decode(TOKEN.replace(":", "=").replace("v1=", "v1:"))

    @unittest.expectedFailure
    def test_stale_minutes_keep_the_input_checks_syntax(self) -> None:
        """Any digits pass as given, as the workflow's input check takes them."""
        module = parameters()
        for value in ("08", "00", "1234567"):
            with self.subTest(value=value):
                checked = module.validate({**FORM, "stale_status_minutes": value})
                self.assertEqual(checked["stale_status_minutes"], value)
                token = module.encode(checked)
                self.assertIn(f",stale:{value},", token)
                self.assertEqual(module.decode(token), checked)
        for value in ("", "-1", "8 ", "1e3"):
            with self.subTest(value=value):
                with self.assertRaises(module.ParameterError):
                    module.validate({**FORM, "stale_status_minutes": value})

    @unittest.expectedFailure
    def test_a_long_model_id_passes_and_records_no_token(self) -> None:
        """The model has no length bound, as the input check sets none."""
        module = parameters()
        model = "a" * module.MAX_TOKEN
        checked = module.validate({**FORM, "model": model})
        self.assertEqual(checked["model"], model)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(module.footer_field(module.encode(checked)), "")
        self.assertIn("::warning::run parameters not recorded", out.getvalue())


class FooterTest(unittest.TestCase):
    """The footer carries the token; the round history reads it loosely."""

    @unittest.expectedFailure
    def test_latest_footer_of_the_pull_request_carries_the_token(self) -> None:
        """Later footers win; another pull request's and a stranger's do not count."""
        found = history(
            [
                (BOT, footer("https://r/1", f" params={TOKEN}")),
                (BOT, footer("https://r/2", f" params={FUSION_TOKEN}")),
                (
                    BOT,
                    footer("https://r/3", f" params={TOKEN}").replace("pr=41", "pr=9"),
                ),
                ("mallory", footer("https://r/4", f" params={TOKEN}")),
            ]
        )
        self.assertEqual(found.last_params, FUSION_TOKEN)
        self.assertEqual(found.last_run, "https://r/2")

    @unittest.expectedFailure
    def test_a_superseded_or_late_footer_sets_no_parameters(self) -> None:
        """A stale footer, or a retry read before the latest round, is not the source."""
        found = history(
            [
                (BOT, footer("https://r/0", f" params={TOKEN}")),
                (
                    BOT,
                    footer(
                        "https://r/1",
                        f" since=2026-11-01T00:30:00Z params={FUSION_TOKEN}",
                    ),
                ),
                (BOT, footer("https://r/2", f" stale=1 params={TOKEN}")),
                (
                    BOT,
                    footer(
                        "https://r/3", f" since=2026-11-01T00:10:00Z params={TOKEN}"
                    ),
                ),
            ]
        )
        self.assertEqual(found.last_params, FUSION_TOKEN)
        self.assertEqual(found.last_run, "https://r/1")

    @unittest.expectedFailure
    def test_a_latest_footer_without_a_token_inherits_nothing(self) -> None:
        """An older round's token does not stand in for the latest footer's."""
        found = history(
            [
                (BOT, footer("https://r/0", f" params={FUSION_TOKEN}")),
                (BOT, footer("https://r/1")),
                (BOT, footer("https://r/2", f" stale=1 params={TOKEN}")),
            ]
        )
        self.assertEqual(found.highest, 1)
        self.assertIsNone(found.last_params)
        self.assertIsNone(found.last_run)

    @unittest.expectedFailure
    def test_older_footers_still_parse_and_carry_none(self) -> None:
        """A footer written before the token counts as before and records nothing."""
        found = history([(BOT, footer("https://r/1"))])
        self.assertEqual(found.highest, 1)
        self.assertIsNone(found.last_params)
        self.assertIsNone(found.last_run)

    @unittest.expectedFailure
    def test_a_malformed_token_breaks_no_other_field(self) -> None:
        """round=, handback= and antagonistic= read as they would without it."""
        for token in ("v9:<junk> with spaces", "x" * 2000):
            with self.subTest(token=token[:20]):
                found = history(
                    [
                        (
                            BOT,
                            "<!-- code-monkey https://r/1 failed round=3 pr=41"
                            f" handback=5 antagonistic=6 params={token} -->",
                        )
                    ]
                )
                self.assertEqual(found.highest, 3)
                self.assertEqual(found.anchored, frozenset({5, 6}))
                self.assertEqual(found.last_params, token)
                with self.assertRaises(parameters().ParameterError):
                    parameters().decode(token)

    @unittest.expectedFailure
    def test_every_footer_records_the_run_parameters(self) -> None:
        """The comment's marker ends with the token the run was given."""
        comments = import_module("issue_comment")
        github = import_module("bot_github")
        with tempfile.TemporaryDirectory() as holder:
            path = Path(holder) / "result.json"
            for token, expected in ((TOKEN, f" params={TOKEN} -->"), ("v9:x", " -->")):
                result = {
                    "repository": REPO,
                    "issue": 7,
                    "verdict": "author-failed",
                    "reasons": ["timed out"],
                    "dry_run": False,
                    "pull_number": 41,
                }
                path.write_text(json.dumps(result), encoding="utf-8")
                with (
                    self.subTest(token=token),
                    patch.object(reads, "api_page", return_value=[]),
                    patch.object(comments.recheck, "listed_refusal", return_value=None),
                    patch.object(
                        github, "api_write", return_value={"html_url": "u"}
                    ) as write,
                    redirect_stdout(io.StringIO()),
                ):
                    comments.run_comment(
                        argparse.Namespace(
                            result=path,
                            run_url="https://run",
                            since="t",
                            run_parameters=token,
                        )
                    )
                    body = write.call_args.args[2]["body"]
                    self.assertTrue(body.endswith(f" pr=41{expected}"), body)


class InheritTest(unittest.TestCase):
    """The select job takes the parameters from the named pull request's footer."""

    def run_inherit(
        self,
        last: tuple[str | None, str | None, str],
        bot: dict[str, Any] | None = None,
    ) -> tuple[argparse.Namespace, dict[str, Any], str]:
        """Inherit for one pull request with the footer read stubbed."""
        module = import_module("parameter_inheritance")
        args = argparse.Namespace(
            org="org",
            model="claude-opus-5.5",
            fusion_tier="",
            max_rounds="10",
            stale_status_minutes="8",
            include_dotgithub=False,
            inherit_run_parameters=True,
        )
        out = io.StringIO()
        with (
            patch.object(module, "last_token", return_value=last) as read,
            redirect_stdout(out),
        ):
            record = module.inherit(
                args, bot or {"login": BOT}, FORK_ORGS, [("alpha", 41)]
            )
        if bot is None:
            read.assert_called_once_with("org", ("alpha", 41), BOT, FORK_ORGS)
        return args, record, out.getvalue()

    @unittest.expectedFailure
    def test_inherited_values_replace_the_form(self) -> None:
        """Every value the token holds reaches the selector and the record."""
        args, record, out = self.run_inherit((FUSION_TOKEN, "https://r/2", ""))
        self.assertEqual(
            (args.model, args.fusion_tier, args.max_rounds),
            ("fusion", "max", "4"),
        )
        self.assertEqual(args.stale_status_minutes, "0")
        self.assertIs(args.include_dotgithub, True)
        self.assertEqual(record["inherited_parameters"]["max_runtime_minutes"], "60")
        self.assertEqual(record["inherited_from"], "https://r/2")
        self.assertEqual(out, "")

    @unittest.expectedFailure
    def test_an_invalid_field_keeps_the_form_and_warns(self) -> None:
        """Fails closed: nothing inherited, a warning naming the field."""
        bad = TOKEN.replace("rounds:10", "rounds:99")
        args, record, out = self.run_inherit((bad, "https://r/2", ""))
        self.assertEqual((args.model, args.max_rounds), ("claude-opus-5.5", "10"))
        self.assertEqual(record["inherited_parameters"], {})
        self.assertIsNone(record["inherited_from"])
        self.assertIn("::warning::", out)
        self.assertIn("max_rounds", out)
        self.assertIn("max_rounds", record["inheritance_note"])

    @unittest.expectedFailure
    def test_no_token_or_no_app_keeps_the_form(self) -> None:
        """A history before this change, or a run without the App, says so."""
        args, record, out = self.run_inherit((None, None, ""))
        self.assertEqual(args.model, "claude-opus-5.5")
        self.assertEqual(record["inherited_parameters"], {})
        self.assertIn("no round footer records", record["inheritance_note"])
        self.assertEqual(out, "")
        _, record, _ = self.run_inherit(
            (TOKEN, "u", ""), bot={"login": "x", "placeholder": True}
        )
        self.assertIn("no App", record["inheritance_note"])

    @unittest.expectedFailure
    def test_summary_lists_the_inherited_values(self) -> None:
        """Each value appears beside its name, with the footer's run."""
        outputs = import_module("selection_outputs")
        _, record, _ = self.run_inherit((TOKEN, "https://r/2", ""))
        lines = outputs.inheritance_lines(record)
        self.assertIn("Run parameters inherited from https://r/2:", lines)
        self.assertIn("| max_runtime_minutes | `180` |", lines)
        self.assertEqual(outputs.inheritance_lines({}), [])

    @unittest.expectedFailure
    def test_needs_one_pull_request_alone_in_pull_requests_mode(self) -> None:
        """Zero or several pull requests, issues, or another mode are refused."""
        select = import_module("select_issues")

        def args(*extra: str) -> argparse.Namespace:
            return select.build_parser().parse_args(
                [
                    "--org",
                    "org",
                    "--output-dir",
                    "out",
                    "--model",
                    "m",
                    "--guidance-repository",
                    "org/.github",
                    "--fork-orgs",
                    "f.json",
                    "--inherit-run-parameters",
                    *extra,
                ]
            )

        accepted = args("--mode", "pull-requests", "--pull-requests", "org/alpha#41")
        self.assertEqual(select.parse_targets(accepted)[2], [("alpha", 41)])
        for extra in (
            ("--mode", "pull-requests", "--revise-only"),
            ("--mode", "pull-requests", "--pull-requests", "org/a#1 org/b#2"),
            (
                "--mode",
                "pull-requests",
                "--pull-requests",
                "org/a#1",
                "--issues",
                "org/a#3",
            ),
            ("--mode", "branches", "--pull-requests", "org/a#1"),
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaises(reads.SelectionError),
            ):
                select.parse_targets(args(*extra))


class SettleTest(unittest.TestCase):
    """The select job's effective values and the budgets that follow from them."""

    @unittest.expectedFailure
    def test_budgets_follow_the_inherited_runtime(self) -> None:
        """An inherited runtime sets both timeouts; off, the form's stand."""
        module = parameters()
        environ = {variable: FORM[name] for name, variable in module.FORM_ENV.items()}
        inherited = module.decode(FUSION_TOKEN)
        for selection, runtime, model in (
            ({"model": "claude-opus-5.5"}, "180", "claude-opus-5.5"),
            ({"inherited_parameters": inherited}, "60", "fusion"),
        ):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as holder:
                work = Path(holder)
                (work / "selection.json").write_text(
                    json.dumps(selection), encoding="utf-8"
                )
                output = work / "output"
                with patch.dict("os.environ", environ, clear=True):
                    module.main(
                        [
                            "settle",
                            "--selection",
                            str(work / "selection.json"),
                            "--output",
                            str(output),
                        ]
                    )
                lines = dict(
                    line.split("=", 1)
                    for line in output.read_text(encoding="utf-8").splitlines()
                )
                self.assertEqual(lines["agent_timeout"], runtime)
                self.assertEqual(lines["author_timeout"], str(int(runtime) + 20))
                self.assertEqual(lines["model"], model)
                self.assertEqual(module.decode(lines["params"])["model"], model)
                self.assertEqual(
                    set(json.loads(lines["run_parameters"])), set(module.INHERITED)
                )


if __name__ == "__main__":
    unittest.main()
