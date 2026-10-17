# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Copilot review bodies copied verbatim from organisation pull requests.

Each is the ``body`` of a review by ``copilot-pull-request-reviewer``,
named for the format and the verdict it should produce. The source
pull request and review id follow each name.
"""

from __future__ import annotations

BODIES: dict[str, str] = {
    # gerrit-clone-action#304, review 5366612856
    "v2-green-clean": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "## Copilot review overview\n"
        "\n"
        "### 🟢 Approval recommended\n"
        "\n"
        "No unresolved issues were identified; follow the stated merge order.\n"
        "\n"
        "**Review effort:** Balanced\n"
        "**Findings:** None\n"
    ),
    # test-rust-project#11, review 5464621166
    "v2-blue-clean": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🔵 Needs a closer look\n"
        "\n"
        "The first live run performs an irreversible crates.io publication and therefore warrants final human review.\n"
        "\n"
        "**0 open findings**\n"
        "\n"
        "🧠 **Review effort:** Balanced\n"
    ),
    # dependamerge#539, review 5425770192
    "v2-blue-with-findings": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "## Copilot review overview\n"
        "\n"
        "### 🔵 Needs a closer look\n"
        "\n"
        "Signature-sensitive asynchronous recovery needs human validation, particularly the unresolved refusal-handling gap.\n"
        "\n"
        "**Review effort:** Balanced\n"
        '**Findings:** 1 <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture>\n'
        "\n"
        "<details open>\n"
        "<summary><strong>Open (1)</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Recheck merge refusal after approval recovery](#discussion_r4193156909) · New\n'
        "</details>\n"
    ),
    # github-code-monkey-bot#4, review 5454189313
    "v2-yellow-open-findings": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🟡 Changes recommended\n"
        "\n"
        "The shared-module template PR must land before these copied changes are merged.\n"
        "\n"
        "<details open>\n"
        "<summary><strong>1 open finding</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Land template PR before refreshing copied shared files](#discussion_r4216991778) · New\n'
        "</details>\n"
        "\n"
        "🧠 **Review effort:** Balanced\n"
        "\n"
        "---\n"
        "\n"
        "Give feedback about Copilot approvals in [this survey](https://survey.alchemer.com/s3/9011660/CCR-Public-Preview-Autoapprove-feedback-survey) to enter a drawing for a $150 gift card.\n"
    ),
    # dependamerge#543, review 5407070987
    "v2-overview-yellow": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "## Copilot review overview\n"
        "\n"
        "### 🟡 Changes recommended\n"
        "\n"
        "An empty required environment input bypasses the intended secret gate.\n"
        "\n"
        "**Review effort:** Balanced\n"
        '**Findings:** 1 <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture>\n'
        "\n"
        "<details open>\n"
        "<summary><strong>Open (1)</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Reject empty environment input before running the action](#discussion_r4178370628) · New\n'
        "</details>\n"
    ),
    # gerrit-clone-action#263, review 5022960157
    "legacy-no-comments": (
        "## Pull request overview\n"
        "\n"
        "Copilot reviewed 2 out of 3 changed files in this pull request and generated no new comments.\n"
    ),
    # gerrit-clone-action#267, review 5049400713
    "legacy-comments": (
        "## Pull request overview\n"
        "\n"
        "Copilot reviewed 9 out of 9 changed files in this pull request and generated 4 comments.\n"
    ),
    # gerrit-clone-action#263, review 5022650710
    "legacy-suppressed": (
        "## Pull request overview\n"
        "\n"
        "Copilot reviewed 2 out of 3 changed files in this pull request and generated 1 comment.\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "<details>\n"
        "<summary>Suppressed comments (1)</summary>\n"
        "\n"
        "**.github/workflows/testing.yaml:241**\n"
        "* This sync also builds the editable project under PEP 517 isolation, where `hatchling` and `hatch-vcs` may be resolved independently of `uv.lock`. Keeping those packages in the dev group does not make the isolated build environment consume their locked artifacts, so this path still permits unpinned downloads. Please make the build environment consume locked build tools or otherwise constrain and verify those artifacts.\n"
        "```\n"
        "          uv sync --locked\n"
        "```\n"
        "</details>\n"
    ),
}
