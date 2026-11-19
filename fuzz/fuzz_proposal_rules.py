# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fuzz the rules the publisher applies to an untrusted proposal.

The first byte of an input picks a target and the rest is its
payload. Each oracle restates, from the raw input, what the code
documents:

- ``safe_path``: "Reject paths that escape the tree or touch git
  metadata." A path it accepts resolves inside the checkout and
  names no ``.git`` component.
- ``protected``: docs/DESIGN.md section 8 step 3 lists three classes
  of protected path; "Names and directories compare in lower case"
  (proposal_policy). No path in a listed class gets past it.
- ``check_change_mode``: "no executable files, symlinks, submodules
  or mode changes" (DESIGN.md section 8 step 3); a refusal is a
  ``Rejection``.
- ``compose_message``, which drives ``split_message``,
  ``check_headline`` and ``check_body`` as the publisher does: the
  subject is the first line, within the limit, typed and
  capitalised, with no trailing punctuation; "The line after the
  subject must be blank"; body lines wrap at 72 unless they carry a
  URL; and "every agent-written trailer that names someone goes".
- ``comment_safe``: no newline, no raw HTML, no live @-mention, and
  the text a reader sees is the text flattened.
- ``defuse_mentions``: "A zero-width space after each @ that would
  start a user or team mention"; only zero-width spaces are added.
- ``log_safe``: "safe to print from a job whose log GitHub parses":
  one line that opens no workflow command, holds no legacy ``##[``
  command, and a strict UTF-8 stream accepts.
- ``bounded_reasons``: at most ``MAX_RESULT_REASONS`` reasons, each
  cut to ``MAX_REASON`` characters.
- ``proposal_check.read_manifest``: "Agent output; malformed is a
  verdict, not an operational fault": any manifest is read or raises
  ``Rejection``, and one it reads matches the trusted selection.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from harness_support import Breach, arguments, as_text, encodes, on_path

on_path()
policy = import_module("proposal_policy")
proposal_check = import_module("proposal_check")
model = import_module("proposal_model")

IDENTITY = policy.Identity(
    coauthor="Copilot <223556219+Copilot@users.noreply.github.com>",
    bot_login="code-monkey[bot]",
    bot_email="1+code-monkey[bot]@users.noreply.github.com",
)
BASE = "a" * 40
REBASE = "b" * 40
ENTRY: dict[str, Any] = {
    "repository": "lfreleng-actions/target",
    "number": 7,
    "branch": "code-monkey/issue-7",
    "base_sha": BASE,
    "default_branch": "main",
    "fork_org": "lfreleng-bot-forks",
    "fork_repository": "lfreleng-bot-forks/target",
}
REVISE_ENTRY: dict[str, Any] = {
    **ENTRY,
    "task": "revise",
    "rebase_sha": REBASE,
    "round": 2,
    "pull_request": {"head_sha": "c" * 40, "number": 9, "url": "pull/9"},
    "threads": [{"thread_id": "PRRT_1", "comment_id": 11}],
}

# DESIGN.md section 8 step 3, restated from its text.
INSTRUCTION_NAMES = (
    "agents.md",
    "claude.md",
    "gemini.md",
    "copilot-instructions.md",
    "skill.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    "codeowners",
    "reuse.toml",
    ".gitlint",
)
INSTRUCTION_DIRS = (
    ".copilot",
    ".claude",
    ".cursor",
    ".agents",
    ".windsurf",
    ".clinerules",
    ".gemini",
    ".codex",
    ".aislop",
)
GITHUB_DIRS = ("instructions", "prompts", "agents", "skills", "copilot")
ROOT_FILES = (
    "security.md",
    ".pre-commit-config.yaml",
    ".zizmor.yml",
    ".zizmor.yaml",
    ".enable-sudo-and-containers",
)
TYPES = "Fix|Feat|Chore|Docs|Style|Refactor|Perf|Test|Revert|CI|Build"
SUBJECT = re.compile(rf"(?:{TYPES})(?:\([^()]+\))?!?: [A-Z].*")
NAMES_SOMEONE = re.compile(r"[A-Za-z][A-Za-z-]*-by:", re.I)
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x1f\x85\u2028\u2029"
ZWSP = "\u200b"
# Where GitHub starts a mention: the text's start, whitespace, or
# punctuation that ends a word. Kept narrow, to what is certain.
MENTION_LEAD = frozenset(" \t([{,;:!?*~\"'")
MANIFEST_OUTCOMES = ("proposed", "abstain", "handoff", "author-failed")


def listed_protected(path: str) -> bool:
    """Whether DESIGN.md lists the path as protected."""
    parts = path.split("/")
    lowered = [part.lower() for part in parts]
    return (
        lowered[-1] in INSTRUCTION_NAMES
        or (len(parts) == 1 and lowered[0] in ROOT_FILES)
        or parts[-1].startswith(("LICENSE", "LICENCE"))
        or "LICENSES" in parts[:-1]
        or any(part in INSTRUCTION_DIRS for part in lowered[:-1])
        or (len(parts) > 2 and lowered[0] == ".github" and lowered[1] in GITHUB_DIRS)
    )


def oracle_path(path: str, safe: bool, protected: bool) -> None:
    """A safe path stays in the checkout; no listed path passes as unprotected."""
    if safe:
        inside = posixpath.normpath(posixpath.join("/checkout", path))
        if not inside.startswith("/checkout/") or posixpath.normpath(path) != path:
            raise Breach(f"safe_path accepted {path!r}, outside the checkout")
        if ".git" in path.split("/") or "\\" in path or "\0" in path:
            raise Breach(f"safe_path accepted {path!r}, which touches git metadata")
    if listed_protected(path) and not protected:
        raise Breach(f"protected let {path!r} through")


def oracle_mode(status: str, old: str, new: str, accepted: bool) -> None:
    """Only a regular file, of unchanged mode and type, is replayable."""
    replayable = (
        new == "100644"
        and not status.startswith("T")
        and not (status.startswith("M") and old != new)
    )
    if accepted != replayable:
        raise Breach(f"check_change_mode({status!r}, {old!r}, {new!r}) -> {accepted}")


def oracle_message(message: str, limit: int, result: tuple[str, str]) -> None:
    """An accepted message keeps every rule; its trailers name only the bot pair."""
    headline, body = result
    lines = message.rstrip("\n").split("\n")
    if headline != lines[0].rstrip() or len(headline) > limit:
        raise Breach(f"subject {headline!r} is not the first line within {limit}")
    if not SUBJECT.fullmatch(headline) or headline[-1] in ".;:!?,":
        raise Breach(f"subject {headline!r} breaks the subject rules")
    if len(lines) > 1 and lines[1].strip():
        raise Breach("accepted a message with no blank line after the subject")
    text, _, block = body.rpartition("\n\n")
    trailers = block.split("\n")
    if trailers[-2:] != [f"Co-authored-by: {IDENTITY.coauthor}", IDENTITY.sign_off]:
        raise Breach("the composed trailers do not close with the bot's pair")
    if any(NAMES_SOMEONE.match(line) for line in trailers[:-2]):
        raise Breach("an agent-written trailer naming someone survived")
    for line in text.split("\n") if text else []:
        if len(line) > 72 and "http://" not in line and "https://" not in line:
            raise Breach(f"accepted a body line over 72 characters: {line!r}")


def oracle_comment(text: str, safe: str) -> None:
    """One escaped, quiet line that reads as the input flattened."""
    if any(char in LINE_BREAKS for char in safe) or "<" in safe or ">" in safe:
        raise Breach(f"comment_safe left a line break or markup in {safe!r}")
    if any(safe[i + 1 : i + 2] != ZWSP for i, char in enumerate(safe) if char == "@"):
        raise Breach(f"comment_safe left a live mention in {safe!r}")
    if html.unescape(safe).replace("@" + ZWSP, "@") != " ".join(text.split()):
        raise Breach("comment_safe changed the text a reader sees")


def oracle_mentions(text: str, defused: str) -> None:
    """Only zero-width spaces are added, and none is missing before a login."""
    if defused.replace(ZWSP, "") != text.replace(ZWSP, ""):
        raise Breach("defuse_mentions changed more than the mentions")
    for i, char in enumerate(defused):
        lead = defused[i - 1] if i else " "
        after = defused[i + 1 : i + 2]
        if char == "@" and lead in MENTION_LEAD and after.isascii() and after.isalnum():
            raise Breach(f"defuse_mentions left a live mention in {defused!r}")


def oracle_log(safe: str) -> None:
    """One line that opens no workflow command and a strict UTF-8 log accepts."""
    if any(char in LINE_BREAKS for char in safe):
        raise Breach(f"log_safe left a line break in {safe!r}")
    if safe.lstrip().startswith("::") or "##[" in safe:
        raise Breach(f"log_safe output {safe!r} is a workflow command")
    if not encodes(safe):
        raise Breach(f"log_safe output {safe!r} breaks a strict UTF-8 log")


def oracle_reasons(raw: Any, reasons: list[str]) -> None:
    """At most the cap, each a string within its bound, the input's own if text."""
    if len(reasons) > 20 or any(len(reason) > 2000 for reason in reasons):
        raise Breach("bounded_reasons kept more than its bounds")
    if not isinstance(raw, list) and reasons:
        raise Breach("bounded_reasons invented reasons from a non-list")
    items = cast(list[Any], raw) if isinstance(raw, list) else []
    if isinstance(raw, list) and all(isinstance(item, str) for item in items):
        if reasons != [item[:2000] for item in items[:20]]:
            raise Breach("bounded_reasons changed the reasons it kept")


def oracle_manifest(raw: bytes, check: Any, manifest: dict[str, Any]) -> None:
    """A manifest read is the file's JSON object, on the selection's identity."""
    loaded: Any = json.loads(raw.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise Breach("read_manifest accepted a file that holds no JSON object")
    parsed = cast(dict[str, Any], loaded)
    # Serialised, so a NaN the file holds still equals itself.
    if json.dumps(parsed) != json.dumps(manifest):
        raise Breach("read_manifest returned something other than the file")
    schema, outcome = parsed.get("schema"), parsed.get("outcome")
    if type(schema) is not int or schema != 1 or outcome not in MANIFEST_OUTCOMES:
        raise Breach(f"read_manifest accepted schema {schema!r}, outcome {outcome!r}")
    bases = (BASE, REBASE) if check.task == "revise" else (BASE,)
    if (
        parsed.get("repository") != ENTRY["repository"]
        or parsed.get("issue") != ENTRY["number"]
        or parsed.get("branch") != ENTRY["branch"]
        or parsed.get("base_sha") not in bases
    ):
        raise Breach("read_manifest accepted a manifest for another selection")
    if outcome == "handoff" and check.task != "revise":
        raise Breach("read_manifest let an issue session hand off")
    if outcome != "proposed" and check.verdict != outcome:
        raise Breach(f"read_manifest recorded {check.verdict!r} for {outcome!r}")
    if any(len(reason) > 2000 for reason in check.reasons) or len(check.commands) > 50:
        raise Breach("read_manifest kept more than its bounds")
    for command in check.commands:
        code = command["exit_code"]
        if len(command["command"]) > 200 or not (code is None or type(code) is int):
            raise Breach(f"read_manifest kept a command outside its shape: {command}")


def check_path(payload: bytes) -> None:
    """``safe_path`` and ``protected`` together, as the diff walk applies them."""
    path = as_text(payload)
    oracle_path(path, policy.safe_path(path), policy.protected(path))


def check_mode(payload: bytes) -> None:
    """``check_change_mode`` on a status and two modes split at NUL bytes."""
    status, _, rest = as_text(payload).partition("\0")
    old, _, new = rest.partition("\0")
    try:
        policy.check_change_mode("f", status, old, new)
    except policy.Rejection:
        oracle_mode(status, old, new, False)
        return
    oracle_mode(status, old, new, True)


def check_message(payload: bytes) -> None:
    """``compose_message`` with the subject limit taken from the first byte."""
    limit = payload[0] if payload else policy.DEFAULT_TITLE_LIMIT
    message = as_text(payload[1:])
    try:
        result = policy.compose_message(message, IDENTITY, limit)
    except policy.Rejection:
        return
    oracle_message(message, limit, result)


def check_text(payload: bytes) -> None:
    """``comment_safe``, ``defuse_mentions`` and ``log_safe`` on one text."""
    text = as_text(payload)
    oracle_comment(text, policy.comment_safe(text))
    oracle_mentions(text, policy.defuse_mentions(text))
    oracle_log(policy.log_safe(text))


def check_reasons(payload: bytes) -> None:
    """``bounded_reasons`` on the JSON the payload holds, or on its text."""
    text = as_text(payload)
    try:
        raw: Any = json.loads(text)
    except (ValueError, RecursionError):
        raw = text
    oracle_reasons(raw, policy.bounded_reasons(raw))


def check_manifest(payload: bytes) -> None:
    """``read_manifest`` on the payload as manifest.json, for an issue or a round."""
    entry = REVISE_ENTRY if payload[:1] == b"r" else ENTRY
    raw = payload[1:]
    check = model.Check.from_entry("key", entry)
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "manifest.json"
        path.write_bytes(raw)
        try:
            manifest = proposal_check.read_manifest(path, check)
        except policy.Rejection:
            return
    oracle_manifest(raw, check, manifest)


TARGETS = (
    check_path,
    check_mode,
    check_message,
    check_text,
    check_reasons,
    check_manifest,
)


def check(data: bytes) -> None:
    """The fuzzed function: one input, one target, its contract or ``Breach``."""
    if data:
        TARGETS[data[0] % len(TARGETS)](data[1:])


def seed(target: int, payload: str) -> bytes:
    """An input for the target at that index."""
    return bytes([target]) + payload.encode("utf-8", "surrogatepass")


def manifest_seed(task: str, **fields: Any) -> bytes:
    """A manifest input for an issue (``i``) or a revision (``r``)."""
    data: dict[str, Any] = {
        "schema": 1,
        "repository": ENTRY["repository"],
        "issue": ENTRY["number"],
        "base_sha": BASE,
        "branch": ENTRY["branch"],
        **fields,
    }
    return seed(5, task + json.dumps(data))


SEEDS: tuple[bytes, ...] = (
    seed(0, "scripts/proposal_policy.py"),
    seed(0, "docs/AGENTS.md"),
    seed(0, ".GitHub/Prompts/x.md"),
    seed(0, "src/LICENSES/x.txt"),
    seed(0, "a/../../etc/passwd"),
    seed(0, "a/.git/config"),
    seed(1, "M\x00100644\x00100644"),
    seed(1, "M\x00100755\x00100755"),
    seed(1, "A\x00000000\x00120000"),
    seed(1, "T\x00100644\x00100644"),
    seed(2, "2Fix(fuzz): Add a seed\n\nWhy it is needed.\n\nSigned-off-by: A <a@b>"),
    seed(
        2,
        "2Feat: Add a long one\n\n"
        + "word " * 20
        + "\n\nSee https://example.com/"
        + "a" * 90
        + "\n\nRefs: 1\nReviewed-By: Someone <s@x>",
    ),
    seed(2, "\x08Fix: Too long a subject\n"),
    seed(2, "2fix: lower case\nno blank line"),
    seed(3, "ping @alice, mail a@b.com, uses actions/checkout@v4\n::error::x"),
    seed(3, "<details>\n##[group]x\n:::warning::y\n@org/team"),
    seed(3, "a\ud800b"),
    seed(4, json.dumps(["one", "two" * 1200, 3, None])),
    seed(4, json.dumps({"reasons": "not a list"})),
    manifest_seed("i", outcome="proposed", commands=[{"command": "t", "exit_code": 0}]),
    manifest_seed("i", outcome="abstain", reason="Needs a product decision."),
    manifest_seed("i", outcome="handoff"),
    manifest_seed("i", schema=True, outcome="proposed"),
    manifest_seed(
        "r",
        outcome="handoff",
        base_sha=REBASE,
        responses=[
            {"thread_id": "PRRT_1", "comment_id": 11, "reply": "No.", "resolve": False}
        ],
        round_summary="Disputed.",
    ),
)


def main() -> None:
    """Fuzz ``check`` under Atheris, from the seeds unless files are named."""
    import atheris  # type: ignore[import-not-found]

    fuzzer: Any = atheris
    fuzzer.instrument_all()
    fuzzer.Setup(arguments(sys.argv, SEEDS), check)
    fuzzer.Fuzz()


if __name__ == "__main__":
    main()
