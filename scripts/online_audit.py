# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Audit every proposal with zizmor, online.

The author session holds no credential, so its own zizmor audit runs
offline, and offline zizmor skips every audit that needs the GitHub
API without saying so; ``stale-action-refs`` is one. The
organisation's required zizmor gate runs online, so a proposal could
pass every check its author ran and still fail that gate.

This module exports the base and head trees of every proposal, runs
zizmor online over each, and compares the two: a finding is new when
the head holds more findings for its ``(rule, path)`` than the base
does. It never decides for itself whether a change touches what
zizmor reads; zizmor decides what it collects, and a tree it collects
nothing from has no findings. Findings the target repository already
carries never block a proposal. Every other failure to run the audit
is a ``Rejection``, never a pass. See DESIGN.md section 8.
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, cast

from git_bounded import git, kill_group
from proposal_policy import PublishError, Rejection, defuse_mentions

ZIZMOR_TIMEOUT = 300
# zizmor exits 0 with no findings and 11 to 14 with findings by
# highest severity; anything else is a failure to audit.
AUDITED_EXITS = frozenset({0, 11, 12, 13, 14})
# zizmor's exit when collection yields no input at all: no findings.
NO_INPUTS_EXIT = 3
# The job hands the token over under this name; zizmor sees GH_TOKEN.
TOKEN_VARIABLE = "ZIZMOR_GITHUB_TOKEN"
# Variables that would turn the audit offline, point it at another
# configuration, or offer zizmor a second token to choose from.
DROPPED_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_HOSTNAME")
MAX_FINDING_LINES = 15
MAX_LINE = 200
FOOTER = "zizmor ran online; offline audits skip these checks (AGENTS.md §4.5)"
# Where zizmor discovers its configuration in a directory input, in
# its own order. The head is audited with the base's file, never its
# own, so a proposal cannot switch off the audit that judges it.
CONFIG_PATHS = (
    ".github/zizmor.yml",
    ".github/zizmor.yaml",
    "zizmor.yml",
    "zizmor.yaml",
)


def base_config(base_dir: Path) -> Path | None:
    """The configuration zizmor would discover in the base tree, if any."""
    for name in CONFIG_PATHS:
        candidate = base_dir / name
        if candidate.is_file():
            return candidate
    return None


def export_tree(clone: Path, commit: str, target: Path) -> Path:
    """Check one commit out into its own directory, beside the clone.

    A detached worktree rather than ``git archive``: the archive honours
    ``export-ignore`` from the tree it exports, so a proposal could
    leave its own workflow out of the audit.
    """
    target = target.resolve()
    try:
        git(
            clone,
            "-c",
            "core.hooksPath=/dev/null",
            "worktree",
            "add",
            "--quiet",
            "--detach",
            str(target),
            commit,
        )
    except PublishError as exc:
        raise Rejection(f"online audit: could not export {commit[:7]}: {exc}") from exc
    return target


def zizmor_environment(token: str) -> dict[str, str]:
    """The job's environment with the token as GH_TOKEN and nothing offline."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ZIZMOR_") and name not in DROPPED_VARIABLES
    }
    env["GH_TOKEN"] = token
    return env


def run_zizmor(
    binary: str,
    directory: Path,
    token: str,
    config: Path | None,
) -> list[Any]:
    """Audit one exported tree online; return zizmor's findings.

    ``--collect=all`` audits tracked files a ``.gitignore`` would hide,
    and ``--strict-collection`` fails on an input zizmor cannot parse
    rather than drop it. ``config`` is the base's configuration, or
    None for none; the tree's own is never discovered. zizmor's
    no-inputs exit is no findings: a tree it collects nothing from,
    such as the base of a repository's first workflow.
    """
    argv = [
        binary,
        "--persona",
        "auditor",
        "--min-severity",
        "informational",
        "--no-progress",
        "--format",
        "json",
        "--collect=all",
        "--strict-collection",
        *(["--config", str(config)] if config else ["--no-config"]),
        ".",
    ]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=directory,
            env=zizmor_environment(token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise Rejection(f"online audit: zizmor could not start: {exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=ZIZMOR_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        kill_group(proc.pid)
        proc.communicate()
        raise Rejection(
            f"online audit: zizmor did not finish within {ZIZMOR_TIMEOUT} seconds"
        ) from exc
    finally:
        kill_group(proc.pid)
    if proc.returncode == NO_INPUTS_EXIT:
        return []
    if proc.returncode not in AUDITED_EXITS:
        detail = stderr.decode("utf-8", "replace").strip()[-MAX_LINE:]
        raise Rejection(
            clean(
                f"online audit: zizmor failed to run (exit {proc.returncode}) {detail}"
            )
        )
    try:
        findings: Any = json.loads(stdout)
    except (ValueError, RecursionError) as exc:
        raise Rejection("online audit: zizmor produced unreadable output") from exc
    if not isinstance(findings, list):
        raise Rejection("online audit: zizmor produced unreadable output")
    return cast("list[Any]", findings)


def dig(value: Any, *names: str) -> Any:
    """Follow object keys through untrusted JSON; None where one is missing."""
    for name in names:
        if not isinstance(value, dict):
            return None
        value = cast("dict[str, Any]", value).get(name)
    return value


def primary_location(finding: dict[str, Any]) -> Any:
    """The first location zizmor lists for a finding, or None."""
    locations = finding.get("locations")
    if isinstance(locations, list) and locations:
        return cast("list[Any]", locations)[0]
    return None


def finding_key(finding: Any) -> tuple[str, str] | None:
    """The ``(rule, path)`` a live finding counts under; None when ignored."""
    if not isinstance(finding, dict):
        return None
    data = cast("dict[str, Any]", finding)
    if data.get("ignored") is True:
        return None
    ident = data.get("ident")
    path = dig(primary_location(data), "symbolic", "key", "Local", "verbatim_path")
    rule = ident if isinstance(ident, str) and ident else "unknown"
    where = posixpath.normpath(path) if isinstance(path, str) and path else "unknown"
    return rule, where


def finding_message(finding: dict[str, Any]) -> str:
    """The rule's description and the primary location's annotation."""
    desc = finding.get("desc")
    text = desc if isinstance(desc, str) else ""
    note = dig(primary_location(finding), "symbolic", "annotation")
    if isinstance(note, str) and note:
        text = f"{text} ({note})" if text else note
    return text or "no description"


def new_findings(base: list[Any], head: list[Any]) -> list[tuple[str, str, str]]:
    """Head findings past the base's count for their ``(rule, path)``."""
    allowed = Counter(key for key in map(finding_key, base) if key)
    added: list[tuple[str, str, str]] = []
    for finding in head:
        key = finding_key(finding)
        if key is None:
            continue
        if allowed[key] > 0:
            allowed[key] -= 1
            continue
        rule, path = key
        added.append((rule, path, finding_message(cast("dict[str, Any]", finding))))
    return added


def clean(text: str) -> str:
    """One bounded, mention-free line of text that came from the proposal."""
    flat = " ".join(defuse_mentions(text).split())
    return flat if len(flat) <= MAX_LINE else flat[: MAX_LINE - 1] + "…"


def rejection_text(added: list[tuple[str, str, str]]) -> str:
    """One line per new finding, bounded, then the footer."""
    lines = [
        clean(f"online audit: new zizmor finding {rule} in {path}: {message}")
        for rule, path, message in added[:MAX_FINDING_LINES]
    ]
    hidden = len(added) - MAX_FINDING_LINES
    if hidden > 0:
        lines.append(f"online audit: {hidden} more new finding(s) not shown")
    lines.append(FOOTER)
    return ";\n".join(lines)


def audit(clone: Path, base_sha: str, head: str, workdir: Path, token: str) -> str:
    """Audit the proposal online; return the summary line or raise Rejection."""
    if not token:
        raise Rejection(
            f"online audit: no GitHub token in {TOKEN_VARIABLE}; "
            "zizmor would run offline and skip the online audits"
        )
    binary = shutil.which("zizmor")
    if binary is None:
        raise Rejection("online audit: zizmor is not installed on the runner")
    base_dir = export_tree(clone, base_sha, workdir / "audit-base")
    head_dir = export_tree(clone, head, workdir / "audit-head")
    config = base_config(base_dir)
    before = run_zizmor(binary, base_dir, token, config)
    after = run_zizmor(binary, head_dir, token, config)
    added = new_findings(before, after)
    if added:
        raise Rejection(rejection_text(added))
    return "online audit: no new zizmor findings"
