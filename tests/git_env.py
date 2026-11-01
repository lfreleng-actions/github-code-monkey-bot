# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""An environment for test fixtures free of git's repository-local variables.

git exports ``GIT_DIR``, ``GIT_INDEX_FILE`` and others to the hooks it
runs. In a linked worktree ``GIT_DIR`` names that worktree's admin
directory, and it overrides ``git -C <path>``. A suite run from a
``pre-commit`` hook would otherwise build its fixtures inside the
developer's own repository.
"""

from __future__ import annotations

import functools
import os
import subprocess

# Indexed config variables: ``GIT_CONFIG_COUNT`` names how many pairs
# there are, but a stray pair is dropped too.
INDEXED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


@functools.cache
def local_variables() -> frozenset[str]:
    """The names ``git rev-parse --local-env-vars`` prints.

    Asked with every ``GIT_*`` variable removed, so the answer does not
    depend on the repository the caller happens to be in.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        cwd=os.path.abspath(os.sep),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return frozenset(proc.stdout.split())


def clean_git_env() -> dict[str, str]:
    """A copy of ``os.environ`` without git's repository-local variables."""
    names = local_variables()
    return {
        key: value
        for key, value in os.environ.items()
        if key not in names and not key.startswith(INDEXED_PREFIXES)
    }
