# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch an untrusted proposal artifact without letting it expand unbounded.

The mechanics live in the shared ``artifact_fetch``: find the artifact
through the API, refuse one whose zip exceeds the sum of the permitted
file caps, stream it to disk under that limit, and extract the
permitted files alone, each read with a hard stop. This module holds
the proposal's cap table, ``PROPOSAL_FILES``: the three files the
publisher takes from an author session and the cap on each.

Exit status: 0 accepted, with ``artifact_id=<id>`` on stdout; 3 no
such artifact; 4 artifact refused. Anything else is an operational
failure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import artifact_fetch
import bot_github as github
from artifact_fetch import NOT_FOUND, REFUSED, Missing, Refused

__all__ = ["NOT_FOUND", "PROPOSAL_FILES", "REFUSED", "Missing", "Refused", "main"]

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_USAGE_BYTES = 1024 * 1024

# (name, byte cap, required)
PROPOSAL_FILES: artifact_fetch.Profile = (
    ("manifest.json", MAX_MANIFEST_BYTES, True),
    ("changes.bundle", MAX_BUNDLE_BYTES, False),
    ("usage.json", MAX_USAGE_BYTES, False),
)


def fetch(
    repository: str, run_id: str, name: str, output: Path
) -> tuple[int, list[str]]:
    """Find, bound, download and extract one proposal artifact.

    Returns the artifact ID, which names the author session that
    produced it, and the files extracted.
    """
    return artifact_fetch.fetch_from_run(
        repository, run_id, name, output, PROPOSAL_FILES
    )


def main(argv: list[str] | None = None) -> None:
    """Fetch the proposal; the exit status says what happened."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        artifact_id, copied = fetch(
            args.repository, args.run_id, args.name, args.output
        )
    except Missing as exc:
        print(f"proposal: {exc}", file=sys.stderr)
        raise SystemExit(NOT_FOUND) from exc
    except Refused as exc:
        print(f"proposal refused: {exc}", file=sys.stderr)
        raise SystemExit(REFUSED) from exc
    except (OSError, subprocess.SubprocessError, github.GitHubError) as exc:
        parser.exit(1, f"proposal fetch: {github.safe_message(exc)}\n")
    print(f"accepted: {', '.join(copied)}", file=sys.stderr)
    # Stdout is the step's output file: each upload gets a new ID, so
    # the report can tell a rerun author session from a publish retry.
    print(f"artifact_id={artifact_id}")


if __name__ == "__main__":
    main()
