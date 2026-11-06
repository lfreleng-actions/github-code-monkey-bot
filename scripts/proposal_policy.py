# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Rules a proposal must follow, stated without git or GitHub plumbing.

The publisher's offline check (``proposal_check``) walks a bundle and
asks these functions whether each path, mode, message and title is
acceptable. Keeping the rules here means a reviewer can read the
policy in one place, and the tests can exercise it without a clone.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

MAX_COMMITS = 5
# createCommitOnBranch accepts at most 100 file changes per call.
MAX_FILES_PER_COMMIT = 100
# A commit message larger than this is not a message a reviewer reads.
MAX_MESSAGE_BYTES = 64 * 1024
MAX_ADDED_BYTES = 4 * 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024
DEFAULT_TITLE_LIMIT = 50
BODY_LINE_LIMIT = 72
MAX_PR_TITLE = 256
# GitHub rejects pull request bodies over 65,536 characters.
MAX_PR_BODY = 65_536
# An agent's abstention or failure reason as recorded and as it may
# appear in an issue comment; the manifest itself may be up to 1 MiB.
MAX_REASON = 2_000
MAX_COMMENT_REASON = 4_000
SHA_RE = re.compile(r"[0-9a-f]{40}")
KEY_RE = re.compile(r"[A-Za-z0-9_.-]+")
COMMIT_TYPES = (
    "Fix",
    "Feat",
    "Chore",
    "Docs",
    "Style",
    "Refactor",
    "Perf",
    "Test",
    "Revert",
    "CI",
    "Build",
)
# Matches the organisation's gitlint rule (AGENTS.md 6.6): a
# capitalised type from the list, and a capitalised description.
HEADLINE_RE = re.compile(
    r"^(?:" + "|".join(COMMIT_TYPES) + r")(?:\([^()]+\))?!?: [A-Z].*$"
)
TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z-]*: .+$")
URL_RE = re.compile(r"https?://")

# Files an agent harness reads as instructions, or that decide who
# reviews and which gates run: a proposal that changed one would be
# the agent rewriting its own instructions or the checks it reports.
# Names and directories compare in lower case.
PROTECTED_NAMES = frozenset(
    {
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
    }
)
PROTECTED_ROOT_FILES = frozenset(
    {"security.md", ".pre-commit-config.yaml", ".zizmor.yml", ".zizmor.yaml"}
)
PROTECTED_DIRS = frozenset(
    {
        ".copilot",
        ".claude",
        ".cursor",
        ".agents",
        ".windsurf",
        ".clinerules",
        ".gemini",
        ".codex",
        ".aislop",
    }
)
PROTECTED_GITHUB_DIRS = ("instructions", "prompts", "agents", "skills", "copilot")
# Licence files keep their upper-case convention, so ``src/licenses.py``
# stays editable while ``LICENSE.txt`` and ``LICENCE`` do not.
LICENCE_PREFIXES = ("LICENSE", "LICENCE")
LICENCE_DIR = "LICENSES"
WORKFLOW_PREFIX = ".github/workflows/"
MODE_REGULAR = "100644"
MODE_EXECUTABLE = "100755"
MODE_SYMLINK = "120000"
MODE_SUBMODULE = "160000"
LABEL = "code-monkey"
VERDICTS = (
    "proposed",
    "abstain",
    "handoff",
    "rejected",
    "author-failed",
    "publish-failed",
)
MANIFEST_OUTCOMES = ("proposed", "abstain", "handoff", "author-failed")
# The one manifest schema this publisher reads (docs/DESIGN.md 18.2).
MANIFEST_SCHEMA = 1
# AGENTS.md §11.6: a pipeline says in each pull request and its report
# how review findings reach a fix. Here a later round of this workflow
# revises the pull request (DESIGN.md section 20), and a human reads
# every round before anything merges.
REVISION_DISCLOSURE = (
    "Review findings on this pull request are answered by a later "
    "revision round of this workflow, up to ten rounds per antagonistic "
    "review round; after that a maintainer, or the next antagonistic "
    "review, takes on any finding that remains, and a maintainer "
    "reviews every round before merge."
)


class PublishError(Exception):
    """An operational failure: the publisher could not do its job."""


class Rejection(Exception):
    """The proposal broke a rule; recorded as a verdict, not an error."""


class Superseded(Rejection):
    """Another run's push moved the branch first; no segment's cap counts it."""


@dataclass(frozen=True)
class Identity:
    """The trailer identities the publisher appends to every commit."""

    coauthor: str
    bot_login: str
    bot_email: str

    @property
    def sign_off(self) -> str:
        """The bot's DCO trailer."""
        return f"Signed-off-by: {self.bot_login} <{self.bot_email}>"


def parse_title_limit(gitlint_text: str | None) -> int:
    """Read a gitlint subject limit, or fall back to the organisation rule."""
    if gitlint_text is None:
        return DEFAULT_TITLE_LIMIT
    section = None
    for raw in gitlint_text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "title-max-length" and line.startswith("line-length"):
            _, _, value = line.partition("=")
            if value.strip().isdigit():
                return int(value.strip())
    return DEFAULT_TITLE_LIMIT


def safe_path(path: str) -> bool:
    """Reject paths that escape the tree or touch git metadata."""
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..", ".git") for part in parts)


def protected(path: str) -> bool:
    """Agent instruction files, review and gate configuration, licences.

    The agent must never add, edit, delete or rename one of these.
    """
    parts = path.split("/")
    name = parts[-1]
    lowered = [part.lower() for part in parts]
    if lowered[-1] in PROTECTED_NAMES:
        return True
    if len(parts) == 1 and lowered[0] in PROTECTED_ROOT_FILES:
        return True
    if name.startswith(LICENCE_PREFIXES) or LICENCE_DIR in parts[:-1]:
        return True
    if any(part in PROTECTED_DIRS for part in lowered[:-1]):
        return True
    return (
        len(parts) > 2
        and lowered[0] == ".github"
        and lowered[1] in PROTECTED_GITHUB_DIRS
    )


def check_change_mode(path: str, status: str, old_mode: str, new_mode: str) -> None:
    """Refuse anything ``createCommitOnBranch`` cannot reproduce faithfully."""
    if status.startswith("T"):
        raise Rejection(f"{path}: type change (file/symlink) is not replayable")
    if new_mode == MODE_SYMLINK:
        raise Rejection(f"{path}: symlinks cannot be created through the API")
    if new_mode == MODE_SUBMODULE:
        raise Rejection(f"{path}: submodules are out of scope")
    if new_mode == MODE_EXECUTABLE:
        raise Rejection(
            f"{path}: executable files cannot be set or preserved through "
            "the API; leave this step to a human"
        )
    if new_mode != MODE_REGULAR:
        raise Rejection(f"{path}: unsupported mode {new_mode}")
    if status.startswith("M") and old_mode != new_mode:
        raise Rejection(f"{path}: mode change {old_mode} -> {new_mode}")


def split_message(message: str) -> tuple[str, list[str], list[str]]:
    """Return headline, body lines and the trailer block.

    The line after the subject must be blank before anything else is
    interpreted, so a trailer glued to the subject is a rejection
    rather than a trailer.
    """
    lines = message.rstrip("\n").split("\n")
    headline = lines[0].rstrip()
    rest = lines[1:]
    if rest and rest[0].strip():
        raise Rejection("commit message lacks a blank line after the subject")
    while rest and not rest[-1].strip():
        rest.pop()
    trailers: list[str] = []
    body = list(rest)
    while body and TRAILER_RE.match(body[-1]):
        trailers.insert(0, body.pop())
    while body and not body[-1].strip():
        body.pop()
    while body and not body[0].strip():
        body.pop(0)
    return headline, body, trailers


def check_headline(headline: str, limit: int) -> None:
    """Apply the subject rules a machine can judge."""
    if len(headline) > limit:
        raise Rejection(f"subject exceeds {limit} characters: {headline!r}")
    if not HEADLINE_RE.match(headline):
        raise Rejection(
            f"subject lacks a capitalised Conventional Commit type: {headline!r}"
        )
    if headline.rstrip()[-1] in ".;:!?,":
        raise Rejection(f"subject ends with punctuation: {headline!r}")


def check_body(body: list[str]) -> None:
    """Wrap rule: 72 columns unless the line carries a URL."""
    for line in body:
        if len(line) > BODY_LINE_LIMIT and not URL_RE.search(line):
            raise Rejection(f"body line exceeds {BODY_LINE_LIMIT} characters: {line!r}")


def comment_safe(text: str) -> str:
    """Flatten untrusted text for a one-line issue comment.

    Newlines would let agent output add rendered blocks, raw HTML
    would let it wrap the verdict after it in a collapsed element or
    a comment, and an @-mention would notify whoever it names. The
    markup is escaped, which GitHub renders as the characters typed,
    and a zero-width space after each @ keeps the text readable
    without the ping.
    """
    flat = " ".join(text.split()).replace("@", "@\u200b")
    return html.escape(flat, quote=False)


# A check name in a round note: the selection bounds the packet, and
# a note names each run briefly.
MAX_CHECK_NAME = 100


def failing_checks_text(names: list[Any]) -> str:
    """The failing checks a round set out to fix, as one safe line, or empty.

    Check names come from the workflows of the code under review, so
    each is flattened and escaped like agent text, and bounded.
    """
    safe = [
        comment_safe(name[:MAX_CHECK_NAME])
        for name in names
        if isinstance(name, str) and name.strip()
    ]
    return ", ".join(safe)


MENTION_RE = re.compile(r"(?<![\w@/.-])@(?=[A-Za-z0-9][A-Za-z0-9-]*(?:/[\w.-]+)?)")


def defuse_mentions(text: str) -> str:
    """Stop agent-written text notifying people, keeping it readable.

    A zero-width space after each @ that would start a user or team
    mention; addresses such as ``user@example.com`` and references
    such as ``actions/checkout@v4`` are left as they are.
    """
    return MENTION_RE.sub("@\u200b", text)


def log_safe(text: str) -> str:
    """Make untrusted text safe to print from a job whose log GitHub parses.

    A newline followed by ``::error::`` or ``::stop-commands::`` would
    let agent output spoof annotations or alter log-command parsing.
    """
    flat = " ".join(text.split())
    return flat.replace("::", ": :").replace("##[", "# #[")


def coauthor_for(model: str, mapping: dict[str, Any]) -> str:
    """Map a model identifier prefix to its trailer identity."""
    for prefix, trailer in mapping.items():
        if isinstance(trailer, str) and model.startswith(str(prefix)):
            return trailer
    raise PublishError(f"no co-author mapping for model {model!r}")


# A trailer whose key ends in -by (Signed-off-by, Co-authored-by,
# Reviewed-by and the like) makes a claim about a person.
IDENTITY_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z-]*-by:", re.I)


def compose_trailers(trailers: list[str], identity: Identity) -> list[str]:
    """Close the block with the assistant's co-author and the bot's sign-off.

    The proposal is untrusted and no person takes part in a session,
    so every agent-written trailer that names someone goes: a signed
    commit must not credit a human, or claim their DCO certification,
    on the agent's word. Trailers that name no one stay in order.
    """
    kept = [line for line in trailers if not IDENTITY_TRAILER_RE.match(line)]
    kept.append(f"Co-authored-by: {identity.coauthor}")
    kept.append(identity.sign_off)
    return kept


def compose_message(message: str, identity: Identity, limit: int) -> tuple[str, str]:
    """Check one commit message and return the headline and composed body."""
    headline, body, trailers = split_message(message)
    check_headline(headline, limit)
    check_body(body)
    composed = compose_trailers(trailers, identity)
    body_text = "\n".join(body).rstrip("\n")
    full_body = (body_text + "\n\n" if body_text else "") + "\n".join(composed)
    return headline, full_body


def check_pull_request_text(
    title: Any,
    body: Any,
    *,
    single_headline: str | None,
) -> tuple[str, str]:
    """Check the title and body the agent proposed; return them stripped.

    The body needs no closing keyword: the publisher opens every body
    with its own ``Closes #N`` line.
    """
    if not isinstance(title, str) or not title.strip():
        raise Rejection("manifest lacks a pull request title")
    if not isinstance(body, str) or not body.strip():
        raise Rejection("manifest lacks a pull request body")
    title = title.strip()
    if single_headline is not None and title != single_headline:
        raise Rejection("single-commit pull request title must equal the subject")
    if len(title) > MAX_PR_TITLE:
        raise Rejection(f"pull request title exceeds {MAX_PR_TITLE} characters")
    check_pull_request_body_size(body)
    return title, body.rstrip("\n")


def check_pull_request_body_size(body: str) -> None:
    """Refuse a composed body GitHub would reject, before any write happens."""
    if len(body) > MAX_PR_BODY:
        raise Rejection(
            f"pull request body exceeds {MAX_PR_BODY} characters after provenance"
        )


def provenance_block(
    *,
    model: str,
    run_url: str,
    issue_url: str,
    base_sha: str,
    commands: list[dict[str, Any]],
) -> str:
    """The block that heads every pull request body, before agent text."""
    lines = [
        "<details>",
        "<summary>Authored by an AI agent; human review required</summary>",
        "",
        f"- Issue: {issue_url}",
        f"- Model: `{model}` via GitHub Copilot CLI",
        f"- Run: {run_url or 'n/a'}",
        f"- Base: `{base_sha}`",
        "",
    ]
    if commands:
        lines += ["| Command | Exit |", "| --- | --- |"]
        for item in commands:
            # Agent-written text: one line, then escaped, so it stays in
            # its cell and cannot close the details block.
            command = " ".join(str(item["command"]).split())
            command = command.replace("|", "\\|").replace("`", "'").replace("<", "&lt;")
            code = item["exit_code"]
            lines.append(f"| `{command}` | {code if code is not None else '?'} |")
        lines.append("")
    lines += [
        "The `github-code-monkey-bot` workflow opened this pull request.",
        "Nobody has merged it; a maintainer must review it first.",
        REVISION_DISCLOSURE,
        "</details>",
        "",
    ]
    return "\n".join(lines)
