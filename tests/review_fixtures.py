# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Copilot review bodies copied verbatim from organisation pull requests.

Each is the ``body`` of a review by ``copilot-pull-request-reviewer``,
named for the format and the verdict it should produce. The source
pull request and review id follow each name.
"""

from __future__ import annotations

BODIES: dict[str, str] = {
    # github-code-monkey-bot#36, review 5557503469: the current
    # overview format, with the effort footer in <sub> and the skill
    # promotion link in <a>.
    "v2-blue-clean-current": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🔵 Needs a closer look\n"
        "\n"
        "Review-body parsing and the unverified App-token request path warrant final human validation.\n"
        "\n"
        "**No open issues** 🎉\n"
        "\n"
        "<sub>Review effort: Balanced</sub>"
    ),
    # github-code-monkey-bot#36, review 5557455765
    "v2-yellow-open-current": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🟡 Changes recommended\n"
        "\n"
        "Two unresolved visibility-parsing defects can incorrectly report a review as clean.\n"
        "\n"
        "<details open>\n"
        "<summary><strong>2 open issues</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Handle escaped backticks without skipping hidden comment content](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4302403039) · New\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Account for raw HTML block context when parsing headings and fences](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4302403060) · New\n'
        "</details>\n"
        "\n"
        "<details>\n"
        "<summary><strong>2 resolved since last review</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Fail closed on unsupported raw HTML blocks](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4297855825)\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Ignore escaped backticks when detecting code spans](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4297855730)\n'
        "</details>\n"
        "\n"
        "<sub>Review effort: Balanced</sub>"
    ),
    # github-code-monkey-bot#36, review 5550838957: a first review in the
    # current format, with the file table and the skill promotion in <a>.
    "v2-yellow-first-current": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🟡 Changes recommended\n"
        "\n"
        "Classification and record-validation gaps can falsely report clean reviews, and paginated reads can exceed the wait deadline.\n"
        "\n"
        "<details open>\n"
        "<summary><strong>5 open findings</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Require complete assessment records before reporting clean](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998308) · New\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Require only whitespace after closing Markdown fences](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998436) · New\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Select the earliest recognized assessment heading](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998503) · New\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Classify oversized review bodies instead of reporting them clean](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998578) · New\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Enforce deadline budget across paginated thread reads](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998657) · New\n'
        "</details>\n"
        "\n"
        "<details>\n"
        "<summary><strong>What changed in this PR</strong></summary>\n"
        "\n"
        "Adds Copilot review requests, bounded waiting and verdict reporting to the trusted publication pipeline. Revision rounds remain a follow-up.\n"
        "\n"
        "**Changes:**\n"
        "- Classifies reviews as clean, findings or pending.\n"
        "- Requests and awaits reviews using separate write and read tokens.\n"
        "- Reports verdicts on issues and in run summaries, with tests and documentation.\n"
        "\n"
        "| File | Description |\r\n"
        "| ---- | ----------- |\r\n"
        "| tests/​test_workflow.py | Pins review steps, tokens and defaults. |\r\n"
        "| tests/​test_review_verdict.py | Tests classification and untrusted bodies. |\r\n"
        "| tests/​test_publish.py | Tests verdict reporting and rerun markers. |\r\n"
        "| tests/​test_copilot_review.py | Tests requests, polling and records. |\r\n"
        "| tests/​review_fixtures.py | Supplies real review bodies. |\r\n"
        "| scripts/​review_verdict.py | Classifies assessments and findings. |\r\n"
        "| scripts/​publish.py | Adds review commands. |\r\n"
        "| scripts/​proposal_report.py | Adds review columns and totals. |\r\n"
        "| scripts/​issue_comment.py | Includes verdicts in outcome comments. |\r\n"
        "| scripts/​copilot_review.py | Requests, polls and records reviews. |\r\n"
        "| README.md | Describes the review flow. |\r\n"
        "| prompt/​author.md | Updates publication and review expectations. |\r\n"
        "| docs/​DESIGN.md | Documents review contracts and rollout limitations. |\r\n"
        "| .github/​workflows/​code-monkey.yaml | Integrates review steps and extends timeout. |\r\n"
        "| .github/​workflows/​code-monkey-cron.yaml | Exposes and forwards the review toggle. |\n"
        "</details>\n"
        "\n"
        "🧠 **Review effort:** Balanced\n"
        "\n"
        "---\n"
        "\n"
        '💡 <a href="/lfreleng-actions/github-code-monkey-bot/new/main?filename=.github/skills/code-review/SKILL.md" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Add a `code-review` agent skill</a> or configure MCP servers for context-aware, tailored reviews. <a href="https://docs.github.com/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review?tool=webui#mcp-servers-and-agent-skills" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Learn more in the docs.</a>'
    ),
    # github-code-monkey-bot#36, review 5577731412: no open issues, and
    # three findings on code unchanged since the last review, which
    # Copilot lists in the body alone and opens no thread for.
    "v2-blue-previously-missed-current": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🔵 Needs a closer look\n"
        "\n"
        "Unresolved classifier gaps and the unverified live App-token request path require fixes and human validation.\n"
        "\n"
        "**No open issues** 🎉\n"
        "\n"
        "<details>\n"
        "<summary><strong>3 previously missed issues</strong></summary>\n"
        "\n"
        "In code that hasn't changed since last review\n"
        "\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Inline code masking hides suppressed entries</summary>\n'
        "\n"
        "`scripts/​review_text.py:249`\n"
        "\n"
        "Masking inline code erases code-formatted suppressed entries before they can be counted. With a green assessment, zero threads and `### Suppressed comments (0)`, the entry ``**`scripts/x.py:12`**`` becomes bold `x` characters with no path or line number. Classification therefore returns `clean` with zero suppressed findings, even though listed entries must override the stated zero. Preserve entry information before masking, or mark the affected suppressed section unreadable. Add a regression while keeping ordinary inline examples elsewhere ignored.\n"
        "</details>\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Bold-italic suppressed entries are not recognized</summary>\n'
        "\n"
        "`scripts/​review_verdict.py:123`\n"
        "\n"
        "Bold-italic entries bypass this matcher. With a green assessment, zero threads and `### Suppressed comments (0)`, either `***scripts/x.py:12***` or `___scripts/x.py:12___` returns `clean` with zero suppressed findings; the ordinary bold form counts one. Recognize these additional emphasis forms so listed findings still override the stated zero, and add both cases to the entry-format regression tests.\n"
        "</details>\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Normalization hides suppressed findings after heading boundaries</summary>\n'
        "\n"
        "`scripts/​review_verdict.py:308`\n"
        "\n"
        "Removing strong tags can create a section-ending heading and hide a suppressed finding. With a green assessment and zero threads, `### Suppressed comments (0)` followed by a blank line, `<strong>### Context</strong>`, another blank line and `**scripts/x.py:12**` returns `clean` with `suppressed=0`. The wrapped hashes are not a Markdown heading, but normalization makes `SUPPRESSED_END` stop before the entry. Preserve the original boundaries or fail closed when normalization creates one, and add this regression. The suggested guard takes the fail-closed approach.\n"
        "</details>\n"
        "</details>\n"
        "\n"
        "<details>\n"
        "<summary><strong>1 resolved since last review</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Accept punctuation after closing underscores in bold finding entries](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4318742999)\n'
        "</details>\n"
        "\n"
        "<sub>Review effort: Balanced</sub>"
    ),
    # github-code-monkey-bot#36, review 5551026701: the older form of
    # the same section, "Previously missed (N)".
    "v2-blue-previously-missed-legacy": (
        "<!-- ccr-overview-v2 -->\n"
        "\n"
        "### 🔵 Needs a closer look\n"
        "\n"
        "Parsing, head verification and rerun handling can report incorrect review outcomes, while an initial read failure leaves an incomplete record.\n"
        "\n"
        "**0 open findings**\n"
        "\n"
        "<details>\n"
        "<summary><strong>5 resolved since last review</strong></summary>\n"
        "\n"
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Classify oversized review bodies instead of reporting them clean](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998578)\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Select the earliest recognized assessment heading](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998503)\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Require only whitespace after closing Markdown fences](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998436)\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Require complete assessment records before reporting clean](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998308)\n'
        '- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> [Enforce deadline budget across paginated thread reads](https://github.com/lfreleng-actions/github-code-monkey-bot/pull/36#discussion_r4296998657)\n'
        "</details>\n"
        "\n"
        "<details>\n"
        "<summary><strong>Previously missed (4)</strong></summary>\n"
        "\n"
        "In code that hasn't changed since last review\n"
        "\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Revalidate the pull request head after polling</summary>\n'
        "\n"
        "`scripts/​copilot_review.py:222`\n"
        "\n"
        "The head is read only before polling. If a push changes the pull request from A to B during the wait, this still accepts a clean review of A and the issue comment announces a clean review without identifying the old commit. B has not been reviewed. Verify that the head still matches before accepting the verdict, within the read budget, and return pending if it changed or cannot be verified. Add a test where the head changes during polling.\n"
        "</details>\n"
        "\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Handle initial head read failures as pending results</summary>\n'
        "\n"
        "`scripts/​copilot_review.py:273`\n"
        "\n"
        "The initial head read is outside the error handling in await_review(). If it exhausts retries, review-await exits without writing a complete pending record or its failure reason; after a successful request, result.json still contains only the requested flag. Convert this read failure to pending and serialize it through the normal result-writing path. Add a handler test for a failed head read.\n"
        "</details>\n"
        "\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Deduplicate comments using only the latest bot outcome</summary>\n'
        "\n"
        "`scripts/​issue_comment.py:169`\n"
        "\n"
        "If reruns produce pending → clean → pending, posted() finds the first pending marker and suppresses the third comment. The latest issue outcome still says clean while the current report says pending. Deduplicate against only the latest bot outcome for this run, rather than any historical matching verdict, and test a verdict that changes back to an earlier value.\n"
        "</details>\n"
        "\n"
        "<details>\n"
        '<summary><picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/medium-v2-light.png" alt="Medium severity" width="62" height="18" align="texttop"></picture> Accept closing fences longer than their opening fence</summary>\n'
        "\n"
        "`scripts/​review_verdict.py:47`\n"
        "\n"
        "Markdown permits a closing fence longer than its opener, but this pattern requires an exact-length match. A green assessment followed by a three-backtick example closed with four backticks, then a suppressed-comments section, is incorrectly classified as clean: the parser removes the suppressed findings through the end of the body. Accept closing runs of the same character that are at least as long as the opener, retaining the whitespace-only suffix check. Add regression cases for both backticks and tildes.\n"
        "</details>\n"
        "</details>\n"
        "\n"
        "🧠 **Review effort:** Balanced"
    ),
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
