<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐒 GitHub Code Monkey

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-code-monkey-bot) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-code-monkey-bot/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-code-monkey-bot)
<!-- prettier-ignore-end -->

Scheduled AI authoring of pull requests for open GitHub issues across
the `lfreleng-actions` organisation. A reusable workflow selects the
highest-ranked triaged issues, runs one Copilot CLI coding agent per
repository against a checkout of that repository, and publishes the
result as GitHub-signed commits on a bot branch with a pull request.
Nothing merges without a maintainer.

The [design document](docs/DESIGN.md) covers the architecture, the
trust model, inputs, credentials and the rollout plan.

## How it works

```text
select (trusted)      author (untrusted, matrix)    publish (trusted)
  App: read token       no write credential           App: per-repo token
  rank, one per repo    checkout + Copilot CLI        verify bundle
  selection.json -----> bundle + manifest ----------> signed commits, PR
```

The author job never holds a credential that can write to a
repository. It leaves a git bundle and a manifest; the publish job
verifies both offline against the trusted selection, then replays
the commits through GitHub's `createCommitOnBranch`, which signs
them, opens the pull request, requests a Copilot review of it and
waits for the verdict, which the issue comment and the run report
state. A review with findings, or a maintainer's comment, brings a
revision round: a later session reads the findings, revises the
same pull request in place, answers each thread, and the review
runs again, up to ten rounds. The agent reads the organisation's
`AGENTS.md` at a recorded commit and works to its rules.

## Where bot branches live

The publisher never pushes a branch to a target repository and never
opens a pull request from one. It forks the target into the bot
organisation for that project, pushes there, and opens the pull
request from the fork, so the target's workflows run the change as
an outside contribution: no secrets, a token that cannot write,
and the organisation's approval gate before any workflow runs. The mapping
lives in [`config/fork-orgs.json`](config/fork-orgs.json):

| Target organisation | Fork organisation |
| ------------------- | ----------------- |
| `onap` | `lfreleng-bot-forks-onap` |
| `opendaylight` | `lfreleng-bot-forks-opendaylight` |
| `o-ran-sc` | `lfreleng-bot-forks-oransc` |
| every other organisation, `lfreleng-actions` included | `lfreleng-bot-forks` |

The fork organisations hold nothing but bot forks, have Actions
switched off, and accept the App's installation and nothing else.
The [design document](docs/DESIGN.md) section 4.3 explains the
reasoning.

## Schedule and dispatch

`code-monkey-cron.yaml` runs live at 02:00 UTC on weekdays, so its
pull requests are ready for review by the start of the European
working day, and reads the previous working day's issues triage. It
raises at most ten pull requests and posts them to Slack, one link
per line, or says that it raised none; a failed scheduled run posts
a failure notice instead. Disabling the workflow in the Actions UI
stops it. Organisation
administrators can dispatch it by hand, live unless they tick
`dry_run`, and without a Slack post, with a model, a mode
(`select`, `branches`, `pull-requests`), issue and runtime caps,
a category list, and either a repository list to scan or a list of
issues to work outright (`repo#N`, `owner/repo#N` or URLs), which
skips the scan and the triage filters. A list of the bot's open pull
requests runs a revision round on each; an issue whose earlier
attempt is still open gets a round too, and scheduled runs revise
before they take new issues unless `revise` is off.

## Reusable workflow

```yaml
jobs:
  code-monkey:
    permissions:
      issues: read
      contents: read
      actions: read
    uses: lfreleng-actions/github-code-monkey-bot/.github/workflows/code-monkey.yaml@<sha>
    with:
      org: my-org
      mode: pull-requests
      # Dry-run by default. Until bot branches publish from forks
      # (docs/DESIGN.md 4.3), a live run names its targets here and
      # keeps to repositories whose workflows hold no secrets.
      dry_run: true
      repositories: 'test-python-project'
      # Trusted jobs default to block mode; name the allow-list.
      egress_allow_config: '@<allow-list commit sha>'
      github_app_client_id: ${{ vars.BOT_APP_CLIENT_ID }}
    secrets:
      copilot_token: ${{ secrets.COPILOT_CLI_TOKEN }}
      github_app_private_key: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      copilot_review_token: ${{ secrets.COPILOT_REVIEW_TOKEN }}
```

Live runs need a GitHub App installed on the organisation with the
permissions in the design document (section 10.1) and a personal
fine-grained PAT carrying Copilot Requests and no repository grants.
GitHub adds no Copilot reviewer when the App asks, so a caller that
wants the review requested passes `copilot_review_token`: a
fine-grained PAT of a user with a Copilot seat, carrying Pull
requests: write on the organisation, which bills the reviews to that
seat (section 10.4). Without it the request falls back to the App's
token and the review stays `pending (not requested)`.

## Development

```bash
uv run python -B -m unittest discover -s tests -v
prek run --files <changed files>
aislop ci
```
