# auto-factory

A software factory for Claude Code. Give it a Jira issue or a plain task; get
back pushed branches, ready for pull requests, that agents specified, built,
tested (logic, UI and security) and reviewed. A human always opens or reviews
the pull requests and merges.

```
/auto-factory:code https://your-site.atlassian.net/browse/PROJ-123
/auto-factory:code task "Add CSV export to the invoice list"
```

The same plugin also reviews a colleague's merge request against its ticket
(`/auto-factory:review`).

It assumes nothing about your project: no stack, framework or human language
is built in. `/auto-factory:code init` learns each project (code, history, style, team
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
| 3 | Missing information → multiple-choice questions to you | you | `qa-log.md` |
| 4 | Spec: criteria, **one related test-data scenario** with cases per criterion, security requirements (OWASP ASVS) and abuse cases, UI check plan; with 2 or more repositories, a part per repository and the contract they share | agent | `spec.md` |
| 4b | Plan: one builder, or packages built in parallel that never share a file (a contract first) | agent | `plan.md` |
| 5 | Build in the project's style; authorization and data-isolation tests **written first**; Playwright UI check for user-visible changes | agent | code, tests, `ui/check.js`, `build.md` |
| 6 | Gate, per changed repository: its checks, path rules and scanners; the apps the job needs, started in dependency order with the scenario seeded; e2e and the Playwright UI check (screenshots, console/network errors, accessibility) | fx | `round-N/checks.md`, `ui.md`, `security.md` |
| 7 | Parallel reviews of the whole change: security (ASVS, verified by a second agent), code (tests vs spec, style), UX and UI with Playwright, and with 2 or more repositories the fit checker (do they work with each other?) | agents | `round-N/review-*.md` |
| 8 | Changes requested → back to 5 (max 3 rounds) | | |
| 9 | Approver: APPROVE or ESCALATE | agent | `decision.md` |
| 10 | Ask you which branch the pull request goes into; sync with it, scan for secrets, push the branch, open the PR when `pr.create` is on (draft if incomplete), delete the clone | you + fx | `pr-body.md` (ready to paste into the PR) |
| 11 | You review and merge, or send it back with `rework` | you | |
| 12 | Learn: propose lessons and knowledge updates from what happened, for your approval | agents + you | `lessons.md`, `knowledge/` |

Jira is updated along the way (start status, comments, review status, PR
link) through your own Jira connection or an API token. Local tasks skip Jira.

## Watching the factory

`/auto-factory:code board` opens the factory board, which also shows each review of a
colleague's merge request: its merge requests, which reviewers are running and which are done,
the check together, and what waits for you. It is one HTML page for
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
in your earlier jobs), the tokens the agents used, read from each agent's transcript
on this machine with every step's re-read context counted (and a rough cost when
`dashboard.usd_per_mtok` is set) and links to its files; then recent activity
and the finished jobs. Click an agent for what it does and its recent runs.
The page reloads itself every few seconds and every fx command rewrites it:
several Claude Code windows on one machine all show on this one page. No
server, nothing to install.

## Several builders on one ticket

After the spec, a planner decides what builds the job with the fewest tokens;
time does not count. Every extra builder pays again for reading the spec, the
knowledge and the code, so a job keeps one builder unless it has large parts
that are truly separate, where one builder would carry them all in a context
that costs more at every step. Then it is split into packages:

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
  As many run at once as the machine has room for (**Several jobs at once**);
  `split.max_builders` puts a ceiling on one job. A merged package's stack is
  stopped for you.
- **Change requests.** A builder that needs a file it does not own, or a
  different contract, stops and asks; the planner reassigns files or reopens
  the contract, and the work goes on.
- **Integrator.** When everything is merged, one builder checks the whole,
  fixes only what joins the packages, updates the logic and writes the report.
  Gate and reviews then run as usual; each finding goes back to the package
  that owns its file, the rest to the integrator.

## Several jobs at once

Give one command several tickets (`/auto-factory:code C2P3-1,C2P3-2 C2P3-3`): the session
runs them side by side, each job going on whenever it is ready, and asks you the questions
of several jobs together. Or open a Claude Code session for each ticket, in one project's
factory repository or in several projects'. Either way the jobs share the machine through
fx, with no server and nothing to set up.

One session drives at most 3 jobs over its life: each job stays in its context, read again at
every step, so a fourth makes every step of the others dearer too. Given more, the session runs
what it has room for and prints the commands to paste into new sessions, 3 tickets each; the
board shows every session's jobs on one page.

- **Ports.** Every clone that runs the project's stack (a job, a package, a candidate, a
  review's sandbox) gets its own block of `ports.step` ports, handed out for the whole
  machine. Two projects never get the same ones, and a port something else already
  listens on is passed over. A job started by an older version keeps the ports it has;
  its packages, which had ports one apart then, each get a block before they start again.
- **Room.** Steps that install, test, scan or start the app (`setup`, the gate, the checks
  before a push, a sandbox, a selftest) first take a place on the machine. What the
  machine has is read when it is needed: CPUs, memory, what Docker was given, what other
  programs use, and whether the system says memory is short. What a step uses is measured
  while it runs and remembered for that machine. A step that does not fit waits in a
  line, first come first served, and starts by itself. With one job nothing changes: the
  only step on the machine starts at once.
- **No number is set for one machine.** Every default is a share: a tenth of Docker's
  memory is kept free; a step nobody measured yet counts as a quarter of Docker's
  memory and of the CPUs and a tenth of the machine's memory; a package being built
  counts as a tenth of the machine's memory and a quarter of its CPUs until its stack
  or its tests were measured. So a larger machine runs more at once and a smaller one
  fewer, with the same settings. A machine that does not tell how much memory it has
  holds no step back for memory.
  `/auto-factory:code capacity` shows what the machine has, who runs, who waits and why, and takes
  your wishes for this machine (`fx capacity --set pressure=strict`).
- **Builders.** A split build opens only as many packages as there is room for, counted
  over every job and session. Agents run their own heavy commands through `fx run`,
  which queues them the same way. When the session that was building a job's packages
  is closed, the session that goes on with the job starts those packages again.
- **One job, one session.** A job is driven by the session that last worked on it;
  another open session has to take it over on purpose. One job runs one long step at a
  time, and two commands of a job never wipe each other's notes.
- **Shared files.** When a project keeps files in its factory repository that several
  jobs change (UI tests, reference images), a lane in `factory.json` lets one such job
  build at a time; the next one does its intake and spec and waits. What you allow for
  one job (an existing test it may change, a finding you accept) is written by
  `fx override` into `overrides/<job>.json`: it holds for that job only, and committing
  it disturbs no other job's gate.
- **Nothing is left running.** A step that is stopped stops what it started. When fx
  itself was killed outright (no memory left), the job's next step first stops the
  checks that were left running in its clone. Containers of clones that are gone or of
  jobs that are over are listed by `/auto-factory:code capacity` and stopped when you say so.

- **One run of the checks for a commit.** The builder runs the checks with `fx checks`,
  and fx keeps the result with the commit and a fingerprint of everything else the
  script reads: the script, `factory.json`, the factory repository's files, the
  reference images, and the environment the script runs in (a builder's
  `SKIP_SLOW=1 fx checks` is not the gate's run). The gate and `fx pr` do not run the
  script again on that same commit with those same inputs, and say so. A failure is
  never reused and every other step still runs. A gate that expected to reuse a pass
  and finds something changed waits for the room the checks need before it runs them.
  `fx reuse --off` turns it off.
- **Rounds that needed no builder.** After a human accepts the findings a round failed
  on, the next round judges the ZAP reports it has instead of scanning again. A pass on
  an unchanged commit, with nothing the checks read changed either, gives the failed
  rounds back, and a run that something outside stopped does not count, once for a
  commit.
- **Shared folders, read as committed.** A job that does not hold a lane reads the
  lane's folders as committed, from its own copy, once the project's script reads
  `FACTORY_LANE_<PATH>`. How long each step waited for room is in `/auto-factory:code status`,
  `/auto-factory:code capacity` and the board.
- **A gate in two phases.** The checks script takes one place; the app with e2e, the UI
  check and the scanners takes another, after it. Each is measured as its own kind of
  step and gives its room back when it ends, so another job's checks can run while a
  gate scans. The second phase keeps the gate's turn in the line.
- **CPUs per step.** Every step is given a number of CPUs when it starts: all of them
  alone, about its share beside others. The project's commands read it as
  `FACTORY_CPUS` (`--workers=${FACTORY_CPUS:-3}` for Playwright); ZAP, in the gate, runs
  limited to it.
- **A job waiting for you rests its stack.** When a job stops for your answers and
  nothing of it runs, its containers are stopped (`docker stop`: containers and volumes
  stay; one that keeps data in memory keeps running) and started again with the answers.

How many tickets finish in a day is still set by the heaviest step. On a machine that
fits one gate, two gates overlap only where one's checks fit beside the other's scans.

## What blocks a job

**A limit of the app the environment cannot set.** Tests and the security scanner call the app far more often than a
person does, and meet its limits: an answer 429 after 60 calls a minute, an account locked after 5 wrong passwords,
a captcha. They then misread what they see (a scanner took a 429 for SQL injection) and cost whole rounds. So every
such limit is read by the code from an environment variable, and the job's test stack runs with it raised:

- The limits live in the parameter table of each module's `logic/<module>/README.md`, with two columns: **Env** (the
  variable the code reads it from) and **Test value** (what the test stack runs with). Agents find them in the code,
  per project and per feature (`/auto-factory:code limits`, also part of `init`), and you approve them.
- fx gives the test values to the job's app (`app.limits_env`: as a process's environment, or the file
  `$FACTORY_LIMITS_ENV` names, which a container stack must load), and checks that the line the table cites holds
  the variable.
- A limit the code does not read so (`MISSING`) stops **every** job of the project, except the fix task fx writes for
  it (read it from the environment, today's value as the default); jobs start again once it is merged. A factory
  whose limits were never worked out gets no job either.
- Limits nobody listed are caught three ways, each stopping until fixed: the code reviewer (a limit the job adds),
  the librarian (one that reached the base from elsewhere) and the gate (a scan the app refused).
- Nothing turns this off.

**A route the wrong user can call.** The permission table says, for every route of the app, which kind of user may
call it: not logged in, each role, an API key, and whether only on its own data. In every gate fx logs in as each of
them on the job's own test stack and calls the routes the job's change can reach, reads and writes:

- A refused call must look like a refusal and leave the data as it was (fx compares a fingerprint of the database
  before and after); a refused id must answer exactly like an id that exists nowhere, so nobody learns what exists;
  an allowed write must change the data. After each allowed write the database is put back from a snapshot.
- The table's human part is the `## Permissions` section of each module's logic (a cell `yes`, `no` or `own` per
  kind of user); the technical part (logins, ids, bodies, snapshot commands, and a script that adds the accounts and
  data the project's seed lacks, on the test stack only) is `stack/permissions.json` of the factory. An agent works
  both out from the code (`/auto-factory:code permissions`, also part of `init`); you only confirm a few rules in
  plain words and the rows it doubts.
- `fx selftest` runs the table on the base branch. What the job brings stops it: a mismatch goes to the builder; a
  route it adds without a row waits for its rows, which come from the ticket or from you, never from the builder.
  What the base branch already gets wrong stops no job but one that touches that route: you hear of it at once and
  its fix task runs first.
- Which rows a gate calls: the rows the job changes or adds, the routes the base branch did not have, and the routes
  whose handler is a file the job changed or a file that uses one (followed by name, two steps; a config file by the
  keys it changed, such as `config('mail.from.address')`; a class also by the project's own interfaces it implements,
  which a provider binds and a controller asks for). A change to a layer every route goes through, or to code
  such a layer uses (middleware, auth, policies, route files, providers, sessions, the dependencies; your own list in
  `permissions.full_paths`), calls the whole table, and so does `permissions.scope: "full"` or `FX_PERM_FULL=1`. The rows not called neither pass nor fail; `permissions.md`, the gate and the pull request say
  which were called and why. `fx selftest` and `fx permissions try <job>` always call the whole table. Reads are
  called for several users at once (`permissions.parallel`), writes one at a time. An answer 429 (too many calls)
  is called again after waiting, and if it stays it is noted as not checked, never as a wrong permission.
- No job starts until the table is confirmed and run on the base branch. It costs no tokens, a few minutes of each
  gate. Nothing turns this off.

The rest:

- A changed repository's checks script, `checks/<repo>.sh` (your lint, type check, tests,
  build), failing. The gate goes through the changed repositories in merge order and stops at
  the first that fails; `checks.md` says which.
- A user-visible change without a Playwright UI check, or a UI check with a
  failed step, a browser console error, a server error, or an accessibility
  violation at a blocking level (default: critical and serious).
- A screen far from its design, when the UI reviewer finds the code is the cause. The UI check measures each
  screen the spec gives a design code with the auto test's own comparer: element by element against the design
  package, the design drawn with the data the web shows, at the auto test's screen size and thresholds (read from
  the project's auto test repository, `ui_checks.design.test_repo`). A heavy difference does not fail the gate,
  since it can be the package's own way of drawing: the UI reviewer, on Opus, first looks at each pair of images,
  then decides each measured screen in a `## Design` table (`code`, `design`, `different data`), and fx refuses a
  review that leaves one out or calls a heavy difference fine.
- A text on a changed screen that is not the one the spec quotes (`## UI text`: every text word for word, where it
  shows, a text with a number in its form for one and for many), and a state the screen must have that the app does
  not handle (the network lost, no right, a failed load shown as an empty list, a server error, not found...). The
  UX reviewer brings each state about and compares each text in a `## Text` table; fx refuses a review without it,
  and a text marked `no` makes it CHANGES. When the design package's Spec answers its own edge questions, lists the
  states each screen must have and what each button leads to (design-qa 2.25 and later), the spec turns those rows
  into criteria; a row the design has not settled is asked about, never filled in. With or without them, the spec
  answers the same edge questions for each changed screen (limits, ways in, rights, closing, files, buttons,
  filters...) as cases of kind `edge`, from the issue, the design, then what the project already does.
- What keeps the ticket, the design and the code together: a point the issue itself lists as open (an `OI`, "waits
  for the next design package") is never built from its proposal; a text or a state the issue has and the package
  lacks is built and reported as the design being behind; and when the issue names a later version of the package
  than the one in hand (each package says its own in its CHANGES.md), the spec asks before going on. What these
  checks cannot know: whether an answer of the Spec is right, only that it is there and which drawn state it cites.
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

Scanners and Playwright are installed for the factory itself (`/auto-factory:code
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
| Pages behind the login | `security.zap_auth` (web login) | Worked out from the code by an agent at `init` (`login.md`), never written by hand: a command that prints a test account's session cookie, a `check` page that must answer 2xx with it, and the account's `role`. The value goes only to the ZAP container, which sends it only to the app's host, and is taken out of its reports. |
| API routes no page links to | `security.zap_openapi` and `security.zap_api_auth` (API login) | Where the app serves its OpenAPI description, such as `/openapi.json`, and how a client of the API logs in (a token or key), kept apart from the web login. |
| Web and API scans side by side, each on its own copy of the app | `security.zap_parallel: true` | Off by default (one after the other on the job's app). On: the API scan runs on a second copy of the app with its own database (and its own box in the sandbox), at the same time as the web scan: about half the time, and neither can break the other's login or data. Costs a second stack's memory while ZAP runs, its first setup, and on this machine a second clone of each repository. |
| Nothing destructive the scan should leave alone | `security.zap_exclude` | Regexes of URLs ZAP never crawls, posts to or attacks, such as an admin action that revokes the API key the API scan uses. What is left out is not scanned, so keep the list short. |

Every scan logs in first and calls each `check` page, and calls it again after the scan. The API login is
proved again right before the API scan, and taken anew if the web scan before it ended it. A check that fails, a
login lost during the scan, or a scan most of whose answers refused it (401/403: no login; 429: a limit; 80% by
default, `security.zap_refused_pct`) stops the gate without a builder (`next: login`): the agent works the login
out again, and asks you what the code does not tell. 404 and 422 do not count: an active scan guesses ids and
bodies that do not exist, and many of them only add a note that the scan reached less of the app. A job whose own change alters login keeps its way for itself until its pull requests are merged. Every agent
that needs to log in uses the same saved way, through `fx login <job> --kind web|api`.
| What is already in the code and its history | `fx audit-scan` | Every scanner over a whole branch. One task file per rule to fix, ready for `fx new --local`. |

Run `fx selftest` again after changing `zap_mode`, `zap_openapi`, `zap_auth` or the ZAP
image, including after pulling a newer `:stable`: other scans find other old problems.
A selftest keeps each long step that passed (the checks script, the permission table, the ZAP web scan, the
ZAP API scan) and uses it again while the commits and what that step reads are unchanged, so after fixing a
login only the scan that failed runs again (`--fresh` runs everything). The permission table logs everyone in
once, before its snapshot of the database: after putting the database back it only proves each login on its
check page.
Until then the gate and `fx doctor` say so, and header or ZAP findings do not block
because of what the base branch already has.

Scanners cannot judge business rules, such as whether one customer can read
another's order: the security reviewer and its verifier look for those in each
change, and `/auto-factory:code audit` can add a deeper audit by agents. Vulnerabilities in
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
like (`security.strip_env` adds names, `security.pass_env` lets one through).
They run in a login shell, for your PATH; a profile that exports a token again (a
`~/.bashrc` that sources a key file) does not hand it back: fx unsets those names once
more after the profile, before the command.
fx never commits the `copy_into_worktree` files or `.env*` files when it saves
left-over work (names ending in `.example`, `.sample`, `.dist` or `.template`,
such as `.env.example`, are ordinary files), and redacts well-known key formats and local env secrets (the
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
- **Mobile.** A repository that is a mobile app has its own `mobile` block in its entry in
  `repos`; with several repositories, no other repository is taken for an app.

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
repository, then the agents' own; each best-of-N candidate gets a block of its own (`ports.step`
defaults to at least 10; `fx init` raises it when it adds a repository, `fx doctor` fails
when it is smaller than the repositories plus 3, and a new job never takes a block that
overlaps one an unfinished job holds).

**One job per issue.** `/auto-factory:code PROJ-123` starts one job, whatever the
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

**Parent issues.** A parent issue's children are each a job of their own: Claude reads them
with its Jira connection, and `/auto-factory:code PROJ-101 PROJ-102` runs them side by side.

A human still reviews and merges each pull request. The learning reads every one of them,
and a job's logic changes are applied once all its pull requests are merged; when some are
merged and the others closed without a merge (state `partial` in `fx logic pending`), you
decide whether to apply them. A rework after one pull request was merged leaves that
repository out: it runs from its base when another app needs it, and its pull request
stays recorded for the learning; with every pull request merged, start a new job.

## Reviewing a colleague's merge request

```
/auto-factory:review https://gitlab.example.com/acme/api/-/merge_requests/7
/auto-factory:review PROJ-123
/auto-factory:review
```

The last form lists the merge requests waiting for your review. It needs `glab`, logged
in; a token with only `read_api` is enough, since the factory never writes to GitLab. Run
it in the factory repository of that project, because the review follows its knowledge,
lessons and logic. One factory can review several repositories: see below.

1. **Merge conflict first.** With a conflict the review stops before any agent runs, and
   you are asked what to put on the ticket.
2. **The ticket, without the code.** An agent writes the requirement checklist from the
   ticket alone. The code is cloned only after that, so what the ticket asks is not worked
   out from what the code does.
3. **Scanners, CI and discussion.** gitleaks, osv-scanner and semgrep on what the merge
   request changes; the CI result and the discussion as GitLab shows them.
4. **The code runs, for real.** fx starts it in a throw-away sandbox with your `setup` and
   `app.up`, and a runner agent calls the API for every requirement, at least two cases each.
   Each call goes through `fx review call`, which reaches only that sandbox and logs the request
   and response in `run/calls.jsonl`, each row chained to the one before. fx refuses a `run.md`
   that passes a requirement on a call it did not log. No reviewer starts before this is done.
5. **Reviewers in parallel.** Requirement, code and security; UX and UI too when it changes
   `ui_paths`, with the run's results in hand. The requirement reviewer follows each requirement from where it starts to what it returns,
   with concrete values, and compares that with the ticket, `logic/` and what the code did
   before. It has to write what it followed (`## Logic traced`), and fx sends it back if a
   requirement has no path or no cases, if it calls something implemented that was never run, or
   if it contradicts a failed call without saying why. fx can check that the work is there, not
   that the reasoning is right or the cases well chosen, so you see both in the report.
6. **A verifier** tries to disprove every blocking finding, and what it disproves is dropped.
   Before all this, fx asks for the tickets around this one: the parent, its other sub-tasks and the
   linked ones, because the worst bug may show only against a ticket not built yet. For a UI change it
   also asks for the ticket's design packages, unpacks them as data, and the sandbox stays up so UX and
   UI can look in a real browser through `fx review ui`. There the UI reviewer, on Opus, measures each
   screen the merge request changes against the package with the auto test's comparer, as a job does.
   fx refuses a UI review with no `## Components`
   table (each element, what the Spec asks, what the code uses, its props, what overrides it), one that
   leaves a measured screen out of its `## Design` table, and a UX
   review with no `## Text` table (each string against its source), and a wrong row with no finding.
   Each finding says how to see it (`Repro:`), when it is fixed (`Accept:`) and, with `logic/`, what it
   was checked against (`Checked against:`); on a screen it says whose mistake it is (`Type:`).
7. **You choose.** fx sorts what is left by where it points, from the diff: findings on what
   the author changed, open questions about the ticket, and pre-existing issues. Only the
   first group can go out, and only what you pick. For each one you are told whether it is
   serious and why, and whether the merge request can be merged with it: the reviewers lean
   **must be fixed before merging** for a blocking finding and **can wait for a follow-up
   ticket** for a note, and you decide. The comment opens with the verdict (not ready to merge,
   or can be merged), and what you let wait goes in a sub-task of its own. The full `review.md` stays in
   `jobs/_reviews/<KEY>-mr<iid>/round-<N>/`, which goes to S3 only with `jobs_store.include_reviews`.
8. **You confirm.** Every Jira comment and transition is shown to you first, and fx only
   posts the exact draft you saw. You merge on GitLab yourself: the factory never comments,
   approves or merges there.

```json
"jira": { "transitions": { "review_changes": "Doing", "conflict": "Doing" } }
```

These two name the status a ticket goes back to a dev in, per project. Empty, the ticket
keeps its status and you are told. A clean review never moves a ticket: where a deploy moves
tickets on after a merge from their submit status, as a `jira-bridge` does, a ticket has to
stay there, and one sent back to a dev has to return to it before the merge.

A review is not a job: it has no build, gate or pull request. `/auto-factory:code status`
still lists the open ones, and `resume <KEY>` picks one up, also in a new session. After a new commit, run it again: the next round looks first at what changed, and the
requirement reviewer says which earlier findings were fixed and which are still open. At the
end you are asked whether the review missed anything, and it proposes lessons from that: see
**How it learns**. Nothing is saved without your approval.

You are asked one thing at a time. Chosen findings can open sub-tasks for the dev and for whoever owns
the design or the Spec, instead of only a comment, each finding with its own number. Every ticket key is
a link, and a code another ticket defines is too, with `"jira": {"code_owner": {"MSG": "ABC-1", "API-01":
"ABC-24"}, "subtask_type": "Subtask"}`. At the end, what you decided that `logic/` or `knowledge/` says
otherwise is listed, one at a time, and changed if you say so.

A merged merge request can be reviewed too, when a ticket has no open one: it reads the merge
request's own diff and runs the target branch as it is now, and the report says so.

**The sandbox is not optional, and there is no setting to turn it off.** If it cannot start, because
the sandbox VM is not there, `app.up` is missing or the app fails to come up, the review stops before
anything of the merge request runs, and asks you. You can fix it, stop, go on without running the code,
or run it on this machine without isolation; the last two take your reason in your own words, which is
then printed on the Jira comment. Neither can be chosen before the sandbox has failed. Every comment says
what really ran: the commit, the number of calls, each requirement's result, and what was simulated.

**Where the code runs.** A merge request is a colleague's code nobody has reviewed yet, and its install
scripts (`postinstall`, composer's scripts) run the moment it is set up. So fx runs it in a sandbox it
enforces itself, not one a setting has to remember:

- One Linux VM per machine, `fx-sandbox` (Lima; `fx sandbox install`, part of `/auto-factory:code
  install`). It mounts nothing of this machine and has its own Docker, never this machine's. It is set
  up once per machine; after the machine (or WSL) restarts, the next review or job starts it again (a job
  keeps its box and what it installed).
  - Its size is yours to say. The first `fx sandbox install` asks (`next: ask-size`) how much memory
    and how many CPUs it may have, with what fx suggests; your answer (`fx sandbox install --memory
    <GiB> --cpus <n>`) is kept on this machine for every later install and reset. From then on fx
    measures what the whole VM uses while each command of a box runs (any that runs over 15 seconds),
    and suggests a size from that history: the most it used and a quarter more, half again a size that
    ran out of memory, more CPUs when its load stayed above them, and less when it never used half.
    It leaves this machine what the factory's own steps there were measured to use (a mobile app's
    build and virtual phones) and at least 4 GiB. The suggestion is put to you when a gate ran out
    of memory, in `fx sandbox status` and in `fx doctor`; `fx sandbox size` shows it with the
    history. The VM's size changes only on your answer (it restarts the VM: with jobs' boxes
    running, only with `--restart`).
- Inside it, each review is its own box (a Sysbox container) with its own files, Docker (so a compose
  stack works as it does here), database, volumes and network, deleted when the review ends. Boxes do
  not see each other and share no writable cache. The clone and the factory's scripts are copied in at
  the same paths, so `setup`, `app.up` and `review.checks` run unchanged; `jobs/`, `local/` and `.git` of
  the factory repository are not copied.
- The network: while dependencies install (`setup`, and the images its compose stack pulls) the box
  reaches only package and image registries, through a proxy of its own (`sandbox.allow` adds a host,
  such as a company registry, which you confirm). Then the proxy goes and the box has no way out at all:
  the app, the checks, the runner's commands and the browser of the UI check run with no internet. The
  app's ports reach this machine's `127.0.0.1` only.
- A web and the mocked run of a desktop app are checked by a browser inside the box. A mobile app, and a
  desktop app's real build on Windows, are built and run on this machine against the box's API; the
  comment says so.
- `sandbox.prepare` adds to the box image what a repository's setup needs (php and composer, a JDK),
  built once in the VM from the factory's settings.

The log stays in `round-<N>/run/` and is never pushed to the jobs store.

**A job runs in a box of its own too (3.8).** Not only a colleague's code: a dependency the builder
adds, or one already in the project's lockfile, runs its install script the moment it is installed.
So each job gets its own box in the same VM, for the job's whole life, and everything of the job that
runs the project's code runs there: `fx setup`, the agents' commands (`fx run --job <job> -- <command>`,
every test, linter or script, not only the heavy ones), the gate's checks script, e2e, `ui/seed.sh`,
the login and permission-table commands, the app and the browser of the UI check.

- **Network:** the box reaches the package registries only during an install: `fx setup`, and
  `fx run --job <job> --install -- npm install <name>` when the builder adds a dependency. Every other
  command has no internet.
- **Files:** the job's clones here stay the original; the agents edit them as before. Before each
  command fx copies what changed into the box; after it, what the command changed in a clone comes
  back, but only files git sees (a lockfile, a migration it generated), never what git ignores
  (`node_modules`, `vendor`, builds), never a link, and never `.git/`, `.claude/`, `.vscode/`,
  `.idea/`, `.devcontainer/` or `.envrc`, which this machine or its agents would run by themselves.
- **A job waiting for you** (questions, approval, its pull request) has its box stopped: files,
  installed dependencies and database stay, the memory comes back. It starts again when the job goes
  on, and is deleted with the job's clones.
- **Each job, best-of-N candidate and parallel package** has its own box, as it has its own block of
  ports and its own stack. Measured on a 6 GB VM with a small web job in each box (Postgres in its own
  Docker, a Node app, then the UI check's Chromium): a box costs about 130 MB at rest and 220 MB while
  Chromium runs; three at once peaked at 1.4 GB of the VM's 6 GB. A project's own build or test suite
  (webpack, a JVM, a large database) adds its own weight on top.
- **No sandbox on this machine:** the job stops before its setup and asks you: set it up, or let this
  job run here (`fx sandbox host --job <job> --reason ... --confirm <code>`), once per job; its pull
  request says so, with your reason.
- A mobile app's build and a desktop app's real Windows build still run on this machine, with their API
  in the box; the pull request says so.
- `init` adds rules that refuse the agents' bare `npm`, `composer`, `php`, `node`, `pytest`, `make`,
  `docker`... here, so they go through `fx run --job`. Best effort, like the rules on key files.
- `fx sandbox clean` removes only this factory's boxes that no job or review uses any more.

**What the sandbox does not cover.** The agents themselves run on this machine. `init` writes rules that
keep them from reading `~/.aws`, `~/.ssh` and the factory's keys (their Read tool, and shell commands
such as `cat` that name those paths), but a script an agent writes and runs could still open a file. So
keep the damage small: give the factory's storage keys only the project's prefix (read-only where they
only read) and rotate them, keep an administrator AWS key and an SSH key that can push to production off
the machine that runs the factory, and read what `fx doctor` advises about the keys it finds.

What this does not force: an orchestrator that ignores `SKILL.md` and never calls `fx` is outside
everything above, since fx can only refuse what goes through it. The hash chain shows a log edited by
accident or in haste, and a determined agent with shell access could still rewrite it. GitHub is not
supported yet.

### Several repositories, one factory

The factory reviews the merge requests of every repository of the project (`repos` in
`factory.json`; those on GitLab), each by its own entry: `ui_paths`, `setup`, `app`, `mobile`,
and `review.checks` for what its sandbox runs.

```json
"repos": [
  { "name": "fe", "url": "git@gitlab.com:group/fe.git", "base_branch": "main" },
  { "name": "admin", "url": "git@gitlab.com:group/admin.git", "base_branch": "main", "ui_paths": ["src/**"] },
  { "name": "node", "url": "git@gitlab.com:group/node.git", "base_branch": "main", "setup": "./ops/prepare.sh",
    "review": { "checks": ["pnpm ds:check", "pnpm eslint ."] },
    "app": { "up": "./ops/up.sh {port} {job}", "down": "./ops/down.sh {job}", "health": "/health" } }
]
```

A repository's `setup` and `app` in its entry in `repos` say how the sandbox starts it, and
`review.checks` what its own checks are. Without `app`, its review stops at the sandbox and asks you.

`init` teaches every repository of the project (`fx review learn <name>` teaches one outside
init), each in `knowledge/<repo>/` next to the shared files, so no reviewer reads one
repository's rules as another's. `logic/` and `lessons.md` are shared. A repository with no knowledge yet is still
reviewed, from the ticket and its code alone, and it has no database schema to look at.

A ticket may have merge requests in several of them, and looked at alone each would seem to be
missing what the others deliver. So a ticket is searched in every repository, and you choose
which to review:

1. **A conflict in any of them stops all of them,** before any agent runs.
2. **One checklist,** for the whole ticket.
3. **Each merge request is reviewed by its own repository's rules,** and its reviewers can read
   the others' diffs. A requirement another one delivers counts as delivered.
4. **Then together.** The fit checker reads them side by side: calls, fields, roles,
   errors, whether an order of merging matters, and whether the ticket fell between them.
5. **One comment.** You pick from all the findings, and the ticket gets one comment listing every
   merge request, with one move and one assignee. You still merge each one yourself.

If you leave out a merge request of the ticket, the reviewers are told, and a requirement it may
deliver counts as `cannot tell`. A repository GitLab does not answer for is named, so its merge
requests are not silently left out. Clones stay on the machine until the check together is done.

## Baselines on S3

Screenshots to compare against make a repository heavy for good: PNGs barely
compress, and every version stays in the history. With the blocks below in
`factory.json`, the images live on S3 by hash, the folder is git-ignored, and a
sorted list of path, sha256 and size in git says which images belong to each
commit. Without them nothing changes.

Each project's factory has its own place on S3, which `init` always asks you for. It
has one folder per part of the factory, so later parts only add a folder next to this
one:

```
s3://<bucket>/<prefix>/      this project's factory (no prefix when the bucket is only its own)
  baselines/sha256/<2 chars>/<hash>
  <another part>/…           later
```

```json
"storage": { "type": "s3", "bucket": "", "region": "", "endpoint": "", "prefix": "" },
"baselines": { "dir": "qa/ui-qa/baselines", "manifest": "qa/ui-qa/baselines.lock.json" }
```

- `endpoint` is empty for AWS, or an S3-compatible service such as MinIO. Objects
  are never overwritten and never deleted; cleaning up old ones is up to you. `fx
  doctor` fails when two factories on this machine share a bucket and prefix.
- `baselines.folder` names its folder in the storage (default `baselines`).
- `repo` is `factory` (default: the list and folder are in the factory repository)
  or the name of the project's repository that holds them, so each of its branches
  carries its own list (`project` still works for a project of one repository);
  commands then need `--job` or `--clone`. That repository's `.gitignore` must ignore
  the folder, which `fx baselines migrate` does on the job's branch.
- Keys are never in `factory.json` or git. Put a read key in `s3-read.env` and a write
  key in `s3-write.env` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optionally
  `AWS_SESSION_TOKEN`) in this machine's keys folder, which `fx baselines status`
  prints as `keys_folder`: `~/.config/code-factory/keys/<project>-<hash>/`, outside
  the factory repository and its clones. Or name AWS profiles in
  `storage.read_profile` and `storage.write_profile`. Every part of the factory uses
  them, except that jobs may have a write key of their own (see **Jobs on S3**). The read key
  needs `s3:GetObject` and `s3:ListBucket`, the write key `s3:PutObject`. fx signs its
  requests itself, in its own process: the keys reach no environment, your own `AWS_*`
  variables pass through untouched and are not used, and `/auto-factory:code init` keeps agents'
  Read tool away from the folder. Code running as your user that goes looking for the
  file could still read it, like every other local secret, so keep the write key only
  on machines that upload images. Keys still in `local/` from an older version keep
  working, and `fx doctor` says how to move them.

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
history, and the audit reports the next audit compares with. With a `jobs_store` block,
each folder is also kept in this project's place on S3. Without it nothing changes.

```json
"jobs_store": { "folder": "jobs", "auto": true, "include_audits": false,
                "exclude": ["_audits/*/sandbox/", "**/node_modules/", "**/vendor/", "**/composer-cache/"],
                "max_file_mb": 50 }
```

```
s3://<bucket>/<prefix>/
  baselines/...                                  unchanged
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
folder, then:

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
holds a local secret or an S3 key, stays on the machine, and so do an archive fx cannot read
(damaged, with a password), excluded folders, symlinks and files over `max_file_mb`. The snapshot
lists each with its path and reason, never its value. `_audits/*/sandbox/` is always left out. If
`job.json` itself has a finding, that job is not pushed at all.

**Reading `held_back`.** `fx jobs push`, `fx jobs status`, `fx status`, `fx pr`, the board and
`fx doctor` all name the files that stayed on this machine, for example
`{"path": "reports/net.log", "reason": "secret: aws-access-token"}`. Those files are on this machine
alone, and a broken machine loses them. The job still reads `saved`, because pushing again changes
nothing. Decide for each one: take the secret out of the file so that it goes up with the next push,
or accept that it stays. gitleaks can be wrong, and a file held back by mistake is still not on S3.
What you excluded yourself is in `skipped` but not in `held_back`. `held_back: null` means fx 1.4.0
pushed that job and did not note them: `fx jobs push` finds out and uploads nothing. After a restore,
`not_restored` names what its push left behind, and nothing on the new machine is held back.

**What is looked for, and what is not.** Looked for: gitleaks' rules, inside archives down to 3
levels; the values of your local env files; S3 keys in key files and the static keys of
`storage.*_profile`; a Laravel `APP_KEY` and Sanctum tokens. Not looked for: a password in a sentence
such as `connecting with password xxx to mariadb`, what is in an image, archives deeper than 3 levels,
a profile's temporary keys (SSO, a role), and values encoded in base64 or a URL. gitleaks is a filter
and no guarantee: a job's reports can still hold sensitive data, so keep the bucket private.

**Audit reports are off by default** (`include_audits`), and `init` asks about them separately:
they describe holes that are not fixed yet, and anyone with the read key could read them.

**Reviews of colleagues' merge requests are off by default too** (`include_reviews`), for the same
reason. Turned on, each goes up when it ends and comes back with `fx jobs pull`. Its diffs stay on
your machine, since they are someone else's code.

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

## Mobile apps

A mobile app (Flutter, React Native or native) goes through the same steps as a web: only the way of running
changes. Where a web opens in a browser, the app is built from the code under test with the project's own commands and
runs on virtual phones the factory makes for itself, driven by Appium:

- **Asked at set-up, and later**: `/auto-factory:code init` asks whether the project has a mobile app (this
  repository, another one, not yet, no). An app that comes later is caught when it runs: a repository that has
  become an app stops its UI check with `next: setup-mobile`.
- **Command line or a window, asked for each ticket**: the phones run with nothing on the screen (recommended), or in
  a window (the emulator's, iOS Simulator) to watch the app; `fx new --phones`, `fx review start --phones`.
- **Versions**: per platform the oldest version the app supports that the machine can run (`mobile.min_os`), the
  newest, and one in the middle (or the ones `mobile.versions` names). Android system images are installed by the
  factory after the user agrees; an older iOS runtime is downloaded in Xcode.
- **Code and review**: the builder's `ui/check.js` (and a reviewer's `review ui` script) drives the app with `t.see`,
  `t.tap`, `t.type`, `t.swipe`... on the smallest phone with the oldest version and the largest with the newest, per
  platform, one phone at a time, the app
  built from the job's commit and pointed at the job's own API. Crashes count as console errors; a control without an
  accessibility label is an accessibility finding.
- **The design, measured**: the comparer reads an app's screen from its accessibility tree (boxes, texts, roles) and
  its colours from the screenshot. A shipped build shows no styles, so fonts, corners, borders and shadows are not
  measured on an app: the AI look judges them on the two images.
- **Phones**: iOS simulators (macOS only) and Android emulators, made once and kept (`factory ...`); a phone the user
  had running is left as it is. On Windows the factory runs in WSL and the Android emulator runs on Windows. How many
  phones run at once follows the machine's memory. `fx mobile doctor` and `fx mobile devices` show what this machine
  has; `/auto-factory:code install` installs Appium (3.9.0, uiautomator2 8.7.3, xcuitest 12.22.8, WebdriverIO 9.32.0)
  and bundletool (1.18.3), and says how to get the Android SDK, a system image and Xcode.
- **Not yet**: real phones (an iPhone needs Appium's WebDriverAgent signed with an Apple team).

## Desktop apps

A desktop app built with Tauri 2 is a web front end in a window of the system's web view, with a Rust core the
front end calls through Tauri's IPC. The usual way is to build the web first and wrap the same front end as the app,
so a desktop app goes through the web's steps, with one more way of running its UI check:

- **Asked at set-up, and later**: `/auto-factory:code init` asks whether the project has a desktop app (this
  repository, another one, not yet, no) and fills the `desktop` block (`framework: tauri`, where `tauri.conf.json`
  is, the mocks file, whether the front end is also a website). A repository that has become one stops its UI check
  with `next: setup-desktop`.
- **Twice, in Chromium**: the builder's `ui/check.js` (and a reviewer's `review ui` script) runs once as the website
  and once as the app's window: the front end served by `app.up`, at the window's size from `tauri.conf.json`, with
  `window.isTauri` set and Tauri's IPC answered here. On Windows the web view is WebView2, a Chromium, so what the
  front end draws is close to what users see. Steps, screenshots and errors are named `[web]` or `[desktop]`.
- **The IPC, answered by mocks**: the repository keeps a mocks file (for example `ui/desktop-mocks.js`), one answer
  per Rust command the front end calls, written by the builder with the command; a check can answer its own cases
  (`t.ipc.handle`), see what was asked (`t.ipc.calls`) and send events from the Rust side (`t.ipc.emit`). Events,
  channels and errors behave as in Tauri (checked against `@tauri-apps/api` 2.12). Tauri's window, app, os,
  opener, notification and log plugins answer by themselves, as on Windows. A command nothing answers fails the
  check; `ui.md` lists every command called and what answered it.
- **The real app on Windows**: with `desktop.build.windows` set, the gate and `review ui` also run the script on the
  real app (`[app]`): built on Windows from the job's code with the project's command (a copy of the code on Windows
  keeps `node_modules` and Rust's `target` between builds; each build is kept by commit), started with its own
  WebView2 data and driven over WebView2's DevTools protocol, its Rust side answering. So its window's CSP, CORS from
  `http://tauri.localhost` and the IPC to the real Rust code are checked too; `t.restart()` closes and opens the app
  with its data kept. The factory runs in WSL: with WSL's mirrored network Playwright reaches the app straight, with
  NAT through a small `node.exe` that carries each connection (Windows' firewall blocks the other ways). A Windows
  program outlives the WSL process that started it, so the app is stopped by the path of its own copy and its data
  folder, never by name. `fx desktop doctor` says what the machine has (Rust and the Visual Studio Build Tools on
  Windows, the WebView2 Runtime, Node.js on Windows); `fx desktop clean` removes the builds and copies on Windows. A
  machine without them notes that the real app was not run; a build that fails fails the check.
- **The Rust side** is tested by `cargo test` in the repository's `checks.sh`.
- **Not yet**: installers and auto-update; macOS and Linux builds of the app. Electron, .NET and Flutter desktop apps
  are not supported.

## Requirements

- Claude Code (terminal or IDE extension) with a Jira connection, or a Jira API
  token (`JIRA_EMAIL`, `JIRA_API_TOKEN`). Not needed for local tasks.
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
/plugin install auto-factory@code-factory
```

Then check the machine: `/auto-factory:code install` lists what the
factory uses (git, Python, Node.js, Playwright, gh or glab and their logins,
gitleaks, osv-scanner, semgrep, Docker, the Jira connection), asks which
missing tools to install, and installs them without administrator rights
(Homebrew when it is there, otherwise release downloads, pip or npm, into the
factory's own tools folder). For what needs an administrator password or a
login, it gives you the exact commands to run. Init runs the same check first.
The tools of the scanners in `security.required` (gitleaks, osv-scanner and semgrep by default) are
marked required, and `fx doctor` fails without them, because every gate would.

There are two commands: `/auto-factory:code` (a job from a ticket, and everything
around jobs: init, install, status, the board) and `/auto-factory:review` (a colleague's
merge request). Alternatively `bash install.sh` installs them as personal skills
(the commands are then `/auto-factory-code` and `/auto-factory-review`); do not use both.

Update, from a terminal:

```
claude plugin marketplace update code-factory
claude plugin update auto-factory@code-factory
```

After an update, open a new session (or run `/reload-plugins`) and run init again in each
factory repository (quick; it keeps your files and refreshes permissions). A factory set up
for one repository (one `project.url`) is converted by that run (see **A project of several
repositories**).
An install only moves when the plugin's version changes: commits pushed under the
same version never reach it, and update reports it is already at the latest.

## One factory per project

The factory serves a whole project, with one or more repositories.

- **One factory per project, with one or more repositories**, listed under `repos` in
  `factory.json`, in merge order. A project of one repository works as before.
- **init** takes `--project-name <name>` and `--repo <name>=<git URL>` (repeatable;
  `--project <url>` is short for a project of one repository). Run it again with a new
  `--repo` to add a repository.
- **Settings per repository.** A repository's entry holds its `name`, `url`,
  `base_branch` and `app.needs`/`app.env`, and may set others (`setup`, `app`, `e2e`,
  `*_paths`, `pr.provider`, `security.accepted_rules`, `mobile`, `review.checks`…); what it
  does not set comes from the top level of `factory.json`, and what it sets replaces the
  project's whole value. With several repositories, `mobile` is only ever a repository's own.
- **Checks and knowledge per repository**: `checks/<repo>.sh`; shared files at the top of
  `knowledge/` and each repository's own in `knowledge/<repo>/`, with its own `.ref`.
  Citations are `repo:path` and `repo:path:lines` once a project has 2 or more
  repositories. `fx scan`, `fx style-sample`, `fx code open`, `fx selftest`,
  `fx audit-scan`, `fx setup`, `fx checks` and `fx knowledge` take `--repo <name>`.
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
- **Reviews of merge requests** cover every repository in `repos`; the older
  `review.repos` list is folded into it.
- **Removed:** `/auto-factory:code feature`, `fx feature`, `fx plan` and `fx workspace`
  (each now stops with exit code 3 and says what to do instead); the scope-writer agent;
  `fx new --upstream` and `--feature`; `api-notes.md`; intake's `MULTI-REPO` verdict; the
  per-machine `workspace.json`. A parent issue's children are each a job of their own.

**An existing factory** (one `project.url` in `factory.json`) still reads as a project of
one repository, with its `checks.sh` where it is, so jobs keep working. To bring it to the
new form, update the plugin and run `/auto-factory:code init` again in the factory
repository:

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
- the repositories of `review.repos` become repositories of the project, and their
  knowledge moves from `knowledge/repos/<name>/` to `knowledge/<name>/`;
- a `workspace` block leaves `factory.json`.

Commit and push the result. Jobs started before still load as jobs of that one repository.

### Upgrading from code-factory 2.0

3.x is the plugin `auto-factory`, in place of 2.0's `code-factory`, with the same factory: one factory for
a whole project and its repositories (`repos` in `factory.json`, `checks/<repo>.sh`, `knowledge/<repo>/`, one job
per issue across its repositories). It adds the review of a colleague's merge request (`/auto-factory:review`), UI
checks of mobile apps on virtual phones, and several jobs at once on one machine. Its commands are
`/auto-factory:code` and `/auto-factory:review` (2.0: `/code-factory:factory`).

**Jobs in progress carry over**: a job started on 2.0 or 3.0.0 goes on with 3.0.2 from the stage it reached, in
the clones it has. Once per machine, then once per factory repository:

1. **Let what is running end.** Wait until no gate, checks or pull request command of a job is running, then close
   every Claude Code session that runs jobs: a session keeps the plugin it started with. Apps a job left running
   keep their ports; jobs started afterwards take others.
2. **Replace the plugin**, from a terminal:

   ```
   claude plugin marketplace update code-factory
   claude plugin uninstall code-factory@code-factory
   claude plugin install auto-factory@code-factory
   ```

   Installed with `install.sh` instead: pull this repository and run `bash install.sh` again; it removes 2.0's
   `/factory`.
3. **Check the tools again**, in a new session: `/auto-factory:code install`. This machine's keys stay where 2.0
   kept them (`~/.config/code-factory/keys/<keys_id>/`, from `keys_id` in `factory.json`).
4. **Jira**: 3.x reads Jira through the Atlassian connector of Claude, or `JIRA_EMAIL` and `JIRA_API_TOKEN` in
   the environment; 2.0's `jira.env` file is not read. Jira steps a job left waiting are done when it resumes.
5. **In each factory repository**: `/auto-factory:code init`. It keeps `factory.json`, `checks/`, `knowledge/`,
   `logic/` and `lessons.md` as they are and refreshes this machine's settings (at most it adds `keys_id`, lays
   out `factory.json` again and makes the checks scripts executable); then `fx doctor`. Commit and push what it
   changed: the gate refuses to run while the factory's settings are not committed.
6. **Carry on with each job**, in a new session: `/auto-factory:code status` lists them, and
   `/auto-factory:code resume <KEY>` continues one from its stage.

**Installed 3.0.0?** It ran one repository per factory. Update the plugin first on every machine that uses the
factory (`claude plugin marketplace update code-factory`, then `claude plugin update auto-factory@code-factory`),
since 3.0.0 cannot read a factory once it is converted. Then, in each factory repository, run
`/auto-factory:code init` again, commit and push:

- a factory 3.0.0 made for one repository (`project.url`, `checks.sh`) converts in place: `checks.sh` becomes
  `checks/<repo>.sh` and the repository's knowledge moves into `knowledge/<repo>/`. Its open jobs carry on;
- a 2.0 factory on which 3.0.0's init ran keeps its `repos`. Init then says that `project.url` and `checks.sh` at
  the top are not read any more: remove them in a commit once no job made by 3.0.0 is open. Each such job carries
  on in the repository it was cloned from (3.0.2 and later).

Then step 6 above.

### Upgrading to 3.4: fewer tokens

3.4 spends far fewer tokens on each job, most of all when one session drives several. What changes by itself
once the plugin is updated:

- `/auto-factory:code` and `/auto-factory:review` run on Sonnet, whatever model the session was on, and the
  rules of `init`, `audit`, `install` and the review sit in files of their own, read only when that command runs.
- Jira updates and reading an issue without a token go to small agents; the session keeps one line of each.
- The planner aims at the fewest tokens, not the shortest time, and keeps one builder unless the job has large
  parts that are truly separate.
- A later round continues the builder only while its context is small (`builder_resume_max_tokens`, default
  150000); otherwise a new builder starts from `build.md` and the branch. The builder reads each file once and
  opens only the screenshots it needs.
- The spec written again after your answers starts from its own draft, on Sonnet (`models.spec_after_answers`).
  The review's runner, which calls the API, runs on Sonnet.
- The board shows the tokens agents really used, read from their transcripts, priced per kind of token.

What a factory made before 3.4 keeps from its own `factory.json`: `models.high.builder` and `models.librarian`
stay `opus` there. To have them on Sonnet too, set both to `"sonnet"`, commit and push.

### Upgrading to 3.5: what to do before the next job

- **The app's limits** (**What blocks a job**): a factory gets no new job until `/auto-factory:code limits` has
  worked them out. Run it once in each factory repository; a limit the code does not read from the environment
  becomes a fix task that the other jobs wait for.
- **The scanner's logins**: run `fx selftest` again. When it stops with `next: login`, the login agent works the web
  login and the API login out (`security.zap_auth`, `security.zap_api_auth`), each with a check page. Until then a
  gate whose scan cannot log in stops without a builder.
- One session drives at most 3 jobs; agents get their instructions from a file fx writes (`prompt_file`), and the
  tokens an agent used are recorded on the command that reads its result (`--usage`).
- A gate no longer sends a builder for what is not in the code: a ZAP finding a second scan does not see again is
  marked unsure, and a run the machine broke (a browser crash, a timeout) runs once more.

### Upgrading to 3.6: the permission table

- A factory gets no new job until `/auto-factory:code permissions` has worked the table out, you confirmed it and
  `fx selftest` ran it on the base branch. Run it once in each factory repository; jobs already running finish
  without it.
- What the base branch already gets wrong is listed for you, and each route becomes a fix task to start first.

### Upgrading to 3.7: the review sandbox

- `/auto-factory:review` runs nothing of a merge request until this machine has the sandbox VM:
  `/auto-factory:code install` (step 7) or `fx sandbox install`, after `brew install lima` on macOS, or
  QEMU and Lima on Linux and in WSL ("Where the code runs", above).
- A repository whose setup needs tools (php, composer, a JDK) lists their install commands in
  `sandbox.prepare`; a private registry goes in `sandbox.allow`.
- Run `/auto-factory:code init` once more: it adds the rules that keep agents from this machine's key
  files.

### Upgrading to 3.8: every job in a box

- Jobs need the sandbox VM too (above). A job already in progress gets its box at its next command,
  and its setup runs again in there once.
- A VM set up before 3.8 empties a box's `/tmp` when a waiting job's box starts again: `fx sandbox reset`
  once (a factory under `/tmp` loses its clone's files otherwise).
- Run `/auto-factory:code init` once more: it adds the rules that send the agents' project commands
  through `fx run --job`.
- The VM's memory: one box per job at once (and per candidate or package). Its 6 GB held three small web
  jobs at once with room to spare (above); since 3.13 you choose it, from what fx measured (below).

### Upgrading to 3.17.1: a review's stack reaches its own services

- A review's box (`fx review sandbox`) left only `localhost` out of its proxy: a container that called another service
  of the stack by name (an init container creating a bucket on `http://minio:9000`) got the proxy's 403, the app never
  came up and the review stopped at `ask-no-sandbox`. The box now gets the compose services of the clones and the
  factory in `no_proxy`, for its setup and every container, as a job's box always did. A project's own `NO_PROXY`
  workaround in its compose file is no longer needed.

### Upgrading to 3.17.0: rounds the factory lost, the permission table before it runs, a job from another branch

- `fx gate <job> --dry-run` runs in any stage after a build (review, approve, and pr while nothing is pushed) and with
  no round left, so trying a changed seed, permission table or script never needs `fx stage --force`.
- `fx rounds <job> --give-back <n,...> --reason "<what in the factory failed them>"`: rounds a person says the factory
  lost (a seed without a row, a permission table that could not judge a cell), not the code. Two steps like
  `fx override`; the rounds stop counting, and a job that went to its pull request for want of rounds, with nothing
  pushed, goes back to build. The approver is told which rounds were given back and why.
- The permission table: a job's own file under another name than the technical part's (`permissions.json` where fx
  reads `permissions-<repo>.json`) is named at once instead of being left unread; `out_of_scope` in the job's file
  counts; routes without a row set the round aside before any cell is called, in seconds. `fx permissions check <job>`
  says the same without a gate: the job's file and staged rows against the routes of its clone.
- A job started from another branch than the repository's `base_branch` (an earlier attempt's, say): only what
  differs from both counts as its work (sensitive paths, UI, ZAP and permission scope), so what it merged in from the
  base branch is not flagged; protected and frozen paths are checked against the base branch (what the pull request
  brings), so a protected test the other branch changed is caught. A job from the base branch itself is measured as before. `fx new` and `fx repos` say so.

### Upgrading to 3.16.0: one test with the checks script's own setup

- `fx checks <job> --only <stage> -- <test arguments>` runs one stage of the checks script, with the setup it needs (the
  project's containers, a migrated test database), passing the arguments to its test runner: what a builder uses for
  one test file or one test when `fx run` has no runtime or database for it. The script gets `FACTORY_ONLY_STAGE` and
  `FACTORY_TEST_ARGS` (init.md, "Stages"); the run is never kept for the gate. A checks script written before 3.16.0
  does not read them: `fx doctor` names it, and `/auto-factory:code init` adds it. Until then `--only` stops with that
  advice.
- The builder's prompt names each repository's `protected_paths` (the existing files the gate refuses to see changed).
  Another existing test may change only when the spec changes what it asserts; the builder lists each one in the
  report under `## Existing tests changed`.

### Upgrading to 3.16.0: fixes

- A job of one repository in a factory of several read the top level of `factory.json`, not its repository's entry:
  the gate started no app (no ZAP, no permission table) and used the wrong `protected_paths`, `ui_paths` and the like.
- `fx override --unprotect` reads the protected_paths of the job's repositories; `<repo>:<path>` limits it to one.
- `fx override --accept` refuses a finding of `fx suppressions` at once: only `security.allow_suppressions` allows
  suppressions, for the whole factory.
- A folder in `copy_into_worktree` (built assets an app bundles) is copied with what it holds, its small text files
  are searched for secrets, and `fx pr` and `fx logic check` no longer crash on it. A local file put in `local/<repo>/`
  after the clone was made reaches the clone at the next `fx setup`, `fx checks` or gate. `fx logic check --job` of a
  job that stages no logic file answers at once.
- `fx pr` keeps only a pull or merge request's address, not the link in a CLI's notice of its own new release; an
  address kept wrongly before is found again from the branch.
- `fx audit-import` reads the security-audit skill's `severity.overall_severity` and the place from its trace (the
  sink, and the entrypoint when it is elsewhere) or evidence.

### Upgrading to 3.15.1: a factory in a folder of its repository

- Several factories kept in one git repository (`<project>/<repo>/factory.json`) could not run a gate or `fx checks`:
  the gate's frozen copy of the factory is a worktree of the whole repository, and it read `factory.json` at the top
  of it ("this factory repository is not set up yet"). It now reads the factory's own folder in that worktree. A
  factory that has its repository to itself is not affected; `FACTORY_NO_FREEZE=1` is no longer needed.

### Upgrading to 3.15.0: a job's box that broke under it

- The sandbox VM's unattended upgrades ran needrestart, which restarted containerd and Sysbox under the running
  boxes. A box stayed "Up", but every command in it failed (`unsafe procfs detected`), and the gate counted that as
  the code's FAIL; a box started again by hand had lost the images its setup pulled, and the next round failed too.
- needrestart in the VM now only lists what it would restart, and never containerd, Docker or Sysbox
  (`/etc/needrestart/conf.d/fx-sandbox.conf`). A VM made before 3.15.0 gets the file before its next box;
  `fx sandbox status` shows `auto_restarts`, and `fx doctor` checks it.
- Before a job's command, fx looks at its box once: a box whose runtime went away is made again, with the job's setup,
  and a box whose Docker lost the setup's images runs the setup again. fx says so in one line.
- A round whose checks or app failed for that reason is `void: true` with `next: gate`: it does not count, and no
  builder is sent. `fx sandbox box --job <job> --recreate` makes one job's box again without making the whole VM again.

### Upgrading to 3.15.0: an e2e suite kept in the factory repository

- The e2e map looked for test files only in the project's clone, so a factory that keeps its suite in its own
  repository (a `qa/` folder, often a lane's) ran the whole suite in every job. The map now says
  `"tests_root": "factory"`: its `tests` are then paths of the factory repository, and `paths`, `shared` and `none`
  stay the project's. The chosen paths are relative to `FACTORY_E2E_ROOT` (the factory, or the job's copy of a lane's
  folder). A test of that suite not committed yet runs in every job that reads it there.
- An entry with `"tests": []` is an area no e2e test opens: a change there runs no e2e test.
- `fx e2e check` names a suite that is in the factory when the map does not say so. Run `fx e2e check` after
  updating; add `"tests_root": "factory"` to a map whose tests are in the factory repository.

### Upgrading to 3.15.0: smaller fixes

- `fx tidy` no longer appends the line that points at `open.md` to `open.md` itself on every run, and takes out the
  copies an older tidy left there.
- `fx tidy` moves the change history of `logic/shared.md` into `logic/history.md`. A `shared.md` still over the
  limit gets advice that works (`logic_by_hand`), in tidy and in `fx doctor`, instead of a `--split` that refuses it.
- `fx doctor` no longer counts `docker run --pull=never` (or a `docker run` of an image setup pulls) as a fetch.

### Upgrading to 3.14.0: a job runs the e2e tests its change reaches

- A gate ran the whole end-to-end suite for every job, every round (about 970 browser tests and 28 minutes on one
  project, for a one-line fix of one static page). fx now chooses the e2e tests a job's change reaches, from a map the
  factory keeps for each repository, `knowledge/<repo>/e2e-map.json`: every e2e test file (`tests`), what every page
  goes through (`shared`), what reaches no page (`none`), and each area's code with the tests of its pages (`map`).
- The checks script and the `e2e` command get `FACTORY_E2E_TESTS` (a file, one test file per line, maybe none), or
  `FACTORY_E2E_FULL=1`, with `FACTORY_E2E_WHY`, `FACTORY_CHANGED_FILES` and `FACTORY_BASE_REF`. The whole suite runs
  when the map cannot say: no map or a broken one, a changed file in `shared` or in no entry, or `fx gate <job> --full`
  and `fx checks <job> --full`. A test file no entry lists, and one the job changed, always run. A script that does not
  read the new variables runs as before.
- A job never runs the whole suite by itself: `fx selftest` does, and your CI should on the base branch.
- `round-<N>/checks.md` says what ran and why. `fx e2e show <job>` shows what a job would run, and `fx e2e check`
  lists the tests no entry lists and the globs that match nothing.
- To use it, run `/auto-factory:code init` again on a factory made before. It writes the map and the lines of the
  checks script that read `FACTORY_E2E_TESTS`.

### Upgrading to 3.14.0: a builder runs the whole checks once

- A builder ran the whole checks script (19 minutes on one project) twice, only to see one test it had fixed. It now
  runs only the tests its change touches, and a failed test alone until it passes. It reads every failure of a run
  before it fixes anything, and runs the whole script once, at the end.

### Upgrading to 3.14.0: test databases in the sandbox

- A test database's every write waited for the disk through each layer under it: the box, the VM's disk, this
  machine's disk, and Hyper-V under WSL. A suite that resets its tables for every test ran several times slower than
  on the team's own machine. The sandbox VM's disk no longer waits for those flushes. Nothing in the VM needs them:
  its clones, test databases and images are all made again by a job's setup. After the machine itself crashes, run
  `fx sandbox reset`.
  - A VM made before 3.14.0 changes before its next box. `fx sandbox status` shows `disk_flushes`.
- Init runs the server-side tests in the test runner's parallel mode when the stack has one, with one database per
  process. It keeps that mode only when selftest gets the same result as a run in one process; otherwise it goes back
  to one process and writes why in `testing.md`. Init asks you before it adds a package (paratest, pytest-xdist) to
  the repository.
- The checks script marks its stages (`echo "::fx-stage <name> $(date +%s)"`). fx keeps each stage's seconds in
  selftest, the gate and `fx checks`. A stage of a job's run that takes 3 times as long as in selftest (and a minute
  more) is named in `checks.md`: that is the machine or the sandbox, not the code. Run init again to get the marks
  into an existing factory's scripts.

### Upgrading to 3.13.0: the sandbox's size is yours, suggested from what it used

- 3.12.0 gave the VM half of the machine by itself. Now no size is picked for you: run `fx sandbox install` once
  (or `/auto-factory:code install`); it asks how much memory and how many CPUs the VM may have (`next: ask-size`),
  with fx's suggestion, and keeps your answer. fx then measures what the VM uses and suggests a new size when it
  needs more (or could give some back): when a gate ran out of memory (`next: ask-size`), in `fx sandbox status`
  and in `fx doctor`. `fx sandbox size` shows the suggestion and the history (`sandbox-usage.jsonl` in fx's state
  folder); `FACTORY_VM_WATCH=0` turns the measuring off. A mobile app's phones and build and a desktop app's real
  Windows build run outside the VM; the suggestion leaves this machine what they were measured to use.

### Upgrading to 3.13.0: a gate's permission scan calls what the job can reach

- A gate called every cell of the permission table in every round (461 on one project, over an hour). It now calls
  only the rows the job's change can reach, and the whole table when the change touches a layer every route goes
  through (above). The scope is in `permissions.md`, `permissions.json`, the gate's summary and the pull request.
  `fx permissions try <job>` (on the job's running app) and `fx selftest` (on the base branch) call it all, whenever
  you choose; `fx audit-scan --base <branch>` runs ZAP over a whole branch.
- With `app.limits_env: "file"`, fx looked for `$FACTORY_LIMITS_ENV` in every file of the factory folder before each
  command of the app, ignored test output included (2.7 GB of screenshots on one project): it reads only the files
  git has, once per command of fx while they are unchanged.
- The reads of the table are called for several users at once (`permissions.parallel`, 4 by default); a 429 is
  called again after waiting, then noted as not checked.
- ZAP from WSL with Docker Desktop reaches the job's app through `host.docker.internal`: it fell back to the host
  network there, which is Docker Desktop's own VM, and scanned nothing.
- ZAP did the same: the whole app in every round (about 31 minutes a gate on one project, for a job that changed only
  a config file). Now it scans what the job's change reaches, the routes the permission scan found:
  - the web scan runs an Automation Framework plan (`zap/zap-plan.yaml`): the classic spider over the whole app, the
    passive rules on all it saw, and the attack (`zap_mode: full`) only inside those routes, from their pages (their
    ids from the permission table's requests). A route with a parameter is one target, not one per row the crawl
    found (`/users/{user}/lock` was 300 targets with 300 users); the attack runs at low strength and stops after
    `security.zap_affected_minutes` (6 by default). A browser crawl of those routes is off
    (`security.zap_ajax_minutes`: give a single-page app 2 or 3). A ZAP that has not ended 30 seconds after its
    plan said it was over is stopped (it once waited half an hour with its report written);
  - the API scan reads the OpenAPI description cut down to them (`zap/openapi-scope.json`); when none of them is in
    it as fx reads it, the whole description;
  - no route reached (only tests, documents, images, or code no route uses): no ZAP, and the base branch's scan
    stands;
  - a change to a layer every route goes through, no permission table, or `fx gate --full`: the whole app, as before.
  `security.md` and the pull request say which, why and how many routes of how many. Measured on a copy of that
  project, a change to the five account limits of its config file: 16 of 80 routes, and the web scan took 7 minutes
  (a minute of spider, an attack that ended by itself after 5½ minutes) where it took about 31.
  The scan is logged in, so it cannot attack the pages only a guest sees (the login form, a forgotten password):
  the app sends a logged-in user away from them. That was so before, in the whole-app scan too.
- When you want everything anyway: `fx gate <job> --full` (that round calls the whole table and ZAP scans the whole
  app), `permissions.scope: "full"` and `security.zap_scope: "full"` (every round), or `fx permissions try <job>`
  and `fx audit-scan` whenever you choose. A round that scanned only some routes is never taken for a whole-app scan
  of the same commit: `--full` scans again.

### Upgrading to 3.13.0: a job's stack and its box's proxy

- While a job's box installs (setup), its containers had the box's proxy, which let through only the registries, so
  a service calling another one by name (`http://minio:9000`) got `403 Filtered`. The services of the compose files of
  the job's clones and of the factory (their names, `container_name` and `hostname`) and `host.docker.internal` are now
  in `NO_PROXY`/`noProxy`.
- A setup that fails says which containers ended with an error, and `setup.log` ends with their last lines and exit
  codes, kept as they died, before compose removed them.
- A second gate, `fx checks` or `fx pr` on a factory commit already frozen no longer stops on `SameFileError` (the
  reference images' list copied onto itself through a lane's link).

### Upgrading to 3.12.0: rounds the machine was too small for

- The sandbox VM was 6 GiB and 4 CPUs on every machine. A web job's browser tests (several Playwright workers,
  full-page screenshots) beside its app, database and cache ran it out of memory: Chromium was killed and as many
  tests as workers failed with `Target crashed`, on other pages each run. 3.12.0 gave it half of the machine; since
  3.13.0 you choose its size (above).
- A gate whose checks script or e2e failed while the VM's kernel killed processes for memory, or whose failed
  tests are all browsers that crashed, says nothing about the code: the round does not count, no builder is sent
  (`void: true`, `next: ask-size` since 3.13.0), and fx names the VM's memory and the size it suggests. A run the
  VM killed processes in is not run a second time as flaky. Once for a commit and a size of the VM: failing so again
  on the same ones counts. What was seen is kept in `machine-short.json` in the round's
  folder; `fx checks` says it too (`machine_short`). A container over its own memory limit is not counted.

### Upgrading to 3.11.2: a job checks its own limits

- `fx limits check <job>` checks the limits as the job's gate does: the job's staged logic over the factory's, and
  the code in the job's clone. Its answer says what it read (`read`); without a job it reads the factory's logic
  only. `fx limits tasks` and `fx limits mark` refuse a job instead of ignoring it.

### Upgrading to 3.11.1: a scan stops for its login only when it was refused

- A ZAP scan stops the gate for its login (`next: login`) only when most of its answers were 401, 403 or 429, as
  ZAP's samples of its 4xx answers tell (80% by default, `security.zap_refused_pct`). 404 and 422 are the scan's own
  guesses at ids and bodies: many of them only add a note that the scan reached less of the app. A report without
  samples counts every 4xx as before, unless the login's check page still answered 2xx after the scan.

### Upgrading to 3.11: a two-level logic index, a selftest that goes on, ZAP scans that keep their logins

- The logic index has two levels: `logic/README.md` lists the modules, and each module's `README.md` lists its
  features in its feature table, so neither outgrows the file limit. `fx tidy` (or `fx logic index`) turns an
  older index of one row per feature into that; `fx logic index` keeps both tables. A Vietnamese overall log
  headed `Nhật ký tổng` now moves to `logic/history.md` too.
- `fx selftest` keeps each long step that passed (the checks script, the permission table, the ZAP web scan, the
  ZAP API scan) in `jobs/_selftest/resume.json`, and the next selftest uses it again while the commits and what
  the step reads are unchanged: after a fix, only what failed runs again. `--fresh` runs everything.
- The permission table logs everyone in once, before its snapshot of the database, and after putting the
  database back only proves each login on its check page; a write's data fingerprint is taken once, not twice.
- The API scan's login is proved again right before the API scan, and taken anew when the web scan ended it.
  `security.zap_exclude` keeps URLs out of every ZAP run. `security.zap_parallel: true` runs the API scan at the
  same time as the web scan, on a second copy of the app with its own database (and box): run `fx selftest`
  again after turning it on.
- Notes on the skill also take out the project's own words in page and route paths.

### Upgrading to 3.10: a GitHub login on every machine, long files fully split

- Notes on the skill cannot be turned off: until `gh auth login` was run on a machine, `/auto-factory:code init`
  does not finish, `fx doctor` fails and no new job starts there (a job already running goes on).
- A knowledge part with no headings left to split at (one long table) is cut into pieces of whole blocks, a long
  table between its rows. `fx tidy` lists the `fx logic ctx --split` command of every logic file still over the
  limit, and `fx doctor` fails until none is: re-run `/auto-factory:code init` to finish it.

### Upgrading to 3.9: scope, shared limits, a box that installs through Docker

- The factory repository is fetched before `init`, `scan`, `new`, `knowledge ctx`, `logic ctx` and
  `lessons collect`. Behind with a clean tree, fx pulls; behind with changes of its own, it stops and asks.
- Optional settings, asked by `init` (Phase 2, **Scope**): a repository's `scope` (`include` / `exclude`
  globs: legacy code the factory never learns from) and `logic_modules` (the modules whose logic and
  permission rows it keeps; the permission table lists the other modules' routes in `out_of_scope`).
- `repos[].needs`: another repository whose code a repository's scripts read (a checks script that reads
  the back end, an `app.up` that builds it). Jobs, selftests and reviews get a read clone of it, and every
  script its path in `FACTORY_CLONE_<NAME>`: a job's box has no network to fetch it.
- Limits of every route are written once, in `logic/shared.md`'s `## Limits` table, by an agent of their own
  (`/auto-factory:code limits`); two rows citing the same lines with different variables are refused.
- In a job's box, `docker build` and `docker run` (compose too) install through the box's proxy by
  themselves; the operating systems' package mirrors and `api.github.com` are allowed during an install. A
  host added to `sandbox.allow` reaches a job's existing box at its next install.
- `fx doctor` checks the factory's scripts (`bash -n`, changed since the last selftest, fetching where there
  is no network) and an empty repository login that hides the project's. Run `fx selftest` once after the
  upgrade so doctor knows which commit it ran on.
- Small jobs can take the light lane: intake proposes which steps to leave out, fx checks the real diff and puts
  back what it needs. Gates read the factory from a git worktree at its pushed commit, so another session's
  unfinished edits in the factory folder no longer stop them.
- Long files: no knowledge or logic file grows past 12 KB (`learning.file_max_kb`). fx splits a longer knowledge
  file into a folder with an index (`README.md`) and one file per `##` part; logic keeps its history in
  `history.md` and a module's open points in `open.md`. Re-run `/auto-factory:code init` once after the upgrade:
  it runs `fx tidy` and has the logic writer split feature files that are still too long.
- Notes on the skill itself: when an agent finds that the skill or fx really needs changing, `fx skill-note`
  posts it as a public issue on this repository, with the project's names, links, ticket keys, paths and
  secrets removed and nothing about the user or the machine. fx installs `gh` when it is missing; posting needs
  `gh auth login` once.

## Set up a project: `init`

1. Create an empty private repository for the project's factory (for example
   `shop-factory`), clone it and open **that folder** in VS Code.
2. In Claude Code: `/auto-factory:code init`. Give it the project's name and
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
(`--import <project folder>`, a project of one repository); remove that folder from the project afterwards
with a normal commit.

Init detects whether the project already has code.

**Existing code.** For each repository, it inventories the code in a read-only clone
(`fx code open --repo <name>`, `fx scan --repo <name>`), reads it in depth (module map, data model, API, roles and sensitive data, UI, tests and
how test data is built), learns the **code style** from real files and linter
configs (`fx style-sample --repo <name>`) into `knowledge/<repo>/style.md` with excerpts, and learns from git
history, merged PRs, finished Jira issues and the Confluence pages you pick.

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
logic of every feature, in the team's feature-logic-docs format (a list of
modules, `shared.md`, one folder per module with its feature table, its rules and
the only parameter table, one file per feature: inputs, back end, front end,
navigation, a Mermaid flow, open points). fx keeps both tables with `fx logic
index`, so no index outgrows the file limit, and records every change in
`logic/history.md`. Every rule cites the code that
implements it and the test that covers it (or "no test"), and `fx logic check`
verifies both and the format. Back-end/front-end differences and anything the
code does not decide become open points or questions, never guesses. In each
job the spec is compared with the logic: where the issue says something else,
the factory comments on the issue and asks you whether it is a change of logic
or a mistake in the issue. The builder stages the updated logic files with the
code; they go into the pull request description and are applied to `logic/`
once the pull request is merged, after your approval. Logic whose cited code
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
/auto-factory:code <jira link or key> [more]    run issues side by side (space or comma separated)
/auto-factory:code task "<what to do>"          a job without Jira
/auto-factory:code PROJ-123 --best-of 3         3 builders in parallel, a judge picks one
/auto-factory:code audit                        deep security audit (Cloudflare security-audit skill)
/auto-factory:code resume PROJ-123              continue a stopped job
/auto-factory:code rework PROJ-123 "..."        send the PR back to the builder
/auto-factory:code lessons                      learn now: lessons and knowledge updates
/auto-factory:code status                       all jobs
/auto-factory:code capacity                     what the machine has, which steps run, which wait and why
/auto-factory:code cleanup PROJ-123             delete a job's leftover clones (never unpushed work)
/auto-factory:code init                         set up or refresh the factory, add a repository

/auto-factory:review [<MR link or KEY>]         review a colleague's merge request against its ticket
```

An issue that needs several repositories is still one job (see **A project of several
repositories**).

`audit` runs Cloudflare's open-source
[security-audit skill](https://github.com/cloudflare/security-audit-skill)
over the whole repository (it asks before installing it and warns about the
usage cost), reports the confirmed findings, turns them into lessons, and lets
you pick findings to fix as factory jobs, each ending in its own PR.

## How it learns

After each job (or once per run, or only on `/auto-factory:code lessons`: see
`learning.ask_after`) the factory looks at what it can learn from:

- its own reviewers' blocking findings and failed gates;
- your rework notes;
- review comments on each of the job's pull requests (GitHub with `gh`, GitLab with `glab`);
- from `/auto-factory:review`: what you say it missed; what people said and pushed on the merge
  request after it, read until the merge request is merged or closed; findings you did not post
  or the verifier disproved; and those that were posted and then fixed;
- commits you or a teammate pushed to a PR branch after the factory's last
  push: the corrections a human had to make.

Pull requests of earlier jobs are read again until they are merged or closed,
so comments and edits made days later still count. From these it proposes new
lessons, rewordings and removals; you choose what to keep. Every lesson gets an
id (`L12`); reviewers cite it when a finding breaks a lesson, so a lesson that
keeps being broken is flagged (reword it or turn it into a lint rule, semgrep
rule or test), and one that names files or symbols the code no longer has is
flagged as possibly obsolete.

`lessons.md` stays short. Each lesson carries topic tags (`[code]`, `[test]`,
`[security]`, `[ui]`, `[data]`, `[spec]`, and `[review]` for what only the reviewers of a
colleague's merge request should read), and each agent gets only the
lessons for its topics plus the untagged ones. A lesson no job broke in 10 jobs
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
`/auto-factory:code rework` also brings in commits a human pushed to the PR branches before
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
  jobs/<KEY>/                     each job's files, reports and screenshots (git-ignored; on S3 with a jobs_store)
  jobs/_reviews/<KEY>-mr<iid>/    each review of a colleague's merge request (git-ignored; on S3 only with include_reviews)
  jobs/_reviews/_ticket-<KEY>/     several merge requests of one ticket, reviewed together (same)
  local/                          files every clone needs, such as .env, and baselines keys (local, git-ignored)
  baseline.json                   last selftest (local)
<project>-factory-work/<KEY>/<repo>/  the job's own clone of each repository (<KEY>/ alone with one), deleted once its branches are pushed
<project>-factory-work/_read-<repo>/  a short-lived read-only clone (init, intake, audit; fx code open)
each project repository           receives only factory/<KEY>-… branches and pull requests
~/.cache/code-factory/            the factory's own tools, cloned references and fetched baselines
  overrides/<KEY>.json            what a human allowed for that one job (commit and push)
~/.cache/code-factory/run/        this machine only: who holds which place and ports, what each kind of step used here
~/.config/code-factory/           this machine only, never in git: each project's keys folder (keys/<project>-<hash>/, fx where prints it),
                                  and this machine's own settings for sharing it (machine.json, written by fx capacity --set)
```

## Repository layout

```
.claude-plugin/marketplace.json
plugins/auto-factory/.claude-plugin/plugin.json
plugins/auto-factory/skills/code/     SKILL.md (/auto-factory:code), fx, mobile.py, desktop.py, factory-ui.js, factory-desktop.js, templates/;
                                      init.md, review.md, audit.md, install.md: the rules of those
                                      commands, read only when one runs, so a job does not carry them;
                                      jira-push.md, jira-fetch.md: what the small agents that update
                                      Jira and read an issue without a token follow
                                      sandbox/fx-sandbox.yaml: the VM where merge requests run
plugins/auto-factory/skills/review/   SKILL.md (/auto-factory:review): follows the rules of code/SKILL.md
                                      and code/review.md
plugins/auto-factory/skills/code/shared/  what the UI checks measure with: the screen comparer, the app
                                      runtime, the reading of a design package
plugins/auto-factory/agents/          factory-*: intake, spec-writer, builder, 4 reviewers,
                                      security verifier, approver, judge, librarian,
                                      knowledge reviewer, logic writer, planner,
                                      fit checker, review checklist,
                                      requirement reviewer
install.sh                            alternative install as personal skills
tests/                                the tools' tests (standard library only):
                                      python3 -I -m unittest discover -s tests
```

Bump `version` in `plugins/auto-factory/.claude-plugin/plugin.json`,
`skills/code/VERSION` and `skills/code/shared/VERSION` on every change so
installs pick it up.
