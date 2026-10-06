# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch an untrusted proposal artifact without letting it expand unbounded.

``download-artifact`` extracts an archive before anything can check
its size, so a small, highly compressible upload could fill the
publisher's disk. This fetch instead finds the artifact through the
API, refuses one whose zip exceeds the sum of the permitted file caps,
streams the zip to disk under that limit, inspects the zip directory,
and extracts the permitted files alone, each read with a hard stop so
a zip that lies about sizes cannot expand past its cap.

Exit status: 0 accepted, with ``artifact_id=<id>`` on stdout; 3 no
such artifact; 4 artifact refused. Anything else is an operational
failure.
"""

from __future__ import annotations

import argparse
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import monkey_github as github
from monkey_evidence import ACCEPTED_FILES

# The three permitted files at their caps, plus zip overhead.
ZIP_LIMIT = sum(limit for _, limit, _ in ACCEPTED_FILES) + 1024 * 1024
MAX_ENTRIES = 16
CHUNK = 64 * 1024
TIMEOUT_SECONDS = 300
NOT_FOUND = 3
REFUSED = 4


class Refused(Exception):
    """The artifact breaks a bound; the proposal is not accepted."""


class Missing(Exception):
    """The run holds no such artifact."""


def find_artifact(repository: str, run_id: str, name: str) -> tuple[int, int]:
    """The id and zip size of the one live artifact of this name in the run."""
    entries = github.api_object(
        f"repos/{repository}/actions/runs/{run_id}/artifacts"
        f"?name={quote(name, safe='')}&per_page=10"
    ).get("artifacts")
    if not isinstance(entries, list):
        raise github.GitHubError("artifact listing carried no artifacts")
    live = [
        cast("dict[str, Any]", entry)
        for entry in cast("list[Any]", entries)
        if isinstance(entry, dict)
        and cast("dict[str, Any]", entry).get("name") == name
        and not cast("dict[str, Any]", entry).get("expired")
    ]
    if not live:
        raise Missing(f"no artifact named {name}")
    if len(live) > 1:
        raise Refused(f"{len(live)} artifacts named {name}")
    artifact_id = github.require_int(live[0], "id", "artifact")
    size = live[0].get("size_in_bytes")
    if type(size) is not int or size < 0:
        raise github.GitHubError("artifact carries no size")
    return artifact_id, size


def download(repository: str, artifact_id: int, target: Path) -> None:
    """Stream the zip to ``target``, stopping past ZIP_LIMIT or the deadline."""
    written = 0
    expired = threading.Event()
    with target.open("wb") as sink:
        proc = subprocess.Popen(
            ["gh", "api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = proc.stdout
        if stdout is None:
            proc.kill()
            raise github.GitHubError("could not read the artifact download")

        def expire() -> None:
            expired.set()
            proc.kill()

        # A read blocks until gh writes or exits, so no check between
        # reads can enforce a deadline; killing gh ends the read instead.
        timer = threading.Timer(TIMEOUT_SECONDS, expire)
        timer.start()
        try:
            while True:
                chunk = stdout.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > ZIP_LIMIT:
                    raise Refused(f"artifact zip exceeds {ZIP_LIMIT} bytes")
                sink.write(chunk)
        except BaseException:
            # Refusing mid-stream: stop gh rather than drain the rest.
            proc.kill()
            raise
        finally:
            stdout.close()
            # Still under the timer, so this wait is bounded too.
            proc.wait()
            timer.cancel()
    if expired.is_set():
        raise github.GitHubError(
            f"artifact download timed out after {TIMEOUT_SECONDS} seconds"
        )
    if proc.returncode != 0:
        raise github.GitHubError(
            f"artifact download failed (gh exit {proc.returncode})"
        )


def extract(archive: Path, output: Path) -> list[str]:
    """Extract the permitted files from the zip, each under its cap."""
    caps = {name: (limit, required) for name, limit, required in ACCEPTED_FILES}
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES:
                raise Refused(f"artifact holds {len(entries)} entries")
            found: dict[str, zipfile.ZipInfo] = {}
            for entry in entries:
                if entry.filename not in caps:
                    continue
                if entry.filename in found:
                    raise Refused(f"artifact holds {entry.filename} twice")
                # A recorded type must be a regular file; symlinks and
                # the like are refused. No type bits means none recorded.
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if entry.is_dir() or kind not in (0, stat.S_IFREG):
                    raise Refused(f"{entry.filename} is not a regular file")
                if entry.file_size > caps[entry.filename][0]:
                    raise Refused(f"{entry.filename} exceeds its cap")
                # zipfile raises NotImplementedError or RuntimeError for
                # these on read; refuse them here instead.
                if entry.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    raise Refused(f"{entry.filename} uses unsupported compression")
                if entry.flag_bits & 0x1:
                    raise Refused(f"{entry.filename} is encrypted")
                found[entry.filename] = entry
            output.mkdir(parents=True, exist_ok=True)
            copied: list[str] = []
            for name, (limit, required) in caps.items():
                if name not in found:
                    if required:
                        raise Refused(f"artifact lacks {name}")
                    continue
                with bundle.open(found[name]) as source:
                    # Read past the cap by one byte: a header that
                    # understates the size cannot expand further.
                    content = source.read(limit + 1)
                if len(content) > limit:
                    raise Refused(f"{name} expands past its cap")
                (output / name).write_bytes(content)
                copied.append(name)
            return copied
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise Refused(f"artifact is not a valid zip: {exc}") from exc


def fetch(
    repository: str, run_id: str, name: str, output: Path
) -> tuple[int, list[str]]:
    """Find, bound, download and extract one proposal artifact.

    Returns the artifact ID, which names the author session that
    produced it, and the files extracted.
    """
    artifact_id, size = find_artifact(repository, run_id, name)
    if size > ZIP_LIMIT:
        raise Refused(f"artifact zip is {size} bytes, over {ZIP_LIMIT}")
    with tempfile.TemporaryDirectory() as holder:
        archive = Path(holder) / "proposal.zip"
        download(repository, artifact_id, archive)
        return artifact_id, extract(archive, output)


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
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"proposal fetch: {message}\n")
    print(f"accepted: {', '.join(copied)}", file=sys.stderr)
    # Stdout is the step's output file: each upload gets a new ID, so
    # the report can tell a rerun author session from a publish retry.
    print(f"artifact_id={artifact_id}")


if __name__ == "__main__":
    main()
