# code-factory

A software factory for Claude Code. Give it a Jira issue or a plain task; get
back pushed branches, ready for pull requests, that agents specified, built,
tested (logic, UI and security) and reviewed. A human always opens or reviews
the pull requests and merges.

```
/code-factory:factory https://your-site.atlassian.net/browse/PROJ-123
/code-factory:factory task "Add CSV export to the invoice list"
```

It assumes nothing about your project: no stack, framework or human language
is built in. `/code-factory:factory init` learns each project (code, history, style, team
rules), or interviews you and scaffolds a new one.

**One factory per project, with one or more repositories.** A project is
whatever one team ships together: a backend, a web app and a mobile app, say,
or a single repository. Its factory lists them all in `factory.json`, each with
its own checks, knowledge and way to run. One issue is one job, whichever
repositories it needs: intake names them, the spec gives each its part and the
contract they share, and each changed repository gets its own branch and pull
request, linked to the others.

**The factory never lives inside a project.** Each project gets its own
factory repository (for example `shop-factory`, private), which holds the
settings, checks, knowledge and lessons. The project's repositories receive
nothing but the job branches and pull requests, and your own checkouts of them
are never touched: every job works in its own clones, deleted as soon as its
branches are pushed. Reading the code for init, audits or knowledge updates
uses short-lived clones that are deleted afterwards, so no project code stays
on the machine between jobs.

## How a job runs

| # | Step | Who | Writes |
|---|---|---|---|
| 1 | Read the issue (or task) into the job folder. With one repository: ask you which branch to take the code from, clone it for this job and create branch `factory/KEY-…` | you + fx | `issue.md` |
| 2 | Intake: clear enough? risk? user-visible? With several repositories, also which ones the issue needs to change (`REPOS:`); then you are asked each one's branch to start from, and fx clones them, all on the same branch name | agent, you + fx | `intake.md` |
| 3 | Missing information → multiple-choice questions to you, one at a time, each explained first | you | `qa-log.md` |
| 4 | Spec: criteria, **one related test-data scenario** with cases per criterion, security requirements (OWASP ASVS) and abuse cases, UI check plan; with 2 or more repositories, a part per repository and the contract they share | agent | `spec.md` |
| 4b | Plan: one builder, or packages built in parallel that never share a file (a contract first) | agent | `plan.md` |
| 5 | Build in the project's style; authorization and data-isolation tests **written first**; Playwright UI check for user-visible changes | agent | code, tests, `ui/check.js`, `build.md` |
| 6 | Gate, per changed repository: its checks, path rules and scanners; the apps the job needs, started in dependency order with the scenario seeded; e2e and the Playwright UI check (screenshots, console/network errors, accessibility) | fx | `round-N/checks.md`, `ui.md`, `security.md` |
| 7 | Parallel reviews of the whole change: security (ASVS, verified by a second agent), code (tests vs spec, style), UX and UI with Playwright, and with 2 or more repositories the fit checker (do they work with each other?) | agents | `round-N/review-*.md` |
| 8 | Changes requested → back to 5 (max 3 rounds) | | |
| 9 | Approver: APPROVE, or ESCALATE with what a human must look at, in plain words first | agent | `decision.md` |
| 10 | Ask you which branch each pull request goes into; sync with it and scan for secrets in every repository first, then push each branch, open the PRs when `pr.create` is on (draft if incomplete), link them to each other, delete the clones | you + fx | `pr-body.md` (ready to paste into the PR; it opens with a summary for people) |
| 11 | You review and merge, or send it back with `rework` | you | |
| 12 | Learn: propose lessons and knowledge updates from what happened; you decide on each one | agents + you | `lessons.md`, `knowledge/` |

Jira is updated along the way (start status, comments, review status, PR
link), in the way chosen in init (see **Jira**). Local tasks skip Jira.

## How the factory explains

Whenever the factory needs you to decide something, in a job or in any other command, it
explains first and asks in the same message. One decision per question, never a batch.

- **The easy version first:** what the user of the app does and sees, why it matters, and a
  little of the code in plain words, without function names, line numbers or ids, and no
  analogies unless you ask.
- **Then the technical version:** the code with `path:line`, and the gate and review results.
- Both say what is uncertain and what ran: "not measured", "inferred from code", which checks
  were skipped. Both are short but complete, and more detailed when you ask.
- **The question** is in plain words too, since the question box may cover the chat: what
  happens in the app and what has to be decided, with no internal codes. Each option says what
  happens if you choose it, including who has more to do and what stays broken. The
  recommended option comes first, with its reason.

That is how you get a job's questions (one at a time), a spec that differs from `logic/`, a gate
or review that stops a job or needs a human, the approver's ESCALATE and the final report,
lessons (one change at a time), updates to `logic/` and `knowledge/` (one job or file at a
time) and audit findings to fix (one at a time). Questions that only ask for information,
such as which branch each repository starts from or which tools to install, may still come
together. The agents write both versions themselves: each question in
`questions.md` has an `Easy:` and a `Technical:` line, and the approver's `## For the human`
has an easy part and a technical part.

What the factory posts for people to read or act on comes in two parts as well: the comment
when intake turns an issue down, the comment on an issue that differs from `logic/`, the
questions posted to Jira, the comment when the pull request is open, and the pull request
description. Each opens with `## Summary`, for people who do not read code (the reporter, PM,
QA): what happens for the user of the app, what has to be done and by whom, and what was not
checked. `## Technical part` follows with what fx writes; where the two differ, the technical
part is the standard. When the factory's `language` is Vietnamese, the headings are
`## Tóm tắt` and `## Phần kỹ thuật`. The orchestrator writes the summary to a file, and fx takes
it with `--summary-file` (on `fx result` for intake and spec, `fx ask-jira` and `fx pr`) and
scrubs it for secrets like the rest. A comment without a summary still goes, and fx's output
says the summary is missing. Short comments (work started, answers recorded) stay as they are.

## Watching the factory

`/code-factory:factory board` opens the factory board, one HTML page for
every factory repository on your machine (`~/.cache/code-factory/board.html`).
It opens on the flow map: one row per job and one column per step, so you see where each job is,
which steps it has passed and who is working on it, and only the row of a job with many agents
grows. A switch next to the filters goes to the office layout.
The office has one room per step a job goes through (intake,
product, engineering, gate, review, security, approval, push), plus your desk
and the knowledge room. Each room shows the jobs in it and, per job, the agents
running now with their model, what they are doing and for how long, the file
they will write, and the packages of a split build or the candidates of a
best-of-N build; a room with more than four jobs lists them one per line.
Free agents sit at the bottom of their room. Your desk holds what waits for you
(answers, the branch to push into) and sessions that seem to have stopped.
You can filter by project, show only what waits for you, or find a job by key.
Below the office, one row per job opens its steps (with several repositories, each one's
branch and pull request), its timeline (parallel
builders side by side), the expected finish (from the typical time of each step
in your earlier jobs), tokens the agents reported (and a rough cost when
`dashboard.usd_per_mtok` is set) and links to its files; then recent activity
and the finished jobs. Click an agent for what it does and its recent runs.
The page reloads itself every few seconds and every fx command rewrites it:
several Claude Code windows on one machine all show on this one page. No
server, nothing to install.

## Several builders on one ticket

After the spec, a planner decides what finishes the job fastest. A small or
tightly knit job keeps one builder. Otherwise it is split into packages:

- **One owner per file.** Every package lists the files it may change, and no
  file belongs to two packages. A package that changes anything else is not
  merged and goes back to its builder. In a job that changes several
  repositories, each package belongs to one of them.
- **Contract first.** What packages share (routes, API shapes, migrations,
  types, translation keys) is a contract package built first, then frozen;
  the others build against it.
- **Own clone, branch, port and database** for every package, so builders,
  their tests and their app never meet. They run in the background and each
  is merged as soon as it is done; packages waiting for it start right away.
  There is no limit on how many run at once unless you set
  `split.max_builders`.
- **Change requests.** A builder that needs a file it does not own, or a
  different contract, stops and asks; the planner reassigns files or reopens
  the contract, and the work goes on.
- **Integrator.** When everything is merged, one builder checks the whole,
  fixes only what joins the packages, updates the logic and writes the report.
  Gate and reviews then run as usual; each finding goes back to the package
  that owns its file, the rest to the integrator.

## What blocks a job

- A changed repository's checks script, `checks/<repo>.sh` (your lint, type check, tests,
  build), failing. The gate goes through the changed repositories in merge order and stops at
  the first that fails; `checks.md` says which.
- A user-visible change without a Playwright UI check, or a UI check with a
  failed step, a browser console error, a server error, or an accessibility
  violation at a blocking level (default: critical and serious).
- A secret in a commit (checked every round, and again right before the push:
  the factory never pushes a branch with a secret in its history); a
  vulnerability the job introduced at or above the blocking severity (default:
  medium) found by gitleaks, osv-scanner, semgrep, the header check or OWASP ZAP.
  Lower severities and pre-existing findings go to the review, and
  `fx audit-scan` lists the pre-existing ones. Header and ZAP findings block
  only after `fx selftest` has recorded the base branch's baseline, and that
  baseline must come from the same scans (fx asks for a new one otherwise). A ZAP
  finding the base already has elsewhere still counts as new on a new page, unless
  it is about what the whole site lacks, such as a missing header; a vulnerable JS library
  counts by name and version, whichever bundle holds it. A
  dependency vulnerability nobody has rated yet counts as high. osv-scanner reads
  what is committed, so neither a `.gitignore` nor an install changes what it sees,
  and a job that leaves a manifest with dependencies without a lockfile (by deleting
  it, or adding a manifest) is blocked as `osv-no-lockfile`. A package a job adds to a
  `package.json` or `composer.json` that its own lockfile does not list is blocked as
  `osv-lock-stale`; this compares what is asked for (the package, what an alias points to, a
  git dependency's repository), not versions, and does not cover `pyproject.toml`, `Gemfile`
  or `Cargo.toml`, peers or local folders. A known-vulnerable
  dependency counts per lockfile: on the base in `tools/` and added in `api/`, it is new in
  `api/`, and moving a lockfile makes its old findings new. A rule a human
  has accepted in `security.accepted_rules`, as `{"rule": ..., "path": "<glob>",
  "reason": ...}` (`*` stays in one folder, `**` crosses folders; for ZAP and the header
  check, the path on the site), stops blocking there and stays in the report; secrets and
  suppressions cannot be accepted. The older `{rule: reason}` still reads, but covers
  every file, also in later jobs, and `fx doctor` says so. Semgrep skips what
  `security.semgrep_exclude` names (default `node_modules`, `vendor`, `dist`, `build`
  and minified files), except files the job itself adds or changes there, which are scanned
  anyway, at any size (a name without a slash matches at any depth, one with a slash from
  the top).
  Findings below high in test code are reported without blocking, and when the file only
  sits in a test folder without a test's own name, such as `tests/helpers.py`, a human must
  look.
- A scanner suppression the job adds (`nosemgrep`, `nosem`, `gitleaks:allow`,
  entries in `.gitleaksignore`, `.semgrepignore` or `osv-scanner.toml`, a
  changed `.gitleaks.toml`), unless `security.allow_suppressions` is true. The
  scans never read those files from the job's clone (the builder writes it):
  gitleaks runs with its built-in rules or `security.gitleaks_config` from the
  factory repository, semgrep with `--disable-nosem`, osv-scanner with its own
  config. Changing those files is always a sensitive path.
- The gate's own settings differing from what is committed and pushed: `factory.json`,
  every checks script and scanner rules are compared by content, whatever git has been told
  about them (`assume-unchanged`, `skip-worktree`, a filter), and commits only on this
  machine that change them must be pushed first. A job's clone or the factory repository
  with `refs/replace` or a grafts file, or a shallow job clone, which show git another
  history; a clone whose HEAD is not the job's branch. The base branch each repository is
  measured from is read from that repository's own remote at every gate, never from the clone,
  so the gate needs to reach it. The gate also fails when something fx did not start already
  answers on one of the job's ports, since the checks of the running apps would read that.
- With baselines on S3: an image that cannot be fetched, a list with an unsafe
  path, a list that is gone from a branch that had one or from the branch it goes into, or a committed list naming
  a hash S3 does not have (or that fx cannot check, because S3 or the read key is
  unavailable).
- A required scanner (`security.required`, default gitleaks, osv and semgrep)
  that is missing or fails. A manifest already without a lockfile counts as osv
  failing when it leaves osv nothing to read: commit the lockfile, or take `osv` out
  of `security.required` to accept that. Manifests that declare no dependency, and
  those under `docs/`, `examples/`, test code or vendored folders, do not count.
  osv-scanner must be v2 or newer. `fx doctor` and `fx selftest` try semgrep and osv-scanner for real, so you learn
  this before the first gate: no network for semgrep's rules means pointing
  `security.semgrep_config` at local rules.
- A security finding that survives an independent verifier agent, which must
  answer every blocking finding (one `confirmed | rejected | unsure` line each).
  A security review that asks for changes without numbered `N. [blocking]`
  findings goes back to the reviewer before any verifier runs.
- Missing test cases from the spec, or test data that does not reflect real
  relations; clear departures from the project's code style.
- Touching existing tests, frozen paths (`.claude/`, `AGENTS.md`, `CLAUDE.md`)
  or sensitive paths (the last always needs a human).

Scanners and Playwright are installed for the factory itself (`/code-factory:factory
install`), never into your project, at pinned versions where fx picks the version
(Playwright 1.64.1 unless the machine has a global Playwright to match, axe-core
4.10.3, semgrep 1.100.0 when installed with pip or pipx); Homebrew and release
downloads take the tool's current release. A missing
tool, or a scanner that fails to run or runs out of time, is listed as skipped in the gate and in
the pull request, never counted as a clean result: a required one fails the
gate, any other hands the pull request to a human. The exception is ZAP when
Docker is missing or not running: unless `security.required` names it, that is
only listed as skipped, so a laptop with Docker off does not send every job to a
human (a ZAP run that fails with Docker up still does). A `factory.json` without a
`security` block scans with the template's defaults (all scanners on); only an
explicit `false` turns a scanner off, and the gate lists it as skipped. Pin
`security.zap_image` as `name@sha256:<digest>`; a moving tag is noted in the
gate. Either way the gate runs the image `fx selftest` used, by its id: a tag that moves
afterwards changes nothing until the next selftest, and with that image gone ZAP counts
as not run, so the job goes to a human. ZAP runs as your user and reaches the app through `host.docker.internal`
(on Linux via `--add-host host.docker.internal:host-gateway`; the host network
only when the app listens on 127.0.0.1 alone, with a note).

Each changed repository is scanned in its own clone, with its own
`security.semgrep_exclude`, `security.accepted_rules` and base-branch baseline (a repository's
entry in `factory.json` can set them; otherwise the project's apply). The header check and ZAP
run against each started app of a changed repository. With 2 or more repositories,
`checks.md`, `ui.md` and `security.md` have one `## Repo: <name>` section each.

### What the security scans can and cannot see

`fx doctor` says, in the row `what the security gate leaves out`, which of these
apply to your factory.

| To see | Set | Note |
|---|---|---|
| Attacks on the running app: SQL injection, XSS and the like | `security.zap_mode: "full"` | Off by default: ZAP writes junk through every form it finds, so `app.up` must give each job a throw-away database. Stops after `security.zap_minutes`. |
| Pages behind the login | `security.zap_auth.command` | Prints the header's value for a test account once the app runs. The value goes only to the ZAP container, which sends it only to the app's host, and is taken out of its reports. |
| API routes no page links to | `security.zap_openapi` | Where the app serves its OpenAPI description, such as `/openapi.json`. |
| What is already in the code and its history | `fx audit-scan` | Every scanner over a whole branch. One task file per rule to fix, ready for `fx new --local`. |

Run `fx selftest` again after changing `zap_mode`, `zap_openapi`, `zap_auth` or the ZAP
image, including after pulling a newer `:stable`: other scans find other old problems.
Until then the gate and `fx doctor` say so, and header or ZAP findings do not block
because of what the base branch already has.

Scanners cannot judge business rules, such as whether one customer can read
another's order: the security reviewer and its verifier look for those in each
change, and `/code-factory:factory audit` can add a deeper audit by agents. Vulnerabilities in
the operating system packages of a container image are not checked.

The gate catches mistakes and makes going around it hard, but it is not a wall. Agents and
project code run as your user, with a shell, on the same machine, and fx has nowhere to keep
a record they cannot touch: they can edit a job's `job.json`, write a finding into the
git-ignored `baseline.json`, read the S3 keys or delete the ZAP image. So a green gate is not
proof that nothing went around it. What stops a bad change is that a human reads and merges
every pull request. If you need a line no agent can cross, have your host's CI run the scanners
again as a required check on the pull request.

Project code never sees fx's credentials: setup, the checks scripts, the apps, e2e,
`seed.sh`, `ui/check.js` and the scanners run without `JIRA_*`, `GH_TOKEN`,
`GITHUB_TOKEN`, `GITLAB_TOKEN`, `GLAB_*`, `CI_JOB_TOKEN`, model API keys and the
like (`security.strip_env` adds names, `security.pass_env` lets one through), and
the files in this machine's keys folder (the S3 keys and `jira.env`) are read only
inside fx's own process, never put in any environment.
fx never commits the `copy_into_worktree` files or `.env*` files when it saves
left-over work (names ending in `.example`, `.sample`, `.dist` or `.template`,
such as `.env.example`, are ordinary files), and redacts well-known key formats, the
keys in the keys folder (the S3 keys and the token of `jira.env`) and local env secrets (the
values of variables named like a secret, and other values that look generated;
not host names, email addresses or `${OTHER}` references) from the text it posts
to the pull request and Jira.

## A project of several repositories

An issue can need the backend, the web app and the mobile app at once. They all belong to
one factory: `factory.json` lists them under `repos`, in merge order (a backend before the
apps that call it).

```json
"project": { "name": "shop" },
"repos": [
  { "name": "backend", "url": "git@github.com:team/shop-api.git", "base_branch": "main",
    "setup": "composer install",
    "app": { "up": "php artisan serve --port {port}", "health": "/up", "needs": [], "env": {} } },
  { "name": "web", "url": "git@github.com:team/shop-web.git", "base_branch": "develop",
    "app": { "up": "npm run dev -- --port {port}", "needs": ["backend"],
             "env": { "VITE_API_URL": "{url:backend}" } },
    "ui_paths": ["src/**"] }
]
```

- **Names** are lower case letters, digits and `-`, start with a letter, are unique, and
  never `repos`; a name may not end in `-c<digits>` or hold `--`, the shapes of best-of-N
  candidates' and split packages' clones. No two repositories share a URL, and the
  factory's own is refused.
- **Settings per repository, with the project's as default.** `name`, `url`, `base_branch`,
  `app.needs` and `app.env` belong to a repository only. Other settings, such as `setup`,
  `app`, `e2e`, `copy_into_worktree`, the `*_paths`, `security.semgrep_exclude`,
  `security.accepted_rules`, `ui_checks`, `pr.provider` and `baselines`, may be set in a
  repository's entry; one it does not set comes from the top level of `factory.json`. A
  repository's value replaces the project's whole value: lists are not merged. Jira,
  storage, models, reviewers, rounds and learning stay project-wide. The template's
  `_doc_repos` lists every key.
- **Checks.** Each repository has its own checks script, `checks/<repo>.sh`, run in that
  repository's clone. Everything under `checks/` (a helper the scripts source too) must be
  committed and pushed before a gate runs.
- **Local files.** A repository's own untracked files (`copy_into_worktree`, such as
  `.env`) go in `local/<repo>/` of the factory repository; fx looks there first, then in
  `copy_from` or `local/`. `fx doctor` warns when two repositories copy the same file.
- **Knowledge.** The shared files (`README.md` as the index, `overview.md`, `domain.md`,
  `rules.md`, `security.md`, `conventions.md`, `definition-of-done.md`) are at the top of
  `knowledge/`; each repository's own (`README.md`, `architecture.md`, `style.md`,
  `stack.md`, `api.md`, `ui.md`, `testing.md`, `environment.md`) are in `knowledge/<repo>/`.
  Each agent gets the shared files for its role and the files of the job's repositories
  (intake and the spec writer: of every repository). Knowledge and logic cite code as
  `repo:path` or `repo:path:lines`; a project of one repository may keep a plain `path`.
  `logic/` is one set for the whole project.

**Apps start as the job needs them.** The job's apps are those of the repositories it
changes that have an `app.up`, plus every app they need (`app.needs`, followed all the
way). fx starts them in dependency order, each on its own port with its own database: a
repository the job changes runs from the job's clone, any other from a checkout of its base
branch. Each app gets `FACTORY_URL_<NAME>` (upper case, `-` as `_`) for every app started
before it and for itself, and all of them as JSON `{name: URL}` in `FACTORY_APP_URLS`;
`app.env` may use `{url:<repo>}` and `{port}`. `fx app up <job>` prints every URL. ZAP
scans each changed repository's app, running `security.zap_auth.command` in that app's
environment: set `security.zap_auth` in each repository's entry, or log in at the app that
holds the accounts through `$FACTORY_URL_<NAME>`. Each job has a block of ports: one per
repository, then the agents' own, then the best-of-N candidates' apps (`ports.step`
defaults to at least 10; `fx init` raises it when it adds a repository, `fx doctor` fails
when it is smaller than the repositories plus 3, and a new job never takes a block that
overlaps one an unfinished job holds).

**One job per issue.** `/code-factory:factory PROJ-123` starts one job, whatever the
repositories:

1. **Which repositories?** Intake reads the issue, the shared knowledge and every
   repository's, and names the ones the issue needs to change (`REPOS: backend, web`). A
   project of one repository skips this.
2. **Which branch does each start from?** You are asked for each repository. fx then
   clones each in `<factory>-work/<KEY>/<repo>`, every one on the same branch
   `factory/<KEY>-<slug>`. A repository the job only runs is a checkout of its base,
   never changed or pushed.
3. **One spec.** With 2 or more changed repositories, `spec.md` has a `## Repo: <name>`
   part for each and a `## Shared contract` with what they must agree on (routes,
   payloads, events, types, versions, deploy order); each acceptance criterion names its
   repository. The builder keeps every repository to that contract, and when the build
   must change it, it updates the contract in the spec and says so in its report. A split
   build gives each package one repository.
4. **The gate runs per changed repository**, in merge order: uncommitted files,
   protected and frozen paths and its checks script; then one logic check over every
   clone. A changed repository without commits, while another has some, is "not
   changed": its checks and scans are skipped and it gets no pull request; `fx repos <job>
   --drop <repo>` takes it out of the job (run-only when another repository's app needs
   it), refused while its clone holds work not on the remote. A run-only clone with
   changes or commits fails the gate until `fx repos <job> --add <repo>` makes it a
   changed one, keeping that work. e2e runs per repository against its own app (`BASE_URL`); `seed.sh` and
   `ui/check.js` run in the repository whose screens (`ui_paths`) the job changes.
5. **The reviews look at the whole change**, and with 2 or more changed repositories the
   **fit checker** reviews too: it reads every branch side by side against the shared
   contract and follows each connection in the code (calls, fields, roles, errors, deploy
   order), and checks that no part of the issue fell between them. It writes
   `round-N/review-fit.md`, which counts like any other review.
6. **One pull request per changed repository.** `fx pr` takes one target branch for all
   (`--target develop`) or one per repository (`--target backend=main --target
   web=develop`). First, in every repository: a work-in-progress commit when the job is
   incomplete, a sync with the target (its checks run again when that changed anything),
   and a secret scan of the commits to push. A secret in any repository means nothing is
   pushed anywhere. Then each branch is pushed and, when `pr.create` is on, its pull
   request opened or updated with that repository's provider; then every description is rewritten to list the other pull
   requests, in merge order, with that repository's part of the spec and the shared
   contract. A changed repository with no commits gets no pull request. fx pushes to each
   repository's URL in `factory.json`, never to the clone's own `origin`, runs none of the
   clone's git hooks, and names the repository to `gh` and `glab`. Jira gets one
   comment and one transition for the job. Running `fx pr` again after a failure skips
   the repositories already pushed and unchanged; the clones are deleted only when every
   repository is pushed.

**Parent issues.** A parent issue's children are each a job of their own. In mode `token`,
`fx jira children <KEY>` lists them (mode `mcp`: Claude reads them with the connector), and
`/code-factory:factory PROJ-101 PROJ-102` runs them one after another.

A human still reviews and merges each pull request. The learning reads every one of them,
and a job's logic changes are applied once all its pull requests are merged; when some are
merged and the others closed without a merge (state `partial` in `fx logic pending`), you
decide whether to apply them. A rework after one pull request was merged leaves that
repository out: it runs from its base when another app needs it, and its pull request
stays recorded for the learning; with every pull request merged, start a new job.

## Jira

A factory reaches Jira in one of two ways. `init` explains both and asks which, before it
reads any Jira history, and keeps the answer as `jira.mode` in `factory.json`, so the whole
team works the same way. It recommends `mcp` when Claude has the Atlassian connector.
Running init again keeps `mcp` or `token` (changing it is a decision of its own); an older
value (`agent`, `api`, `auto`) or no mode is asked again.

| | `mcp` | `token` |
|---|---|---|
| Who calls Jira | Claude, with its own Atlassian connector; fx only queues the Jira work, even when a token exists | fx itself, with the Jira REST API, for every Jira read and write |
| A parent issue's child issues, finished issues and statuses (`init`) | Claude reads them with the connector | fx reads them (`fx jira children`, `finished`, `statuses`), so no connector is needed |
| To set up | nothing, once the connector is connected | `jira.env` in this machine's keys folder, on each machine |
| Attachments | not downloaded; `issue.md` lists their names | images downloaded, up to 5 MB each (`jira.max_attachment_mb`) |
| What init checks | the connector lists the project's site, with read and write Jira scopes | once you have created `jira.env`, `fx doctor --issue <issue link>` reads that issue |
| `fx doctor` | one row: the connector does the Jira work, which fx cannot check | `jira.base_url` is set, `jira.env` is there and complete; with `--issue`, reading the issue works |

For `token`, create an API token at https://id.atlassian.com/manage-profile/security/api-tokens
and put it in the keys folder yourself, never in the conversation. `fx where` prints the
folder as `keys_folder`: `~/.config/code-factory/keys/<project>-<hash>/`, the same folder as
the S3 keys, outside the factory repository and its clones.

```
# <keys_folder>/jira.env   (the folder chmod 700, the file chmod 600)
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
```

fx reads the file inside its own process and never puts it in any environment. The token is
never in an environment variable, `factory.json` or git: fx does not read `JIRA_EMAIL` or
`JIRA_API_TOKEN` from the environment, and `fx doctor` warns when they are set there, since
any agent's shell can read them. With `token` and the file missing or incomplete, fx stops
with exit code 4 and says where the file goes and what it holds, never printing a value. The
token is redacted like the other local secrets in what fx posts or prints, and a job file
that holds it stays on the machine instead of going to S3; the email is an address, not a
secret. In mode `mcp` fx never opens the file, so `fx doctor` says to delete one left there.
`/code-factory:factory init` keeps agents' Read tool away from the keys folder. `fx install` reports the
mode and whether `jira.env` is there and complete (null in mode `mcp`).

fx sends the token to this factory's Jira site only, `jira.base_url`, which init sets, and
only over https: never to the site of a link to another one, and never along a redirect to
another site (Jira Cloud's attachment links need no token). Without `jira.base_url`, mode
`token` stops with exit code 4.

With `token`, fx does every Jira read the factory needs, so a machine without the connector
runs every command, parent issues and `init` included, and Claude leaves Jira to fx even when it
has the connector: one way per mode. Confluence is not Jira: in both modes Claude reads its
pages with the connector, and without one it skips them and says so.

Older values keep working: `agent` means `mcp`, `api` means `token`, and `auto` or no mode at
all uses the token when `jira.env` holds both values, the connector otherwise. `fx doctor`
then advises choosing `mcp` or `token` with init.

## S3 or this machine only

`init` asks once whether this project's factory uses S3 at all. It recommends S3 when the
team works from several machines or the reference images weigh more than a few MB.

- **No.** Nothing goes to S3. `factory.json` gets no `storage`, `baselines` or `jobs_store`
  block, jobs stay in `jobs/` on this machine (a broken machine loses them), and reference
  images, if the project has any, stay in git. The rest of init does not ask about S3 again.
- **Yes.** Jobs always go there (**Jobs on S3**), and reference images too when you choose so
  (**Baselines on S3**). `fx doctor` fails when `factory.json` has `storage` but no
  `jobs_store`: S3 is set up but jobs are not kept there. Add `jobs_store` with init, or
  remove `storage`.

On yes, init first tells you in one message what to have ready:

- **A bucket** that already exists, is private and is this project's own. fx never creates
  one, and `fx doctor` fails when two factories on this machine share a bucket and prefix.
- **The region**, unless an endpoint is set.
- **An endpoint** only for an S3-compatible service such as MinIO; empty for AWS.
- **A prefix** only when the bucket holds other things.
- **Key files** in this machine's keys folder, which `fx where` prints as `keys_folder`
  (the folder `chmod 700`, the files `chmod 600`), each with `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY` and optionally `AWS_SESSION_TOKEN`:
  - `s3-read.env` (`s3:GetObject`, `s3:ListBucket`) on every machine;
  - `s3-write.env` (`s3:PutObject`) only on machines that upload reference images;
  - `s3-jobs-write.env` (`s3:PutObject` on `<prefix>/jobs/*` only) on machines that run jobs.

  Or AWS profile names in `storage.read_profile`, `storage.write_profile` and
  `storage.jobs_write_profile`, which need the `aws` CLI. Never a key in the conversation:
  you put the files there yourself.
- **No lifecycle rule on `jobs/sha256/` alone.** fx deletes nothing on S3, and old snapshots
  would point at files that are gone. Encryption is the bucket's.

Then init asks for the place, writes `storage` and `jobs_store` together (`auto` on; audit
reports only if you say yes to a question of their own), asks whether reference images go on
S3 or stay in git when the project has them, runs `fx doctor`, and tells you what is missing.
Running init again on a factory that uses S3 says so and keeps it: changing that is a
decision of its own.

The place has one folder per part of the factory:

```
s3://<bucket>/<prefix>/      this project's factory (no prefix when the bucket is only its own)
  jobs/…                     every job's folder (Jobs on S3)
  baselines/…                reference images, when they are kept on S3 (Baselines on S3)
```

```json
"storage": { "type": "s3", "bucket": "", "region": "", "endpoint": "", "prefix": "" }
```

Keys are never in `factory.json` or git. fx signs its requests itself, in its own process:
the keys reach no environment, your own `AWS_*` variables pass through untouched and are not
used, and `/code-factory:factory init` keeps agents' Read tool away from the keys folder. Code running as
your user that goes looking for the file could still read it, like every other local secret,
so keep the write key only on machines that upload images. Keys still in `local/` from an
older version keep working, and `fx doctor` says how to move them. Objects are never
overwritten and never deleted; cleaning up old ones is up to you.

## Baselines on S3

Screenshots to compare against make a repository heavy for good: PNGs barely
compress, and every version stays in the history. With a `baselines` block next to
`storage` in `factory.json`, the images live on S3 by hash, the folder is git-ignored,
and a sorted list of path, sha256 and size in git says which images belong to each
commit. Without it, they are ordinary files in git.

```json
"baselines": { "dir": "qa/ui-qa/baselines", "manifest": "qa/ui-qa/baselines.lock.json" }
```

- `baselines.folder` names its folder in the storage (default `baselines`).
- `repo` is `factory` (default: the list and folder are in the factory repository)
  or the name of the project's repository that holds them, so each of its branches
  carries its own list (`project` still works for a project of one repository);
  commands then need `--job` or `--clone`. That repository's `.gitignore` must ignore
  the folder, which `fx baselines migrate` does on the job's branch.
- Fetching images takes the read key, uploading them the write key
  (see **S3 or this machine only**).

| Command | What it does |
|---|---|
| `fx baselines status` | Files missing, wrong or not on the list; is the repository behind? |
| `fx baselines sync` | Pull if possible, and bring the folder in line with the list |
| `fx baselines record` | Hash the folder and rewrite the list; uploads nothing |
| `fx baselines push` | Upload what S3 lacks and confirm every hash; `--commit` commits the list |
| `fx baselines migrate` | From images in git to S3; `--commit`, with your yes, takes them out of the index in a normal commit. History is not rewritten, so the repository does not shrink |
| `fx baselines diff` | Before and after images of two commits |

There is nothing running in the background: fx checks at the start of `fx gate`,
`fx selftest`, `fx new` and `fx baselines sync`. If the factory repository is behind
and clean, it runs `git pull --ff-only`; with uncommitted changes it stops with exit
code 3 and touches nothing; if the remote cannot be reached it warns and goes on when
the images are complete. A running gate or selftest holds a lock that keeps any pull
away until it ends, so the checks scripts and `factory.json` never change under it: that is
the one safe moment, and a command that finds the lock taken skips the pull and says
so. A job's clones are never pulled; `fx pr` merges each target into its clone.

The gate only fetches what is missing or wrong, checks hash and size before an atomic
rename, and refuses paths that are absolute, contain `..`, or go through a symlink out
of the folder. It never generates a missing image and never skips one it cannot get:
the gate fails and names it. Files that differ from the list without `record` are kept
in the job's folder and restored from the list, and the report says so.

A job that changes images goes: regenerate, `record`, gate (with the local images of
the edited list), `push`, then commit the list (`fx baselines push --job <job> --commit`
does those last two, in that order; `fx pr` stops until it has). A committed list
naming a hash S3 lacks is caught by the gate, `fx doctor` and `fx pr`. Every change to
the list shows in `checks.md`, in the reviewers' prompts and in the pull request, with
the counts and how to see the images before and after: a builder could otherwise
change a baseline to hide a UI bug.

With `repo: factory` the folder is shared by all jobs on a machine, so run one job
that changes baselines at a time.

If `factory.json` loses these blocks, say a script copied an old file over it, `fx doctor`
fails and `fx baselines` says the settings may have been lost, because a list in fx's
format is still in the factory repository.

## Jobs on S3

`jobs/` is not in git, so a broken machine loses every job's spec, reviews, decisions and
history, and the audit reports the next audit compares with. A factory that uses S3 keeps
each folder in its place there too, with the `jobs_store` block that init writes together
with `storage`. Without S3, jobs stay on this machine.

```json
"jobs_store": { "folder": "jobs", "auto": true, "include_audits": false,
                "exclude": ["_audits/*/sandbox/", "**/node_modules/", "**/vendor/", "**/composer-cache/"],
                "max_file_mb": 50 }
```

```
s3://<bucket>/<prefix>/
  baselines/...                                  reference images (Baselines on S3)
  jobs/
    sha256/<2 chars>/<hash>                      a file's content, never overwritten
    snapshots/<job>/<UTC>-<8 chars>.json         what one push saw, never overwritten
```

| Command | What it does |
|---|---|
| `fx jobs push [<job>...] [--all] [--dry-run]` | Scan for secrets, upload what S3 lacks, then write a snapshot |
| `fx jobs status` | Which jobs are not saved or changed since their last push; needs no network or key |
| `fx jobs list` | The jobs on S3 and their snapshots |
| `fx jobs pull <job>... \| --all [--at <UTC>] [--force]` | Get jobs back, the newest snapshot unless you name a time |

With `auto` on, a job goes up after `fx pr` and audits after `fx audit-import`, never at a
gate or a round. A failed upload never fails the step: it is a warning, and `fx status`, the
board and `fx doctor` keep showing the job as not saved.

**On a new machine.** Clone the factory repository, put the read key in this machine's keys
folder (`fx where` prints it as `keys_folder`), then:

```
fx jobs list
fx jobs pull --all
```

Finished jobs work as before with `fx show`, `fx status`, the board and `fx rework`. An
unfinished one keeps its stage and asks for a person: each changed repository's branch is
cloned again if it is on the remote (otherwise `fx new <key> --again`), and each run-only one
at its base. Code
that was never pushed is not in a snapshot, because clones are not in `jobs/`, and an agent of
an earlier session cannot be continued. A restored job's paths, setup and ports are this
machine's. A job already here
is refused, and `--force` moves it to `jobs/_replaced/`.

**What stays behind.** Nothing goes unscanned: without gitleaks, or if it fails, nothing is
pushed, and a gitleaks too old to open archives is told to upgrade. A file gitleaks finds, or that
holds a local secret, an S3 key or the token of `jira.env`, stays on the machine, and so do an archive fx cannot read
(damaged, with a password), excluded folders, symlinks and files over `max_file_mb`. The snapshot
lists each with its path and reason, never its value. `_audits/*/sandbox/` is always left out. If
`job.json` itself has a finding, that job is not pushed at all.

**Reading `held_back`.** `fx jobs push`, `fx jobs status`, `fx status`, `fx pr`, the board and
`fx doctor` all name the files that stayed on this machine, for example
`{"path": "reports/net.log", "reason": "secret: aws-access-token"}`. Those files are on this machine
alone, and a broken machine loses them. The job still reads `saved`, because pushing again changes
nothing. Decide for each one: take the secret out of the file so that it goes up with the next push,
or accept that it stays. gitleaks can be wrong, and a file held back by mistake is still not on S3.
What you excluded yourself is in `skipped` but not in `held_back`. `held_back: null` means an older fx
pushed that job and did not note them: `fx jobs push` finds out and uploads nothing. After a restore,
`not_restored` names what its push left behind, and nothing on the new machine is held back.

**What is looked for, and what is not.** Looked for: gitleaks' rules, inside archives down to 3
levels; the values of your local env files and the token of `jira.env`; S3 keys in key files and the static keys of
`storage.*_profile`; a Laravel `APP_KEY` and Sanctum tokens. Not looked for: a password in a sentence
such as `connecting with password xxx to mariadb`, what is in an image, archives deeper than 3 levels,
a profile's temporary keys (SSO, a role), and values encoded in base64 or a URL. gitleaks is a filter
and no guarantee: a job's reports can still hold sensitive data, so keep the bucket private.

**Audit reports are off by default** (`include_audits`), and `init` asks about them separately:
they describe holes that are not fixed yet, and anyone with the read key could read them.

**Reviews of merge requests stay out.** `fx jobs` does not push, pull or count
`jobs/_reviews/` (see **What lives where**).

**Keys.** Reading uses the read key. Pushing needs a write key on every machine that runs jobs,
while reference images should only be writable from the machines that upload them, and code
running in a job could overwrite them with a shared key, since not overwriting is only the
client's word. So fx looks for `s3-jobs-write.env` or `storage.jobs_write_profile` first, and
the shared write key after that. Give that key `s3:PutObject` on `<prefix>/jobs/*` and nothing
else, and `fx doctor` stops advising it.

**A snapshot is not signed.** A restore checks paths and hashes, and keeps fx's deletes and clones
on this machine's own folders, but not who wrote it: anyone holding a key that writes under
`jobs/` can put words in a job's files. Agents treat them as data, like an issue's text.

**fx never deletes anything on S3.** Do not set a lifecycle rule on `jobs/sha256/` alone, or
old snapshots point at files that are gone. There is no client-side encryption: the bucket's own
is what protects these files. `qa/test-results`, `node_modules` and `baseline.json` are not
kept, as they can be made again.

## Requirements

- Claude Code (terminal or IDE extension). For Jira, Claude's Atlassian connector or a
  Jira API token in `jira.env`, chosen in init (see **Jira**). Local tasks need neither.
  Confluence pages are read only through the connector.
- `git` 2.38+, `python3` 3.9+, Node.js 20+ (UI checks).
- Optional: `gh` (GitHub) or `glab` (GitLab), so learning can read review
  comments on the pull requests, and to open them automatically when
  `pr.create` is true in `factory.json` (by default the factory only pushes
  the branch and a human opens the pull request);
  Homebrew or pip to install the scanners; Docker for ZAP. gitleaks, osv-scanner
  and semgrep are required by default (`security.required`); without gitleaks the
  gate fails, and fx still
  checks the branch for well-known key formats, secret-looking assignments
  (validation rules and short labels next to a word like `password` do not
  count) and local env secrets before every push.

## What a run costs

Measured end to end with real agents (Opus orchestrator) on a small Node.js
app of about 15 files with 7 tests, October 2026:

| Run | Wall time | Cost |
|---|---|---|
| `init` (knowledge, logic, independent review, selftest) | 10 min | $3.4 |
| One back-end + front-end feature: 2 spec questions, planner split into 2 parallel builders + integrator, 4 reviewers, approver, push | 39 min | $9.5 |
| `lessons` after the merge (lesson from a human fix, logic applied, knowledge refreshed) | 5 min | $1.8 |

Larger code bases cost more for `init` (it reads everything) and for each
builder's reading list; the review and approval steps grow with the size of
the change, not of the repository.

## Install

```
/plugin marketplace add <owner>/<this-repo>
/plugin install code-factory@code-factory
```

Then check the machine: `/code-factory:factory install` lists what the
factory uses (git, Python, Node.js, Playwright, gh or glab and their logins,
gitleaks, osv-scanner, semgrep, Docker, and the Jira mode with whether `jira.env`
is there and complete), asks which missing tools to install, and installs them without administrator rights
(Homebrew when it is there, otherwise release downloads, pip or npm, into the
factory's own tools folder, `~/.cache/code-factory/tools`). For what needs an administrator password or a
login, it gives you the exact commands to run. Init runs the same check first.
The tools of the scanners in `security.required` (gitleaks, osv-scanner and semgrep by default) are
marked required, and `fx doctor` fails without them, because every gate would.
Run it once on each machine.

The command is `/code-factory:factory`, written in full everywhere in this README.
Alternatively `bash install.sh` installs it as a personal skill (the command
is then `/factory`); do not use both. A personal skill and its agents have plain names, so
a different `factory` skill, or other `factory-*.md` agent files, would be overwritten:
install.sh stops, and changes nothing, when it finds any that it did not install, or that
changed since. `bash install.sh --force` replaces them. To keep them, install code-factory
as a plugin: a plugin's skill and agents carry its name, so nothing clashes.

Update, from a terminal:

```
claude plugin marketplace update code-factory
claude plugin update code-factory@code-factory
```

Then open a new session (or run `/reload-plugins`) and run init again in each
factory repository (quick; it keeps your files and refreshes permissions: the rule for the
older version's fx goes, and a rule for another plugin's fx stays). A factory set up with
1.0.0 is converted by that run (see **What changed since 1.0.0**).
An install only moves when the plugin's version changes: commits pushed under the
same version never reach it, and update reports it is already at the latest.

## What changed since 1.0.0

Version 2.0.0 makes the factory project-wide.

- **One factory per project, with one or more repositories**, listed under `repos` in
  `factory.json`, in merge order. A project of one repository works as before.
- **init** takes `--project-name <name>` and `--repo <name>=<git URL>` (repeatable;
  `--project <url>` is short for a project of one repository). Run it again with a new
  `--repo` to add a repository.
- **Settings per repository.** A repository's entry holds its `name`, `url`,
  `base_branch` and `app.needs`/`app.env`, and may set others (`setup`, `app`, `e2e`,
  `*_paths`, `pr.provider`, `security.accepted_rules`…); what it does not set comes from
  the top level of `factory.json`, and what it sets replaces the project's whole value.
- **Checks and knowledge per repository**: `checks/<repo>.sh`; shared files at the top of
  `knowledge/` and each repository's own in `knowledge/<repo>/`, with its own `.ref`.
  Citations are `repo:path` and `repo:path:lines` once a project has 2 or more
  repositories. `fx scan`, `fx style-sample`, `fx code open`, `fx selftest`,
  `fx audit-scan`, `fx setup` and `fx knowledge` take `--repo <name>`.
- **One job per issue, covering every repository it needs.** Intake names them
  (`REPOS:`), you choose each one's base branch, and fx clones them
  (`<factory>-work/<KEY>/<repo>`), all on one branch name. One spec with a part per
  repository and a shared contract; build and gate per repository; the apps the job needs
  start in dependency order, each on its own port with its own database. With 2 or more
  changed repositories, the fit checker joins the reviewers (`round-N/review-fit.md`).
- **One branch and pull request per changed repository**, linked to each other. `fx pr`
  checks every repository first (work-in-progress commit, sync with the target, secret
  scan) and pushes nothing if one has a secret; then pushes each and opens its pull
  request (with `pr.create` on); then writes the links into every description.
  `--target` takes `<repo>=<branch>`, repeatable.
- **Removed:** `/code-factory:factory feature`, `fx feature`, `fx plan`, `fx workspace`
  and the `--repo <factory folder>` that came before a command (each now stops with exit
  code 3 and says what to do instead); the scope-writer agent; `fx new --upstream` and
  `--feature`; `api-notes.md`; intake's `MULTI-REPO` verdict; the per-machine
  `workspace.json` and `features/` state. A parent issue's children are each a job of
  their own (`fx jira children <KEY>` lists them).
- **Version 2.0.0** in `VERSION`, `plugin.json` and `marketplace.json`. `bash install.sh`
  removes the agent files it installed that this version no longer has.

**An existing factory from 1.0.0** (one `project.url` in `factory.json`) still reads as a
project of one repository, with its `checks.sh` where it is, so jobs keep working. To bring
it to the new form, update the plugin and run `/code-factory:factory init` again in the
factory repository:

- the repository is named after `project.name` (lower case, `-` for anything else; `main`
  when nothing is left), and gets `project.url` as its `url`, the `base_branch`, and the
  settings that belong to a repository: `setup`, `dev_server`, `app`, `e2e`,
  `copy_into_worktree`, `copy_from`, `protected_paths`, `ui_paths`, `pr.provider`,
  `security.accepted_rules` and `learning.knowledge_map`. Everything else stays at the top
  as the project's default;
- `keys_id` is written from the same name and URL as before, so the keys folder stays the
  same;
- `checks.sh` moves to `checks/<repo>.sh`, and `architecture.md`, `style.md`, `stack.md`,
  `api.md`, `ui.md`, `testing.md`, `environment.md` and `.ref` move from `knowledge/` into
  `knowledge/<repo>/`, with `git mv` when git tracks them. `knowledge/README.md` and the
  shared files stay; update the index where it names a moved file. init's output lists
  what moved;
- a `workspace` block leaves `factory.json`. With merge-request review's `review` key,
  `project.url` and `base_branch` also stay at the top, for that review.

Commit and push the result. Jobs started with 1.0.0 still load as jobs of that one
repository.

## Set up a project: `init`

1. Create an empty private repository for the project's factory (for example
   `shop-factory`), clone it and open **that folder** in VS Code.
2. In Claude Code: `/code-factory:factory init`. Give it the project's name and
   each repository's name and git URL, in merge order (or your local folder of one:
   the factory then uses its remote and never touches the folder). Init runs
   `fx init --project-name shop --repo backend=<url> --repo web=<url>`; for a project
   of one repository, `--project <url>` is short for that.
3. Commit and push the factory repository so the team shares it. A teammate
   clones it, opens it and runs init once; the repositories are cloned as needed.

**Adding a repository later.** Run init again and give the new one: fx adds it to
`repos` with its own `checks/<repo>.sh` and `knowledge/<repo>/`, then init learns it as
below. A name or URL already in the project is refused, and so is the factory's own URL.

Init refuses to run inside a project's own repository. If you used an older
version that kept a `factory/` folder inside the project, init imports it
(`--import <project folder>`, a project of one repository), leaving alone what belongs
to merge-request review (see **What lives where**); remove that folder from the project
afterwards with a normal commit.

Init asks you two things that set how the factory works, each explained before you
choose: how it reaches Jira (see **Jira**), before it reads any Jira history; and whether
it uses S3 at all (see **S3 or this machine only**).

Init detects whether the project already has code.

**Existing code.** For each repository, it inventories the code in a read-only clone
(`fx code open --repo <name>`, `fx scan --repo <name>`), reads it in depth (module map,
data model, API, roles and sensitive data, UI, tests and how test data is built), learns
the **code style** from real files and linter configs (`fx style-sample --repo <name>`)
into `knowledge/<repo>/style.md` with excerpts, and learns from git
history, merged PRs, finished Jira issues and the Confluence pages you pick (the Jira
issues are read by fx in mode `token` and by Claude's Atlassian connector in mode `mcp`;
Confluence only through the connector, and skipped without it).

**Reading rules.** Every main module is read, however large the project
(split between parallel agents), and must appear in its repository's module map
(`knowledge/<repo>/architecture.md`). Every statement in the knowledge cites the code
(`repo:path:lines`, or `path:lines` in a project of one repository), the database schema,
a document or your answer; `fx knowledge check` verifies every cited path,
line range and excerpt against the code, the module map's coverage, and that
no secret is copied. The code style is learnt per architectural role
(controller, model, policy, component, store, tests…) from the most recently
changed files; older patterns are recorded as "Legacy (do not copy)", and
where docs or Confluence disagree with the code you are asked. A fresh agent
then reviews the knowledge against the code before you approve it. With
`app.schema` set, the real database structure is read too.

**Project logic.** Next to the knowledge, init writes `logic/`: the business
logic of every feature, in the team's feature-logic-docs format (an index with
an overall log, `shared.md`, one folder per module with its rules and the only
parameter table, one file per feature: inputs, back end, front end, navigation,
a Mermaid flow, open points, change history). Every rule cites the code that
implements it and the test that covers it (or "no test"), and `fx logic check`
verifies both and the format. Back-end/front-end differences and anything the
code does not decide become open points or questions, never guesses. In each
job the spec is compared with the logic: where the issue says something else,
the factory comments on the issue (a summary for people, then the technical part)
and asks you whether it is a change of logic or a mistake in the issue. The
builder stages the updated logic files with the code; they go into the pull
request description and are applied to `logic/` once the pull request is merged,
after your approval, one job at a time. Logic whose cited code
was changed outside the factory is refreshed like the knowledge.

In every job the builder must read, before changing a file, the file itself,
two files of the same role, its tests and its callers, and list them in its
report (`## Read before changing`); fx refuses a report without the list and
the code reviewer checks it.

**New project.** It asks for references to take the style from (another
repository, a starter or template, a convention document), then asks about
purpose and requirements; language, framework and database; architecture and
folders; tests, lint and CI; authentication, product languages and
deployment. Each time it asks whether to scaffold now (as a normal factory job
ending in a PR) or only record the decisions.

Both then interview you (business rules, sensitive data, ASVS level, product
languages, style and test policy, definition of done, Jira statuses, lessons),
write `knowledge/` (small files per topic, shared ones at the top and each repository's
in `knowledge/<repo>/`; each agent reads only its own), record which commit each
repository's knowledge describes (`fx knowledge mark`), configure each repository's checks,
paths and running app (`app.up`, `app.needs`) and the UI checks, install the tools, prove
everything with `fx selftest` (every repository; `--repo` for one), list what is already
vulnerable with `fx audit-scan` (every repository; `--repo` for one), and optionally run a
deep security audit. Files every clone needs but git must not hold (such as `.env`) go in
the factory repository's git-ignored `local/<repo>/` folder (or `local/` for a file every
repository shares).

Commit `factory.json`, `checks/`, `knowledge/`, `logic/`, `lessons.md` and
`.gitignore` in the factory repository and push them, so the team shares them: the gate
does not run on changes to `factory.json`, a checks script or scanner rules that are only
on this machine.

## Commands

```
/code-factory:factory <jira link or key> [more]   run issues one after another
/code-factory:factory task "<what to do>"         a job without Jira
/code-factory:factory PROJ-123 --best-of 3        3 builders in parallel, a judge picks one
/code-factory:factory audit                       deep security audit (Cloudflare security-audit skill)
/code-factory:factory resume PROJ-123             continue a stopped job
/code-factory:factory rework PROJ-123 "..."       send the PR back to the builder
/code-factory:factory lessons                     learn now: lessons and knowledge updates
/code-factory:factory status                      all jobs
/code-factory:factory cleanup PROJ-123            delete a job's leftover clones (never unpushed work)
/code-factory:factory init                        set up or refresh the factory, add a repository
```

An issue that needs several repositories is still one job (see **A project of several
repositories**).

`audit` runs Cloudflare's open-source
[security-audit skill](https://github.com/cloudflare/security-audit-skill)
over the project's code (it asks before installing it and warns about the
usage cost), reports the confirmed findings, turns them into lessons, and asks
you one finding at a time whether to fix it as a factory job, each ending in its
own PR.

## How it learns

After each job (or once per run, or only on `/code-factory:factory lessons`: see
`learning.ask_after`) the factory looks at what it can learn from:

- its own reviewers' blocking findings and failed gates;
- your rework notes;
- review comments on each of the job's pull requests (GitHub with `gh`, GitLab with
  `glab`);
- commits you or a teammate pushed to a PR branch after the factory's last
  push: the corrections a human had to make.

Pull requests of earlier jobs are read again until they are merged or closed,
so comments and edits made days later still count. From these it proposes new
lessons, rewordings and removals; you decide on each change, one at a time. Every lesson gets an
id (`L12`); reviewers cite it when a finding breaks a lesson, so a lesson that
keeps being broken is flagged (reword it or turn it into a lint rule, semgrep
rule or test), and one that names files or symbols the code no longer has is
flagged as possibly obsolete.

`lessons.md` stays short. Each lesson carries topic tags (`[code]`, `[test]`,
`[security]`, `[ui]`, `[data]`, `[spec]`), and each agent gets only the
lessons for its topics plus the untagged ones (a lesson tagged `[review]` is left
alone: see **What lives where**). A lesson no job broke in 10 jobs
(`learning.graduate_after_jobs`) is proposed for a move into the knowledge file
that fits (`fx lessons graduate`; a repository's file is named `<repo>/<file>.md`). Above 40 lessons (`learning.max_lessons`),
learning proposes merges first, and `fx doctor` warns.

A short trend (rounds, blocking findings,
reworks, lines humans changed; last 10 jobs against the 10 before) shows
whether the factory is getting better.

The project knowledge follows the code: `knowledge/<repo>/.ref` records the
commit each repository's knowledge describes (`knowledge/.ref` in a factory whose
knowledge is still flat). When a repository's base branch has moved on in ways that matter
(dependencies, lint configs, CI, schema, API, auth, environment, tests, UI), a
librarian agent drafts updated knowledge files for it (`fx knowledge stale`, then
`fx knowledge ctx --repo <name>`) and you approve each one.
`/code-factory:factory rework` also brings in commits a human pushed to the PR branches before
the builder starts.

## Security standards

Reviews follow **OWASP ASVS 5.0** (level per project, default L2) and tag
findings with **OWASP Top 10:2025**, **OWASP API Security Top 10** and **CWE**
ids. Security is handled before the build (requirements and abuse cases in
the spec, test-first authorization tests, dependency checks), after it
(scanners in the gate, ASVS review with independent verification, approver),
and periodically (`audit`).

## What lives where

```
<project>-factory/                the factory repository for one project (open this in VS Code)
  factory.json                    settings, with the project's repositories under repos (commit)
  checks/<repo>.sh                each repository's checks for the gate (commit)
  knowledge/*.md                  what init learned that the whole project shares (commit)
  knowledge/<repo>/               each repository's own knowledge, and .ref, the commit it describes (commit)
  logic/                          business logic of every feature, the team's source of truth (commit)
  lessons.md                      rules (L1, L2…) learned from reviews, your corrections and audits (commit)
  jobs/<KEY>/                     each job's files, reports and screenshots (git-ignored; also on S3 when the factory uses S3)
  local/                          files every clone needs, such as .env (local, git-ignored)
  baseline.json                   last selftest (local)
<project>-factory-work/<KEY>/<repo>/  the job's own clone of each repository, deleted once the job's branches are pushed
<project>-factory-work/_read-<repo>/  a short-lived read-only clone (init, intake, audit; fx code open)
each project repository           receives only factory/<KEY>-… branches and pull requests
~/.cache/code-factory/            the factory's own tools, cloned references, fetched baselines and the board
~/.config/code-factory/           this machine only, never in git
  keys/<project>-<hash>/          the keys folder: S3 keys and jira.env (fx where prints it as keys_folder)
```

code-factory keeps what belongs to this machine in these two folders of its own. Run
`/code-factory:factory install` once on each machine and put the keys in its keys folder.
`fx where` prints every path, and for each repository its URL, base branch, knowledge
folder and checks script. The board (`~/.cache/code-factory/board.html`) is a page of its
own and shows jobs only.

**Merge-request review data.** A factory repository may also hold the data of
merge-request review, which is not part of code-factory. code-factory leaves it alone:

- **Settings.** The `factory.json` keys only merge-request review uses (`review`,
  `models.review`, `jira.transitions.review_changes` and `conflict`, `jira.code_owner`,
  `jira.subtask_type`, `jobs_store.include_reviews` and the `review_*` entries of
  `learning.sources`) are ignored: never an error, never removed or rewritten.
- **`jobs/_reviews/`** holds reviews of merge requests, not jobs. `fx status` and the board
  do not list it, `fx cleanup` does not touch it, and `fx jobs` never pushes, pulls or
  counts it: not with `--all`, not with `include_reviews` on. Naming one in `fx jobs push`,
  `fx jobs pull` or any other fx command, by its path or by its folder's name alone, is
  refused, since code-factory does not handle reviews of merge requests.
- **Lessons tagged `[review]`** stay in `lessons.md` exactly as written, with their ids. No
  agent gets them, they are never proposed for a move into the knowledge, and the learning
  never writes a new one.
- **`knowledge/repos/`** is ignored: no agent gets it, and `fx knowledge check` does not
  check it.

## Repository layout

```
.claude-plugin/marketplace.json
plugins/code-factory/.claude-plugin/plugin.json
plugins/code-factory/skills/factory/   SKILL.md, fx, factory-ui.js, templates/
plugins/code-factory/agents/           intake, spec-writer, builder, 4 reviewers,
                                       security verifier, approver, judge, librarian,
                                       knowledge reviewer, logic writer, planner,
                                       fit checker
install.sh                             alternative install as a personal skill
tests/                                 fx's tests (standard library only):
                                       python3 -I -m unittest discover -s tests
```

Bump `version` in `plugins/code-factory/.claude-plugin/plugin.json`,
`skills/factory/VERSION` and `.claude-plugin/marketplace.json` on every change so installs
pick it up.
