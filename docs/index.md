<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# GitHub Code Monkey Bot

Scheduled AI authoring of pull requests for open GitHub issues. A
reusable workflow selects the highest-ranked triaged issues, runs
one Copilot CLI coding agent per repository against a checkout of
that repository, and publishes the result as GitHub-signed commits
on a branch in a bot fork organisation, with a pull request against
the target. Nothing merges without a maintainer.

## How a run works

```text
select (trusted)
  | ranked issues, one per repository, base commit, guidance
  | commit SHA, evidence ID, digests
  v
author (untrusted, one runner per issue)
  | checkout + Copilot CLI; no credential that can write
  | git bundle + manifest
  v
publish (trusted, one runner per issue)
  | verify the bundle offline
  | fork token: find or create the fork, push the branch
  | target token: open the pull request, comment on the issue
  v
report (trusted)
```

## Where bot branches live

The publisher never pushes a branch to a target repository. It
pushes to a fork held in a bot fork organisation and opens the pull
request from there, so the target's workflows run the change as an
outside contribution, with no secrets and an approval gate.

| Target organisation | Fork organisation |
| ------------------- | ----------------- |
| `onap` | `lfreleng-bot-forks-onap` |
| `opendaylight` | `lfreleng-bot-forks-opendaylight` |
| `o-ran-sc` | `lfreleng-bot-forks-oransc` |
| every other organisation | `lfreleng-bot-forks` |

## Where to go next

- [Design](DESIGN.md): the architecture, trust model, selection,
  signing, publishing, credentials, rollout and data contracts.
- [Guidance for agents](ORG-AGENTS-GUIDANCE.md): the rule every AI
  agent in the organisation follows when it opens a pull request.
