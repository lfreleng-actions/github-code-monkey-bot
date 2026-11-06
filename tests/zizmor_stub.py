# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A stand-in ``zizmor`` on PATH that records each call and answers per tree.

The online audit runs zizmor once in ``audit-base`` and once in
``audit-head``. The stub answers each with fixed JSON, records its
arguments, working directory and environment, and can fail, emit
something other than JSON, or hang, so the tests can drive every
path the audit takes without the network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT = """\
import json, os, sys, time
spec = json.load(open({spec!r}, encoding="utf-8"))
with open({log!r}, "a", encoding="utf-8") as log:
    log.write(json.dumps({{
        "argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)
    }}) + "\\n")
tree = os.path.basename(os.getcwd()).removeprefix("audit-")
time.sleep(spec["sleep"])
sys.stdout.write(spec["raw"] if spec["raw"] is not None else json.dumps(spec[tree]))
sys.exit(spec["exits"].get(tree, spec["exit"]))
"""


def finding(
    ident: str, path: str, *, line: int = 1, ignored: bool = False
) -> dict[str, Any]:
    """One finding in the shape zizmor's ``--format json`` emits."""
    return {
        "ident": ident,
        "desc": f"{ident} description",
        "determinations": {"confidence": "High", "severity": "High"},
        "locations": [
            {
                "symbolic": {
                    "key": {"Local": {"verbatim_path": f"./{path}"}},
                    "annotation": f"{ident} at line {line}",
                    "kind": "Primary",
                },
                "concrete": {"location": {"start_point": {"row": line}}},
            }
        ],
        "ignored": ignored,
    }


class StubZizmor:
    """Write the stub into ``root/bin`` and read back what it recorded."""

    def __init__(
        self,
        root: Path,
        *,
        base: list[Any] | None = None,
        head: list[Any] | None = None,
        exit_code: int = 0,
        exits: dict[str, int] | None = None,
        raw: str | None = None,
        sleep: float = 0,
    ) -> None:
        """Lay out the answers and the executable; nothing runs yet.

        ``exits`` overrides ``exit_code`` for one tree, by name.
        """
        self.bin = root / "stub-bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = root / "stub-calls.jsonl"
        spec = root / "stub-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "base": base or [],
                    "head": head or [],
                    "exit": exit_code,
                    "exits": exits or {},
                    "raw": raw,
                    "sleep": sleep,
                }
            ),
            encoding="utf-8",
        )
        script = self.bin / "zizmor"
        script.write_text(
            f"#!{sys.executable}\n" + SCRIPT.format(spec=str(spec), log=str(self.log)),
            encoding="utf-8",
        )
        script.chmod(0o755)

    def path(self) -> str:
        """PATH with the stub ahead of everything else."""
        return str(self.bin) + os.pathsep + os.environ.get("PATH", "")

    def calls(self) -> list[dict[str, Any]]:
        """Every recorded call, oldest first."""
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line
        ]
