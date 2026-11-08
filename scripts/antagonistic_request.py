# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Which published pull requests to nominate for an antagonistic review.

Once Copilot's review of a published head is clean, the workflow asks
the review bot, a different model, for an antagonistic review
(docs/DESIGN.md section 20.5). The nomination decides nothing: the
review bot's own selection re-checks the clean review of the current
head, the author, its round cap and whether it already reviewed the
head. So the rule here is conservative and a forged result costs at
most a skipped selection on the other side.

The publish job records the state in ``result.json`` for the comment;
the report job recomputes the rule from every result for the request
job, and never trusts the recorded state.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, cast

import proposal_model as model
import review_outcome
from round_caps import AT_CAP, at_cap

STATES = ("requested", "off", "skipped", "dry-run")
# The checks a nomination accepts: the antagonistic mode does not wait for CI.
ACCEPTED_CHECKS = ("passing", "running", "none")
PULL_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)",
    re.ASCII,
)


def unmet(item: dict[str, Any]) -> str | None:
    """The first nomination condition a result fails, or None when it qualifies."""
    if item.get("verdict") != "proposed":
        return "not a published proposal"
    if item.get("dry_run") is not False:
        return "a dry run"
    if not item.get("pull_request_url"):
        return "no pull request was opened"
    if review_outcome.review_verdict(item) != "clean":
        return "Copilot's review of the head is not clean"
    head = review_outcome.published_commit(item)
    commit = cast("dict[str, Any]", item["review"]).get("commit_id")
    if head is None or commit != head:
        return "Copilot's clean review is not of the published head"
    if item.get("mergeable") is False:
        return "the head conflicts with the default branch"
    if review_outcome.checks_failed(item):
        return "the head has failing checks"
    if review_outcome.checks_state(item) not in ACCEPTED_CHECKS:
        return "the head's checks are unknown"
    if at_cap(item):
        return AT_CAP
    return None


def state(
    item: dict[str, Any], *, switch: bool, repository: str
) -> dict[str, str | None]:
    """The ``antagonistic`` record for a result, as the publish job sees it.

    ``requested`` means the result is nominated and the request job
    will ask; whether the dispatch landed is that job's summary.
    """
    if not switch:
        return {"state": "off", "reason": None}
    if item.get("dry_run") is not False:
        return {"state": "dry-run", "reason": None}
    if item.get("mode") != "pull-requests":
        return {"state": "skipped", "reason": "not pull-requests mode"}
    if not repository:
        return {"state": "skipped", "reason": "no antagonistic_repository"}
    reason = unmet(item)
    if reason is not None:
        return {"state": "skipped", "reason": reason}
    return {"state": "requested", "reason": None}


def requested(item: dict[str, Any]) -> bool:
    """Whether the result records a nomination the request job will act on."""
    record = item.get("antagonistic")
    return (
        isinstance(record, dict)
        and cast("dict[str, Any]", record).get("state") == "requested"
    )


def nominee(url: str) -> str | None:
    """A validated pull request URL as ``owner/repo#N``, or None."""
    match = PULL_URL.fullmatch(url)
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}#{match['number']}"


def nominees(results: list[dict[str, Any]], urls: list[str]) -> list[str]:
    """``owner/repo#N`` for each validated URL whose result qualifies, in order.

    ``urls`` are the report's own validated links; a result counts only
    when its link is among them, so nothing outside the organisation
    or of another shape is ever named.
    """
    allowed = {url.lower() for url in urls}
    named: list[str] = []
    for item in results:
        url = item.get("pull_request_url")
        if not isinstance(url, str) or url.lower() not in allowed:
            continue
        if unmet(item) is not None:
            continue
        pull = nominee(url)
        if pull is not None and pull.lower() not in {n.lower() for n in named}:
            named.append(pull)
    return named


def output_lines(pulls: list[str]) -> str:
    """The ``$GITHUB_OUTPUT`` line for the nominated pull requests."""
    return f"antagonistic_pull_requests={json.dumps(pulls, separators=(',', ':'))}\n"


def main(argv: list[str] | None = None) -> None:
    """Record the ``antagonistic`` state in ``result.json``, offline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--switch", choices=("true", "false"), required=True)
    parser.add_argument("--repository", default="")
    args = parser.parse_args(argv)
    try:
        result = model.load_json(args.result, "result")
        result["antagonistic"] = state(
            result, switch=args.switch == "true", repository=args.repository
        )
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"antagonistic request: {message}\n")
    print(f"antagonistic review: {result['antagonistic']['state']}")


if __name__ == "__main__":
    main()
