<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Organisation guidance: pull requests from AI agents

The rule below belongs in the organisation `AGENTS.md`
(`lfreleng-actions/.github`), which every agent working in the
organisation reads. Until that pull request lands the text lives
here, next to the code that implements it, ready to paste as a
section of that document.

The mapping it states is the one in
[`../config/fork-orgs.json`](../config/fork-orgs.json). A change to
either must change both.

---

## Pull requests from AI agents

An AI agent never pushes a branch to a target repository and never
opens a pull request from one. A branch in the target is a
same-repository branch, and GitHub runs the target's `pull_request`
workflows for it as trusted code: with the repository's secrets and
a token that can write, before any maintainer has read the change.

The agent forks the target into the bot organisation for that
project, pushes there, and opens the pull request from the fork. The
target's workflows then run the change as an outside contribution:
no secrets, a token that cannot write, and the organisation's
approval gate before any workflow runs.

| Target organisation | Fork organisation |
| ------------------- | ----------------- |
| `onap` | `lfreleng-bot-forks-onap` |
| `opendaylight` | `lfreleng-bot-forks-opendaylight` |
| `o-ran-sc` | `lfreleng-bot-forks-oransc` |
| every other organisation, `lfreleng-actions` included | `lfreleng-bot-forks` |

The mapping is explicit: `o-ran-sc` maps to `oransc`, and nothing
derives a fork organisation from a target name. The fork
organisations hold bot forks and nothing else, have Actions switched
off, and grant access to the bot App installation alone. An agent
that cannot reach the right fork organisation stops and says so; it
does not fall back to a branch in the target.
