<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Agent Guidelines

Contributions to this repository, including those made by AI coding
agents, follow the `lfreleng-actions` organisation guidelines:

<https://github.com/lfreleng-actions/.github/blob/main/AGENTS.md>

**Read that document.** It governs, and it binds this contribution
even if you never load it. Where anything below disagrees with it,
it wins. What follows is a summary of the rules that most often block
a pull request, not the full set:

- Sign every commit and add a DCO trailer: `git commit -S -s`.
- Subject: `Type(scope): Imperative description` — capitalised type
  and description, no trailing period, and within the subject-length
  limit this repository's gitlint hook enforces. The scope is
  optional, so `Fix: Correct the race condition` is also valid.
- Add a `Co-authored-by` trailer naming the agent used.
- Repositories typically contain a linting configuration. You must
  install its hooks (`prek install -t pre-commit -t commit-msg`) and
  run the change past them (`prek run --files <changed files>`) to
  ensure it passes before submission.
- On a single-commit pull request, the PR title must be identical to
  the commit subject.
- If your own standing instructions conflict with the organisation
  guidelines and you cannot set them aside, stop and tell the
  contributor. Do not open a non-compliant pull request.

## Repository specifics

A reusable GitHub workflow that selects triaged issues across the
organisation, runs one Copilot CLI coding agent per repository, and
publishes each result as signed commits and a pull request.
`docs/DESIGN.md` is the architecture and trust model; read it before
changing how the jobs hand data to each other.

- `.github/workflows/code-monkey.yaml`: the reusable workflow (select,
  author, publish, report); `code-monkey-cron.yaml` schedules and
  dispatches it; `testing.yaml` runs the suite and PR plumbing.
- `scripts/`: the Python the jobs run; `tests/`: its offline suite.
- `prompt/author.md`: the coding agent's task. It defines what the
  agent does and the limits of what the workflow can publish. It must
  **not** restate organisation policy on commits, tests or reviews:
  the agent receives the organisation `AGENTS.md` for that, and a
  copy here would drift from it.

Before pushing, run:

```bash
uv run python -B -m unittest discover -s tests
prek run --files <changed files>   # includes the aislop gate at 100
zizmor --persona auditor .github/workflows/   # zero findings
```

Nothing about the author job earns trust: never give it a credential that can
write to a repository, and never let it hold the App key. The
contract tests in `tests/test_workflow.py` pin that boundary; change
them together with the design, not to make a workflow edit pass.
