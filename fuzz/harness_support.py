# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What every fuzz harness here shares, none of it needing Atheris.

Each harness is a ``fuzz_*.py`` file beside this one. It fuzzes one
plain ``check(data)`` function, which raises ``Breach`` when the code
under test breaks the contract the harness states; the unit suite
replays every seed through the same function (tests/test_fuzz_harnesses.py).
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class Breach(Exception):
    """An input on which the code under test broke its documented contract."""


def on_path() -> None:
    """Put the workflow's scripts where a harness can import them."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def arguments(argv: Sequence[str], seeds: Sequence[bytes]) -> list[str]:
    """The libFuzzer arguments, with a seed directory after any given.

    Named input files run once each, so they get no seeds; otherwise
    the seeds are written to a fresh temporary directory appended to
    the arguments, and a run with none fuzzes from them.
    """
    args = list(argv)
    if any(not arg.startswith("-") and Path(arg).is_file() for arg in args[1:]):
        return args
    directory = Path(tempfile.mkdtemp(prefix="fuzz-seeds-"))
    for index, seed in enumerate(seeds):
        (directory / f"seed-{index:03d}").write_bytes(seed)
    return [*args, str(directory)]


def as_text(data: bytes) -> str:
    """Decode an input as text that JSON could carry, lone surrogates included."""
    try:
        return data.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def encodes(text: str) -> bool:
    """Whether a strict UTF-8 stream, as a job's log or summary file is, takes the text."""
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return True
