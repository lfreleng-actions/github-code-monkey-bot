<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Design: Scheduled AI Authoring of Pull Requests

Status: **implemented and live** (§15). The workflow, scripts and
offline tests exist, the bot fork organisations hold the App
installation and the publisher writes through them (§4.3), live
`pull-requests` dispatches have raised signed, reviewed pull
requests, and the weekday schedule runs live and posts its pull
requests to Slack (§11). §17 records the decisions taken on the
first draft's open questions and the points rollout settles.

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
- **Acting on a review in the same run.** The publisher requests
  and awaits the Copilot review of each pull request it opens and
  reports the verdict (§8); a revision round in a later run
  answers it (§20), one round per run, so the trusted jobs never
  push on the strength of a review the same agent session produced.
  The same run never acts on its own review; the next run does, and
  the caller may start that run itself (§20.6), with a fresh select
  job that re-reads everything and a fresh session.
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
`workflow_run` trigger. The schedule runs at 02:00 UTC instead and
reads the previous working day's triage (§11). Triage's
`excluded-repos.txt` lists repositories that produce noise; this
workflow keeps its own list with the same format, seeded
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

The runtime token can upload artifacts to its run, so an artifact's
name, producer run and upload digest prove the run, never the job
that made it. A trusted job reads an artifact by an id and digest a
trusted job vouched for, and by nothing less: within a run, the
select job's outputs; across runs, a notice on the select job's own
check run (§6 "Overlapping runs"). The selector disregards evidence nothing trusted
vouches for, so it never makes selection skip work.

### 4.2 What the design trusts the caller with

The calling workflow supplies both the App key and the
`assets_repository`/`assets_ref` coordinate that the trusted jobs
execute. The design trusts the caller with both: a caller able to
point the assets at hostile code could as well edit its own workflow
to run that code with the same key. Every caller here pins the assets
to its own `github.sha`, and `issues-triage` makes the same
assumption.

The pull request plumbing job executes the pull request's own
scripts with the native token alone. It proves the trusted jobs'
block-mode egress, that the invocation namespace keeps two calls'
artifacts apart, and that the workflow and its scripts work as one
commit. A local `uses:` reads the workflow from the merge commit
(`github.sha`), so the job checks the assets out from that commit in
the base repository too. Taking them from the pull request head
would pair the workflow and the scripts from different commits, and
fail every branch cut before `main` changed the interface between
them. GitHub gives a fork's
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
   owner from the table, and finds or creates the fork there. It
   looks the fork up by the target's name first, then by parent
   among the target's forks: a fork made before a rename of the
   target keeps its old name, and the publisher renames it after the
   target. A `422` because another repository holds that name fails
   the publish. GitHub answers a fork request for a parent the
   organisation already forks with the existing fork, and the
   reply's name, not the target's, is the one renamed or polled.
   Creating or renaming a fork through the API needs the App
   installed on the fork organisation with access to all its
   repositories and Administration: write, plus Contents: read on
   the source. A fork under an old name reads as missing by name,
   and the creation token of §8 step 6 is the one that renames it.
2. It syncs the fork's default branch with the target
   (`merge-upstream`), so the recorded base commit is present. The
   sync fast-forwards over every commit upstream merged since the
   last one, and GitHub refuses any that touch `.github/workflows/`
   unless the token holds Workflows: write; Dependabot pin bumps
   make that most weeks. Every fork token carries Workflows: write,
   whatever the proposal edits. Two publishes in one run may sync the
   same fork at once, and the one that loses the race answers 422
   with the fork synced all the same; the publisher retries it with
   a short backoff and then lets the base-commit lookup decide, as it
   does after a 409 for a diverged fork, while any other status fails
   at once.
3. It creates `code-monkey/issue-<n>` in the fork at the base
   commit and replays the commits there through
   `createCommitOnBranch`, with a token minted on the fork
   organisation's installation for that one fork.
4. It opens the pull request on the target with head
   `<fork-org>:code-monkey/issue-<n>`, using a token minted on the
   target organisation's installation. The request sets
   `maintainer_can_modify` to `false`: GitHub limits maintainer
   edits to forks owned by a user account, and requires a token
   that can write to the fork. The fork here belongs to an
   organisation and this token has no access to it, so `true` fails
   with HTTP 422.
5. A prior attempt (§6) is a pull request whose head repository is
   the designated fork, in one of three states. An open one is live
   work, a merged one finished the issue, and one closed unmerged is
   an attempt a human rejected. The lookup reads every page. Where
   the branch has more than one, an open one speaks for the issue
   first, then a merged one, then the newest closed one. A scan
   skips the issue in all three states; a named issue (§6 "Explicit
   issues") overrides a closed one alone. A branch in that fork with
   no pull request is an orphan: a publish that pushed and then
   failed to open the pull request leaves it on purpose, and the next
   run selects the issue again, marked `resume`, so the publisher can
   adopt the branch and finish. A same-named branch in any other
   fork, or in the target itself, does not count.

**Installation.** The publisher has no same-repository path. The
four organisations exist and each holds an installation of the App
on all repositories with Contents: write, Workflows: write and
Administration: write (§10.1). Every fork organisation has Actions
switched off, which means a bot branch triggers no workflow where it
lands, and Workflows: write on a fork token can run nothing there;
the target organisation requires approval before workflows
run for pull requests from outside contributors, and the change
gets CI once a maintainer has read it.

### 4.4 The pre-flight gate

The contract tests in `tests/test_workflow.py` run when a pull
request changes the workflow. A scheduled run executes whatever is on
the default branch, and nothing in that path re-checks the boundary
before the first App token mint. `scripts/preflight.py` closes that:
it runs from the pinned assets checkout in the select job, before any
`create-github-app-token` step, and fails the run closed on drift.

Before the read mint it re-runs the workflow contract tests and
`zizmor --persona auditor` against the checked-out workflow files,
checks `config/bot.json` names one lower-case App slug and
`config/fork-orgs.json` maps to allowed fork organisations alone,
checks each credential has the shape of the thing it claims to be
without printing it, requires a commit-pinned and loaded allow-list
in block mode, and refuses a live run whose `assets_sha` differs from
`job.workflow_sha`. After the mint it compares the `app-slug` the
action returned with the configured slug, so another App's key wired
into this workflow fails rather than acts, and probes the token
against this repository to prove it holds no `push`, `maintain` or
`admin`. The publish job repeats the identity check on the fork
token before `push` and on the pull request token before `open`.

The same module, tests and step shape run in every bot repository of
the organisation; this repository's copy differs in the module it
imports for `gh` and the job names alone.

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
  publisher **rejects** a bundle in which any commit adds or
  changes an executable file, a symlink, a submodule, or any file
  mode; any edit to an existing executable file counts, even one
  that keeps its mode, and a deletion passes. It checks each commit
  against its parent, so a later commit that undoes such a change
  does not save the chain. The prompt states the rule in those
  terms, tells the agent to leave executable files untouched and to
  abstain when a change cannot avoid editing one, and to check every
  commit with `git log --raw` before handing back, rewriting the
  commit that introduced any finding (§7.3). For other steps the
  replay cannot take, it tells the agent to leave them out, complete
  the rest, and open the pull request body with an `INFO` banner
  listing the follow-up a human has to make.
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
- **Transient failures.** A `5xx` or a timeout on a branch write is
  ambiguous: GitHub may have applied it and lost the reply. Each
  such write goes through `write_with_recovery`, which reads the
  write back before deciding (issue #100). A commit whose branch
  still sits at its `expectedHeadOid` did not land and goes again
  from the same head, since the compare-and-swap refuses a second
  copy should the first arrive late; a head one commit on, with that
  commit's parent, tree and message, is the commit landing, taken
  without a second copy; any other head, a vanished branch, or a
  read-back that fails, fails the replay as before. The fork sync
  is idempotent and retries a `5xx`, or one of gh's status-less
  wordings (`bot_github.TRANSIENT_WORDINGS`), on its own backoff,
  and records the incident even when the sync then ends on a
  tolerated `409` or `422`. A create read back as the branch at the
  base counts as created, but not as this run's, since a branch
  standing there before reads the same: a later failure keeps it, as
  for any create without a clear answer. A delete read back as no
  branch counts as done. A create or delete not found goes again.
  Every write recovered this way adds a line to the result's
  `warnings`, so the report shows the incident the run rode out.
- **An existing branch.** The publisher never deletes a branch it did
  not create. The one existing branch it moves is a closed attempt's,
  when an operator named its issue again (§6) and not otherwise. The
  selection then records the closed pull request it admitted the issue
  past as `retry_of`, which `check.json` carries from the trusted
  selection; a scan never sets it. The publisher reads every page of
  pull requests from that branch on the target: the bot opened each, a
  human closed each unmerged, and the branch still sits at the head of
  the one `retry_of` names. Nothing there postdates the bot's own
  work, and the closed pull request keeps those commits. The publisher
  then moves the branch from that checked head to the base with
  `updateRefs` and `beforeOid` (the §20.3 compare-and-swap), so a push
  after the check is never overwritten, and records a warning naming
  the pull request. A head that moved goes to the adoption test below
  and is otherwise a conflict; a move that did not apply fails the
  publication and keeps the branch. A replay that fails after the
  move does not delete the branch, which this run did not create: the
  publisher moves it back to the closed attempt's head by the same
  compare-and-swap, from the head the run left, so a later named run
  can retry. The publisher reads that head back from the branch,
  since a commit can land and lose its reply: when the branch holds a
  leading part of the replay's chain, by the adoption test below, and
  at least the commits whose reply arrived, its tip is the head;
  otherwise the head is the last commit whose reply arrived, or the
  base. A push since the move makes that refuse, and the branch stays
  untouched. It adopts an existing branch as the run's own earlier
  work on one condition: the whole chain is what the replay would
  have made. That means based on the recorded commit with no other
  history, the same number of commits, and each commit with one
  parent (the one before it), the tree the offline check recorded,
  the bot as author, a Verified signature and the checked message.
  Neither a matching
  final tree nor matching messages is enough on its own: anyone with
  push access, or a stale run, could produce either with other
  content. A pull request counts as the earlier attempt's when
  the bot opened it from the target, and never otherwise. The result
  reports an adopted branch without a new pull request, or in
  `pull-requests` mode opens the pull request an earlier attempt
  never opened; the outcome comment's marker keeps any comment from
  repeating. Any other existing branch is a rejection. A maintainer
  may delete the branch after a merge, though, so
  before any write the publisher re-reads the issue and every pull
  request from the branch (§8 step 8) and refuses on a closed issue
  or a pull request another run published meanwhile.
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
- has an author whose `author_association` is `OWNER` or `MEMBER`,
  and who is not an App or bot, or carries the `agent-approved`
  label, a maintainer's opt-in. Triage gives anyone's issue a Type,
  so any other author (`COLLABORATOR`, `CONTRIBUTOR`, `FIRST_TIMER`,
  `FIRST_TIME_CONTRIBUTOR`, `MANNEQUIN`, `NONE`, or an association
  that is absent or unreadable) needs the label. `no-agent` takes
  precedence: the selector skips an issue carrying both labels. The
  search payload decides before any per-issue read, the direct read
  decides again with the final word, and a skip counts once, under
  `untrusted_author`. Every site calls one helper,
  `issue_authors.admits`, so the sites cannot drift;
- has no assignee, unless the caller sets `include_assigned`: an
  assignee means a human has claimed the work;
- has no pull request, open, closed or merged, from its
  `code-monkey/issue-<n>` branch **in the designated bot fork**
  (§4.3: the workflow already tried, or a human declined the
  result); a pull request from a same-named branch in any other
  repository does not count, since anyone can open one, and the
  lookup names the fork organisation in its `head` filter so that
  such pull requests cannot push the bot's own off the page;
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
waits for the next run. A repository whose pull request takes a
revision round (§20.1) gets no new issue in the same run, since
revisions come first and take the repository. Each skip counts under
`one_per_repo` and lands in `skipped_issues` with a reason naming
the repository and a `chosen` field naming the entry that took it;
the summary then shows what waited and for what. The rule does not
bind revision rounds: each round is its own job with its own
checkout of the default branch head and one fork branch, and two
rounds of one repository share no state, so every bot pull request
with work outstanding gets a round within the cap. Two such pull
requests may conflict at merge time whatever order the rounds take,
and the round after the first merge resolves that (§20.2).

**Cap.** The first `max_pull_requests` survivors form the selection;
each yields at most one pull request, so the value bounds the pull
requests a run can raise. `0`
lifts the cap as far as the Actions matrix limit of 256 jobs, which
the selector applies itself so a broad run degrades to a bounded
selection instead of failing at matrix expansion. A second bound
holds the serialised selection under 12 MiB, inside the 16 MiB the
verifier accepts, since 256 entries at the per-issue body and
comment caps would otherwise exceed it.

**Two scans.** A full scan, the 02:00 schedule's, revises the bot's
open pull requests with work outstanding (§20.1) and then works new
issues by the rules above. A **`revise_only`** scan (§9.1) does the
first half alone: it reads no issue, so no candidate,
ranking or category applies, and the revision scan runs under the
full `max_pull_requests` cap as it does in a full scan, revising
every pull request of a repository with work outstanding. The
caller's second schedule runs one every hour, every day (§11), so a
published pull request's findings, failing checks and maintainer
comments reach the next slot rather than wait for the next 02:00.
A `revise_only` scan names nothing: the input
check refuses it beside `issues` or `repositories`, in a mode other
than `pull-requests`, or with `revise` off. Beside named
`pull_requests` it changes nothing, since a named run scans nothing
and works what it names and nothing else.

**Overlapping runs.** Scans share one concurrency group, but a live
dispatch naming `issues` or `pull_requests` locks on its own run
(§11), since a group built from its target list would pass GitHub's
400-character limit. So nothing stops a scheduled scan, the 02:00 run
or a `revise_only` slot, from running beside a named dispatch, or two
dispatches beside each other, and both can select one issue or pull
request. One of them is then wasted: a revision stands down at the
author job when the other has already pushed (§20.2), and a new issue
spends a whole session the publish job then refuses. Before it
selects, the select job lists the caller workflow's other runs that
are `in_progress` or `queued`, with the native token, which holds
`actions: read` and `checks: read` on the caller repository the runs,
their artifacts and their check runs live in; a caller grants both.
For each it reads the `monkey-evidence-*` artifacts that run's
select jobs published, found through every page of that run's
own artifact listing: the name must carry the run's id and the
listing must name the run as its producer. A run id and an upload
digest prove which run uploaded an artifact, not which job, and the
untrusted author job holds that run's Actions runtime token (§4.1),
so it can upload a look-alike whose name, producer and digest all
check out. An artifact counts when a select job of that run vouches
for it, and not otherwise. After its upload, the select job prints one
`::notice` titled `monkey-evidence-vouch` naming the artifact, its id
and the digest `upload-artifact` reported, and the check reads the
check run annotations of the run's jobs named `Select`, or
`<caller job> / Select`, that succeeded, in every attempt. No job
holds `checks: write`, so a job's own workflow commands alone
annotate its check run, which must carry one such notice,
no more, for the job's own run and attempt; a job with none, or with
two or more, vouches for nothing. The check reads annotations rather
than the job's log: the API serves them itself, while a log
download redirects to a rotating storage host that the block-mode
egress allow-list refuses, which would leave the check failing open
on every run. The check disregards evidence no select job vouches
for, or whose id or digest differs from the vouch, with a
`::warning::` naming it, and that
evidence holds nothing, so a forged or unverifiable artifact can
never make selection skip work:
the worst it costs is the duplicate attempt the publish job's locks
and compare-and-swap already stop. The zip's bytes must then match
the vouched digest. Every
attempt's evidence counts: a rerun of failed jobs may reselect one
caller leg while another leg's author jobs retry on its first
selection, and no name ties evidence to its leg, so a full rerun's
superseded selection holds a little longer rather than a leg's work
going unheld. That run holds every issue and pull request in its
`selection.json`, a revision's issue and its pull request both,
unless the selection is a dry run or a `select`-mode run, which
publish nothing. The selector skips a held
target, named or scanned, ahead of the cap, the budget and the
one-per-repository rule, counts it under `held` and records it in
`skipped_issues` with a reason naming the run that works it, so the
summary shows it like any other skip. The selector records a round
under its pull request, whichever of its numbers another run holds,
and passes over it before reading its packet: a named round by the
pull request it names, a scanned round by the pull request and issue
its ranking reads, and a named round whose issue alone another run
holds after building it. A held scan target frees its
place for the next candidate, so the run may work a second issue of
the repository the other run works in. A run whose select job has not
finished has published nothing and holds nothing, so two runs that
select at nearly the same time would both pass the check. The run
id breaks that tie: before it applies holds, the check lists the
other `in_progress` runs with a **lower** id and polls each one's jobs
every 10 seconds until every one of its select jobs, in every attempt
and caller leg, has finished: a select job vouches in its last step,
so a finished one has vouched or never will, and one that succeeded
says nothing of another still running. A wait cut short by the shared
bound may last 0 seconds, and the summary records it all the same.
Those waits share one five-minute bound of their own, beside the check's
deadline below, and both sit well inside the select job's 20 minutes.
At the bound, or on a failed read of its jobs, the check stops
waiting for a run still selecting, warns with a `::warning::`, and
that run holds nothing unless it has vouched by the time the check
reads it; the summary records the time already spent either way, and
for a wait cut short it states what that run holds once the check has
read its evidence (`the wait ended before it finished, and it holds
nothing`, or the count of targets a select job had already vouched
for).
The check does not wait for
a run with a higher id, since that run waits for this one, and a
`queued` run holds nothing until it starts.
The selection summary records each wait (`waited 20s for run <id> to
publish its selection`), and `selection.json` carries the same lines
as `overlap_waits`. The publish job's re-read (§8 step 8) still covers
what the tie-break cannot. The check fails open: a run listing,
an artifact listing, a job listing, a select job's annotations, a
vouched
artifact missing from the listing, a download, a digest or a
selection that fails prints a `::warning::` and holds nothing for
that run, even where its
other evidence read cleanly, or for every run when the listing fails,
and selection proceeds as before.
A run **past its report** holds nothing: before it reads a run's
evidence, the check lists that run's jobs with `filter=latest`, and
when they include a job named `Report`, or `<caller job> / Report`,
every such job has started (`in_progress` or `completed`, any
conclusion), there are no fewer of them than `Select` jobs, and
every `Select`, `Author (…)` and `Publish (…)` job has completed, the
run holds nothing. The report job needs every publish job of its
leg, so such a run has nothing left to write to any target; a caller
that calls the reusable workflow more than once holds until every
leg is past its report, never released by one leg's report while
another still publishes;
the parent of a continuation (§20.6) is past its report by
construction, since its `continue` job needs the whole call. The
listing is `filter=latest`, never `filter=all`: it lists the jobs of
the run's latest attempt, where jobs the attempt did not re-run
appear as finished copies and a job it will re-run appears
once GitHub creates it. So a re-run that publishes again holds until
its own report starts, and a re-run of the `continue` job alone
holds nothing; `filter=all` would show an earlier attempt's finished
report while a later attempt still publishes. A job listing that
fails warns and that run holds nothing, as above. Job names and
states come from GitHub, which takes the names from the trusted
workflows, so the author job can change neither.
The whole check runs under one
five-minute deadline, well inside the select job's 20 minutes, since
a stalled download alone may take five: past it the check warns, the
runs read in full still hold and the rest hold nothing. The check
saves wasted work and no more; the publish job's locks and the
compare-and-swap (§11, §20.3) remain the safety net.
`scripts/run_overlap.py` holds the check, and `scripts/overlap_wait.py`
the tie-break wait and the run listing both share; each reads at most
20 other runs.

**Guidance ref.** `guidance_ref` may name a branch, a tag or a
commit. The selector resolves it to a commit, following an annotated
tag, and refuses a ref whose target is anything but a commit, so the
recorded provenance always names the commit whose `AGENTS.md` the
agent read.

**Explicit repositories.** When `repositories` names one or more
repositories, the candidate scan covers those alone and lifts the
exclusion list and the `.github` rule; an archived, template, forked
or non-public repository stays out of scope whoever names it. The
select job hands `repositories` and `exclude_repos` to
[`list-splitter-action`](https://github.com/lfreleng-actions/list-splitter-action)
before the input check and the read mint: each splits on commas and
whitespace, drops repeats without regard to case, and must hold bare
repository names of `org`, every bad name reported before the run
fails. The splitter works offline and takes no token. The read
mint's scope is the splitter's names plus the guidance repository,
and the selector receives the split lists, never the raw inputs; it
still checks each name against `[A-Za-z0-9_.-]+`, so its own tests
stand behind the splitter's. An empty `repositories` means the whole
organisation; a list given but naming no repository, such as
separators alone, fails the input check rather than widening the scan.

**Explicit issues.** When `issues` names one or more issues, the run
works those and scans nothing. The splitter accepts `repo#N`,
`owner/repo#N`, `repo/issues/N` and the issue URL, refuses a
reference to another owner, and publishes each as `owner/repo#N`;
the selector reads that form and nothing else. Naming an issue is an
operator's decision, so the triage signals do not apply: no Type,
label, assignee or category filter, no author rule, no ranking, and
no one-per-repository rule, since the operator chose the set. The
summary lists the author association of each named issue admitted:
the operator sees whose issue they approved. What
remains is what the pipeline cannot work around: a repository the
author job cannot clone or the publisher cannot fork (archived,
template, fork, non-public or empty), a closed or absent issue, a
number that names a pull request, the `no-agent` opt-out, an open
or merged bot pull request from an earlier attempt, and an open pull
request linked to close the issue. A bot pull request a human closed
unmerged does not exclude a named issue: the operator decided to try
again, and the run works the issue afresh on the same
`code-monkey/issue-<n>` branch, which the publisher resets from the
closed attempt's head to the base (§5 "An existing branch"). A merged one
does: the work landed, and redoing it takes a new issue. An open one
becomes a revision round where §20.1 allows, and a skip otherwise.
The publisher re-makes these checks, on a scan and a named run
alike, before it writes (§8 step 8): the selection's reads may be an
hour old by the time the author session ends.
A named run reads each distinct repository on its own and never
lists the owner, so an owner at the listing ceiling can still have
an issue named; a skip never stops the issues beside it, while a
permission, rate-limit or server failure still fails the run. The
selection records every named issue it skipped with its reason, in
`skipped_issues`, naming the earlier pull request and its state
where one is the cause, and the summary lists them, so a maintainer
who named five issues and sees three in the matrix learns why. Issues and `repositories`
exclude each other: the input check fails a run naming both. An
`issues` list given but naming no issue, such as separators alone,
is an input error rather than a scan, and fails before selection.
The read mint scopes to the issues' repositories plus the guidance
repository.
The token action finds the installation through the first repository
in that scope, so a step before the mint reads each named repository
with the native token and keeps the ones GitHub reports as existing
and public, since that token also reads the private caller
repository, which the App installation may not cover; a run whose named
repositories all fail that read stops there, and the step names a
scan repository that fails it.

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

**Previous rejection.** When the bot's latest outcome footer on the
issue says `rejected`, the select job reads that run's publish
result (`result.json`, section 18.4) from the caller repository with
the run's own token (`github.token`, `actions: read`), since the App
token may not reach it, and adds its reasons to the entry as
`previous_rejection` under a fixed heading: at most 10 reasons, each
cut to 2,000 bytes and 8 KiB in all, counted as `selection.json`
serialises them, escapes included, with a count of what it dropped.
The records count against the selection's 12 MiB budget like the
entries do: one that would overrun it keeps a note and drops its
reasons. The prompt asks the agent to address each reason first. The
read fails soft: a run in another repository, a missing, ambiguous or
expired artifact, a download the bounded fetch refuses, or a result
too deeply nested to parse leaves a fixed note in place of the
reasons, and selection goes on; the select job replaces a lone
surrogate in a reason rather than fail on it. A history
longer than the round reads span, or a failed read of it, adds
nothing. The reasons quote the refused proposal, so they are data,
like the issue body.

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
- A placeholder git identity, `Code Monkey
  <code-monkey@example.invalid>`, which the packet step configures in
  the checkout, plainly not an account: the publisher replaces every
  `*-by` trailer and authors each commit as the App (§5), so the
  local identity need do no more than pass the target's hooks. The App's
  own sign-off runs to 113 characters and fails gitlint's B1 in a
  target whose `.gitlint` lacks the organisation's trailer exemption;
  the placeholder's fits within 72 columns. The runtime context names
  it as a placeholder, and the bot identity never reaches the author
  job's git configuration.

### 7.2 Copilot CLI invocation

Pinned `@github/copilot@1.0.83` on Node 22. Triage verified 1.0.80;
this workflow needs `--usage-output-file`, which 1.0.80 lacks and
1.0.83 provides with an otherwise identical tool set.

The CLI and `aislop` install with `npm ci` from the lockfile committed
under `tools/copilot-cli/`, read from the verified assets checkout,
into `$RUNNER_TEMP/copilot-cli`, outside anything the session reads.
The lockfile pins the whole dependency tree by integrity hash, so a
run never resolves a package from the registry and two runs on one
commit install the same tree. Its `.npmrc` refuses install scripts,
and Dependabot bumps the pin after a seven-day cooldown.

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
   (§5). The local identity is the placeholder of §7.1, which the
   replay replaces.
2. *Replay limits.* No added or edited executable file, symlink or
   submodule, and no mode change: the publisher refuses any edit to
   an existing executable file, even one that keeps its mode, and
   lets a deletion pass. The agent leaves executable files
   untouched, puts new code in new regular files run through their
   interpreter, creates any link or submodule a test needs at run
   time, abstains naming the file when the change cannot avoid
   editing one, and checks `git log --raw <base>..HEAD` for those
   modes in every commit before writing the manifest, rewriting the
   commit that introduced one rather than undoing it in a later
   commit, since the publisher checks each commit against its
   parent. At most five commits and 100 file changes per commit;
   under 4 MiB in total; a subject within the target's `.gitlint`
   `title-max-length`, or 50 where it sets none, as the publisher
   enforces (§8) even where the target's gitlint is looser; no
   change to a protected file, which the prompt names by class
   (agent instruction files, review and gate configuration,
   licences) with the examples §8 lists, abstaining and naming the
   file when the change cannot avoid one.
   Workflow files are in scope. Where the fix needs a step the
   replay cannot take, the agent finishes the rest and opens the
   pull request body with an `INFO` banner naming the human
   follow-up.
3. *Publication and review.* The agent cannot push, so the workflow
   publishes its commits, opens the pull request, requests the
   Copilot review, waits for it and reports its verdict. The review
   arrives after the session ends and the agent never sees it; a
   later session answers it in a revision round (§20), and a human
   reads every round. So the prompt asks for a change
   that passes review the first time: the agent reviews its own diff
   as a reviewer would and names in the body what a reviewer would
   question. It states the review-cycle steps that need GitHub
   (requesting, waiting, replying, resolving) as steps the session
   cannot take, and never tells the agent to skip them or that
   something else takes them; `tests/test_workflow.py` pins the
   prompt to the workflow's review steps.

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

The author job runs harden-runner in `audit` mode. It needs the npm
registry for the locked toolchain tarballs, PyPI, GitHub, the
Copilot API and whatever hook repositories the target's
`.pre-commit-config.yaml` pins. A block-mode allow-list
that covers every repository's toolchain is a maintenance load this
design defers; the audit log records what each session reached.

The select, publish and report jobs run in `block` mode with the
organisation's allow-list from `harden-runner-block-action`, pinned
by commit in both callers, with `allow_list_summary: 'true'` on the
select job alone. `block` is the reusable workflow's default; a
caller may pass `audit` to diagnose a blocked endpoint. The cron
caller's `options` job runs in `block` mode too, loading the same
coordinate it forwards, without a token or a summary.

Each loader also passes the caller's supplemental allow-list, its
unpinned flag and any inline extra endpoints, empty by default, so
no trusted job blocks an endpoint the caller allowed. Naming a
supplemental list makes it required, so a missing list fails at the
loader rather than at the first blocked connection. The pre-flight
pin check covers the baseline alone: a same-organisation
supplemental may follow its default branch, a rule the action
enforces.

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
   streams it to disk under that limit, retrying a download that
   gets no reply or a transient `5xx`, checks the zip directory
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
   the mutation's requirements; total added bytes under the cap; no
   addition, edit, deletion or rename of a protected path, as
   `protected` in `proposal_policy.py` defines it. It covers three
   classes, since a change to one is the agent rewriting its own
   instructions or the checks it reports as passed:
   - *agent instruction files*: a file named `AGENTS.md`,
     `CLAUDE.md`, `GEMINI.md`, `copilot-instructions.md`,
     `SKILL.md`, `.cursorrules`, `.windsurfrules` or `.clinerules`
     at any depth; anything under a `.copilot`, `.claude`,
     `.cursor`, `.agents`, `.windsurf`, `.clinerules`, `.gemini` or
     `.codex` directory at any depth; and anything under
     `.github/instructions/`, `.github/prompts/`, `.github/agents/`,
     `.github/skills/` or `.github/copilot/`;
   - *review and gate configuration*: `CODEOWNERS`, `REUSE.toml` and
     `.gitlint` at any depth, an `.aislop` directory at any depth,
     and `SECURITY.md`, `.pre-commit-config.yaml` and
     `.zizmor.yml`/`.zizmor.yaml` at the root;
   - *licences*: a file whose name starts with `LICENSE` or
     `LICENCE` at any depth, and anything under a `LICENSES`
     directory.

   Names and directories other than the licence ones compare
   without regard to case. Changes
   under `.github/workflows/` set `needs_workflows` in the check
   summary; no token mint depends on it (step 6). Whether they pass
   is the run's `allow_workflow_edits` switch (§9.1), which the
   selection records (§18.1): the check rejects a new proposal that
   adds, edits or deletes a workflow file, naming the file, unless
   the switch is on, and a selection without the field counts as
   off. A revision round may change the workflow files its pull
   request already changes and no other, whatever the switch says
   (§20.3).
   `workflow_gate.py` holds both rules.

   Every proposal then faces an **online zizmor audit**, the last
   part of the check (`online_audit.py`), since the author job's
   offline run skips the audits that need the network, such as
   `stale-action-refs`. The check exports the base and the head into
   two worktrees side by side and runs
   `zizmor --persona auditor --min-severity informational`
   `--format json --collect=all --strict-collection .` in each,
   online, with the native token alone in `GH_TOKEN` and every other
   token and `ZIZMOR_*` variable stripped. It runs whatever the
   proposal changes: zizmor decides what it collects, not the check,
   since a check that guessed which changes zizmor reads (nested
   workflow directories, an `action.yml` symlink whose target
   changes) would let every wrong guess skip the audit unseen.
   `--collect=all` audits tracked files a `.gitignore` would hide,
   and `--strict-collection` fails on an input zizmor cannot parse
   instead of dropping it. Both trees load the base's zizmor
   configuration (`.github/zizmor.yml`, `.github/zizmor.yaml`,
   `zizmor.yml` or `zizmor.yaml`, first found) through `--config`,
   or none through `--no-config`, so a proposal cannot loosen the
   rules that judge it. A finding is new when the head holds more
   findings of that rule in that file than the base does, so
   findings that moved or that the change fixed never block;
   findings zizmor reports as ignored never count. New findings
   reject the proposal, naming each rule and file. zizmor's
   no-inputs exit (3) counts as no findings for that tree, which
   covers a repository with no workflow at all and a proposal that
   adds its first workflow or removes its last. Every other failure
   rejects: a missing token or binary, a timeout, another exit
   outside zizmor's finding codes, or output that is not a JSON
   list. The publish job installs zizmor with the select job's
   steps, from the version in `config/zizmor-version.txt` that both
   jobs and CI read, before the proposal lands in its workspace.
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
   besides, the sole mint in the pipeline that holds it. Both always
   add `workflows: write`, since the step 7 sync can fast-forward
   over upstream workflow changes (§4.3). The two
   mints exclude each other on the fork's existence.
7. **Mint a pull request token** on the target organisation's
   installation for that repository alone, with `pull_requests:
   write` and `metadata: read`: the one token in the pipeline that
   can write to the target. `pull-requests` mode, for a proposal or
   a handoff, alone. The mint runs ahead of the push so a revision
   can set its title first (§20.3); the push step itself holds the
   fork token alone, and `open` and `respond` wait for the push to
   succeed.
8. **Push to the fork.** First re-read the issue with the fork
   token, since the target is public and an author session may take
   an hour: its state and labels, every pull request in any state
   from `<fork-org>:code-monkey/issue-<n>` on the target, and the
   open pull requests linked to close it (`issue_recheck`). A
   closed issue (the reason names the pull request or commit that
   closed it, from the timeline), a `no-agent` label, an open or
   merged pull request from the branch, a closed one the entry's
   `retry_of` does not name, or another linked open pull request is
   a `rejected` verdict with no write to the fork, as for a revision
   whose pull request closed (§20.3). The one open pull request it
   lets through is the bot's own carrying this proposal's chain: a
   rerun after an earlier attempt opened it, which the adoption
   below records. The re-read takes the fork's name as GitHub
   holds it now, before anything renames it: a fork kept from before
   a rename of the target still carries the old name, so the re-read
   finds it by parent as `ensure_fork` does and counts its pull
   requests and chain under that name. A read by name that GitHub
   redirects to a renamed fork takes the name its reply carries. A
   failed read is a failed step: nothing publishes on trust. A
   `branches` run makes the same reads. Then find or create the fork
   (§4.3), sync its default branch, create
   `code-monkey/issue-<n>` there at the base SHA (an existing branch
   is a rejection unless it holds this proposal's own chain, or is a
   named retry's closed attempt the publisher resets to the base,
   §5), then replay commits per §5. The sync, the create, each
   commit and a rollback's delete ride
   out a transient `5xx`: each reads itself back and goes again when
   it did not land (§5).
9. **Open the pull request** (`pull-requests` mode) against the
   default branch with head `<fork-org>:code-monkey/issue-<n>`:
   title from the manifest (equal to the subject on
   a single commit, enforced); a body the publisher heads with its
   own `Closes #<n>` line and a provenance block (run URL, model,
   issue link, commands run, the AI authorship disclosure, and the
   statement of how a later round answers review findings, §11.6
   of the organisation guidance), which nothing the agent writes
   can precede, hide or turn into code, so the merge closes the issue and the disclosure
   always shows; then, below a rule, the manifest's body with
   `@mentions` defused, so agent text cannot notify anyone before a
   human reads it; label `code-monkey` if the label exists. Not a
   draft: a ready pull request notifies code owners, and a draft
   does not by default. Opening it does not bring a Copilot review:
   GitHub's documentation limits ruleset-driven automatic review to
   pull requests "created by a Copilot user", and this App is not
   one, so a target's `copilot_code_review` ruleset leaves a bot
   pull request unreviewed; step 10 asks for one. A `422` that
   says a pull request already exists, when none of the bot's own
   is open from the branch, is a `rejected` verdict, as at step 8:
   someone else got there first. Any other failure
   here keeps the fork branch and records `publish-failed`, since a
   pushed branch is not a publication; the next run's selection sees
   the orphan, selects the issue with `resume` set, and its publisher
   adopts the branch and opens the pull request.
10. **Request the Copilot review** (`request_review`, on by default)
    with the review token (§10.4) when the caller passes one, and
    with the pull request token otherwise, as one `requestReviewsByLogin`
    GraphQL mutation, run once, for the pull request's `node_id`,
    with `botLogins: ["copilot-pull-request-reviewer[bot]"]` (the
    input takes the `[bot]` suffix) and `union: true`, so it adds the
    bot to the reviewers already requested and sends no user or team.
    `gh pr edit --add-reviewer @copilot` is not used: `gh` sends the
    same mutation with `union: false`, replacing the whole reviewer
    set from a read it made first, so a stale read drops the
    CODEOWNERS team request. The mutation selects nothing but
    `clientMutationId` and its reply is not read: reading the pending
    requests back resolves the `requestedReviewer` union for each, and
    the `Base Protections` ruleset requests a team on every pull
    request, which needs organisation Members: read to resolve. The
    review token lacks it (§10.4), so GitHub applies the mutation and
    still answers `FORBIDDEN`. From the App's installation token
    GitHub can accept the mutation without adding the reviewer, so
    the step verifies the request through the issue's events
    (`issues/{number}/events`, every page), which the token can read.
    Before the mutation it reads them and notes the newest event id;
    after the mutation, with or without an error, it counts the
    request as sent when a `review_requested` event naming the Bot
    `Copilot` (or the reviewer's own login) has a newer id, so an
    earlier round's request never counts and no clock comparison
    enters.
    A Copilot request already pending when the step starts counts
    too: its newest request event is a request, not a removal, and
    is newer than Copilot's newest review. When the step fails to
    read the reviews before the mutation, it retries this check,
    against the events it read then, on each read after it: an
    additive request for a reviewer already pending may add no
    event. A Copilot review of the published head counts as well,
    since a fast review clears the
    pending request. A review of an earlier head proves nothing,
    since a revision round always has one, and with no published head
    recorded no review counts. The events lag the request, so the
    step reads the events and the head review again after 2, 4 and 8
    seconds, as the revision publisher reads a lagging head, before
    it calls the request refused. Neither of the obvious reads can
    serve: REST `requested_reviewers` never lists the Copilot bot
    as pending (it lists the ruleset's team alone), and GraphQL
    `reviewRequests` needs the team resolved, which the token cannot
    do; the step reads the REST list for its log line and nothing
    else. When no proof
    holds, a mutation error is a refusal with GitHub's message;
    otherwise a failed read is a refusal that names it, and a
    missing request is a refusal too. A failed read of the pull
    request before the mutation sends nothing and is a refusal. The
    request is best effort: a refusal becomes a warning in
    `result.json` with GitHub's answer, a line in the job log and a
    `::warning title=Copilot review request::` annotation on the run,
    in the publisher's own words with GitHub's text defused, and an
    `INFO` notice at the top of the pull request body, placed once
    with the same token and taken down by a retry whose request
    succeeds, so a maintainer reading the pull request knows whether
    the workflow requested a review; the issue comment and the report
    (`pending (not requested)`) say so too. The publish goes on, and
    no token gains a permission for it. A request that lands logs the
    reviewers the REST read listed, which never include Copilot.
    Either way the log names the
    credential that asked, `review token` or `App token`, never the
    token, so a lapsed or revoked review token shows as a refusal
    under its own name. The App's token cannot add the reviewer
    (§17 item 4); the review token can, and this step alone holds it.
11. **Await the Copilot review** with the job's own token, since the
    target is public: poll for a review by
    `copilot-pull-request-reviewer[bot]` whose `commit_id` is the
    head the publisher pushed (the last commit of the replayed, or
    adopted, chain; the publisher reads the pull request's current
    head to confirm it, never to choose it), read the pull request's
    unresolved review threads through GraphQL, and classify the pair by the
    organisation definition (`AGENTS.md` §11.5) in a pure function
    with offline tests built from real review bodies: **clean** when
    every thread has a resolution, the overview lists no suppressed
    finding and no finding on code unchanged since the last review
    (a section Copilot writes in the body alone and opens no thread
    for, above which the body says "No open issues"), and the
    assessment is `🟢 Approval recommended` (`🟢 Ready to approve`
    in older reviews) or `🔵 Needs a closer look`; **findings**
    otherwise, `🟡 Changes recommended` and the older format with no
    assessment included; **pending** when no review of the head
    arrives within the wait, whose reason states how long polling
    ran, since the reserve for a hit's reads stops it short of the
    nominal wait. The body is text from outside: the
    classifier reads 64 KiB at most and treats a longer body as
    findings, since the part it cannot read may hold suppressed
    findings; drops fenced code and Copilot's one HTML comment, its
    format marker, and reads inline code as prose, so a tag, a comment
    opener, a heading or a label quoted in a closed code span names
    no assessment and counts no finding; fails closed on any HTML
    comment outside
    inline code, a code span left open on its line, an escaped
    backtick, an HTML tag the real bodies never carry, a tag left
    open on its line, or a heading or fence inside a `<details>`
    block before its blank line, since
    parsing what such text hides is a losing game and none of it can
    then supply an assessment or hide a finding. The tag set tracks a
    format Copilot moves under us, so the fixtures hold a body of each
    format seen and a test holds every one to a reading, and a details
    section the classifier does not know, neither a count it reads nor
    a finding's own badged title, fails the body closed rather than
    wait for a rule of its own; takes the
    earliest heading as the
    assessment; and records labels and counts, never the text,
    matching headings on one line, reading a CRLF body as its LF twin,
    and taking an unreadable count as a finding; a `>` inside a quoted
    attribute closes no tag, a line opening with any of the known
    tags starts an HTML block, and a declaration, processing
    instruction or CDATA block outside inline code fails closed as a
    comment does.
    The wait checks its deadline before every read and keeps a
    reserve for the reads a hit needs
    (the listing, the threads page by page, and a second read of the
    head), so it cannot overrun the job's budget by a read's worst
    case. Once a review lands, the publisher reads the head again: a
    push during the wait makes it a review of the old head. That, a
    head that moved before the wait began, a review of another head,
    a read failure the shared wrapper's retries did not cure, a head
    it could not read at all, and no review within the wait all end
    as pending, never clean. When step 10 recorded a refusal
    (`requested: false`), nobody asked for the review the full wait
    would poll for, so the wait shrinks to a grace of five minutes'
    polling on top of the reserve, 15 minutes in all, long enough
    for a maintainer's own request to bring a review; with none it
    ends as `pending (not requested)` with a reason naming the
    shorter wait. A caller's shorter wait stands, and a request that
    landed keeps the full wait. The record goes into `result.json` as
    `review` (§18.4).
    Once the verdict is in, the rest of the same wait reads the
    head's check runs and its combined commit status
    (`head_checks.py`), with the same token, since the target is
    public, and no new mint. A commit status, which services such as
    pre-commit.ci post instead of a check run, counts as a run with
    no annotations: a `pending` one has yet to settle, and `failure`
    and `error` fail. The publish job gates on
    Copilot's review and reports CI: a failing check never stops the
    publication, it records and warns. The read shares the wait's
    deadline, fixed before the review poll starts, and the reserve
    for its last reads, so the job's budget does not grow; a review
    that took the whole wait leaves the checks one read. Runs count
    when their `head_sha` is the published head. While a run has not
    completed it polls every 30 seconds, as long as two reads still
    fit before the deadline. It then reads the failing runs'
    annotations under the same bound (`check_reads.py`, as the
    revision round's packet does), and reads the head once more: a
    push since, or any read that fails, records `unknown`, never
    passing. The state is `passing`, `infrastructure` (every
    failing run failed at harden-runner's egress agent, below),
    `failing` (a run concluded `failure`, `timed_out` or
    `startup_failure`, or a status is `failure` or `error`, and at
    least one is a defect), `running` (a
    run still unsettled at the deadline, with any failures so far),
    `none` (no runs or statuses at all) or `unknown`. A failing run is
    `infrastructure` when one of its failure-level annotations has
    the title `harden-runner agent did not start`, or a message that
    starts, after stripping, with `harden-runner's agent reverted its
    changes`, `harden-runner's agent did not report ready` or
    `harden-runner's agent reported an error` (the block action's
    wordings, `check_reads.AGENT_TITLE` and `AGENT_MESSAGES`); every
    other failing run, and one whose annotations go unread because
    the wait ran out, is a `defect`. The reader classifies every
    failing run before the 30-run bound applies, so the counts of
    both kinds are whole whatever the record keeps, and the bounds
    keep defect runs first, by name, and then infrastructure ones,
    so a run that brings a round is never left out for one that
    does not. Such a run failed before
    the job ran its own steps: it needs a re-run, not a fix. Failing
    names bounded and escaped reach a `::warning title=Checks::`
    annotation on the run, which says when the failures are
    infrastructure and need a re-run.
    A pending commit status whose context `config/stale-statuses.txt`
    lists (`pre-commit.ci - pr`), last updated more than the run's
    `stale_status_minutes` ago (default 8; `0` turns the rule off),
    is **stale** (§13.1): each read sets it apart, so the poll does
    not wait for it and the state comes from the rest. The record
    lists it under `stale` and a second `::warning title=Checks::`
    names it (`pre-commit.ci - pr still pending after 8 minutes; not
    waited for`). A check run is never stale, whatever its name, and
    a failing or passing status keeps its result.
    The record goes into `result.json` as `checks` (§18.4); the
    revision scan (§20.1) of a later run acts on the failures, since
    this round has ended.
    Then the publisher reads whether the pull request can merge
    (`merge_state.py`), with the same
    token, since the target is public, and no write. A head that
    conflicts with the default branch gets no `pull_request` CI,
    since GitHub cannot build its merge ref, so its checks look
    missing rather than failed and the review alone would call it
    ready. The probe runs after the wait, since the base can move
    during it. GitHub computes `mergeable` in the background and
    answers `null` while it does, so the read goes again after 2, 4,
    8, 16 and 32 seconds, about a minute in all. An answer counts
    when the pull request names the published head; the probe treats
    a stale read, or a push since, like a `null`. An answer
    still `null` or for another head, or a read that fails, records
    `null` with a warning: unknown, never mergeable. The probe also reads the default
    branch's head, records whether it moved from the head the
    selection read (`rebase_sha` for a revision, `base_sha` for an
    issue) and, from GitHub's compare, the count of commits the
    published head lacks of it. A read that fails leaves its field `null` with
    a warning and stops nothing.
12. **Comment on the issue** with one line: the pull request URL on
    success; on `abstain`, the agent's reason; on a policy or
    provenance rejection, which check failed and the run URL; on a
    rejection because the issue closed after selection (step 8),
    nothing, since a comment on a finished issue is noise. The
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
    else pastes cannot suppress the comment. A transient `5xx` on the
    post looks for the marker once more: a comment found counts as
    posted, with a warning in `result.json`, and none found fails the
    step with no second post, since a slow first copy may still land.
    An operational failure
    before any write, the offline check or a token mint, posts
    nothing: the target saw no change, the verdict may be unknown,
    and one publisher fault would otherwise comment on every issue
    in the run. Step 13 records it in the report instead; a failure
    during or after the writes (step 7 on) still comments, since the
    fork saw a branch and its rollback. On a published pull request
    the line states the review's verdict: clean; findings, with the
    counts and the assessment and the statement that a maintainer
    must act on them; or pending. A head the probe found conflicting
    leads with that instead: it names the default branch and whether
    it moved during the run, says no CI ran, states the review without
    calling it clean and says the next round rebases it (§20.1); an
    unsettled answer says GitHub did not confirm whether the pull
    request can merge. Failing checks on the head add a sentence
    naming them, bounded and escaped, as do failures among checks
    still running when the wait ended; failures at the egress agent
    add their own, `N check(s) failed at the egress agent and need a
    re-run`, named the same way; passing, absent or unread checks
    add nothing. A pull request nominated for an antagonistic
    review (§20.5) adds a sentence saying the workflow requested one
    from the review bot; when the switch is off, the comment says
    nothing of it. A conflict or a known failing check, settled
    or still running, at the round cap leaves work undone: that
    comment hands over to a human. The marker carries the review
    verdict too, for findings the counts and the assessment, and for
    a pending review the workflow could not request
    `pending/not-requested`, then the probe's answer as `mergeable=`
    `true`, `false` or `unknown` when it ran, then the checks' state
    as `checks=` when the record exists, with a short hash of the
    failing checks' names when it names any (`checks=running/<hex>`;
    infrastructure failures join the hash as a second list, and the
    whole `defects` and `infrastructure` counts join it when they
    exceed the names kept, even when the record keeps none),
    so a rerun whose wait ended differently or whose failures changed,
    resolved a thread under the same verdict, or requested the review
    a first attempt could not, posts again
    instead of leaving the issue at odds with `result.json`; the
    bot's latest outcome for the run alone suppresses a repeat, so an
    outcome that changed and changed back posts as well, and a search
    that fills every page it may read proves nothing and posts. A record
    the publisher does not recognise reads as pending.
13. **Record** `result.json` and a step-summary section. A step that
    runs whatever came before writes a `publish-failed` result when an
    earlier failure (the offline check, a token mint) left none, so
    every selected issue reaches the report. A final
    report job gathers every `result.json` by artifact-name pattern
    and renders one table: issue, verdict, output URL, Copilot
    review verdict, premium requests consumed (from `usage.json`),
    detail. Below the totals it counts the review verdicts and the
    probe's answers (mergeable, conflicting, unconfirmed, not probed),
    lists each conflicting pull request apart with the count of
    commits behind and, at the round cap, that a human must take
    over, counts the head checks' states (`not read` for a result
    with no record and no review), lists each pull request with
    known failing checks, settled or still running, and their names
    and, at the round cap, that a human must take over, lists apart
    each pull request with checks that failed at the egress agent and
    need a re-run, counted in `report.json` as
    `infrastructure_check_failures`, and states,
    once, how a later round answers review findings. A
    report covers one
    selection, its namespace: a retried entry shows its latest
    attempt's row, but the spend total counts each author session
    once, since a rerun author job uploads a new proposal artifact
    and the ID of each proposal artifact names the session that
    produced it. A full rerun selects afresh under a new namespace
    and gets its own report; the earlier report keeps the earlier
    sessions' spend, so a run's cost is the sum of its reports.
    The report job also exposes the pull requests the run opened or
    revised as the workflow's outputs (§18.6), from which the
    scheduled caller posts a Slack digest (§11), and names those it
    nominates for an antagonistic review, which a last job asks the
    review bot for (§20.5).

## 9. Inputs

### 9.1 Reusable workflow (`code-monkey.yaml`)

<!-- markdownlint-disable MD013 -->

| Input | Type | Default | Notes |
| ----- | ---- | ------- | ----- |
| `org` | string | required | Target owner |
| `mode` | string | `pull-requests` | `select`, `branches`, `pull-requests` |
| `dry_run` | boolean | `true` | Author runs; publisher writes nothing |
| `model` | string | `claude-opus-5.5` | Copilot model identifier (§10.3) |
| `max_pull_requests` | string | `'10'` | `0` = up to the matrix limit |
| `categories` | string | `'all'` | `all`, or a list from §6 |
| `max_runtime_minutes` | string | `'180'` | Per agent |
| `stale_status_minutes` | string | `'8'` | §13.1: a listed pending status older than this is stale; `0` turns it off |
| `max_concurrent_agents` | string | `'10'` | 1–30, matrix `max-parallel` |
| `allow_subagents` | boolean | `true` | §7.2 |
| `include_dotgithub` | boolean | `false` | §6 |
| `include_assigned` | boolean | `false` | §6: work issues a human holds |
| `load_repo_skills` | boolean | `false` | §7.2: trust checkout skills |
| `allow_workflow_edits` | boolean | `false` | §8 step 3: let new proposals change `.github/workflows/` |
| `request_review` | boolean | `true` | §8 steps 10–11: request, await, report |
| `request_antagonistic_review` | boolean | `true` | §20.5: ask the review bot after a clean review |
| `antagonistic_repository` | string | `lfreleng-actions/github-code-review-bot` | Holds the antagonistic workflow (§20.5) |
| `repositories` | string | `''` | Commas and/or spaces; split per §6 |
| `issues` | string | `''` | Issues to work instead of scanning (§6) |
| `pull_requests` | string | `''` | Bot pull requests to revise (§20) |
| `revise` | boolean | `true` | §20: revise before working new issues |
| `revise_only` | boolean | `false` | §6: revise, work no new issue |
| `outstanding_only` | boolean | `false` | §20.6: named pull requests take the scan's tests |
| `continues_run` | string | `''` | §20.6: the dispatching run, which must have published |
| `exclude_repos` | string | `''` | Same form; overrides bundled list |
| `guidance_repository` | string | `<org>/.github` | Holds the org `AGENTS.md` |
| `guidance_ref` | string | `main` | Branch or tag recorded and used |
| `harden_runner_egress` | string | `block` | Trusted jobs; author is always `audit` |
| `harden_runner_allowlist` | string | `''` | Allow-list action coordinate |
| `harden_runner_supplemental_allowlist` | string | `''` | Merged with the baseline; required when set |
| `harden_runner_supplemental_unpinned` | boolean | `false` | Same-org supplemental may omit `@ref` |
| `harden_runner_extra_endpoints` | string | `''` | Inline `host[:port]` tokens merged in |
| `github_app_client_id` | string | `''` | Empty limits runs to dry-run |
| `assets_repository` | string | this repo | Prompt and scripts |
| `assets_ref` | string | `''` | Resolved to a SHA in select |

<!-- markdownlint-enable MD013 -->

Secrets: `copilot_token` (model PAT, §10.2), `github_app_private_key`
(select and publish alone), `copilot_review_token` (the review
request step alone, §10.4), `antagonistic_dispatch_token` (the
antagonistic request job's dispatch step alone, §10.5). All four are
optional.

Numeric inputs are strings because reusable-workflow `number`
inputs cannot carry validation and `fromJSON` handles the
conversion at the point of use. The select job rejects values
outside the stated ranges.

### 9.2 Dispatch caller (`code-monkey-cron.yaml`)

Two schedules, `'0 2 * * 1-5'` (the full scan) and
`'17 * * * *'` (`revise_only`, §11), and `workflow_dispatch`
with the inputs below. A `run-name` keeps the `Code Monkey ⏰` prefix
and adds what the run works, so the Actions list tells runs apart: a
schedule names the full scan or the `revise_only` scan by the cron
that fired; a dispatch names its `pull_requests`, else its `issues`,
else its `repositories`, as the form's raw text, else a `revise_only`
or plain scan, and marks a dry run. GitHub truncates a long name, and
nothing reads it back.

| Input | Type | Default |
| ----- | ---- | ------- |
| `dry_run` | boolean | `false` |
| `model` | choice | Claude Opus 5.5 |
| `mode` | choice | `pull-requests` |
| `max_pull_requests` | string | `'10'` |
| `max_runtime_minutes` | string | `'180'` |
| `stale_status_minutes` | string | `'8'` |
| `max_concurrent_agents` | string | `'10'` |
| `allow_subagents` | boolean | `true` |
| `include_dotgithub` | boolean | `false` |
| `include_assigned` | boolean | `false` |
| `load_repo_skills` | boolean | `false` |
| `allow_workflow_edits` | boolean | `false` |
| `request_review` | boolean | `true` |
| `request_antagonistic_review` | boolean | `true` |
| `repositories` | string | `''` |
| `issues` | string | `''` |
| `pull_requests` | string | `''` |
| `revise` | boolean | `true` |
| `revise_only` | boolean | `false` |
| `outstanding_only` | boolean | `false` |
| `continues_run` | string | `''` |
| `categories` | string | `'all'` |

The `model` choice shows display names and the caller maps them to
the identifiers in §10.3. The schedule runs `pull-requests` mode
with `dry_run: false`; the caller passes
`github.event_name == 'workflow_dispatch' && inputs.dry_run`, so a
dispatch keeps the form's choice, which is live unless the operator
ticks `dry_run`. The reusable workflow's own `dry_run` (§9.1) keeps
its default of `true`; the caller alone defaults live. A
schedule passes no inputs and takes the defaults above: up to ten
entries, worked in parallel, and at most ten pull requests.

`max_pull_requests` semantics: `1` raises at most one pull request,
`20` at most twenty, and `0` lifts the cap as far as the matrix
limit; anything non-numeric or negative fails the select job.
`categories` passes straight through: `docs` for a run that fixes
documentation alone, `all` or blank for everything, which is what a
schedule, passing no inputs, gets. The form holds 23 inputs, within
GitHub's limit of 25; the lists `issues` and `repositories` take the
place of what used to be one boolean per category.

`revise_only` is the second schedule or the form's choice: the
caller passes `github.event.schedule == '17 * * * *' ||
inputs.revise_only == true`, since GitHub sets `github.event.schedule`
to the cron string that fired. The schedule entry and the two
expressions that test it, in the `code-monkey` job and the Slack
digest, change together. A `revise_only` dispatch must leave `issues`
and `repositories` empty, keep `pull-requests` mode and `revise` on;
the input check refuses it otherwise (§6).

`stale_status_minutes` bounds the wait on a slow status service
(§13.1). An operator dispatching against a repository whose hooks are
slow, or during a pre-commit.ci incident, lifts it for that run
(`'30'`), or sets `'0'` to wait on the status as on any check; the
schedule takes the default, and the `continue` job forwards the
run's value. The input check refuses anything but a non-negative
integer.

`outstanding_only` is the continuation's input (§20.6): it needs
named `pull_requests` and neither `issues` nor `repositories`, and
the input check refuses it otherwise. `continues_run`, which the
`continue` job sets to its own run's ID, needs `outstanding_only`
and must be a run ID; an operator leaves it empty.

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
| Checks | read | select | A revision's failing check runs and their annotations (§20.1); no job logs |
| Contents | write | publish | Fork sync, branch, commits (fork organisations alone) |
| Pull requests | write | publish | Open PR, label |
| Workflows | write | publish | Fork sync over upstream workflow changes; commits that touch `.github/workflows/` (fork organisations alone) |
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
| Workflows | write | Fork sync; commits under `.github/workflows/` |
| Administration | write | Create a missing fork |

The App holds no Actions permission and no mint requests one: job
logs stay out of every trusted job (§20.1). Checks: read is a later
grant, so the installation must accept the App's permission update
before a run that requests it, or the select job's mint fails.

The select job mints a token with the read levels alone, org-wide by
default and scoped to the named repositories plus the guidance
repository when the caller lists any;
`issues: write` on the App is a ceiling, and each mint requests the
lower level it needs. The publish job mints one token per
repository for the write steps and a separate one for the comment.
The App key never leaves those two jobs and no action in the author
job receives it, including as a post-step input.

Workflow edits are in scope, behind a switch. The estate is
workflows, and an issue labelled `CI` is as fair a target as any
other; a bot that cannot touch `.github/workflows/` would abstain
from a large share of the backlog. A workflow edit is also the
change with the widest reach, so a new proposal may make one when
the operator sets `allow_workflow_edits` and not otherwise (§9.1,
§8 step 3); a
revision round stays within the workflow files its pull request
already changes (§20.3). Both fork mints request
`workflows: write` on every run, whatever the verified diff holds:
the fork sync (§4.3 step 2) fast-forwards over upstream
workflow changes too, and a proposal that edits no workflow would
otherwise fail against a fork whose upstream changed one. The grant
stays on the fork organisations, which hold bot forks alone and run
no Actions; no target-organisation mint requests it.

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

### 10.4 The review request credential

GitHub adds the Copilot reviewer when a Copilot user asks, not when
the App does: the App's installation token gets the mutation
accepted and no reviewer added (§17 item 4). The repository secret
`COPILOT_REVIEW_TOKEN` is a fine-grained PAT owned by a maintainer
with a Copilot seat, resource owner `lfreleng-actions`, all
repositories, with Pull requests: write and the implicit Metadata:
read. Reviews it requests bill to that maintainer's seat. It holds
nothing at organisation level, so it cannot resolve a team reviewer
in a GraphQL reply, and §8 step 10 verifies the request
through the issue's `review_requested` events, which the token can
read, rather than through the mutation's reply. REST
`requested_reviewers` cannot serve either: it never lists the
Copilot bot as pending. Granting
Members: read would make the reply readable, but widens the token
beyond the one mutation it exists for.

The caller passes it as the optional `copilot_review_token`, and the
publish job's `Request Copilot review` step is the one place that
reads it: as `GH_TOKEN` when set, with the pull request
token in its place otherwise, so a caller without the secret keeps
the App's request. No job-level key, job output, other step or the
author job references it, and the await step keeps the job's own
token, since the target is public. `tests/test_workflow.py` pins
that reach. The step logs the name of the credential it used, never
its value. The PAT can write to every pull request in the
organisation, so it widens what that one step could do, not what the
App holds; its expiry needs the same owner and rotation reminder as
the model PAT (§10.2).

### 10.5 The antagonistic dispatch credential

The request for an antagonistic review (§20.5) starts a workflow in
another repository, which neither the App's installation token nor a
job's own token can do. The repository secret
`CODE_REVIEW_DISPATCH_TOKEN` is a fine-grained token, resource owner
`lfreleng-actions`, limited to the one repository
`antagonistic_repository` names, with Actions: write and the
implicit Metadata: read; it can start, cancel or rerun that
repository's workflows and nothing else. The scheduled caller passes
it as the optional `antagonistic_dispatch_token`, and the
`antagonistic` job's `Dispatch antagonistic reviews` step is the one
place that reads it. That job holds `permissions: {}`, checks out
nothing and runs after the report job, so no agent output, script
from a target or publish step runs beside it; the publish job, which
records the nomination, holds no new credential.
`tests/test_workflow.py` pins that reach. Without the secret the
step warns, dispatches nothing and succeeds, so the default stays
harmless until a maintainer sets it. Its expiry needs the same owner and
rotation reminder as the model PAT (§10.2).

## 11. Scheduling and Concurrency

- Cron `17 * * * *`, live, with `revise_only` (§6 "Two
  scans"): every hour, every day, at 17 minutes past (GitHub advises
  against `:00`, when scheduled load peaks and GitHub delays or drops
  runs), it revises the bot's open pull requests with work
  outstanding and works no new issue. Each slot
  costs one select job, which reads the bot's open pull requests, and
  an agent session for each pull request with work outstanding; a
  slot with nothing to do ends after selection. This is how a pull
  request published with a failing check or a review with findings,
  or one a maintainer commented on, gets its next round from the next
  slot, weekends included.
- Continuation (§20.6): once a run's report lists pull requests whose
  round left work outstanding, its `continue` job dispatches this
  caller once for each of them, so the next round starts within
  minutes rather than at the next slot. Each continuation, a live
  dispatch naming one pull request, runs as a named run and locks on
  `code-monkey-named-<run_id>`, below: continuations run in parallel,
  and the publish locks and the overlap check (§6 "Overlapping runs")
  keep two runs off one pull request. Like any dispatch, a
  continuation posts nothing to Slack.
- Cron `0 2 * * 1-5`, live. At 02:00 UTC the run's pull requests
  are ready for review by the start of the European working day. It
  reads the previous working day's triage, which runs at 07:00 UTC
  and takes under thirty minutes, so an issue triaged today waits
  for tomorrow's run. A `repository_dispatch` from triage's caller
  would tie the two together at the cost of a cross-repository
  token; §16 lists it as a later option.
- A scheduled run posts once to Slack. The `notify` job reads the
  reusable workflow's outputs (§18.6), re-validates each link against
  the same pattern, and posts a header, one link per line in report
  order (at most 50 links or 2,500 characters, then `... and N
  more`), and a link to the run. Links the `unmergeable_pull_requests`
  output names follow the others under their own heading, since no
  CI ran on them and each needs a rebase, by a later round or, at
  the round cap, by a human; that output sorts
  no link but those the first one passed, and one that is not a JSON
  array sorts none. The `failing_check_pull_requests` output adds a
  count under the list, by the same rule, of the links published
  with failing checks, pointing at the run report for the names. A
  link the `antagonistic_pull_requests` output names, as
  `owner/repo#N`, gets `(antagonistic review requested)` beside it;
  a name that matches no link adds nothing. A
  run that raised nothing posts
  a line saying it raised none, so an empty night reads
  differently from a broken one, and a run with failed jobs that
  still reported says so under the list. A `revise_only` run says so
  in its title and under the list, and stays quiet when the
  `selected_count` output is `0`, it raised nothing, refused no link
  and every job succeeded: a slot with nothing to do every hour
  should not post. A slot that selected a revision and published no
  link, its proposal refused offline or its round abstained, posts,
  as does one whose count is missing or anything but `0`. `notify-failure`
  needs `notify` and posts when a scheduled run failed, or a cancellation
  stopped it, and the digest did not succeed: the run stopped before its report
  produced outputs, or the digest job itself failed. It says the
  nightly run failed and no digest follows; a digest that posted is
  never repeated.
  Both jobs run in the `production` environment with no token
  permissions, harden the runner in block mode with the forwarded
  allow-list (which admits `slack.com`), and warn and skip when the
  organisation's `SLACK_BOT_TOKEN` or `SLACK_CHANNEL_ID` is missing.
  The Slack token reaches their Slack steps alone and never the
  reusable workflow. Dispatches never post, live or dry; a dispatch
  is live by default, as the schedule is (§9.2).
- Caller concurrency group `code-monkey`, `cancel-in-progress:
  false`, for scheduled runs and live dispatches without `issues`:
  one run at a time, and a dispatch during the scheduled run queues
  behind it. The group sets `queue: max`, so pending runs wait in a
  FIFO queue of up to 100 rather than GitHub's default single slot,
  where a newcomer cancels the run already pending and a dispatch an
  operator made vanishes without trace. GitHub rejects `queue: max`
  beside `cancel-in-progress: true`. A dry `workflow_dispatch`
  writes nothing and takes `code-monkey-dry-run-<run_id>` instead:
  it never waits behind the schedule or a live run. A live dispatch
  that names `issues` or `pull_requests` takes
  `code-monkey-named-<run_id>`, keyed on its own run: dispatches run
  apart rather than evict each other without trace, and the group
  name keeps a fixed length, where one built from the target lists
  would pass GitHub's 400-character limit at about fifteen `repo#N`
  references and fail the run before any job starts. Its scope is
  the named targets alone (§6 "Explicit issues", §20.1), and
  `max_pull_requests` bounds it. Two dispatches naming the same or
  overlapping targets run together, as do a scan and a dispatch; the
  later one's selector skips a target the earlier one already
  selected (§6 "Overlapping runs"), and when both select within the
  same five minutes the run with the higher id waits, up to five
  minutes, for the lower one to publish its selection first, so the
  tie goes to the older run. The publish job's locks below
  serialise their writes to any one pull request or issue, so
  neither can retitle, push or roll back a title under the other
  (§20.3), and the §6 open-pull-request check, the publisher's
  branch-exists refusal and the revision's compare-and-swap limit
  the overlap to a wasted author session, never a duplicate pull
  request or a lost round.
  A scan keeps the shared group even when `repositories` narrows it.
  A `revise_only` run, scheduled or dispatched, is a scan and keeps it
  too, so it queues behind a running full scan and never runs beside
  it, and the reusable workflow's scan lock below holds it the same
  way. `revise_only` slots that fire during a long full scan wait
  their turn in the queue; each idle one then costs a select job of
  a minute or two once the scan ends.
- The reusable workflow locks a live scan on
  `code-monkey-<caller repository>-<owner>` for the whole pipeline.
  A dry run locks on `code-monkey-dry-run-<run_id>-<owner>` and a
  live run naming `issues` or `pull_requests` on
  `code-monkey-named-<run_id>-<owner>`, both keyed on the caller run
  (`github.run_id` in a reusable workflow is the caller's), so the
  legs of one caller run queue on their own lock and never cancel or
  wait on an unrelated run. No group name embeds an input list, so
  none can grow past GitHub's limit. All three groups set
  `cancel-in-progress: false` and `queue: max`.
- The publish job locks once more, on the pull request it revises
  (`code-monkey-publish-<repository>-pull-<n>`) or the issue it
  publishes (`…-issue-<n>`), across named and scanned runs. These
  locks are what serialise two runs on one pull request or issue:
  two named runs, whether their target lists match or merely overlap,
  or a scan and a named run, reach it together, and a revision's
  retitle, push, restore and respond must not interleave with
  another run's (§20.3).
  A dry run writes nothing and locks within its own run. The lock
  sets `queue: max`, so a third run on one pull request waits its
  turn, where the default queue would cancel it with no result, issue
  comment or report row; the revision's compare-and-swap refuses a
  push whose pull request moved while it waited (§20.3).
- `testing.yaml` keys pull request runs on the ref, so a newer push
  cancels a stale run, and gives each manual dispatch a group of its
  own.
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
| Target repository renamed | Its fork keeps the old name, so the by-name lookups miss it | The publisher finds the fork by parent and renames it after the target (§4.3 step 1); selection reads the old name until that publish runs |
| Diff adds or edits an executable file, symlink or submodule, or changes a mode | Publish refused | Agent leaves such files untouched, checks every commit with `git log --raw` before the manifest, and abstains naming the file when it cannot avoid one (§5, §7.3) |
| A revision's branch moves or vanishes after selection | Round's input is stale | Author job stands down with a `::warning::` and an `abstain` manifest; no session, run stays green (§20.2) |
| Two runs overlap | A wasted session on one target; never a second pull request | Caller concurrency group for scans; named-issue dispatches lock per run; the selector skips targets another active run selected and waits for an older run still selecting (§6 "Overlapping runs"), failing open; the publisher re-reads the issue and the branch's pull requests before any write (§8 step 8), so a run that loses the race publishes nothing |
| Model PAT expired | Every agent fails at start | Prefix guard cannot detect; rotation owner (§10.2) |
| Runaway spend | Long sessions in parallel | `max_runtime_minutes` × `max_concurrent_agents` bounds wall-clock; usage report per run |
| Bundle forged by agent | Publisher replays attacker commits | Provenance by artifact ID; policy checks; per-repo token; human review |
| Selection evidence forged by an author job | A later run skips work it should do | Evidence counts when a select job vouches for it in a notice on its own check run; the check disregards anything else with a warning (§6 "Overlapping runs") |
| Bot commits fail DCO check | PR blocked | Trailer matches the API author identity (§5); rollout step 2 confirms against DCOAPP |
| Copilot ignores the App's review request | No review arrives | The review token asks instead (§10.4); without it, or when it lapses, the read-back of the issue's events shows the refusal, recorded as a warning, a `::warning` annotation on the run, the credential's name in the log and `pending (not requested)` (§8 steps 10–11) |
| Review never arrives within the wait | Verdict `pending` | Reported as pending, never clean; a maintainer requests one by hand |
| A check fails at harden-runner's egress agent (a transient resolver failure, the agent reverting at job start) | The run fails before the job's own steps, saying nothing of the change | Classified `infrastructure` from its failure-level annotation (§8 step 11): no revision round, no withheld nomination, reported apart as needing a re-run; a repository that fakes the wording can at most make its own failure spend nothing (§20.1) |
| A status service (pre-commit.ci) queues for hours and never reports | A pending status holds the head unsettled: the wait runs out and the scan reads `checks still running` round after round | A listed context pending past `stale_status_minutes` is stale (§13.1): not waited for, not failing, named in the record, the comment and the report; the next round runs the hooks itself |
| Head's checks fail, or never settle within the wait | Published pull request carries red or unfinished CI | Recorded as `checks`, never a gate: a `::warning` annotation, a sentence in the issue comment, the report and a count in the Slack digest; the revision scan picks the failures up (§8 step 11, §20.1). A read that fails or a head that moves records `unknown` |

<!-- markdownlint-enable MD013 -->

### 13.1 Timers on checks

A timer that gates work applies to the narrowest class that needs it,
carries a documented default and a per-dispatch override, and on
expiry degrades to **record and proceed**, never to failure or to an
indefinite wait. Most mandatory checks in the organisation are fast
lints, but some linting jobs take tens of minutes, soak tests over
twenty and some builds thirty; a blanket limit would turn those into
premature failures. No stale rule covers check runs, then; it covers
the commit status contexts `config/stale-statuses.txt` lists, one per
line (`stale_statuses.py`). Its sole entry is `pre-commit.ci - pr`,
which queues for hours when its infrastructure is slow, often
without ever reporting. Such a status, `pending` and last updated
more than `stale_status_minutes` (default 8) before the read, is
stale: the publish job's poll stops waiting for it (§8 step 11), the
scan does not read the head as `checks still running` (§20.1), the
antagonistic nomination does not withhold for it (§20.5), and the
issue comment, the report and the next round's packet name it so a
human or a later round can judge it. `0` turns the rule off. A check
run that needs a timer of its own gets its own configuration entry,
never a blanket value; when pre-commit.ci gives way to the
organisation's own linting workflow, a check run, the entry goes and
the rule with it.

## 14. Repository Layout

```text
.github/workflows/code-monkey.yaml       reusable workflow
.github/workflows/code-monkey-cron.yaml  schedule and dispatch caller
.github/workflows/testing.yaml           PR plumbing, offline tests, actionlint
prompt/author.md                         agent task (§7.3)
AGENTS.md                                organisation stub (§12 there)
config/excluded-repos.txt                repositories to skip
config/coauthors.json                    model prefix to trailer (§10.3)
config/stale-statuses.txt                status contexts a timer covers (§13.1)
scripts/select_issues.py                 selection policy
scripts/selection_inputs.py              its command line, list and cap parsing
scripts/explicit_issues.py               named issues (§6)
scripts/issue_categories.py              category switches and matching
scripts/selection_outputs.py             selection files and summary
scripts/issue_reads.py                   GitHub reads for selection
scripts/repository_reads.py              owner listing or one repository
scripts/bot_github.py                    gh wrapper, REST and GraphQL (shared)
scripts/bot_evidence.py                  evidence digest verification (shared)
scripts/artifact_fetch.py                bounded artifact extraction (shared)
scripts/preflight.py                     pre-flight gate (shared)
scripts/ledger.py                        run ledger (shared, unused here)
scripts/proposal_fetch.py                proposal cap table over artifact_fetch
scripts/proposal_policy.py               the rules a proposal must pass

scripts/proposal_check.py                offline bundle verification
scripts/proposal_model.py                verdict record and its rendering
scripts/workflow_gate.py                 which workflow files a proposal may change
scripts/publish.py                       replay, open PR, reconcile
scripts/publish_cli.py                   its eight commands' command line
scripts/branch_refs.py                   bot branch and its PR on GitHub
scripts/stale_branch.py                  a closed attempt's branch (§5)
scripts/issue_recheck.py                 the issue re-read before a push (§8)
scripts/branch_chain.py                  a branch read back as replayed work
scripts/git_bounded.py                   git with capped output
scripts/issue_comment.py                 one-line outcome comment
scripts/review_outcome.py                its review sentence and marker token
scripts/merge_state.py                   whether a pull request can merge
scripts/proposal_report.py               merge results into the report
scripts/report_outputs.py                the run's pull request links (§18.6)
scripts/continuation.py                  which rounds continue (§20.6)
scripts/review_verdict.py                classify a Copilot review (§8 step 11)
scripts/review_sections.py               its suppressed and unchanged-code counts
scripts/review_text.py                   what one line of it the classifier reads
scripts/copilot_review.py                request and await the review
scripts/head_checks.py                   the published head's checks (§8 step 11)
scripts/review_request.py                the request and its refusal notice
scripts/revision_reads.py                a bot pull request's state (§20)
scripts/thread_reads.py                  its review threads and their replies
scripts/check_reads.py                   its head's failing checks
scripts/stale_statuses.py                statuses pending too long (§13.1)
scripts/round_reads.py                   the issue's rounds and new comments
scripts/round_caps.py                    the round caps a result reads (§20.1)
scripts/round_recount.py                 the ceiling's rounds after a race
scripts/revision_select.py               revision candidates and entries
scripts/revision_scan.py                 the owner-wide scan, ranked
scripts/revision_rounds.py               the rounds a run works, built on demand
scripts/unreviewed_heads.py              heads with no Copilot review (§20.1)
scripts/revision_check.py                a revision's offline checks
scripts/branch_swap.py                   the compare-and-swap branch move
scripts/revision_publish.py              the push, replies and resolves
scripts/selection_choose.py              the cap, budget and one-per-repo rule
scripts/run_overlap.py                   targets other active runs hold (§6)
scripts/overlap_evidence.py              their vouched selection evidence (§6)
scripts/overlap_wait.py                  the tie-break wait for older runs (§6)
prompt/revise.md                         the revision task (§20.2)
tests/                                   unittest suite, offline
pyproject.toml, uv.lock                  Python tooling
docs/DESIGN.md                           this document
```

The five modules marked shared come verbatim from
`lfreleng-actions/bots-template`, and every bot carries them; a fix
lands in the template first and then propagates here. `ledger.py`
ships so the copied set stays whole, though this bot does not yet
read a prior run's ledger.

The template's linting configuration stays as the repository's
gate: `prek` hooks including gitleaks, gitlint, ruff, mypy,
basedpyright, actionlint, gha-workflow-linter, reuse, markdownlint,
write-good and `aislop` at threshold 100. pre-commit.ci stops the
whole run at 3 minutes and has no network, so it skips four hooks:
`gha-workflow-linter` and `aislop` need the network, and
`monkey-tests` and actionlint are the slowest. The `regression` and
`actionlint` jobs in `testing.yaml` run the last two, and
`linting.yaml` runs the rest of `ci.skip` through the
`generic-workflows` linting reusable.

## 15. Rollout

0. A live `branches` or `pull-requests` dispatch writes into a bot
   fork (§4.3) with Actions switched off, so the agent's code runs
   nowhere until a maintainer approves the pull request's workflows
   on the target. Dry-run and `select` mode mint no write token.
1. Land the workflow with the schedule in dry-run and
   `pull-requests` mode. Inspect diffs, messages and abstentions
   for a week of runs.
2. Dispatch `branches` mode live against one repository named in
   `repositories`. Confirm Verified commits, DCO status, message
   format and that the branch-exists guard trips on a second run.
3. Dispatch `pull-requests` mode live against the same repository.
   Confirm the PR title check, Copilot's automatic review, label and
   provenance block.
4. Flip the schedule to live (done: weekdays at 02:00 UTC, with a
   Slack digest of each run's pull requests, §11). Watch spend from
   `usage.json` and the ratio of merged to closed bot pull requests.

Each step is a signed commit and a PR through the normal review
process. Disabling the workflow in the Actions UI is the kill
switch.

## 16. Later Capabilities

- **A comment at the round cap from the select job.** The publisher
  of a segment's last round says a human must take over; the selector names
  a pull request it skips at the cap later in the summary alone.
- **More than one issue per repository per run**, sequentially in
  one session, if the one-per-repository rule starves the backlog.
- **Stub roll-out.** The organisation `AGENTS.md` is on `main` of
  `lfreleng-actions/.github`; a one-off mode could open its §12 stub
  pull request in every repository, which is itself an issue this
  workflow could work.
- **Dispatch from triage** in place of the fixed 02:00 UTC cron
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
| Workflow edits | Off by default for new proposals; `allow_workflow_edits` turns them on. A revision round changes no workflow file beyond those its pull request already changes. App holds `workflows: write`, granted to every fork mint (§8 step 3, §10.1) |
| Assigned issues | Skipped by default; `include_assigned` overrides (§6) |
| Sub-agent tool | `task`; agent-definition tools denied alongside (§7.2) |
| Skills from the checkout | Off by default; `load_repo_skills` for a repository a maintainer has read (§7.2) |
| Issue feedback | One comment per outcome via a comment-step token; no labelling, which stays with triage (§8 step 8). GitHub has no permission that stops at comments |
| Trigger | Live cron at 02:00 UTC on weekdays, reading the previous day's triage, with a Slack digest; dispatch from triage is a later option (§11, §16) |
| DCO | DCOAPP today, a pre-commit lint soon. The trailer matches the API commit's author identity (§5) |
| Co-author trailer | Copilot, the assistant, for every supported model prefix in `config/coauthors.json` (§10.3) |
| Comments in the packet | `OWNER` and `MEMBER` authors, bounded by count and bytes (§6) |
| Private repositories | Out of scope for v1 (§2, §16) |
| Who requests the Copilot review | A maintainer's PAT with a Copilot seat, held by the request step alone; reviews bill to that seat (§10.4, item 4 below) |

<!-- markdownlint-enable MD013 -->

Still to confirm during rollout rather than before it:

1. **DCOAPP and API commits** (§5, §15 step 2). The app compares the
   `Signed-off-by` trailer with the commit author. Commits made
   through `createCommitOnBranch` carry the App's bot identity as
   author, so the trailer the publisher appends uses that identity.
   The first live `branches` run proves it, and if the app exempts
   bot authors the check passes regardless.
2. **Pre-commit DCO lint and bot commits.** The coming hook runs on
   the agent's local commits, which carry the placeholder identity
   of §7.1 and its `-s` trailer; API replay does not run hooks.
   Confirm the hook accepts the placeholder when it lands.
3. **`createCommitOnBranch` payload limit** (§5). If a 4 MiB diff
   fails in rollout, lower the cap; the agent's abstention path
   covers larger changes.
4. **Requesting a Copilot review with an App token** (§8 step 10).
   GitHub's documentation bills a review "when a bot requests" it
   to the organisation, which implies an App may ask. The live
   `pull-requests` runs showed it cannot: the mutation succeeds
   with the installation token, but the timeline shows no Copilot
   request and no review arrives, while the same request with a
   user token brought a review every time. GitHub never refused, so
   the step reported `sent` until it learnt to read the request
   back; it now records the refusal, and the review stays pending.
   The read-back goes through the issue's `review_requested`
   events, not the mutation's reply: the reply's pending requests
   include the ruleset's team request, which the review token cannot
   resolve, so GitHub answered every applied request with an error.
   REST `requested_reviewers` does not serve either: with Copilot
   pending it lists the team alone, so a read-back through it (#71)
   recorded every landed request as refused.
   Decided: the step uses a credential held by a Copilot user, the
   `COPILOT_REVIEW_TOKEN` secret of §10.4, a maintainer's
   fine-grained PAT with Pull requests: write, whose seat GitHub
   bills for the reviews it requests. No App mint widens for it, and
   without it the step falls back to the App's token and its
   refusal. A live `pull-requests` run confirms the log records
   `review request: sent` and the review arrives.

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
  "allow_workflow_edits": false,
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
  "skipped": {"pull_request": 0, "repository": 12, "label": 5,
              "untrusted_author": 4, "no_type": 3, "closed": 0,
              "category": 0, "assigned": 2, "attempted": 1,
              "linked_pr": 0, "one_per_repo": 30, "cap": 15,
              "revision": 0, "duplicate": 0, "held": 0},
  "explicit_repositories": [],
  "explicit_issues": [],
  "skipped_issues": [],
  "overlap_waits": [],
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
the artifact-name suffix. `skipped` carries every counter, zero or
not, and the summary lists each: `pull_request`, `repository`,
`label`, `untrusted_author` (an author outside `OWNER` and `MEMBER`,
or an App, without `agent-approved`, §6), `no_type`, `closed`,
`category`, `assigned`, `attempted`, `linked_pr`, `one_per_repo`,
`cap`, `revision`, `duplicate` and `held` (another in-progress run
selected the target, §6 "Overlapping runs"). An issue entry's
`author_is_bot`
records whether an App or bot opened the issue. `explicit_issues`
holds the named issues as `owner/repo#N`, in the order given, and
`skipped_issues` one
`{issue, reason}` per named issue or pull request the selection
dropped; both are empty on a scan, but for a target another run
holds and an issue the one-per-repository rule held back, which
`skipped_issues` records on a scan too, the latter with a `chosen`
field naming the entry that took its repository as `owner/repo#N`. `overlap_waits`
holds one line per older run the overlap tie-break waited for (§6
"Overlapping runs"), empty when it waited for none. `explicit_pull_requests`,
`revise` and `revise_only` record the §20 inputs; a `revise_only`
selection holds revision entries alone and `candidates_seen` is 0.
`allow_workflow_edits` records the §9.1 switch the publisher's
check reads (§8 step 3); a JSON `true` turns it on and any other
value leaves it off.
A revision entry adds `task: "revise"`, `rebase_sha`, `checkout_sha`,
`pull_request` (`number`, `url`, `title`, `head_sha`, `mergeable`,
`created_at`), `round` (global, as the footers number it),
`segment_round` (the round's place in its antagonistic segment, which
the cap reads, §20.1), `stale_rounds` (the superseded rounds before
this one, which the ceiling reads with `round`, §20.1), `max_rounds`
(the run's rounds per segment),
`collected_at` (when the selection read the comments; every entry
carries it), `session` (the run id and the select job's attempt:
new on a full rerun, which authors anew, kept across a retry of the
publish job alone; every entry carries it, and the scratch branch
and the reply marker are its), `round_reasons`, `rounds`, `threads` (`thread_id`,
`comment_id`, `path`, `line`, `outdated`, `author`, `maintainer`,
`created_at`, `body`, `replies` as `{author, maintainer, created_at,
body}` for the newest later comments from Copilot or a maintainer,
a maintainer's kept before a reviewer's, `replies_dropped`,
`maintainer_latest` and `maintainer_undated`, read over the whole
thread before the bound trims the replies), `threads_dropped`,
`review` (the §8 step 11 record of the head, with the body),
`antagonistic_review` (the hand-back of §20.1 as `{review_id, round,
run, head_sha, url, author, submitted_at, body, body_truncated}`, or
null),
`checks` (the head's failing check runs and commit statuses, as
`{name, conclusion, app, details_url, title, summary, annotations,
kind}`; a status carries its `context` as `name`, its `state` as
`conclusion`, its `description` as `title`, its `target_url` as
`details_url`, a null `app`, an empty `summary` and no annotations,
bounded as a run is; each annotation
`{level, path, line, title, message}` at level `failure` or `warning`,
and `kind` `defect` or `infrastructure` (§8 step 11);
empty when none failed), `checks_dropped` (the failing runs, and
the annotations of carried runs, the bounds left out: at most 30
runs, 10 annotations each, 2 KiB per title, summary and message,
32 KiB in all, defect runs kept before infrastructure ones;
annotations past the third page of a run count unread, whatever
their level), `checks_unsettled` (the head's runs
whose `status` is not `completed`, queued, in progress or waiting,
and its `pending` statuses,
counted over every run whatever the bounds keep: an empty `checks`
beside a non-zero count says CI had not finished, not that it
passed; a stale status, §13.1, is not counted), `checks_stale` (the
stale statuses, at most 30, each with `context`, `updated_at` and
the run's `minutes`: the hook service did not report, so the result
is unknown), `pr_comments`, `new_comments` and
`new_pr_comments`; an issue entry has `task: "issue"` and
`checkout_sha` equal to `base_sha`. Either kind adds
`previous_rejection` when the bot's latest outcome on the issue was
`rejected` (§6), as `{heading, run_url, reasons, reasons_dropped,
note}`: `note` is null when the select job read the reasons, and otherwise
a fixed text saying why it could not. Beside it the select job writes
`matrix.json`
(`{"include": [{key, repository, repo_name, number, base_sha,
branch, fork_org, fork_repository, task, checkout_sha, rebase_sha,
head_sha, pull_number}]}`), `agents.md` (the guidance bytes whose
SHA-256 the `guidance` block records), `excluded-repos.txt` and
`unreviewed-heads.json` (the `skipped_issues` entries whose reason
says the head has no Copilot review, §20.1, which the report reads
from this artifact). The job outputs `selection_sha256`,
`guidance_sha256` and `unreviewed_sha256` over the exact bytes.

### 18.2 `manifest.json` (author → publish; untrusted)

Written by the agent into the artefacts directory. When the agent
leaves none, the workflow writes one with `"outcome":
"author-failed"` so the publisher has something to report. A
revision round that stood down because its branch moved or vanished
after selection ran no agent, and the workflow writes `"outcome":
"abstain"` with the reason and `"superseded": true` instead (§20.2).
That stand-down alone sets `superseded`: the bundle step deletes the
field from a manifest the agent wrote, and the check honours it on a
revision's abstention alone.

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
`author-failed`, and for a revision `handoff` (§20.3); the last three
need a `reason`. A `proposed` manifest must carry a non-empty
`pr_title` and `pr_body` whatever the commit count, and on a
single-commit branch the title must equal the commit's subject.
`changes.bundle`
sits beside the manifest when the outcome is `proposed`, created by
the workflow (not the agent) from `base_sha..branch`. A revision's
manifest may set `base_sha` to the entry's `rebase_sha` instead,
and adds `round_summary` and `responses`, one `{thread_id,
comment_id, reply, resolve}` per thread answered (§20). `usage.json`
is the CLI's own usage output. Bounds on acceptance: manifest
1 MiB, bundle 32 MiB, usage 1 MiB; regular files, no symlinks.

### 18.3 `check.json` (publish, offline verdict; trusted)

The output of `publish.py check`, produced with no credential but
the native token it hands zizmor for the online audit (§8 step 3).

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
  "stats": {"files_changed": 2, "added_bytes": 1234},
  "online_audit": "online audit: no new zizmor findings"
}
```

`verdict` is `proposed`, `abstain`, `rejected` or `author-failed`
here; `result.json` (§18.4) adds `publish-failed` for a write that
failed after verification, with the branch rolled back.
`reasons` explains anything but `proposed`. `needs_workflows` records
whether the proposal edits `.github/workflows/`, for the check
summary alone; no step gates on it. The workflow rules of §8 step 3
are part of the check itself, and land in `reasons`. `commits[].body` is the
composed message the publisher will send: the agent's body plus the
trailers §5 describes. `pr_body` is the composed body: the closing
line and provenance block first, then the agent's text.
`online_audit` is the online zizmor audit's pass note, blank
when the check rejected the proposal before reaching it; its
rejections land in `reasons`.

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
  "author_session": "<proposal artifact ID>",
  "review": {
    "requested": true,
    "verdict": "findings",
    "assessment": "closer-look",
    "unresolved_threads": 1,
    "suppressed": 0,
    "previously_missed": 0,
    "reasons": ["1 unresolved review thread"],
    "review_id": 5425770192,
    "commit_id": "<40 hex>"
  },
  "antagonistic": {"state": "off", "reason": null}
}
```

URLs are `null` where the mode or a rejection stopped short, and
`author_session` where no proposal arrived. `reasons` starts as the
check's, at most 20, each cut to 2,000 characters: a rejection quotes
the proposal it refused, and the next selection carries them to the
agent (§6). `issue_closed` is `true` on a rejection because
the issue closed after selection (§8 step 8), and absent otherwise:
the comment step then posts nothing on the finished issue, and the
report row still shows the reason. `review` is present on
a published pull request when `request_review` is on: `requested`
says whether the request landed (§8 step 10), `verdict` is `clean`,
`findings` or `pending`, `assessment` is `approval`, `closer-look`,
`changes` or `null`, and `commit_id` is the head the review judged.
On a published pull request the probe (§8 step 11) adds `mergeable`
(`true`, `false`, or `null` when GitHub never settled or a read
failed), `mergeable_state` (GitHub's short word, or `null`),
`default_branch`, `base_moved_during_run` and `base_behind_by`
(`null` where a read failed). A conflict means `false` and nothing
else: the comment, the report and the outputs read `null` as
unconfirmed.
On a published pull request with `request_review` on, the wait
(§8 step 11) adds `checks`: `state` (`passing`, `infrastructure`,
`failing`, `running`, `none` or `unknown`), `commit_id` (the head it
read), `failing` (at most 30 runs and 32 KiB, each with `name`,
`conclusion`, `app`, `details_url`, `title`, `summary`, bounded
`annotations`, each with `level`, `path`, `line`, `title` and
`message`, and `kind`, `defect` or `infrastructure`, as the revision
packet carries them, a failing commit status among them; an entry
without `kind` reads as a defect),
`dropped` (failing runs left out),
`unsettled` (runs not yet completed and statuses still pending),
`infrastructure` (failing runs
the egress agent failed, kept or left out, so the comment and the
report count those the bounds dropped; a record without it counts
the entries marked so), `defects` (the other failing runs, kept or
left out, so a defect the bounds dropped still marks a `running`
head failed; a record without it counts the entries not marked
`infrastructure`), `read_at`, `reason` (why
the state is `unknown`, or `null`) and `stale` (the statuses
§13.1 stopped waiting for, at most 30, each with `context`,
`updated_at` and `minutes`; a record without it has none). The
comment adds one sentence naming them and the minutes, the report
counts the heads with any, and the marker's `checks=` token folds
their contexts into its hash, so a rerun that saw them settle posts
again. Check names come from the code
under review: the comment and the report bound and escape them.
The comment and the report read a `checks` record of the wrong
shape, or for a commit other than the published head, as `unknown`,
and a result with a review but no `checks` as `unknown` too.
The comment and the report read a record with any field missing or
added, or any other verdict, as `pending`, and a `pending` record
whose `requested` is `false` as a review the workflow could not
request rather than one that did not arrive. The report job merges
every `result.json` into one table whose review column names the
verdict (`pending (not requested)` for that case) and, for findings,
the counts of threads, suppressed
findings and findings on unchanged code, a count of the wrong shape
left out.
On a pushed result the publish job adds `antagonistic` (§20.5):
`state` is `requested` (nominated: the request job will ask, and
its summary says whether the dispatch landed), `off` (the switch),
`dry-run`, or `skipped` with `reason` naming the condition that
failed. The comment reads it; the report recomputes the rule.

**Open gap.** The publisher writes this file, but the report cannot
yet prove it did: the author job can reach the Actions runtime
token (§4.1) and upload an artifact of the same name. Until each
result carries a MAC under a key the author never holds, fetched
under the bounds §8 step 1 applies to proposals, the report is
informational, and nothing may write on the strength of it (the
activity log of §19 included). The maintainers have yet to choose
the secret that supplies the key. The next selection's
`previous_rejection` (§6) reads this file too; it writes nothing on
its strength, and the agent treats its bounded reasons as data, so a
forged result can at worst mislead one attempt a human reviews.

### 18.5 Script interfaces

```text
select_issues.py --org ORG --output-dir DIR --mode MODE --model ID
    [--dry-run] [--repositories "a, b"] [--issues "o/r#1,o/r#2"]
    [--exclude-file PATH]
    [--exclude-repos "a,b"] [--include-dotgithub] [--include-assigned]
    --max-pull-requests N [--categories LIST]
    --guidance-repository O/R [--guidance-ref REF]
    [--guidance-path AGENTS.md] [--bot-slug SLUG]

bot_evidence.py verify --directory DIR --expect selection.json=HEX
    --expect agents.md=HEX [--limit-bytes N]
proposal_fetch.py --repository O/R --run-id ID --name ARTIFACT
    --output ACCEPTED

publish.py check --selection PATH --key KEY --proposal-dir DIR
    --workdir DIR --coauthors PATH --output check.json
    [--summary check-summary.md] [--run-url URL]
publish.py apply --check check.json --workdir DIR --mode MODE
    [--dry-run] [--run-attempt N] [--author-session ID]
    --output result.json
publish.py comment --result result.json [--run-url URL]
publish.py review-request --result result.json --output result.json
    [--credential "review token"|"App token"]
publish.py review-await --result result.json --output result.json
    [--wait-minutes N]
publish.py respond --result result.json --check check.json
    --output result.json
antagonistic_request.py --result result.json --output result.json
    --switch true|false [--repository O/R]
publish.py report --results DIR --output-md PATH --output-json PATH
    [--org ORG --github-output PATH] [--unreviewed-heads PATH]
merge_state.py --result result.json --check check.json
    --output result.json
```

`select_issues.py`, `merge_state.py`, `publish.py push`, `open`,
`respond`, `comment`, `review-request` and `review-await` read
`GH_TOKEN`; `report` reads the select job's `unreviewed-heads.json`
when given one (§20.1). Everything else
runs offline. A verdict other than
`proposed` is data, not an error: the scripts exit non-zero for
operational failures alone.

### 18.6 Workflow outputs (report → caller)

A caller cannot read `report.json` without the run's namespace, so
the reusable workflow declares seven `workflow_call` outputs. Five
come from the report job's `Build report` step through
`publish.py report --org ORG --github-output PATH`; `selected_count`
is the select job's own `count`, the length of the trusted matrix,
checked there as digits; `antagonistic_pull_requests` is the
antagonistic job's `requested`, the nominees it dispatched (§20.5):

| Output | Value |
| ------ | ----- |
| `pull_requests` | Single-line JSON array of URLs, in report order |
| `pull_request_count` | The array's length, as a string |
| `unmergeable_pull_requests` | Those whose head conflicts (§8 step 11) |
| `failing_check_pull_requests` | Those with failing checks (§8 step 11) |
| `selected_count` | Entries selected, issues and revisions, as a string |
| `antagonistic_pull_requests` | `owner/repo#N` of each request (§20.5) |
| `continue_pull_requests` | `owner/repo#N` of each to continue (§20.6) |

Outside a live `pull-requests` run, `continue_pull_requests` is
always `[]`.

Results are not yet authenticated (§18.4), so `report_outputs.py`
treats each field as hostile. A URL counts when the whole value
matches `^https://github\.com/<org>/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$`,
`<org>` the run's `org` input, with case folded in ASCII alone, so
no non-ASCII letter that Unicode folds onto an ASCII one (the
dotless `ı` in `gıthub.com`) passes; duplicates, compared the same
way, keep their first place. `unmergeable_pull_requests` passes
the results whose `mergeable` is `false` through the same filter:
it never names a link `pull_requests` would refuse, and
`failing_check_pull_requests` does the same for the results whose
`checks` record shows a known failure: state `failing`, or `running`
with defect failures already recorded (its whole `defects` count,
names kept or not); failures at the egress agent
(`infrastructure`) are not among them. A dry-run
result, or one without a `pull_request_url`, contributes nothing.
The script leaves any other value out and never copies it; it counts
them in `report.json` (`excluded_pull_request_urls`, beside the
`pull_requests` array), in the report and as a `::warning::`. The
keys stay fixed and neither value can hold a newline. A selection that ran no
agent gives `[]` and `0`; a run whose report job did not run gives
empty strings. Like the report, the outputs are informational: they
name pull requests, and nothing may write to a repository on the
strength of them. The scheduled caller's Slack digest (§11) is their
one consumer; a forged result can at most add a link to another pull
request in the organisation. The report's
`antagonistic_pull_requests` output, the request job's input, and
the `continue_pull_requests` output, the caller's `continue` job's
input, are the two exceptions; §20.5 and §20.6 bound them.

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

## 20. Revision Rounds

A pull request the bot opened is not finished until a Copilot review
of its head is clean (organisation guidance §11.5), and a review
with findings, or a maintainer's comment, needs an answer. A
**revision round** sends a session back to the pull request: it
reads the review and the comments, revises the same branch in place,
and the publisher moves the branch, answers each thread, and runs
the §8 review gate on the new head. One run performs one round; the
next run, scheduled or dispatched, performs the next, up to ten per
pull request, the cap the guidance sets, counted afresh for each
antagonistic review that hands the pull request back (§20.1).

### 20.1 Selection

Three ways in, all through the select job and its read token:

- `pull_requests` names the bot's open pull requests to revise,
  through the list splitter in pull-request mode. A named pull
  request gets a round with or without work outstanding: the
  operator decided.
- An issue named in `issues` whose earlier attempt is an open bot
  pull request becomes a round on that pull request rather than a
  skip. With `revise` off the earlier behaviour stands. A closed
  earlier attempt brings no round: the run works the named issue
  afresh (§6 "Explicit issues"), and a merged one keeps it out.
- A scan with `revise` on, the default, searches the owner for the
  App's open pull requests and selects those with **work
  outstanding**, and a `revise_only` scan (§6 "Two scans") does that
  and nothing else; the caller runs one every hour, every day (§11),
  so a published pull request's findings and failing checks reach the
  next slot, not the next 02:00 run. Work
  outstanding is: an unresolved review thread whose opening comment
  comes from Copilot, a listed antagonistic review App or an
  `OWNER`/`MEMBER`; a Copilot review of the head with findings; an
  antagonistic review that handed the head back after the bot's
  latest footer, recorded as `antagonistic review round N handed
  back`; a check run on the head whose conclusion is
  `failure`, `timed_out` or `startup_failure` and whose kind is
  `defect` (§8 step 11), recorded as `failing
  checks` among the round's reasons (the publish job's `checks`
  record, §18.4, reports the same failures on the round that
  published the head, but selection reads them afresh); a head that
  conflicts with the
  default branch (`mergeable` false), recorded as `conflicts with the
  default branch`, since GitHub runs no CI on it and it would
  otherwise wait unseen; or an `OWNER`/`MEMBER` comment
  on the issue, the
  pull request or an unresolved review thread, an opener or a reply,
  after the bot's latest footer. The predicate leaves out, by name, a
  check the head's own round cannot influence (`check_reads.EXCLUDED`,
  empty today); the round cap bounds a check that fails for reasons outside
  the pull request. GitHub computes `mergeable` in the background,
  so the selection reads a `null` again on the §8 step 11 backoff,
  for the head it selected, before the test, which
  adds up to a minute per such candidate; one that never settles is
  no work on its own, so a GitHub hiccup spends no round. A head
  with nothing else whose check
  runs have not all finished (`check_reads.is_unsettled`, the predicate
  the publish job's `running` state uses; the reader sets a stale
  status, §13.1, apart first, so it alone is `nothing outstanding` and is no
  work, and the entry's `checks_stale` names it) skips as `checks still
  running`, not `nothing outstanding`, so the skipped-issues record
  and the report tell it from a green head and the next scan reads it
  again, since the select job waits for no CI; with other work
  outstanding the round goes ahead and the entry's `checks_unsettled`
  tells it CI had not finished; a head whose failures are all
  `infrastructure`, settled, with nothing else outstanding, skips
  with the reason `revision_select.INFRASTRUCTURE_ONLY` (failures
  at the egress agent alone; it needs a re-run), not `nothing
  outstanding`, so the skipped-issues record, the summary and the
  digest show it, and a re-run or the next push clears it; with
  other work outstanding the round goes ahead and the packet carries
  those entries marked `infrastructure` (`CheckPacket.infrastructure`
  counts them whole, each failing run read and classified before
  the bounds, as `outstanding` counts the defects); a head with
  nothing else outstanding, its checks settled with no
  infrastructure failure, and no Copilot review skips with the
  reason `unreviewed_heads.NO_REVIEW`, or `unreviewed_heads.REFUSED`
  when the bot's latest footer on the issue records the review as
  `pending/not-requested` (§8 steps 10–11), not `nothing
  outstanding` (see "Heads with no review" below); a clean head
  with nothing else needs no round; and
  after a handoff (§20.3) neither the disputed threads
  nor a failing check nor a hand-back brings a round on its own, a
  maintainer's comment does. Revisions come
  before new issues, ranked by the originating issue's priority and
  then the pull request's age, the scan reading each hit's pull
  request and issue for the rank before the cap, under the same cap
  but not the one-per-repository rule (§6), so a repository with
  two or more pull requests outstanding gets a round on each, and a pull
  request past the cap costs no
  reads: the selection builds a candidate when the budget has room
  for it, so a failed read of a round it could not admit aborts
  nothing.
  Revisions select in `pull-requests` mode alone, named ones included,
  since a `branches` run publishes none.

**Heads with no review.** A head reaches the scan with no Copilot review
when the publish job's wait ended `pending`, Copilot not having answered
within it or GitHub refusing the request because the review token lapsed
or was never set (`pending (not requested)`, §10.4), or when a
maintainer pushed to the bot branch and nobody asked for a review of the
new head. With nothing else outstanding no round has anything to answer.
Read as `nothing outstanding`, which the scan does not record, a lapsed
token would turn every new pull request into a silent stall, so the scan
records the skip in `skipped_issues` like every other.
`unreviewed_heads.py` lists those heads under their own heading, with
their count and the count whose request GitHub refused, in the selection
summary and in `report.md` and `report.json`. The list reaches the
report as a file, not a job output: a scan may skip hundreds of heads,
more JSON than one environment string holds (`E2BIG`). The report job
fetches the select job's evidence artifact by ID, without failing the
job when the fetch does, and passes `unreviewed-heads.json` on when its
bytes match the select job's `unreviewed_sha256`; otherwise it warns and
the report omits the list. The report prints an entry when it names a
pull request of the owner with one of the two reasons, and counts the
rest, an unreadable file as one. A skip is not a selection, so the Slack
digest's quiet rule (§11) holds; the counts in the step summary are what
tells a human the token lapsed. The scan leaves such a head for
Copilot's own arrival or for a human: the next scan reads it again, and
a review that arrives with findings, a thread, a failing check or a
maintainer's comment brings a round as before. The select job does not
request a review itself: it mints a token with the read levels alone by
design (§10.1), and the review token reaches the publish job's
`Request Copilot review` step and nothing else (§10.4). A re-request path would
need that token: one shape that fits the trust model is a selection
entry of a new kind that runs no author session and whose publish job
runs the request and the wait alone. That is a larger change, left until
the counts show the case occurs.

Whatever the way in, the candidate must be the bot's own: open, opened
by the App, head in the designated fork (§4.3) on a
`code-monkey/issue-<n>` branch; its repository must pass the §6 safety
tests, and its issue must be open and carry no `no-agent` label, so
closing the issue or opting it out stops the rounds. A round a scan
finds also applies the §6 author rule: the selector skips the round
of an issue from an author outside `OWNER` and `MEMBER`, or from an
App, that lacks the `agent-approved` label, and the reason names the
label, so its pull request costs no further rounds until a
maintainer approves the issue. A named round, like a named issue,
applies no author rule. The selector skips a round, named or scanned,
whose pull request or issue another in-progress or queued run already
selected, counts it under `held` and names that run in the reason, so
two runs do not revise one pull request at once (§6 "Overlapping
runs"). A scan honours the
run's repository policy, the named repositories or everything but the
exclusions, before it reads a hit. The round number continues from the
bot's own footers on the issue: the last line of each of its comments,
the first publication's included as round zero, read across the whole
comment history or failing closed when that history outgrows the read. A
marker in a comment's body text is text. The footer names when its run
read the comments (`since=`), and that, not the footer's own time, is
the watermark a maintainer's comment must follow to be new: a comment
posted while a run was reading is work for the next round. The
watermark sits one second before the read, since it has second
precision and a comment must follow it strictly to be new; the
overlap may reread a boundary comment and loses none. The latest
footer is the one with the latest read time, not the latest post: a
retry of an older round that posts after a newer handoff rewinds
neither the watermark nor the pause, and two posts of one read order
by their time. The footers of other pull requests do not count: a
revision round's footer names its pull request (`pr=`), and one
naming another belongs to an earlier attempt, from a pull request a
human closed before an operator named the issue again (§6 "Explicit
issues"), whenever it posts. A footer naming none, a first
publication's or one written before footers named their pull
request, counts from the pull request's opening on. The new pull
request so starts its own round count, cap, watermark and pause
afresh. A footer
whose round published nothing, an abstention, a rejection or a failure,
counts toward the cap and moves no watermark, since it handled no
request.

**Superseded rounds.** A round that loses a race with another run's
push did no work the cap exists to bound: the author job stood it
down because the branch moved or vanished after selection (§20.2),
the push found the branch at another head before its swap or the
swap's compare-and-swap refused it (§20.3), or `respond` found a
handoff's head moved (§20.3). Its footer ends `stale=1` (§20.3), and
`round_reads.round_history` counts such a footer in `stale` alone:
it raises no round number, opens or lengthens no segment, anchors no
review and adds no round summary, so the round costs the pull
request none of its segment's cap, and the next round takes the same
round number. It still ran, so the selector adds `stale` to the
highest round against the ceiling of sixty below. A branch that
vanished reads the same way, though its pull request, merged or
closed, is never selected again; a pull request
the push or `respond` reads as closed is a plain rejection. Racing
runs, or a stand-down a manifest forges past the bundle step, could
then loop without bound, so a guard of its own (`MAX_STALE_ROUNDS`,
ten) skips a pull request whose history holds ten superseded
footers, before any hand-back read, until a human looks. Another
run can post the tenth while a counted round runs, so the publisher
reads the guard for every outcome (`round_caps.guard_reached`, the
recounted `stale_rounds` plus this round if superseded): a counted
round there withdraws its nomination and, unless a clean proposal,
says a human must look.

**The cap per antagonistic segment.** The review bot gives a pull
request up to five antagonistic rounds before a maintainer takes over
(`lfreleng-actions/github-code-review-bot` issues 28 and 58), and each
hand-back needs its own Copilot cycles to answer, so ten rounds is a
cap per **segment**, not per pull request. The first publication
opens the first segment; the first round whose footer carries a
review, as the hand-back its entry held (`handback=<review id>`) or
as the one it answered (`antagonistic=<review id>`, §20.3), opens the
next, and counts as its first. A failed round records the hand-back
its entry held too, so its retries count in the segment it opened. A
later round naming the same review, as a handoff a maintainer resumes
on the same head or a retry of a failed round does, opens nothing, so
each review ID anchors at its first round and the latest of those
anchors starts the segment. `round_reads.round_history` returns
the segment's count beside `highest`, by round number, so a round
posted twice counts once, and
the footer's `round=N` stays global: the round number is monotonic
for history and summaries, and the cap alone reads the segment. The
selector skips a pull request whose segment holds ten rounds, or the
rounds a dispatch's `max_rounds` input sets for that run, unless
the selection finds an unanswered hand-back of the head (the one the
work-outstanding test reads: a listed App typed `Bot`, the current
head, the marker on the body's last line, posted after the
watermark) whose review no footer has anchored yet; that round opens
a new segment and goes ahead whatever the last one counted. The
anchor, not the watermark, decides: a failed round moves no
watermark, and a hand-back that a round's selection read as it
arrived postdates that round's watermark, yet a footer carries
each. No comment resets the count, and a footer counts when the bot
posted it. At the cap the selector reads the hand-back alone, before
the packet, so a capped pull request costs one read. Above every
segment sits a ceiling of sixty rounds (`MAX_TOTAL_ROUNDS`: the first
publication's ten and ten for each of the review bot's five rounds);
at or above it the selector skips first, reading no hand-back,
whatever the segment says or the run's cap, so footer history that
goes wrong still ends the loop. The segment's cap bounds the rounds
that count; the ceiling bounds those and the superseded rounds above
together, which also have their own guard of ten. The entry carries the global
`round`, `segment_round`, `stale_rounds` and `max_rounds`, and the
publisher reads them through one rule, `revision_reads.round_cap_reached`,
with `round` plus `stale_rounds` as the rounds the ceiling counts
(`round_caps.spent_of`): the issue comment's cap notice, the
report's cap note and the antagonistic review nomination all read
that sum, so a round that spends the ceiling's last round offers no
further round the selector would refuse. Two runs that select one
pull request read the same history, so neither count holds the
other's round: after 54 counted and four superseded rounds, a
counted winner and a superseded loser of round 55 spend the sixtieth
between them, yet each selection counts 59. The comment step holds
the issue token, so it recounts the history before it writes
(`round_recount.recount`): it reads every footer of the pull request
but this attempt's own (its run URL and `since`), adds this round
once, and records the sum as `rounds_spent` and the superseded
rounds before it as `stale_rounds`, neither below the selection's.
The comment, then the report and the nomination it recomputes from
`result.json`, read that sum whichever run posts second; a
nomination the recount puts at the cap becomes `skipped`. A read
that fails keeps the selection's count, with a warning. At the cap
the selector skips the pull request with that reason, and the
publisher of the segment's last round has already said a human must
take over until the next antagonistic review hands it back. One key
admits one entry, so a pull request named beside its own issue runs
once.

The entry (§18.1) carries both bases: `base_sha`, the commit the
pull request's chain descends from (the compare merge base), and
`rebase_sha`, the default branch's head. It carries the pull request,
the unresolved threads with the ids a reply and a resolve need and
the newest replies from Copilot or a maintainer on each, read from
the whole thread page by page so a crowd of passers-by hides no
maintainer's answer, since that answer is an instruction as much as
a comment on the pull request is. The reads take in every thread
that counts before the packet's bounds apply, and the whole of each
thread before the bound trims its replies, so they hear a
maintainer's word after the watermark whichever thread or reply
holds it; the threads holding one go first into the bounded packet,
and within a thread a maintainer's replies outrank a reviewer's for
the places the bound allows. The entry carries the
head's review verdict; the head's failing checks, read from the
public check runs API (`filter=latest`, every page, failing closed
past ten) with the failure and warning annotations of each, and from
the public combined status (`commits/{sha}/status`, one page of 100,
failing closed past it; GitHub keeps the latest status per context),
whose `failure` and `error` statuses count as failing checks and
`pending` ones as unsettled, bounded as the threads are; the
maintainers' comments on the pull request,
the maintainers' comments on the issue and the pull request since the
last round, newest first within the bounds and one oversized comment
hiding none behind it, so the instruction that caused the round
reaches the agent whatever the count of older comments, the earlier
round
summaries and the issue packet as before, all as data, bounded as
the issue comments are. An annotation names the failing step,
seldom its cause, which sits in the job log. The select
job reads no log: logs need Actions: read and redirect to a rotating
storage host outside the select job's egress allow-list, so the job
records which checks failed and where, and the session, which has
the checkout and the toolchain, recovers why (§20.2). The read mint
adds Checks: read for this and scopes to the
named repositories as for issues. The combined status needs no
permission on a public repository, and every selection path refuses
one that is not public, so the mint does not ask for Commit statuses,
which the App registration does not hold: GitHub would refuse a mint
asking for it. Should private repositories come into scope (§16), the
App gains Commit statuses: read first, then the mint and §10.1. A
status's `context`, `description` and `target_url` come from whoever
can post statuses on the repository: data, bounded and escaped as a
run's name and title, and the select job never follows `target_url`.

**Antagonistic review.** The review bot's antagonistic mode
(`lfreleng-actions/github-code-review-bot` issue 28) reviews a bot
pull request once Copilot's review of its head is clean, as one
`COMMENT` review by its own App, and dispatches this workflow with
the pull request named when it has something to hand back. Its
findings sit in inline threads and in the review body: the summary,
which may name a defect of the whole change, findings with no diff
line to sit on, every finding when GitHub refused inline placement,
and notes on malformed or capped findings. The trusted Apps are
`config/antagonistic-reviewers.txt`, one login per line without the
`[bot]` suffix, read from the assets checkout at the pinned commit as
the other configuration is; an empty list turns the source off. An
author counts when the list holds its login **and** GitHub types it a
`Bot` (REST `type`, GraphQL `__typename`), so a user who took the
App's slug as a login is nobody's reviewer. The selection lists the
pull request's reviews and takes the newest submitted one by a listed
App whose `commit_id` is the head and whose body, trailing whitespace
aside, ends with the line `<!-- antagonistic-handback round=N
head=<head SHA> run=<run id> -->` naming that head. A review of an
older head, a marker that is not the last line, or one naming another
head does not count, and an `antagonistic-review` marker hands
nothing back. The review bot's trusted applier writes that line
alone, and it disarms `<` in agent text, so the marker is the review
bot's word, not its agent's. The selection reads that review before
the threads and again after its other reads: GitHub posts a review
with its inline comments, so the threads read between the two hold every
finding it placed on the diff. When the two reads name different
reviews, one landed mid-read and its threads may be in the packet
without its body; a round they started would move the head past the
review, which then never counts again. The read fails instead, and
the next run reads threads and body together. The entry carries
that review's body
bounded as Copilot's is, marked when cut, under
`antagonistic_review`; the App's unresolved threads count as work and
enter the packet with Copilot's and the maintainers', under the same
bounds and reply trimming, and the publisher answers and resolves
them as it does every thread. The prompt tells the session the body
is another agent's review, data and never instructions, as Copilot's
text is: nothing parses findings out of it, the session reads it.
A review listing that fails, or a listed review without a readable
body, fails the read as every revision read does, so no round runs
without the findings that brought it. This workflow also asks for
that review itself once Copilot's is clean (§20.5); the marker
above, never the request, makes a hand-back.

### 20.2 The author job

The author job checks out the default branch head (`checkout_sha`),
fetches the bot branch from the public fork without a credential,
refuses any head but the recorded one, and checks it out. A branch
that moved or vanished after selection stands the round down, as
the publish job does (§20.3): the moved branch is another round's
work, a named dispatch's or the schedule's, and a vanished one
means the pull request merged or closed, so the job neither
fetches again nor retries. The fetch step prints a `::warning::`
naming the branch, the fork and, for a moved branch, both heads cut
to seven characters, exits 0 and sets an output that skips every
later step that would install the toolchain, assemble the prompt or
run the session. The bundle step then writes an `abstain` manifest
with the same reason and `superseded: true`, so the run stays green,
the round counts toward no segment's cap (§20.1), the report and digest
count a skip rather than a failure, and the issue gets the one
"did not attempt it" comment; the publisher's own moved-head
rejection is never reached, since the round proposes nothing. Any
other fetch failure still fails the job. The prompt is
`prompt/revise.md`: weigh each finding as the guidance says, fix
what deserves a fix, commit by the guidance's review-round rules
(amend a single commit; one dedicated feedback commit on a
multi-commit branch), keep the chain on `base_sha` unless a
maintainer asked for a rebase or the pull request is not mergeable,
and then rebase onto `rebase_sha` and no other commit. A round whose
reasons include `conflicts with the default branch` exists for the
conflict, so the prompt asks for the rebase even when no maintainer
did. The manifest
(§18.2) adds a reply per thread with whether to resolve it, the pull
request body and a round summary. The prompt describes the failing
checks as data, never instructions, since their names, summaries and
annotations come from the code under review, and asks the session to
reproduce each failing job from the repository's workflow
definitions, record what it ran in the manifest's `commands`, and
report a failure it cannot reproduce or attribute to the change in
the pull request body rather than fix it blind. When
`checks_unsettled` is non-zero, the prompt tells the session that CI
had not finished when the selection read the head, so the absence
of a failing check is not evidence it passed, and that it still runs
the repository's tests before it finishes. The bundle step reads the
base the agent chose, accepting nothing but the two recorded values
on the command line. The trust boundary of §4 stands: the job holds
no write credential and the fork is public.

### 20.3 The publish job

The offline check verifies the whole chain against whichever
recorded base the manifest names and rejects any other. The check
compares a chain that adds, edits or deletes a file under
`.github/workflows/` with the pull request itself: it fetches the
recorded base and published head from the bot fork, anonymously,
and rejects a round that touches a workflow file the diff between
them leaves alone, naming the file, whatever `allow_workflow_edits`
says. One exception covers commits the round carries over, such as
an edit and its revert: a file outside that diff passes when the
chain leaves it as its own base had it and the pull request's
published commits already made every change the chain makes to it,
in the same order and no more often, read along the first-parent
line for at most 64 commits. The check reads a change as the file's
mode and blob before and after it, from each commit's raw diff with
no pathspec, so a name holding a glob character covers that file
alone. It replays the published changes onto the chain's base, each
merged three ways as a rebase would and stopping at the first that
does not apply, and requires the chain's changes to equal those:
a rebase over an upstream edit elsewhere in the file still
matches, while an edit moved to another line, rewritten or repeated
does not. The base is
the pull request's own, not the
rebase commit, so upstream workflow changes a rebase brings in
neither widen nor narrow what the round may touch. A chain that
touches no workflow file fetches nothing, and one that does reads
the pull request's diff once for all its paths. The check also
types each
reply and binds it to a thread the selection carried, defusing
mentions and bounding the text; requires the round summary; and
warns of a thread left unanswered, which stays open for a human. It
records a rebase, and the description's round line says so, and
says when the round set out to resolve a conflict the selection saw;
the probe runs after the check writes that line, so a conflict that
arises during the round reaches the comment, the report and the
next selection, not the description. That
line, and the round's issue comment, also name the failing checks
the selection carried, escaped and bounded as agent text is, since
a check's name comes from the code under review, and, when the
selection's `checks_unsettled` is a count from one to 1,100, say `N
check(s) still running at selection`; any other value says nothing.
A round sent by an
antagonistic hand-back names the review it answers, its round and
its URL, in both places, from the trusted selection fields alone,
never the body, and the check refuses an entry whose URL is not the
one built from the entry's repository, pull request number and
review id; the prompt asks the session for a section headed
`Antagonistic review` in its round summary, and on a proposal in its
pull request body too, answering each finding the body alone holds,
fixed or not and why. The check warns when the round summary
lacks one. Where other rounds cut the summary to 2,000 characters,
the check rejects a hand-back round whose summary, mentions defused,
runs over that bound: a handoff answers in its summary alone, and a
published round's footer records the review as answered. The review
gate (§8) still waits for Copilot alone, not for CI. A
manifest outcome of `handoff`, open to a revision alone, says the
agent changed nothing because it disputes every remaining finding or
needs a human's decision: the check reads its replies the same way,
none may resolve a thread, the publisher replays no chain, posts the
replies, leaves the threads open, comments the handoff on the issue
with the agent's reason and round summary, and skips the review
gate, since the head did not move. The handoff has no swap to refuse
a moved head, so `respond`, which holds the pull request token where
the push step holds none for a handoff, reads the pull request's
state and head before any reply goes up: a pull request closed or
merged since selection is a rejection with no reply, as on the push;
a head that moved during the session is a rejection, since the
replies answer code the agent did not see, and a superseded round
(§20.1); and a state or head it
cannot read is a failure, never a handoff taken on trust. The
next scan leaves the pull
request alone until a maintainer comments, on the issue, the pull
request or one of the disputed threads.

A single-commit revision whose subject changed sets the pull
request's title before the push, with the pull request token minted
ahead of it: the Semantic Pull Request check runs on the push and
compares the title with the subject, so the title must lead
(organisation `AGENTS.md` §10.1). It reads the pull request and the
bot branch first, since the pull request's head can lag a branch
deleted or moved since, and writes when the pull request stays open
and it and the branch both head at the selected commit, or at this
run's own published chain on a retry. A
pull request closed or merged since selection, a head another
session moved, or a branch that reads as gone keeps its title, as
the push will refuse that round, and none of them fails the step.
A push that then
fails with the branch unchanged gives the old title back, with the
same token, while the head and title are still this attempt's, and
after a retitle whose reply GitHub lost, since the record of the old
title goes down before the write; a
publication keeps the new title and an unknown swap outcome restores
nothing, since the branch may hold the new chain. Either title write
reads the pull request back once after a transient `5xx`: the title
in place counts as set, and anything else fails the step, with the
PATCH never sent again. A resend could replace a title a maintainer
or another session set meanwhile, while a title that did not land
costs nothing lasting: a later round or a maintainer sets it.
Neither step writes a result, so the log alone notes it. The push reads
the pull request's state and the bot branch before it writes to the
branch. A pull request closed or merged since selection is a
rejection, found before the publisher touches the fork: the round
stands down with no push, replies or review gate, and its issue
comment says so, as for any rejection. The push then syncs the
fork, replays
the chain onto a temporary branch
(`code-monkey/issue-<n>-r<round>-<session>`, the session being the
run id and select attempt, so two named dispatches revising one pull
request at one round, within one second even, neither resume nor
delete each other's branch, and a full rerun owns a branch of its own)
through `createCommitOnBranch`,
which signs every commit, then moves `code-monkey/issue-<n>` to the new
head with `updateRefs`, `beforeOid` set to the recorded head and
`force: true`: GitHub refuses the move when the branch no longer
holds that head, and the publisher records a rejection rather than
force over work the run did not see. Before it replays, the publisher
asks of the branch the proof the new-issue path asks of an existing
one, the verified chain on the round's base, and adopts the branch
that passes: a rerun after a swap that succeeded and a later step
that failed would otherwise replay against a `beforeOid` the swap
itself retired and refuse the run's own publication. The publisher
reads a branch that fails the proof once more before any write: a
head other than the recorded one, or a branch that reads as gone (a
404), is a rejection with no replay, temporary branch or swap, and
marks the round superseded (§20.1).
GitHub has answered a stale `beforeOid` with a generic error the
compare-and-swap refusal below cannot recognise, so this read, not
the swap, settles a move made before it. A 404 in the adoption proof
likewise means no chain, never a failure, while any other read error
still fails the publication. When the swap
answers with an
error the publisher reads the branch itself, since GitHub may apply
the move and then lose the reply: at the new tip the move stands,
with a warning; at the recorded head the move did not happen and the
failure says the branch holds its old head; elsewhere, the
compare-and-swap refusal, and that message alone, is the rejection,
superseded like the read before the swap,
while any other error there, or no answer, is an outcome the result
names unknown and never calls unchanged, so a human reads the fork
before the next round. After the read above, a move between that
read and the swap is the one way to end there. The publisher reads
the temporary branch's
own creation the same way when its reply is an error or the branch
already exists: a branch at the base belongs to this attempt and the
replay goes on; one carrying the round's verified chain holds an
earlier attempt's finished replay, adopted; one holding anything else
belongs to nobody the publisher may delete for, so the result names
it and the move fails. The
temporary branch goes
whatever happens, and one the cleanup could not remove is a warning
in the result whatever the swap's outcome. A `branches` run moves
nothing: a revision publishes in `pull-requests` mode alone, where
the replies and the review gate follow. The fork mint is the
existing one; no new permission.

With the pull request token, `respond` first reads the pull request's
head: the compare-and-swap guarded the branch move alone, and another
run may have advanced the pull request since, so a head other than
the one this round published, or one it cannot read, leaves every
write below undone with a warning and the publication standing.
GitHub's pull request object lags the ref update, so a read that
still names the head the round began from is a stale read, not a
move: `respond` reads again after 2, 4 and 8 seconds, and warns, naming
the round's tip and the previous head, when the read never
catches up. Any other head skips at once. It
then posts each reply on its thread
prefixed with the AI disclosure and ending in a marker that names
the round and the session,
resolves the threads the agent
marked fixed, and replaces the title and body with the composed
ones, keeping the publisher's header. One refused write is a warning
and the rest proceed, as is a thread grown too long for the bounded
read. A rerun reads each thread first and posts no
reply whose marker a bot comment there already ends with, so a
failure after the replies notifies no reviewer twice, while a later
session at the same round number, one whose predecessor left no
footer, posts its own; the resolve
still runs, since one lost after the reply is its own retry.
A transient `5xx` on a reply reads the thread for its marker once
and never posts again; the resolve runs once with no read-back; the
description edit reads the pull request back once, counts the title
and body in place as written, and otherwise ends with a warning and
never sends again, since a later round may have written its own
description meanwhile and a resend would replace it with this older
one. Each write recovered this way adds a warning to the result.
and the rest proceed. The §8 review gate then requests and awaits
the review of the new head, and the issue comment names the round,
the pull request and the head the push left, since later rounds
replace the same branch, the summary and the verdict, with the round
in its marker, which is how the next selection counts rounds. Every
round whose entry held an antagonistic hand-back, whatever its
verdict, records it in its marker as `handback=<review id>`, which
anchors the segment (§20.1). A proposal or handoff that consumed the
hand-back then ends its marker with `antagonistic=<review id>`, the
receiving-side key `lfreleng-actions/github-code-review-bot` issue 31
will use for an idempotent hand-back; a failed round never writes
it. A round that lost a race with another run's push (§20.1) ends
its marker with `stale=1`, after every other field, and its comment
says the round does not count toward the round cap, with no cap
notice unless it spent the ceiling's last round or was the tenth
superseded round, when the comment says a human must step in. A
counted round other than a clean proposal says the same once the
recount finds ten superseded rounds. The
round history reads every footer form, with any of these fields or
without them. The summary and the reasons are agent text: flattened
to one line, mentions defused and HTML escaped, so they can neither
hide the verdict and
the marker after them nor collapse them into a block. At the cap,
ten rounds in the segment or the run's `max_rounds` (§20.1), every
outcome but a clean proposal says the cap is that number of rounds
per antagonistic review round,
that a human must take over until the next antagonistic review
hands the pull request back, and promises no further round
otherwise, since the selector skips the pull request until then, a
handoff's "once a maintainer comments" included; at the sixty-round
ceiling it says a human must take over, with no exception. The run
report's capped details and the pull request's disclosure of how
the workflow answers findings say the same.

### 20.4 What this changes about trust

Nothing in the author job changes: it still holds no write
credential and never the App key. The publisher gains no permission:
replies, resolves and the description edit are writes to the
target's pull requests, which the pull request token already
covered, and the branch move is a write to the fork, which the fork
token already covered. The select job's read mint gains Checks:
read, for the head's failing checks; no mint gains Actions, so no
trusted job reads a job log. The antagonistic review source reads
reviews and threads the read token already covered, and changes no
permission. The compare-and-swap is the new guard: the
publisher moves an existing branch to the chain it verified alone,
and from the head the trusted selection recorded alone. The agent's
replies are text from outside, defused and bounded like its pull
request body, and a thread the agent disputes stays unresolved for a
human, as the guidance requires.

Open points, listed in §16: a comment at the round cap from the
select job, and rounds within one run.

### 20.5 Requesting the antagonistic review

With `request_antagonistic_review` on, the default, the workflow
asks the review bot for its antagonistic review (§20.1) of each pull
request it published this run whose Copilot review came back clean,
rather than wait for the review bot's own schedule. The rule
nominates a live `proposed` result with a pull request when
Copilot's review is `clean` and judged the published head, the
probe did not find a conflict, the head has no known failing check
and its checks are `passing`, `infrastructure`, `running` or `none`
(never `unknown`; the mode does not wait for CI, and a re-run of a
job the egress agent failed is no reason to withhold it), and the
round is below the sixty-round ceiling. The rule
nominates a clean head at a segment's cap: the next hand-back opens
a new segment. `scripts/antagonistic_request.py` holds the rule.

Three jobs share the work. The publish job's offline
`Record antagonistic review request` step writes the result's
`antagonistic` record (§18.4) for the comment, with no token. The
report job recomputes the rule from every result, ignoring the
recorded state, over the URLs `report_outputs.py` already accepted,
and emits the nominees as `owner/repo#N`. The `antagonistic` job,
skipped in a dry run, outside `pull-requests` mode, with the switch
off, `antagonistic_repository` empty or no nominee, re-validates each
nominee against the run's `org`, refuses the whole list on one bad
entry, and runs `gh workflow run code-review-antagonistic.yaml
-f dry_run=false -f pull_requests=<nominee>` once per pull request
with the token of §10.5. A failed dispatch is a warning naming the
pull request and the job goes on; its `requested` output feeds
`antagonistic_pull_requests` (§18.6).

**Trust.** Nothing authenticates a result yet (§18.4), so a forged
result can nominate another bot pull request in the organisation.
The other side bounds the cost: the review bot's selection re-checks
the bot authorship, the clean Copilot review of the current head,
its own round cap and whether it already reviewed that head, so a
forged nomination at most spends one skipped selection. The request
never counts as a hand-back; the review bot's marker does
(§20.1).

### 20.6 Continuation

A run performs one round per pull request and ends (§2). The
publish job already knows whether the round left work outstanding,
so the scheduled caller starts the next round itself rather than
leave it to the next `revise_only` slot or a person. The next round is a
new run, with a fresh select job that re-reads everything from
GitHub, a fresh session and the same trusted publish.

**The rule.** `scripts/continuation.py` continues a live `proposed`
result whose `pull_request_url` the report accepted (§18.6) when at
least one of these holds: Copilot's review has `findings` and judged
the published head; the head has defect check failures
(`review_outcome.checks_failed`, so failures at the egress agent
alone do not continue); or `mergeable` is `false`. It does not
continue any other verdict (`abstain`, `handoff`, `rejected` and the
failures fall back to the schedule), a result with no `comment_url`,
since the round history counts rounds by their outcome comments and
a round with none would leave the next one the same number, so no
cap would advance (the result's word alone proves nothing, so the
continuation's select job checks the comment itself; see
`continues_run` below), a review still `pending`, which
the next `revise_only` scan picks up, a result whose antagonistic
review the run requested, since the review bot's hand-back dispatches that round
and a continuation would race it, or a round at a cap
(`round_caps.at_cap`: the segment's cap or the sixty-round ceiling).
The report emits the continuing pull requests as `owner/repo#N`,
validated and deduplicated as the antagonistic nominees are, in the
`continue_pull_requests` output (§18.6) of a live `pull-requests`
run: the report job gives `[]` in any other, since no other mode
moves a pull request's head.

**The job.** The cron caller's `continue` job needs the whole
`code-monkey` call and runs when that output is neither empty nor
`[]`, the run is not a dry dispatch, its mode is `pull-requests`
(a schedule's, or the form's choice), and the repository variable
`CODE_MONKEY_CONTINUE` is not `false`. Every continuation is a
`pull-requests` run, so the parent's own mode, a trusted input, must
be too: a forged output of a `branches` run must not move a live
pull request's head. It gates on the output, not
on the call's result, so one entry's failed publish does not stop
another's round. It loads the allow-list and hardens the runner in
block mode, checks out nothing and reads no artifact, re-validates
the list against the organisation (one bad entry refuses it all),
and runs once per pull request:

```bash
gh workflow run code-monkey-cron.yaml --repo "$GITHUB_REPOSITORY" \
  -f dry_run=false -f mode=pull-requests -f max_pull_requests=0 \
  -f pull_requests="$pull" -f outstanding_only=true \
  -f continues_run="$PARENT_RUN" \
  -f model="$MODEL" -f max_runtime_minutes="$RUNTIME" \
  -f max_rounds="$ROUNDS" -f allow_subagents="$SUBAGENTS" \
  -f request_review="$REVIEW" \
  -f request_antagonistic_review="$ANTAGONISTIC" \
  -f load_repo_skills="$SKILLS" -f include_dotgithub="$DOTGITHUB"
```

naming its own run as `continues_run` and
forwarding the run's own model, runtime, round cap, sub-agent choice,
review switches, `load_repo_skills` and `include_dotgithub`, the form
defaults on a schedule, so a dispatch that turned Copilot's or the
antagonistic review off keeps it off for every round it continues,
and one that trusted checkout skills or the `.github` repository
keeps that trust. One pull request per
dispatch keeps a failure to that pull request, and each loop at its
own pace: a continuation continues as soon as its own round ends,
not when the slowest round of a shared run does. The cost is that
sibling continuations select one after another under the overlap
check's tie-break wait (§6 "Overlapping runs"), at most five minutes
in all. A failed dispatch is a `::warning::` naming the pull request,
and the job succeeds; the step summary lists what it dispatched.

**`outstanding_only`.** A continuation names its pull request, but
the name comes from a result the report cannot authenticate, not
from an operator, so it earns none of a named round's waivers.
`outstanding_only` puts it through the scan's tests (§20.1): the
run's repository policy (the exclusions and the `.github` rule), the
author rule and the work-outstanding test, the handoff pause
included, in place of `named by the operator`; the round cap applies
too. A pull request that fails them, its threads
resolved by hand or its checks re-run green after the dispatch, its
repository excluded or its issue's author unapproved, is a named skip
with the scan's reason and costs one select job and no session.

**`continues_run`.** A result's `comment_url` is the result's word,
so a forged result whose round posted no outcome, its publish job
failed, could still name a continuation, and the next round would
repeat the same number with neither cap advancing, as often as
forging and failing repeat. So the continuation names its parent run, and
its select job skips each pull request unless the bot's own footers
on the issue, scoped to that pull request as the round history
scopes them, carry a published round from that run: a `proposed`
footer, not stale, whose run URL is
`<server>/<this repository>/actions/runs/<continues_run>`. The
publisher alone posts that footer, so every continued round follows
a counted one.

**The credential.** The job's own `GITHUB_TOKEN` with `actions:
write` on this repository and nothing else. GitHub's rule that a
workflow token's events start no run exempts `workflow_dispatch`, so
the dispatch starts a run with no stored secret. `workflow_run`
could not serve, since GitHub stops its chains at three levels.

**Never held by its parent.** The continuation's select job runs the
overlap check about a minute after the dispatch, while the parent
run may still be `in_progress`. The parent is past its report by
construction, since `continue` needs the whole call, so the check
releases it (§6 "Overlapping runs") and the continuation is never
held by the run that dispatched it.

**Termination.** No timer bounds the loop; counts do. Ten rounds per
segment and sixty in all (§20.1), three antagonistic rounds on the
review bot's side, a `handoff` pausing until a maintainer comments,
and the rule continuing published rounds alone, checked against the
publisher's own footer, so a run that fails never dispatches itself,
whatever its results read, and an outage cannot loop. Setting
`CODE_MONKEY_CONTINUE` to `false` stops the loop without a commit;
disabling the workflow stops everything.

**Trust.** Results are not yet authenticated (§18.4), so a forged
result could name another bot pull request in the organisation. The
dispatched run's select job re-reads everything from GitHub and,
with `outstanding_only`, works no pull request the scan itself would
not, under the same repository policy, author rule and
work-outstanding test, nor one without the parent run's own counted
round on it. A forged entry costs one select job, the bound
§20.5 accepts, or brings forward a round the next scan would run
anyway; a forged list from a dry or non-`pull-requests` run
dispatches nothing. The change
leaves the author job alone, and the publish job gains nothing.
