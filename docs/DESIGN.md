<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Design: Scheduled AI Authoring of Pull Requests

Status: **implemented, awaiting rollout** (§15). The workflow,
scripts and offline tests exist, the bot fork organisations hold
the App installation and the publisher writes through them (§4.3);
no live run
has yet published a branch. §17 records the decisions taken on
the first draft's open questions and the three points that rollout,
not further discussion, will settle.

This document reuses the architecture, vocabulary and lessons of
[`github-issues-triage`](https://github.com/lfreleng-actions/github-issues-triage)
(its `docs/development/DESIGN.md`, §7 and §13.7 in particular). Where
this design departs from that one, the text says why.

## 1. Problem Statement

The `lfreleng-actions` organisation holds a backlog of open issues
across some thirty repositories. Daily triage now labels each one
with category, Priority and Type, which ranks the backlog; nobody has
capacity to work it. Most issues are small, well-scoped and sit in
repositories with a uniform toolchain and a written contribution
standard, which makes them tractable for a coding agent working under
tight constraints.

## 2. Goal

Each weekday, after triage completes, run a coding agent against the
highest-ranked open issues and open one pull request per issue. Each
pull request:

- comes from a bot-owned branch in a fork held by a bot fork
  organisation (§4.3), never from a branch in the target repository,
  which makes the agent's code an outside contribution to the
  target's CI;
- carries commits that follow the organisation's `AGENTS.md`
  (signed, DCO trailer, Conventional Commit subject, co-author
  trailer, PR title equal to a single commit's subject);
- has passed the repository's own pre-commit hooks, tests and the
  `aislop` gate in the agent's checkout;
- states what changed, why, what the agent ran, and which run
  produced it;
- waits for a human. The organisation requires a RelEng approval
  before merge, and nothing here can supply one.

The measure of success is a pull request a reviewer can merge after
reading it, or reject in less time than the issue would have cost
them.

### Non-goals

- **Merging.** The workflow never merges, never approves and never
  dismisses a review.
- **Revising an open bot pull request.** Copilot reviews every
  pull request on creation; responding to that review, or to a
  human one, is a later capability (§16).
- **Working the `.github` repository.** Its issues track
  organisation-wide tasks that span repositories. It stays out of
  scope unless a dispatcher opts in.
- **Claude Code and Gemini CLI lanes.** Copilot CLI is the single
  harness. The organisation's Enterprise subscription supplies the
  models this design names.
- **Private repositories.** The estate is public. §16 lists what a
  private repository would need.

## 3. Relationship to Issues Triage

The triage workflow runs at 07:00 UTC on weekdays and applies labels, Priority
and Type. This workflow depends on that output:

- Priority drives the ranking (§6).
- Type and category labels drive exclusions (§6) and the commit type
  the agent chooses.
- An issue without a Type has not seen triage yet and waits a day.

The two workflows live in separate repositories, which rules out a
`workflow_run` trigger. The schedule offsets by two hours instead
(§11). Triage's `excluded-repos.txt` lists repositories that produce
noise; this workflow keeps its own list with the same format, seeded
from that one.

Inherited from triage without change: the three-job trusted /
untrusted / trusted layout; artifact retrieval by producer ID and
trusted digest, never by name; a per-invocation namespace
(`run-attempt-uuid`); `INPUT_PERMISSION-*` environment forwarding
for App token permissions the pinned action does not declare; the
`github_pat_` guard on the model token; harden-runner with the
egress allow-list summary printed once; scratch cleanup by plain
deletion under `continue-on-error`.

## 4. Architecture

```text
select (trusted)      author (untrusted, matrix)      publish (trusted)
  App: issues read      no App credential               App: per-repo write
  rank + choose         checkout target repo            verify bundle
  selection.json        Copilot CLI session             replay commits (API)
  SHA + ID + digest --> proposal bundle + manifest -->  open PR, report
```

The shape matches triage. The difference is what the untrusted job
produces. Triage's agent emits JSON that a trusted applier turns
into label writes. This agent emits **commits**: a git bundle and a
manifest. The trusted publisher turns those into a branch and a pull
request. The agent job never holds a credential that can write to
any repository, and it never holds the App key.

### 4.1 Why tool policy cannot contain this agent

Triage confines its agent to `cat`, `jq` and four read utilities and
denies `git`, `gh` and the write tool. Run 35324225186 showed the CLI
auto-approving anything it classifies as a read, so even that
containment rests on the deny list rather than the allow list.

A coding agent needs `git`, the write tool, package managers, test
runners and `prek`. Any interpreter in that set (`python`, `node`,
`bash` itself) is a standing bypass of every shell-level denial.
Tool policy on this job is a productivity setting, not a boundary.

The boundary is:

1. **No write credential in the author job.** Its native
   `GITHUB_TOKEN` has `contents: read`. The model PAT has Copilot
   Requests and nothing else. Public repositories clone without
   authentication.
2. **A trusted publisher that treats the bundle as data.** It
   verifies provenance, inspects the diff against a policy, and
   makes every write through the App with a token scoped to one
   repository.
3. **A human review gate** the organisation already enforces.

Consider everything on the author runner (packet, checkout, model
PAT, Actions runtime token) exposed to the issue text and repository
content the agent reads. A prompt-injected issue can waste model
spend and produce a bad diff. It cannot push, cannot open a pull
request, and cannot reach another repository.

### 4.2 What the design trusts the caller with

The calling workflow supplies both the App key and the
`assets_repository`/`assets_ref` coordinate that the trusted jobs
execute. The design trusts the caller with both: a caller able to
point the assets at hostile code could as well edit its own workflow
to run that code with the same key. Both callers here pin the assets
to their own `github.sha`, and `issues-triage` makes the same
assumption.

The pull request plumbing job executes the pull request's own
scripts with the native token alone. GitHub gives a fork's
`pull_request` run a `GITHUB_TOKEN` without write scopes and no
secrets, and here that token can read nothing beyond public issues and contents,
expires with the job, and runs under block-mode egress. That is the
sandbox GitHub intends for untrusted pull requests, and the job
needs no stronger one.

### 4.3 Publishing from a bot fork organisation

The publisher does **not** push `code-monkey/*` branches into the
target repository. A branch in the target is a same-repository
branch, and GitHub runs that repository's `pull_request` workflows
for it as trusted code: with its secrets and a token that can
write, before any maintainer has read the change. The code on that
branch is agent output shaped by issue text, and may include
workflow edits. Review before merge cannot contain what CI executes
before review.

Bot branches live in a fork of the target instead, held in a
dedicated fork organisation, and the publisher opens each pull
request from that fork. GitHub then treats the change as coming from outside:
the target's workflows run without secrets and with a token that
cannot write, subject to the organisation's approval policy for
outside contributions, and the change still gets full CI.

AI agents that author pull requests for `lfreleng-actions` use
these organisations for their forks:

<!-- markdownlint-disable MD013 -->

| Fork organisation | Holds forks of |
| ----------------- | -------------- |
| `lfreleng-bot-forks` | Every organisation without a dedicated pool, `lfreleng-actions` included |
| `lfreleng-bot-forks-onap` | ONAP project repositories |
| `lfreleng-bot-forks-oransc` | O-RAN-SC project repositories |
| `lfreleng-bot-forks-opendaylight` | OpenDaylight project repositories |

<!-- markdownlint-enable MD013 -->

The names follow one pattern: `lfreleng-bot-forks` is the general
pool, and a project that needs its own takes
`lfreleng-bot-forks-<project>`. The dedicated pools exist so that
each project's forks, and the access to them, stay separate from
the general pool. The rule applies to any agent-authored pull
request in `lfreleng-actions`, not to this workflow alone; with the
names agreed, the organisation `AGENTS.md` is its right home.

Publishing then works as follows, with the §5 signing unchanged:

1. The publisher resolves the fork organisation for the target's
   owner from the table, and finds or creates the fork there.
   Creating a fork through the API needs the App installed on the
   fork organisation with access to all its repositories and
   Administration: write, plus Contents: read on the source.
2. It syncs the fork's default branch with the target
   (`merge-upstream`), so the recorded base commit is present.
3. It creates `code-monkey/issue-<n>` in the fork at the base
   commit and replays the commits there through
   `createCommitOnBranch`, with a token minted on the fork
   organisation's installation for that one fork.
4. It opens the pull request on the target with head
   `<fork-org>:code-monkey/issue-<n>`, using a token minted on the
   target organisation's installation.
5. A prior attempt (§6) is a pull request whose head repository is
   the designated fork, or a surviving branch in that fork; a
   same-named branch in any other fork still does not count.

**Installation.** The publisher has no same-repository path. The
four organisations exist and each holds an installation of the App
on all repositories with Contents: write, Workflows: write and
Administration: write (§10.1). Every fork organisation has Actions
switched off, which means a bot branch triggers no workflow where it
lands; the target organisation requires approval before workflows
run for pull requests from outside contributors, and the change
gets CI once a maintainer has read it.

## 5. Signed Commits Without a Key on the Runner

The organisation enforces commit signatures: one unsigned commit
makes a pull request unmergeable. A GPG or SSH signing key on the
author runner would sit next to the agent, which §4.1 rules out.

The publisher creates commits through the GraphQL
`createCommitOnBranch` mutation with the App installation token.
GitHub signs commits made this way with its own key and shows them
as **Verified**, attributed to the App's bot user. The author job's
local commits exist for the agent's benefit (its hooks run on them,
gitlint checks the message); the publisher re-creates each one from
its tree diff and message rather than pushing the agent's objects.

Consequences the implementation has to handle:

- **Message composition.** The publisher takes the subject and body
  from the agent's commit, drops every agent-written trailer that
  names a person (any `*-by` key: no human takes part in a session,
  and a signed commit must not claim one did), then appends the
  `Co-authored-by` trailer for the **assistant**, Copilot, whichever
  model served it (§10.3), and
  `Signed-off-by: <bot login>[bot] <id+login[bot]@users.noreply.github.com>`.
  It rejects a subject over the repository's `.gitlint` limit or
  lacking a capitalised Conventional Commit type.
- **File modes and symlinks.** `FileAddition` carries a path and
  base64 contents; it cannot set the executable bit or create a
  symlink, and nothing in the mutation says it preserves the mode of
  an existing executable file. The Git Data API can set modes, but
  commits made through it carry no signature, which defeats the
  purpose. An unsigned fallback push would produce an
  unmergeable pull request and more work than it saves, so the
  publisher **rejects** a bundle whose diff adds or changes an
  executable file, a symlink, a submodule, or any file mode. The
  prompt tells the agent to leave those steps out, complete the
  rest, and open the pull request body with an `INFO` banner listing
  the follow-up a human has to make (§7.3).
- **Renames** become a deletion plus an addition. Git history shows
  the rename through similarity detection as usual.
- **Sequencing and rollback.** The publisher creates the branch at
  the agent's recorded base SHA, then replays commits in order,
  passing each result as the next `expectedHeadOid`. A failure after
  a confirmed create (a mismatch, a transient API error, a failed
  pull request call) deletes the branch before the step fails, since
  a half-built branch would count as a prior attempt (§6) and keep
  the issue out of every later run. A create that failed without a
  clear answer deletes nothing: the branch may have existed before,
  and a stray branch costs a human a look while deleting another's
  work loses it. A delete that finds no branch counts as clean. A
  pull request that exists despite a failed call keeps its branch,
  and so does one whose existence the publisher cannot confirm
  either way. The result records `publish-failed` with the reason
  and whether the branch came off.
- **An existing branch.** The publisher never deletes a branch it
  did not create, and adopts one as the run's own earlier work on one
  condition: the whole chain is what the replay would have made. That
  means based on the recorded commit with no other history, the same
  number of commits, and each commit with one parent (the one before
  it), the tree the offline check recorded, the bot as author, a
  Verified signature and the checked message. Neither a matching
  final tree nor matching messages is enough on its own: anyone with
  push access, or a stale run, could produce either with other
  content. A pull request counts as the earlier attempt's when
  the bot opened it from the target, and never otherwise. The result
  reports an adopted branch without a new pull request, or in
  `pull-requests` mode opens the pull request an earlier attempt
  never opened; the outcome comment's marker keeps any comment from
  repeating. Any other existing branch is a rejection.
- **Payload size.** GitHub does not document the mutation's request
  limit. The publisher caps total added bytes at 4 MiB and rejects
  binaries over 512 KiB; the rollout (§15) confirms the cap holds.
  The composed pull request body, provenance included, must fit
  GitHub's 65,536-character limit, checked before any write.

## 6. Issue Selection

The select job holds an App token with `issues: read`,
`metadata: read`, `issue-fields: read` and `issue-types: read`
across the organisation. It builds a candidate list and writes
`selection.json`.

**Candidate.** An open issue (not a pull request) in a **public**,
non-archived, non-template, non-fork repository of the target owner
(the author job clones without a credential, so private and
internal repositories cannot take part), whose repository is not on
the exclusion list and is not `.github` (unless
`include_dotgithub`), and which:

- has a Type (triage has run);
- has no `question`, `breaking-change` or `chore` label;
- has no `no-agent` label (a human opt-out, per issue);
- has no assignee, unless the caller sets `include_assigned`: an
  assignee means a human has claimed the work;
- has no pull request, open or closed, from a `code-monkey/*`
  branch **in the target repository itself** (the workflow already
  tried, or a human declined the result); a pull request from a fork
  branch of the same name does not count, since anyone can open one,
  and the lookup names the target's owner in its `head` filter so
  that such pull requests cannot push the bot's own off the page;
- has no other open pull request linked to close it.

**Ranking.** Priority `Urgent`, then `High`, `Medium`, `Low`; issues
with no Priority sort last. Within a priority, older `created_at`
first.

**Categories.** A run works an issue when the caller enables any of
its categories. Categories follow the labels triage applies, with the issue
Type as a second signal for bugs and features:

<!-- markdownlint-disable MD013 -->

| Category | Label | Or Type |
| -------- | ----- | ------- |
| `bugs` | `bug` | Bug |
| `features` | `feature` | Feature |
| `docs` | `documentation` | |
| `ci` | `CI` | |
| `code_quality` | `code-quality` | |
| `refactor` | `refactor` | |
| `performance` | `performance` | |
| `other` | none of the above | |

<!-- markdownlint-enable MD013 -->

Security, Scorecard and `aislop` work arrives with the security
report (§16): those findings are not issues triage labels, so a
category switch for them would select nothing until then.

**One issue per repository.** After ranking, the selector keeps the
first issue seen for each repository and drops the rest. Two agents
in one repository would race on `main` and on the toolchain cache;
one agent working two issues would juggle branches and widen the
blast radius of a mistake. The rest of that repository's backlog
waits for the next run.

**Cap.** The first `max_pull_requests` survivors form the selection;
each yields at most one pull request, so the value bounds the pull
requests a run can raise. `0`
lifts the cap as far as the Actions matrix limit of 256 jobs, which
the selector applies itself so a broad run degrades to a bounded
selection instead of failing at matrix expansion. A second bound
holds the serialised selection under 12 MiB, inside the 16 MiB the
verifier accepts, since 256 entries at the per-issue body and
comment caps would otherwise exceed it.

**Guidance ref.** `guidance_ref` may name a branch, a tag or a
commit. The selector resolves it to a commit, following an annotated
tag, and refuses a ref whose target is anything but a commit, so the
recorded provenance always names the commit whose `AGENTS.md` the
agent read.

**Explicit repositories.** When `repositories` names one or more
repositories, the candidate scan covers those alone and lifts the
exclusion list and the `.github` rule; an archived, template, forked
or non-public repository stays out of scope whoever names it. Separators are commas,
spaces, or both; the selector normalises and rejects a name that is
not `[A-Za-z0-9_.-]+`.

**Outputs.** `selection.json` lists, per chosen issue: repository,
number, title, body, Priority, Type, labels, `created_at`, the
`main` HEAD SHA observed at selection time, and the comments that
pass the filter below. The job publishes the artifact ID and a
SHA-256 of the file as job outputs. It also publishes a JSON matrix
for the author job.

**Comments.** Comments often carry the clarification the body
lacks; they are also where a passer-by can address the agent
directly. The packet includes a comment when its
`author_association` is `OWNER` or `MEMBER`, in order, up to 20
comments and 64 KiB in total, and drops the rest with a count of
what it dropped. It reads at most three pages of 100 comments,
oldest first, stopping once the count fills, and records
`comments_truncated` when newer comments went unread, so a long
thread cannot exhaust the select job. The issue body itself is always present: external
reports are the point of the exercise, and the prompt treats every
body as data rather than instruction.

## 7. The Author Job

One matrix entry per selected issue, `max-parallel` set from
`max_concurrent_agents`, `timeout-minutes` set from
`max_runtime_minutes` plus twenty minutes of setup slack. Actions
expressions cannot add, so the select job computes both budgets and
publishes them as outputs. Hosted runners cap a job at 360 minutes,
which bounds `max_runtime_minutes` at 330.

### 7.1 Inputs to the session

- A clone of the target repository at the SHA recorded in
  `selection.json`, made without credentials.
- The issue packet for this single issue: title, body, labels,
  Priority, Type, filtered comments (§6), extracted from
  `selection.json` and a per-issue fetch made in the select job.
- The organisation `AGENTS.md`, fetched in the **select** job from
  `lfreleng-actions/.github` at a commit SHA the select job records,
  and carried in the evidence artifact. The author job does not
  fetch it live.
- The repository's own `AGENTS.md`, if present, read from the
  checkout.
- The prompt (`prompt/author.md`), from this repository's checkout
  at the reusable workflow's pinned SHA.

### 7.2 Copilot CLI invocation

Pinned `@github/copilot@1.0.83` on Node 22. Triage verified 1.0.80;
this workflow needs `--usage-output-file`, which 1.0.80 lacks and
1.0.83 provides with an otherwise identical tool set.

```text
cd workspace && copilot --prompt="$prompt" --model="$MODEL" \
  --mode autopilot --no-ask-user \
  --no-custom-instructions --disable-builtin-mcps --no-auto-update \
  --allow-all-tools \
  --deny-tool="$deny" \
  --add-dir "$artefacts" \
  --secret-env-vars=COPILOT_GITHUB_TOKEN \
  --log-dir="$artefacts/copilot-logs" \
  --usage-output-file "$artefacts/usage.json" \
  --share="$artefacts/session-summary.md"
```

The working directory is the target checkout; `--add-dir` grants
the artefacts directory, where the agent writes its manifest and
the workflow keeps the prompt, packet and guidance.

`--no-custom-instructions` stops the CLI loading instruction files
from the checkout on its own terms. The prompt injects the org
`AGENTS.md` text directly and asks the agent to read the local stub.
That keeps the trusted copy authoritative and pinned.

The CLI's `skill` tool runs skills it finds in the working
directory's `.github/skills`. Skills are code the agent runs, and
the checkout is content the agent is there to change, so that trust
is off by default: `$deny` includes `skill` unless the caller sets
`load_repo_skills`. The input exists for dispatch against a
repository whose skills a maintainer has read. It never applies to
content from outside the target repository; the workflow checks out
the default branch of an organisation repository and nothing else.

`gh` and `git push` stay denied. The agent has nothing to
authenticate with, and a call that fails looks to the model like a
bug to work around. The prompt says the workflow opens the pull
request.

`--allow-all-tools` is the honest setting given §4.1. The deny list
holds what the agent must not attempt rather than what it cannot do.

`allow_subagents=false` adds `task` to the deny list. In
`@github/copilot@1.0.83` that is the tool which spawns sub-agents;
`list_agents`, `read_agent` and `write_agent` manage custom agent
definitions and go on the same list. A later CLI pin re-checks these
names against `--available-tools` output.

### 7.3 What the prompt asks for

Two sources give the agent its instructions, and they do not
overlap:

- **The task**, in `prompt/author.md`: which issue, the tractability
  test and when to abstain, the branch to work on, the manifest to
  hand back, and the facts about this workflow that no organisation
  document can know.
- **How to make a change that can merge**, in the organisation
  `AGENTS.md` (§7.1) and the target repository's own stub: commit
  messages, atomic commits, tests, hooks, the `aislop` gate, licence
  headers. The prompt names these sources and does not restate them,
  so a change to organisation policy reaches the agent without an
  edit here. This repository's own `AGENTS.md` records that rule for
  future contributors to the prompt.

The workflow facts the prompt adds:

1. *Signing.* The agent commits with `git commit -s` and no `-S`: the
   runner holds no signing key, and the publisher's replay signs the
   commits and appends the Copilot co-author and the bot's sign-off
   (§5).
2. *Replay limits.* No executable bits, symlinks or mode changes; at
   most five commits and 100 file changes per commit; under 4 MiB in
   total; hands off `AGENTS.md`, `LICENSE*`, `REUSE.toml` and
   `.gitlint`. Workflow files are in scope. Where the fix needs a
   step the replay cannot take, the agent finishes the rest and
   opens the pull request body with an `INFO` banner naming the
   human follow-up.
3. *Publication.* The agent cannot push; the workflow opens the pull
   request and runs its review, so the agent skips those steps of
   the guidance.

The publisher's mechanical checks (§8) do overlap the guidance on
purpose: they are the gate that decides whether a proposal can merge,
not a second statement of the policy, and they cover the subset a
machine can judge.

### 7.4 Outputs

The job runs `git bundle create` for the range from the base SHA to
`code-monkey/issue-N` and uploads `changes.bundle`,
`manifest.json` and `usage.json` as the **proposal** artifact for
that matrix entry, named with the namespace and the issue key. It
uploads the CLI logs and `session-summary.md` separately as the
**session** artifact. The publisher reads the proposal and never
opens the session artifact.

A cleanup step then deletes the CLI's spilled tool output, its
home directory and the checkout, under `continue-on-error`.

### 7.5 Egress

The author job runs harden-runner in `audit` mode. It needs npm,
PyPI, GitHub, the Copilot API and whatever hook repositories the
target's `.pre-commit-config.yaml` pins. A block-mode allow-list
that covers every repository's toolchain is a maintenance load this
design defers; the audit log records what each session reached.

The select, publish and report jobs run in `block` mode with the
organisation's allow-list from `harden-runner-block-action`, pinned
by commit in both callers, with `allow_list_summary: 'true'` on the
select job alone. `block` is the reusable workflow's default; a
caller may pass `audit` to diagnose a blocked endpoint.

## 8. The Publish Job

A trusted matrix job over the same selection, after the whole
author matrix, that runs even when some author entries failed
(`!cancelled()` and a successful selection) so each issue gets a
verdict. Each entry holds the App key and mints tokens for its own
repository alone. A matrix rather than one sequential job because
GitHub exposes no per-entry outputs from a matrix job: the publisher
for an issue finds its proposal by the artifact **name** the author
entry used, which is acceptable because the publisher trusts nothing
in that artifact whatever its name, and step 1 cross-checks its
content against the verified selection.

For each selected issue:

1. **Locate the proposal artifact** by name
   (`monkey-proposal-<namespace>-<key>`). A missing artifact records
   `author-failed` for that issue. The evidence file `selection.json`
   comes by the select job's artifact **ID** and must match its
   digest before anything else happens. `proposal_fetch.py` then
   fetches the untrusted proposal through the API rather than
   `download-artifact`, which would extract it before any size
   check: it refuses a zip larger than the sum of the file caps,
   streams it to disk under that limit, checks the zip directory
   (entry count, regular files, per-file caps, stored or deflated
   and unencrypted) and extracts the manifest, bundle and usage
   files alone, each read with a hard stop. An artifact it refuses
   records `author-failed` the same way, so every issue reaches a
   result, a comment and a report row.
2. **Verify the bundle.** `git bundle verify` against a fresh
   credential-less fetch of the target at the recorded base SHA. The
   bundle's prerequisite must equal that SHA, the history must be
   linear, and the commit count ≤ 5. Manifest fields present, typed,
   and equal to the selection's repository, issue, base and branch,
   for every outcome, so an abstention or failure from the wrong
   artifact cannot land on this issue. Outcome `abstain` or
   `author-failed` then records and moves on. A
   small compressed bundle can hold enormous objects, so the
   publisher caps every read of its content: counts come before
   listings and object sizes before content, a commit message stops
   at 64 KiB, the binary check reads 8 KiB, and any read that passes
   its cap stops git and rejects the proposal. Every git process also
   runs under operating-system limits on memory, CPU time and file
   size, so a pack that inflates past them while git indexes it stops
   that process rather than the runner. Each git process leads its
   own process group, which a timeout or an oversized read kills
   whole, taking helpers such as `index-pack` with it.
3. **Policy-check the diff** across all commits: no executable
   files, symlinks, submodules or mode changes (§5); no path escaping
   the tree and every path valid UTF-8, since the API takes text and
   the check would otherwise see one name while the API creates
   another; at least one and at most 100 file changes per commit,
   the mutation's requirements; total added bytes under the cap; no change to
   `AGENTS.md`, `LICENSE*`, `REUSE.toml` or `.gitlint`. Changes
   under `.github/workflows/` pass and flag the token mint in step 5.
4. **Check the message** of each commit against the rules the org
   guidance makes mechanical: subject length from the target's
   `.gitlint` (fallback 50), capitalised type from the allowed list
   and a capitalised description, as the organisation's gitlint rule
   requires,
   no trailing punctuation, blank line after the subject, body lines
   ≤ 72 outside URL lines, and valid UTF-8 throughout, since the API
   takes text. Compose the trailers (§5).
5. **Resolve the fork organisation.** The matrix entry names it;
   the step requires it to match the bot pool pattern, to differ
   from the target organisation, and to equal what the committed
   mapping (`config/fork-orgs.json`) resolves for the target's owner.
   It then reads, with the native token, whether the public fork
   exists. Dry-run and `select` mode go no further than this step.
6. **Mint a fork token** on the fork organisation's installation:
   for an existing fork, `repositories:` the bare `repo_name` with
   `contents: write` and `metadata: read`; for a missing fork, no
   repository (none exists to name) and `administration: write`
   besides, the sole mint in the pipeline that holds it. Either adds
   `workflows: write` when step 3 saw a workflow change. The two
   mints exclude each other on the fork's existence.
7. **Push to the fork.** Find or create the fork (§4.3), sync its
   default branch, create `code-monkey/issue-<n>` there at the base
   SHA (an existing branch is a rejection unless it holds this
   proposal's own chain, §6), then replay commits per §5.
8. **Mint a pull request token** on the target organisation's
   installation for that repository alone, with `pull_requests:
   write` and `metadata: read`: the one token in the pipeline that
   can write to the target. `pull-requests` mode after a successful
   push alone.
9. **Open the pull request** (`pull-requests` mode) against the
   default branch with head `<fork-org>:code-monkey/issue-<n>`:
   title from the manifest (equal to the subject on
   a single commit, enforced); a body the publisher heads with its
   own `Closes #<n>` line and a provenance block (run URL, model,
   issue link, commands run, the AI authorship disclosure), which
   nothing the agent writes can precede, hide or turn into code, so
   the merge closes the issue and the disclosure always shows;
   then, below a rule, the manifest's body with `@mentions`
   defused, so agent text cannot notify anyone before a human reads
   it; label `code-monkey` if the label exists. Not a draft: a
   ready pull request triggers the automatic Copilot review and
   notifies code owners; a draft does neither by default. A failure
   here keeps the fork branch: the next run's selection sees a
   prior attempt, adopts the branch and opens the pull request.
10. **Comment on the issue** with one line: the pull request URL on
    success; on `abstain`, the agent's reason; on a policy or
    provenance rejection, which check failed and the run URL. The
    reason descends from the untrusted manifest; the publisher cuts
    it to 2,000 characters when recording and again when rendering,
    which keeps the comment inside GitHub's limit with the run URL
    intact. The comment uses a further per-repository token carrying
    `issues: write` and nothing else, minted for this step alone.
    GitHub has no permission that stops at comments: `issues: write`
    also covers labels, assignees, milestones, close and reopen. The
    publisher's code path calls the comment endpoint and no other,
    the token lives in one trusted step, and labelling stays with
    triage. Dry-run skips the comment. Each comment ends with a hidden
    marker naming the run and the outcome; before posting, the
    publisher looks for it on the bot's own comments since the run's
    creation, a time every attempt shares. A retry after a lost reply
    or a later failure then posts nothing twice, and a marker someone
    else pastes cannot suppress the comment. An operational failure
    before any write, the offline check or a token mint, posts
    nothing: the target saw no change, the verdict may be unknown,
    and one publisher fault would otherwise comment on every issue
    in the run. Step 11 records it in the report instead; a failure
    during or after the writes (step 7 on) still comments, since the
    fork saw a branch and its rollback.
11. **Record** `result.json` and a step-summary section. A step that
    runs whatever came before writes a `publish-failed` result when an
    earlier failure (the offline check, a token mint) left none, so
    every selected issue reaches the report. A final
    report job gathers every `result.json` by artifact-name pattern
    and renders one table: issue, verdict, output URL, premium
    requests consumed (from `usage.json`), detail. A report covers one
    selection, its namespace: a retried entry shows its latest
    attempt's row, but the spend total counts each author session
    once, since a rerun author job uploads a new proposal artifact
    and the ID of each proposal artifact names the session that
    produced it. A full rerun selects afresh under a new namespace
    and gets its own report; the earlier report keeps the earlier
    sessions' spend, so a run's cost is the sum of its reports.

## 9. Inputs

### 9.1 Reusable workflow (`code-monkey.yaml`)

| Input | Type | Default | Notes |
| ----- | ---- | ------- | ----- |
| `org` | string | required | Target owner |
| `mode` | string | `pull-requests` | `select`, `branches`, `pull-requests` |
| `dry_run` | boolean | `true` | Author runs; publisher writes nothing |
| `model` | string | `claude-opus-5.5` | Copilot model identifier (§10.3) |
| `max_pull_requests` | string | `'10'` | `0` = up to the matrix limit |
| `categories` | string | `'all'` | `all`, or a list from §6 |
| `max_runtime_minutes` | string | `'180'` | Per agent |
| `max_concurrent_agents` | string | `'10'` | 1–30, matrix `max-parallel` |
| `allow_subagents` | boolean | `true` | §7.2 |
| `include_dotgithub` | boolean | `false` | §6 |
| `include_assigned` | boolean | `false` | §6: work issues a human holds |
| `load_repo_skills` | boolean | `false` | §7.2: trust checkout skills |
| `repositories` | string | `''` | Comma and/or space separated |
| `exclude_repos` | string | `''` | Overrides bundled list |
| `guidance_repository` | string | `<org>/.github` | Holds the org `AGENTS.md` |
| `guidance_ref` | string | `main` | Branch or tag recorded and used |
| `egress_policy` | string | `block` | Trusted jobs; author is always `audit` |
| `egress_allow_config` | string | `''` | Allow-list action coordinate |
| `github_app_client_id` | string | `''` | Empty limits runs to dry-run |
| `assets_repository` | string | this repo | Prompt and scripts |
| `assets_ref` | string | `''` | Resolved to a SHA in select |

Secrets: `copilot_token` (model PAT, §10), `github_app_private_key`
(select and publish alone).

Numeric inputs are strings because reusable-workflow `number`
inputs cannot carry validation and `fromJSON` handles the
conversion at the point of use. The select job rejects values
outside the stated ranges.

### 9.2 Dispatch caller (`code-monkey-cron.yaml`)

`schedule: '0 9 * * 1-5'` and `workflow_dispatch` with:

| Input | Type | Default |
| ----- | ---- | ------- |
| `dry_run` | boolean | `true` |
| `model` | choice | Claude Opus 5.5 |
| `mode` | choice | `pull-requests` |
| `max_pull_requests` | string | `'10'` |
| `max_runtime_minutes` | string | `'180'` |
| `max_concurrent_agents` | string | `'10'` |
| `allow_subagents` | boolean | `true` |
| `include_dotgithub` | boolean | `false` |
| `include_assigned` | boolean | `false` |
| `load_repo_skills` | boolean | `false` |
| `repositories` | string | `''` |
| `bugs`, `features`, `docs`, `ci` | boolean | `true` |
| `code_quality`, `refactor`, `performance`, `other` | boolean | `true` |

The `model` choice shows display names and the caller maps them to
the identifiers in §10.3. The schedule runs `pull-requests` mode
with `dry_run: false` once §15 clears it; until then the schedule
stays dry-run, the path triage took.

`max_pull_requests` semantics: `1` raises at most one pull request,
`20` at most twenty, and `0` lifts the cap as far as the matrix
limit; anything non-numeric or negative fails the select job. The
eight category switches build the `categories` list: untick all but
`docs` for a run that fixes documentation alone. A schedule passes no inputs,
which counts as every category enabled; a dispatch with every switch
off fails before selection. The form holds 19 inputs, within
GitHub's limit of 25.

### 9.3 Mode and dry-run matrix

| `mode` | `dry_run` | Select | Author | Branch | PR |
| ------ | --------- | ------ | ------ | ------ | -- |
| `select` | any | yes | no | no | no |
| `branches` | true | yes | yes | no | no |
| `branches` | false | yes | yes | yes | no |
| `pull-requests` | true | yes | yes | no | no |
| `pull-requests` | false | yes | yes | yes | yes |

Dry-run publishes the verified diff and the composed messages as
an artifact and in the step summary, which is how the prompt and
policy get tuned before anything reaches a repository.

## 10. Credentials

### 10.1 A dedicated GitHub App, under template names

A dedicated App, **LF/RelEng Code Monkey Bot**, distinct from the
triage and code review bots, so authored code carries its own
identity in history and an administrator can revoke its permissions
without affecting the others. The separation also matters the other
way: an App that pushes code must never be one whose approval counts
towards a review. The calling workflow names the credentials by
role, as every bot repository in the organisation does:
`vars.BOT_APP_CLIENT_ID` and `secrets.BOT_APP_PRIVATE_KEY`. The App
is public so the fork organisations (section 4.3) can install it;
OAuth and webhooks stay off. Its permissions:

<!-- markdownlint-disable MD013 -->

| Permission | Level | Used by | For |
| ---------- | ----- | ------- | --- |
| Metadata | read | select, publish | Repository listing |
| Issues | write | select, publish | Scan; one comment per outcome (§8 step 10) |
| Issue fields | read | select | Priority |
| Issue types | read | select | Type |
| Contents | write | publish | Fork sync, branch, commits (fork organisations alone) |
| Pull requests | write | publish | Open PR, label |
| Workflows | write | publish | Commits that touch `.github/workflows/` (fork organisations alone) |
| Administration | write | publish | Create a missing fork (fork organisations alone) |

<!-- markdownlint-enable MD013 -->

The App holds two kinds of installation. On the target organisation
the publisher mints Pull requests: write to open the pull request
and Issues: write for the comment, and no mint there ever requests
Contents, Workflows or Administration. On each fork organisation it
mints the writes that build the branch; the installation covers all
repositories of the organisation, since the fork it writes to may
not exist until the run creates it.

| Fork organisation | Installed | Actions |
| ----------------- | --------- | ------- |
| `lfreleng-bot-forks` | all repositories | disabled |
| `lfreleng-bot-forks-onap` | all repositories | disabled |
| `lfreleng-bot-forks-oransc` | all repositories | disabled |
| `lfreleng-bot-forks-opendaylight` | all repositories | disabled |

| Fork-organisation permission | Level | For |
| ---------------------------- | ----- | --- |
| Metadata | read | Repository listing |
| Contents | write | Sync the fork, branch, commits |
| Workflows | write | Commits that touch `.github/workflows/` |
| Administration | write | Create a missing fork |

The select job mints a token with the read levels alone, org-wide by
default and scoped to the named repositories plus the guidance
repository when the caller lists any;
`issues: write` on the App is a ceiling, and each mint requests the
lower level it needs. The publish job mints one token per
repository for the write steps and a separate one for the comment.
The App key never leaves those two jobs and no action in the author
job receives it, including as a post-step input.

Workflow edits are in scope from the start. The estate is
workflows, and an issue labelled `CI` is as fair a target as any
other; a bot that cannot touch `.github/workflows/` would abstain
from a large share of the backlog. The publisher adds
`workflows: write` to a mint when the verified diff
needs it.

### 10.2 The model credential

The same arrangement triage proved: a personal fine-grained PAT with
Copilot Requests and no repository grants, held as the org secret
`COPILOT_CLI_TOKEN`, presented to the CLI as `COPILOT_GITHUB_TOKEN`
and checked for the `github_pat_` prefix. Its owner's entitlement
pays for every premium request; `usage.json` from each session makes
the spend visible per run. Expiry (15 December 2026) needs an owner
and a rotation reminder.

Copilot Requests scope is enough for `--model`. The PAT grants no
repository access and the author job has no other credential.

### 10.3 Models and the co-author trailer

A check of the four identifiers below against the pinned CLI on
30 September 2026, with the entitlement behind `COPILOT_CLI_TOKEN`,
started a session for each one. An unknown name fails at once with
`Model "..." from --model flag is not available`, before any
request reaches the model. An interactive `/model` shows the same
list. The dispatch form offers the latest release of each family;
the earlier `claude-opus-5` and `claude-sonnet-5` still resolve, so
a caller of the reusable workflow may name them directly.

<!-- markdownlint-disable MD013 -->

| Display name | `--model` |
| ------------ | --------- |
| Claude Opus 5.5 (default) | `claude-opus-5.5` |
| Claude Fable 5.1 | `claude-fable-5.1` |
| Claude Sonnet 5.5 | `claude-sonnet-5.5` |
| GPT-6 Astra | `gpt-6-astra` |

<!-- markdownlint-enable MD013 -->

Every session runs through the Copilot CLI, so every commit carries
`Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
whichever model served it: §6.3 of the org guidance names the
assistant used, not the model behind it. The mapping still goes by
identifier prefix (`claude-`, `gpt-`, `gemini-`), so a model from
an unlisted family fails the publish rather than commit under an
identity nobody chose. It lives in `config/coauthors.json` in this
repository, where a pull request reviews any change to it. An
organisation-level variable would suit a mapping that more than one
workflow shares; triage does not commit, so nothing else reads it
today. Move it when a second consumer appears.

## 11. Scheduling and Concurrency

- Cron `0 9 * * 1-5`, two hours after triage, which takes under
  thirty minutes. A `repository_dispatch` from triage's caller would
  remove the guess at the cost of a cross-repository token; the
  offset is the first cut and §16 lists the dispatch as a later
  option.
- Caller concurrency group `code-monkey`, `cancel-in-progress:
  false`: one run at a time, and a dispatch during the scheduled
  run queues behind it.
- Within a run, the branch-existence rule in §6 and the
  one-issue-per-repository rule make concurrent agents independent.
- Across runs, the `code-monkey/*` pull request check in §6 stops a
  second attempt at an issue whose first attempt a human has not yet
  processed.

## 12. Artefacts and Retention

<!-- markdownlint-disable MD013 -->

| Artifact | Producer | Content | Retention |
| -------- | -------- | ------- | --------- |
| `monkey-evidence-<ns>` | select | `selection.json`, `matrix.json`, `agents.md`, summary | 7 days |
| `monkey-proposal-<ns>-<key>` | author | bundle, manifest, usage | 7 days |
| `monkey-session-<ns>-<key>-<attempt>` | author | CLI logs, prompt, share summary | 7 days |
| `monkey-result-<ns>-<key>-<attempt>` | publish | `check.json`, `result.json`, summary | 90 days |
| `code-monkey-<ns>-<attempt>` | report | `report.md`, `report.json` | 90 days |

<!-- markdownlint-enable MD013 -->

`<ns>` is the select job's namespace and `<key>` the issue key.
Trusted evidence downloads use the producer's artifact ID; the
proposal download uses the name (§8). Names that append the run
attempt let a rerun of the publish job alone avoid immutable-name
conflicts while producer artifacts survive.

## 13. Failure Modes

<!-- markdownlint-disable MD013 -->

| Failure | Effect | Mitigation |
| ------- | ------ | ---------- |
| Agent times out | No proposal artifact | Publisher records `author-failed`; issue eligible next run |
| Agent abstains | Manifest says why | Recorded; publisher comments the reason on the issue (§8 step 8) |
| Hooks need network the runner blocks | Agent cannot pass gates | Author job runs in audit mode (§7.5) |
| `main` moves after selection | Branch created at old base | GitHub reports mergeability; reviewer rebases or agent retries later |
| Diff needs a mode change or symlink | Publish refused | Agent leaves the step out and banners the follow-up (§5, §7.3) |
| Two runs overlap | Duplicate branches | Caller concurrency group; branch-exists refusal |
| Model PAT expired | Every agent fails at start | Prefix guard cannot detect; rotation owner (§10.2) |
| Runaway spend | Long sessions in parallel | `max_runtime_minutes` × `max_concurrent_agents` bounds wall-clock; usage report per run |
| Bundle forged by agent | Publisher replays attacker commits | Provenance by artifact ID; policy checks; per-repo token; human review |
| Bot commits fail DCO check | PR blocked | Trailer matches the API author identity (§5); rollout step 2 confirms against DCOAPP |

<!-- markdownlint-enable MD013 -->

## 14. Repository Layout

```text
.github/workflows/code-monkey.yaml       reusable workflow
.github/workflows/code-monkey-cron.yaml  schedule and dispatch caller
.github/workflows/testing.yaml           PR plumbing and offline tests
prompt/author.md                         agent task (§7.3)
AGENTS.md                                organisation stub (§12 there)
config/excluded-repos.txt                repositories to skip
config/coauthors.json                    model prefix to trailer (§10.3)
scripts/select_issues.py                 selection policy
scripts/issue_categories.py              category switches and matching
scripts/selection_outputs.py             selection files and summary
scripts/issue_reads.py                   GitHub reads for selection
scripts/monkey_github.py                 gh wrapper, REST and GraphQL
scripts/monkey_evidence.py               evidence digests, file caps
scripts/proposal_fetch.py                bounded proposal extraction
scripts/proposal_policy.py               the rules a proposal must pass

scripts/proposal_check.py                offline bundle verification
scripts/proposal_model.py                verdict record and its rendering
scripts/publish.py                       replay, open PR, reconcile
scripts/branch_refs.py                   bot branch and its PR on GitHub
scripts/git_bounded.py                   git with capped output
scripts/issue_comment.py                 one-line outcome comment
scripts/proposal_report.py               merge results into the report
tests/                                   unittest suite, offline
pyproject.toml, uv.lock                  Python tooling
docs/DESIGN.md                           this document
```

The template's linting configuration stays as the repository's
gate: `prek` hooks including gitleaks, gitlint, ruff, mypy,
basedpyright, actionlint, gha-workflow-linter, reuse, markdownlint,
write-good and `aislop` at threshold 100.

## 15. Rollout

0. A live `branches` or `pull-requests` dispatch writes into a bot
   fork (§4.3) with Actions switched off, so the agent's code runs
   nowhere until a maintainer approves the pull request's workflows
   on the target. The schedule stays dry-run; dry-run and `select`
   mode mint no write token.
1. Land the workflow with the schedule in dry-run and
   `pull-requests` mode. Inspect diffs, messages and abstentions
   for a week of runs.
2. Dispatch `branches` mode live against one repository named in
   `repositories`. Confirm Verified commits, DCO status, message
   format and that the branch-exists guard trips on a second run.
3. Dispatch `pull-requests` mode live against the same repository.
   Confirm the PR title check, Copilot's automatic review, label and
   provenance block.
4. Flip the schedule to live. Watch spend from `usage.json` and the
   ratio of merged to closed bot pull requests.

Each step is a signed commit and a PR through the normal review
process. Disabling the workflow in the Actions UI is the kill
switch.

## 16. Later Capabilities

- **Revision mode.** Re-run the agent on an open `code-monkey/*`
  pull request with unresolved Copilot or human review threads,
  within the ten-round cap the org guidance sets.
- **More than one issue per repository per run**, sequentially in
  one session, if the one-per-repository rule starves the backlog.
- **Stub roll-out.** The organisation `AGENTS.md` is on `main` of
  `lfreleng-actions/.github`; a one-off mode could open its §12 stub
  pull request in every repository, which is itself an issue this
  workflow could work.
- **Dispatch from triage** in place of the two-hour cron offset
  (§11), once a cross-repository token has an owner.
- **Private repositories.** A `contents: read` token for the author
  job would be the first credential on that runner and needs its
  own containment answer.

## 17. Decisions and Remaining Questions

The questions the first draft left open, and how they closed:

<!-- markdownlint-disable MD013 -->

| Question | Decision |
| -------- | -------- |
| Model identifiers | Checked live against the pinned CLI; §10.3 |
| Where bot branches live | Bot fork organisations, never the target (§4.3); CI on agent code runs as an outside contribution |
| Mode changes and symlinks | Reject; agent banners the human follow-up in the PR body (§5, §7.3). Unsigned commits are unmergeable and create work |
| Payload cap | 4 MiB added, 512 KiB per binary; rollout confirms (§5) |
| Workflow edits | In scope from day one; App holds `workflows: write`, minted on demand (§10.1) |
| Assigned issues | Skipped by default; `include_assigned` overrides (§6) |
| Sub-agent tool | `task`; agent-definition tools denied alongside (§7.2) |
| Skills from the checkout | Off by default; `load_repo_skills` for a repository a maintainer has read (§7.2) |
| Issue feedback | One comment per outcome via a comment-step token; no labelling, which stays with triage (§8 step 8). GitHub has no permission that stops at comments |
| Trigger | Offset cron at 09:00 UTC; dispatch from triage is a later option (§11, §16) |
| DCO | DCOAPP today, a pre-commit lint soon. The trailer matches the API commit's author identity (§5) |
| Co-author trailer | Copilot, the assistant, for every supported model prefix in `config/coauthors.json` (§10.3) |
| Comments in the packet | `OWNER` and `MEMBER` authors, bounded by count and bytes (§6) |
| Private repositories | Out of scope for v1 (§2, §16) |

<!-- markdownlint-enable MD013 -->

Still to confirm during rollout rather than before it:

1. **DCOAPP and API commits** (§5, §15 step 2). The app compares the
   `Signed-off-by` trailer with the commit author. Commits made
   through `createCommitOnBranch` carry the App's bot identity as
   author, so the trailer the publisher appends uses that identity.
   The first live `branches` run proves it, and if the app exempts
   bot authors the check passes regardless.
2. **Pre-commit DCO lint and bot commits.** The coming hook runs on
   the agent's local commits, which carry the bot identity and the
   `-s` trailer; API replay does not run hooks. Confirm the hook
   accepts the `[bot]` form when it lands.
3. **`createCommitOnBranch` payload limit** (§5). If a 4 MiB diff
   fails in rollout, lower the cap; the agent's abstention path
   covers larger changes.

## 18. Data Contracts

The files the three jobs exchange. Every reader treats a file from a
less trusted producer as hostile input: typed, bounded, and
cross-checked against the trusted `selection.json`.

### 18.1 `selection.json` (select → author, publish; trusted)

```json
{
  "schema": 1,
  "org": "lfreleng-actions",
  "generated_at": "2026-09-18T09:02:11Z",
  "mode": "pull-requests",
  "dry_run": true,
  "model": "claude-opus-5.5",
  "bot": {
    "login": "lf-code-monkey[bot]",
    "email": "123456+lf-code-monkey[bot]@users.noreply.github.com"
  },
  "guidance": {
    "repository": "lfreleng-actions/.github",
    "path": "AGENTS.md",
    "ref": "main",
    "commit": "<40 hex>",
    "sha256": "<64 hex>"
  },
  "candidates_seen": 78,
  "skipped": {"pull_request": 0, "no_type": 3, "closed": 0,
              "assigned": 2, "label": 5, "category": 0,
              "attempted": 1, "linked_pr": 0, "repository": 12,
              "one_per_repo": 30, "cap": 15},
  "issues": [
    {
      "key": "sbom-action-40",
      "repository": "lfreleng-actions/sbom-action",
      "repo_name": "sbom-action",
      "number": 40,
      "url": "https://github.com/lfreleng-actions/sbom-action/issues/40",
      "title": "...",
      "body": "...",
      "labels": ["feature"],
      "priority": "Medium",
      "type": "Feature",
      "created_at": "2026-09-18T18:52:05Z",
      "author_association": "MEMBER",
      "default_branch": "main",
      "base_sha": "<40 hex>",
      "branch": "code-monkey/issue-40",
      "comments": [
        {"author": "login", "association": "MEMBER",
         "created_at": "...", "body": "..."}
      ],
      "comments_dropped": 0,
      "comments_truncated": false
    }
  ]
}
```

`key` is `<repo_name>-<number>` and doubles as the matrix key and
the artifact-name suffix. Beside it the select job writes
`matrix.json` (`{"include": [{key, repository, repo_name, number,
base_sha, branch}]}`), `agents.md` (the guidance bytes whose SHA-256 the
`guidance` block records) and `excluded-repos.txt`. The job outputs
`selection_sha256` and `guidance_sha256` over the exact bytes.

### 18.2 `manifest.json` (author → publish; untrusted)

Written by the agent into the artefacts directory. When the agent
leaves none, the workflow writes one with `"outcome":
"author-failed"` so the publisher has something to report.

```json
{
  "schema": 1,
  "outcome": "proposed",
  "repository": "lfreleng-actions/sbom-action",
  "issue": 40,
  "base_sha": "<40 hex>",
  "branch": "code-monkey/issue-40",
  "reason": null,
  "pr_title": "Feat(sbom): Add CycloneDX 1.6 output",
  "pr_body": "...\n",
  "commands": [{"command": "uv run pytest", "exit_code": 0}]
}
```

`schema` must be the integer `1`; the publisher rejects any other
value, or none, rather than read the fields under a contract it
does not know. `outcome` is `proposed`, `abstain` or
`author-failed`; the last two need a `reason`. `changes.bundle`
sits beside the manifest when the outcome is `proposed`, created by
the workflow (not the agent) from `base_sha..branch`. `usage.json`
is the CLI's own usage output. Bounds on acceptance: manifest
1 MiB, bundle 32 MiB, usage 1 MiB; regular files, no symlinks.

### 18.3 `check.json` (publish, offline verdict; trusted)

The output of `publish.py check`, produced without credentials.

```json
{
  "schema": 1,
  "key": "sbom-action-40",
  "repository": "lfreleng-actions/sbom-action",
  "issue": 40,
  "branch": "code-monkey/issue-40",
  "base_sha": "<40 hex>",
  "default_branch": "main",
  "verdict": "proposed",
  "reasons": [],
  "needs_workflows": false,
  "commits": [
    {
      "sha": "<40 hex>",
      "tree": "<40 hex>",
      "headline": "Feat(sbom): Add CycloneDX 1.6 output",
      "body": "...\n\nCo-authored-by: ...\nSigned-off-by: ...",
      "additions": [{"path": "src/x.py", "size": 1234}],
      "deletions": ["old.py"]
    }
  ],
  "pr_title": "Feat(sbom): Add CycloneDX 1.6 output",
  "pr_body": "...",
  "stats": {"files_changed": 2, "added_bytes": 1234}
}
```

`verdict` is `proposed`, `abstain`, `rejected` or `author-failed`
here; `result.json` (§18.4) adds `publish-failed` for a write that
failed after verification, with the branch rolled back.
`reasons` explains anything but `proposed`. `commits[].body` is the
composed message the publisher will send: the agent's body plus the
trailers §5 describes. `pr_body` is the composed body: the closing
line and provenance block first, then the agent's text.

### 18.4 `result.json` (publish, per issue; trusted)

```json
{
  "schema": 1,
  "key": "sbom-action-40",
  "repository": "lfreleng-actions/sbom-action",
  "issue": 40,
  "verdict": "proposed",
  "reasons": [],
  "dry_run": false,
  "mode": "pull-requests",
  "branch_url": "https://github.com/.../tree/code-monkey/issue-40",
  "commits": ["<40 hex>"],
  "pull_request_url": "https://github.com/.../pull/41",
  "comment_url": "https://github.com/.../issues/40#issuecomment-1",
  "premium_requests": 37.5,
  "agent_seconds": 1543,
  "run_attempt": 1,
  "author_session": "<proposal artifact ID>"
}
```

URLs are `null` where the mode or a rejection stopped short, and
`author_session` where no proposal arrived. The report job merges
every `result.json` into one table.

**Open gap.** The publisher writes this file, but the report cannot
yet prove it did: the author job can reach the Actions runtime
token (§4.1) and upload an artifact of the same name. Until each
result carries a MAC under a key the author never holds, fetched
under the bounds §8 step 1 applies to proposals, the report is
informational, and nothing may write on the strength of it (the
activity log of §19 included). The maintainers have yet to choose
the secret that supplies the key.

### 18.5 Script interfaces

```text
select_issues.py --org ORG --output-dir DIR --mode MODE --model ID
    [--dry-run] [--repositories "a, b"] [--exclude-file PATH]
    [--exclude-repos "a,b"] [--include-dotgithub] [--include-assigned]
    --max-pull-requests N [--categories LIST]
    --guidance-repository O/R [--guidance-ref REF]
    [--guidance-path AGENTS.md] [--bot-slug SLUG]

monkey_evidence.py verify --directory DIR --selection-sha256 HEX
    --guidance-sha256 HEX
proposal_fetch.py --repository O/R --run-id ID --name ARTIFACT
    --output ACCEPTED

publish.py check --selection PATH --key KEY --proposal-dir DIR
    --workdir DIR --coauthors PATH --output check.json
    [--summary check-summary.md] [--run-url URL]
publish.py apply --check check.json --workdir DIR --mode MODE
    [--dry-run] [--run-attempt N] [--author-session ID]
    --output result.json
publish.py comment --result result.json [--run-url URL]
publish.py report --results DIR --output-md PATH --output-json PATH
```

`select_issues.py`, `publish.py apply` and `publish.py comment` read
`GH_TOKEN`. Everything else runs offline. A verdict other than
`proposed` is data, not an error: the scripts exit non-zero for
operational failures alone.

## 19. Activity Log and Blockers

Status: **designed, not built.** The bot keeps one open issue in
this repository, titled `Code Monkey activity log` and labelled
`code-monkey-log`, as its record and as memory between runs. The
selector finds it by label *and* by the bot as its author, so a
human-opened issue with the same label cannot stand in for it.

### 19.1 What it records

Each live run adds one comment, written by the trusted report job,
covering:

- pull requests the run raised, and branches it pushed without a
  pull request (`branches` mode, or a publish that stopped short);
- pull requests the bot raised earlier that a human has since
  reviewed, merged or closed, and the issues those merges closed;
- issues the bot itself opened, such as the human follow-up an
  `INFO` banner describes;
- interrupted work: `author-failed` and `publish-failed` results,
  and abstentions whose reason says the issue needs more iteration;
- which open issues a blocker held back, and the blocker's reason.

The issue body holds the current state, rewritten each run: open
bot pull requests with their review state, work in fork branches
(§4.3) that has no pull request yet, the latest note per issue, and
the blockers below. The comments are the history; the body is what
the next run reads.

### 19.2 Blockers

Linting tool releases, and issues or releases in upstream projects,
sometimes block a class of work until something outside the estate
changes. Maintainers list those in the body, between two markers the
bot preserves when it rewrites everything else:

```yaml
# code-monkey:blockers
- scope: "repo:sigul-docker-k8s"
  reason: "Waiting on upstream sigul 1.3 packaging"
  tracking: "https://pagure.io/sigul/issue/123"
- scope: "category:ci"
  reason: "actionlint 1.7.13 rejects the \$/ self-repository syntax"
  tracking: "https://github.com/rhysd/actionlint/issues/711"
- scope: "issue:lfreleng-actions/java-workflows#47"
  reason: "Needs the Maven 4 release"
  tracking: "https://github.com/apache/maven/releases"
```

A scope names a repository, a category (§6), a label, or one issue.
The selector skips every open issue a blocker matches, counts it
under `blocked`, and the run's comment says what each blocker held
back. The selector reads blockers from the body alone: the body is
editable by maintainers and the bot, while anyone can comment. A
malformed block fails the select job instead of the selector
ignoring it, so a typo cannot release blocked work unnoticed.

### 19.3 Notes the agent leaves itself

The manifest gains an optional `notes` field for what a later
session should know: what the agent tried, what failed, what it
would do next. The publisher bounds it to 2,000 characters and
flattens it like a reason (§8 step 8). The report job files it in
the body against the issue, replacing any earlier note. When the
selector next chooses that issue it adds the note to the packet,
marked as an earlier session's notes; the prompt treats it as data,
the same as issue text, since it descends from issue text.

### 19.4 Credentials and modes

The report job mints a token for this repository alone with Issues:
write, which the App already holds; the select job reads the log
with its existing read token, scoped to include this repository. The
author job never touches the log. Live runs write the log; dry runs
put the same content in the step summary and leave the issue alone,
but still honour its blockers.
