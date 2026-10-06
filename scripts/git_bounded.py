# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Run git with a bound on how much output it may produce.

The publisher reads objects out of an agent's bundle. A small
compressed bundle can carry an enormous blob, tree or commit message,
so every read is capped: past the cap git is stopped and the read
either fails as a rejection (the proposal is too large to publish)
or, for a caller that only inspects the start of an object, returns
what it has.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from proposal_policy import PublishError, Rejection

TIMEOUT_SECONDS = 300
# Operating-system ceilings for every git process. A small compressed
# bundle can expand enormously while git indexes it, before any policy
# check runs; these stop that git process instead of the runner. The
# legitimate work (a shallow fetch of an action repository and a few
# small commits) stays far inside them.
LIMITS = {
    "RLIMIT_AS": 4 * 1024 * 1024 * 1024,
    "RLIMIT_CPU": 240,
    "RLIMIT_FSIZE": 1024 * 1024 * 1024,
}
CHUNK = 64 * 1024
# Enough for any command the checks run on a proposal within the
# policy limits; far below what would strain the runner.
DEFAULT_LIMIT = 8 * 1024 * 1024


def limit_resources() -> None:
    """Apply LIMITS in the child before git starts.

    Best effort per limit: a platform that refuses one (macOS will
    not cap address space) keeps the others. Hosted runners are Linux,
    where all three apply.
    """
    for name, value in LIMITS.items():
        kind = getattr(resource, name, None)
        if kind is None:
            continue
        try:
            resource.setrlimit(kind, (value, value))
        except (ValueError, OSError):
            continue


def kill_group(pgid: int) -> None:
    """Kill git and every helper it started; a group already gone is fine."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def run(workdir: Path, args: tuple[str, ...], *, limit: int, head: bool) -> bytes:
    """Stream git's output, stopping once it passes ``limit`` bytes."""
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(
                ["git", "-C", str(workdir), *args],
                stdout=subprocess.PIPE,
                stderr=errors,
                preexec_fn=limit_resources,
                # Its own session, so git's helpers (index-pack and the
                # like) share one process group that can be killed whole.
                start_new_session=True,
            )
        except OSError as exc:
            raise PublishError(f"git {' '.join(args)}: {exc}") from exc
        timer = threading.Timer(TIMEOUT_SECONDS, kill_group, args=(proc.pid,))
        timer.start()
        chunks: list[bytes] = []
        total = 0
        stdout = proc.stdout
        assert stdout is not None  # noqa: S101 - PIPE guarantees a stream
        try:
            while total <= limit:
                chunk = stdout.read(min(CHUNK, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            overflow = total > limit
            if overflow:
                kill_group(proc.pid)
            stdout.close()
            proc.wait()
            timer.cancel()
            # Whatever the outcome, nothing git started may outlive it.
            kill_group(proc.pid)
        output = b"".join(chunks)
        if overflow:
            if head:
                return output[:limit]
            raise Rejection(
                f"git {' '.join(args[:2])} produced more than {limit} bytes; "
                "the proposal is too large to check"
            )
        if proc.returncode != 0:
            errors.seek(0)
            detail = errors.read(4096).decode("utf-8", "replace").strip()
            if proc.returncode < 0:
                detail = detail or f"stopped after {TIMEOUT_SECONDS} seconds"
            raise PublishError(f"git {' '.join(args[:2])} failed: {detail[:500]}")
        return output


def git(
    workdir: Path,
    *args: str,
    binary: bool = False,
    limit: int = DEFAULT_LIMIT,
    head: bool = False,
) -> bytes | str:
    """Run git in the working clone with bounded output, failing loudly on error."""
    output = run(workdir, args, limit=limit, head=head)
    return output if binary else output.decode("utf-8", "replace")
