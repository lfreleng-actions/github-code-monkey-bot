# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The run's pull request links, for the reusable workflow's outputs.

The report job exposes the pull requests a run opened or revised so a
caller can announce them without knowing the run's namespace. Results
are not yet authenticated (docs/DESIGN.md 18.4), so a link counts only
when the whole value names a pull request in the run's organisation;
anything else is counted and left out, never copied.
"""

from __future__ import annotations

import json
import re
from typing import Any


def pull_request_pattern(org: str) -> re.Pattern[str]:
    """Match a pull request URL in ``org``, compared case-insensitively.

    Case folding is ASCII only: Unicode folding would let a dotless i
    (U+0131), a long s (U+017F) or a Kelvin sign (U+212A) stand in for an
    ASCII letter and so pass a link to another host.
    """
    return re.compile(
        rf"https://github\.com/{re.escape(org)}/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*",
        re.IGNORECASE | re.ASCII,
    )


def pull_request_urls(results: list[dict[str, Any]], org: str) -> tuple[list[str], int]:
    """Live pull request links in report order, once each, and the refused count.

    A dry run, or a result with no link, names no pull request and is
    skipped quietly; a link that fails the pattern is counted.
    """
    pattern = pull_request_pattern(org)
    urls: list[str] = []
    seen: set[str] = set()
    excluded = 0
    for item in results:
        url = item.get("pull_request_url")
        if item.get("dry_run") is not False or url is None or url == "":
            continue
        if not isinstance(url, str) or pattern.fullmatch(url) is None:
            excluded += 1
            continue
        if url.lower() not in seen:
            seen.add(url.lower())
            urls.append(url)
    return urls, excluded


def output_lines(urls: list[str]) -> str:
    """``$GITHUB_OUTPUT`` lines: fixed keys, values that hold no newline."""
    # json.dumps escapes every control character, so the array is one line.
    value = json.dumps(urls, separators=(",", ":"))
    return f"pull_requests={value}\npull_request_count={len(urls)}\n"
