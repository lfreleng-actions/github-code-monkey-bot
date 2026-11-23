<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐒 GitHub Code Monkey

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-code-monkey-bot) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-code-monkey-bot/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-code-monkey-bot)
<!-- prettier-ignore-end -->

Operator-dispatched AI authoring of pull requests for named GitHub
issues across the `lfreleng-actions` organisation. A reusable
workflow selects the issues a dispatch names, runs one Copilot CLI
coding agent per repository against a checkout of that repository,
and publishes the result as GitHub-signed commits on a bot branch
with a pull request.
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
runs again, up to ten rounds, counted afresh for each antagonistic
review that hands the pull request back. The agent reads the organisation's
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

## Dispatch

`code-monkey-cron.yaml` runs when an organisation administrator
dispatches it, and at no other time: it has no schedule, and no run
scans for new issues. A dispatch is live unless it ticks `dry_run`, and
names its work: a
list of issues to work (`repo#N`, `owner/repo#N` or URLs), a list of
the bot's open pull requests to revise, or `revise_only`. The
reusable workflow refuses a run that names none of them before it
selects anything or mints a token. The form also sets a model, a
mode (`select`, `branches`, `pull-requests`), issue, runtime and
round caps (`max_rounds` lifts the ten-round limit per antagonistic
review round for one run; sixty rounds in all stays the ceiling) and
`stale_status_minutes` (default 8: past it, a pending pre-commit.ci
status is stale, not waited for and not failing, and named in the
comment; `30` lifts it during a pre-commit.ci incident and `0` waits
as for any check). A named issue skips the triage filters and the
author rule, since the operator vouches for it; `no-agent` still opts
it out. A named issue whose earlier attempt is still open gets a
revision round on that pull request instead, unless `revise` is off.
`revise_only` revises the bot's open pull requests with work
outstanding across the organisation and takes no new issue.

When a live `pull-requests` run's round leaves a published pull
request with work outstanding (review findings on its head, failing
checks or a conflict), its outcome commented on the issue, below every
round cap and with no antagonistic review requested, its `continue` job
dispatches the next round of that pull request at once, one dispatch per
pull request with `outstanding_only` set, the run's own options and
switches, and `continues_run` naming the run. The continuation works no
pull request a `revise_only` run would not, under the same repository
policy, author rule and work-outstanding test, nor one on which that
run's outcome footer is missing, so the review cycle runs on until the
review is clean, the bot hands the pull request back or it reaches a
cap. Setting the repository variable `CODE_MONKEY_CONTINUE` to `false`
stops that loop. Nothing else revisits a pull request: a Copilot review
still pending when publish stops waiting for it, or a maintainer's
comment or a failing check that arrives after the loop ends, waits for
an operator's `revise_only` dispatch. Each run's name in the Actions
list says what it works, the pull requests or issues named or a
`revise_only` scan, and marks a dry run.

## Reusable workflow

```yaml
jobs:
  code-monkey:
    permissions:
      issues: read
      contents: read
      actions: read
      checks: read
    uses: lfreleng-actions/github-code-monkey-bot/.github/workflows/code-monkey.yaml@<sha>
    with:
      org: my-org
      mode: pull-requests
      # Dry-run by default. Until bot branches publish from forks
      # (docs/DESIGN.md 4.3), a live run names its targets here and
      # keeps to repositories whose workflows hold no secrets.
      dry_run: true
      # Name the work: no run scans for new issues.
      issues: 'test-python-project#1'
      # Required: the logins trusted to open and edit issues.
      issue_authors: ${{ vars.CODE_MONKEY_ISSUE_AUTHORS }}
      # Optional: 'false' turns the author list off; unset keeps it.
      only_known_authors: ${{ vars.ONLY_KNOWN_AUTHORS }}
      # Trusted jobs default to block mode; name the allow-list.
      harden_runner_allowlist: '@<allow-list commit sha>'
      # Optional: a further allow-list merged with the baseline, and
      # inline host[:port] endpoints for short-lived fixes.
      # harden_runner_supplemental_allowlist: 'my-org/my-allow-list@<sha>'
      # harden_runner_supplemental_unpinned: false
      # harden_runner_extra_endpoints: 'example.com:443'
      github_app_client_id: ${{ vars.BOT_APP_CLIENT_ID }}
    secrets:
      copilot_token: ${{ secrets.COPILOT_CLI_TOKEN }}
      github_app_private_key: ${{ secrets.BOT_APP_PRIVATE_KEY }}
      copilot_review_token: ${{ secrets.COPILOT_REVIEW_TOKEN }}
      fork_pool_token: ${{ secrets.FORK_POOL_TOKEN }}
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

The App never creates or renames a fork, so it needs no
Administration permission. A caller that targets repositories whose
fork does not exist yet passes `fork_pool_token`: a classic token,
scoped to `public_repo` alone, of a machine account that belongs to
the bot fork organisations and no other (section 10.6). Without it,
existing forks keep working and a target with no fork fails its
publish.

New pull requests leave `.github/workflows/` alone unless the caller
sets `allow_workflow_edits: true`; the publisher rejects a proposal
that changes a workflow file without it. A revision round may change
the workflow files its pull request already changes and no other,
whatever the switch says (design document sections 8 and 20.3).

The root `.pre-commit-config.yaml` decides the checks a repository
runs, so new pull requests leave it alone unless the caller sets
`allow_precommit_config_edits: true`; the publisher rejects a
proposal that changes it without the switch, and a pull request that
does change it says so in its provenance block. A revision round may
change it when its pull request already changes it and not otherwise,
whatever the switch says.

## Trusted issue authors

The bot works an issue when a listed login opened it and, if
anyone edited its body, a listed login made the last edit. The list
adds to the `OWNER`/`MEMBER`/`agent-approved` rule and never
replaces it. It also filters the issue comments the agent reads and
those that start a revision round, and the publisher re-checks it
before every write, the outcome comment on the issue included. The
dispatch workflow reads the list from the repository variable
`CODE_MONKEY_ISSUE_AUTHORS`, as logins separated by commas and/or
spaces. When the variable is unset or empty, every run fails at
pre-flight:

```bash
gh variable set CODE_MONKEY_ISSUE_AUTHORS --body "alice bob"
```

The repository variable `ONLY_KNOWN_AUTHORS` switches the list on
and off. Unset, empty or `true` applies it; `false` turns it off, so
the list goes unread and the runs fall back to the
`OWNER`/`MEMBER`/`agent-approved` rule, with a warning in each run.
Any other value, `True` or `yes` included, fails the run at
pre-flight. Set it as this repository does:

```bash
gh variable set ONLY_KNOWN_AUTHORS \
  --repo lfreleng-actions/github-code-monkey-bot --body true
```

GitHub records no editor for a title edit, so the check covers the
author and the last body edit (design document section 6).

## Sudo and containers in a target repository

Sessions run without `sudo` and without container access, since
root on the runner could undo its hardening. A target repository
that needs either, to build an image for one, opts in with a regular
file named `.enable-sudo-and-containers` at the root of its default
branch; the file's content is not read. A maintainer adds it through
a reviewed pull request, and the publisher refuses any proposal or
round that adds, edits or deletes it. Keep the marker rare and say
in its pull request why the repository needs it: it gives every
session there root and Docker back (design document section 4.1).

## Development

```bash
uv run python -B -m unittest discover -s tests -v
prek run --files <changed files>
aislop ci
```

### Fuzzing

`fuzz/` holds [Atheris](https://github.com/google/atheris) harnesses
for the code that reads untrusted text:

- `fuzz_proposal_rules.py`: the proposal rules in `proposal_policy`
  (paths, modes, commit messages, comment and log text, reasons) and
  `proposal_check.read_manifest`.
- `fuzz_review_classifier.py`: `review_verdict.classify`,
  `review_text` and `review_sections` on Copilot review bodies.
- `fuzz_operator_input.py`: `explicit_issues.parse_issues` and
  `issue_authors.parse_listed`.

Atheris is not a project dependency; install it in a scratch
environment to fuzz. A harness with no input file named fuzzes from
its own seeds; naming a file replays that one input:

```bash
python fuzz/fuzz_review_classifier.py -max_total_time=60 corpus/
python fuzz/fuzz_review_classifier.py crash-<hash>
```

The unit suite needs no Atheris: `tests/test_fuzz_harnesses.py`
replays every seed through each harness's `check` function. Add an
input the fuzzer finds to the harness's `SEEDS` along with the fix.
