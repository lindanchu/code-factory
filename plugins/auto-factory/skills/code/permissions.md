# Working out the permission table

You work out who may call each route of the project's app, so that fx can call every route as every kind of user on
the job's throw-away test stack and compare what happens with what should. Your prompt gives the fx command, the
factory repository, where the code is, and one of:

- `factory`: the whole table (at init, or on a factory that never had one);
- `job <KEY>` with the routes the gate named: rows for those routes only, in the job's staged logic;
- `ask <lines>`: what the base branch already gets wrong, or routes the table lacks, to settle with the user.

The table catches three kinds of mistakes: (A) someone not logged in opens what needs a login; (B) a lower role uses
a higher role's function; (C) a user or an API key reads or changes someone else's data (another project's, another
customer's), by changing an id in the URL.

## What to read

The routes (`php artisan route:list --json`, the router files, the controllers' attributes), the middleware and
guards on each, the policies and gates, the queries that limit data to its owner, the seeders and fixtures (which
accounts exist, with which role, owning which data), the knowledge's security file (roles and who may do what) and
the logic of each module.

## The human part: in `logic/`

Each module's `logic/<module>/README.md` gets a section `## Permissions` (in the knowledge's language: `## Quyền`),
in a project of several apps `## Permissions: <repo>`:

```
| Method | Route | guest | viewer | admin | key | Note |
|---|---|---|---|---|---|---|
| GET | /projects/{id} | no | own | yes | no | viewer: only the projects assigned to them |
| POST | /admin/projects | no | no | yes | no | |
```

- One row per method and route, exactly as the app names the route (`{id}` as in the router). Every route of the app
  has a row, the framework's own ones too.
- One column per kind of user, with the same names as in the technical part. A Note column is free text.
- A cell is `yes` (allowed on any data), `no` (refused) or `own` (allowed on the caller's own data only: its
  projects, its account, its key's project). `có`, `không`, `riêng` are the same words.

Write what the app **should** do. Where the code does something else, or you cannot tell, write what you believe is
right and put the row in your doubts: the user decides.

## The technical part: `stack/permissions.json`

(`stack/permissions-<repo>.json` in a project of several apps.) Never a password, token or key in it: commands read
them when they run.

```json
{
 "routes": "stack/dx php artisan route:list --json",
 "seed": "bash ../../<factory>/stack/roles-seed.sh",
 "snapshot": "...", "restore": "...", "fingerprint": "...",
 "who": {
  "guest": {"login": null,
            "deny": [{"prefix": "/api/", "status": [401]}, {"status": [302], "location": "/login"}],
            "csrf": {"from": "/login", "field": "_token"}},
  "viewer": {"login": {"command": "...", "header": "Cookie", "check": "/account"},
             "deny": {"status": [404], "same_as_missing": true},
             "csrf": {"from": "/account", "field": "_token"}},
  "key": {"login": {"command": "...", "header": "Authorization", "check": "/api/v1/me"},
          "headers": {"Accept": "application/json"}, "deny": {"status": [401, 404]}}
 },
 "requests": {
  "GET /projects/{id}": {"own": {"id": "1"}, "other": {"id": "2"}, "missing": {"id": "999999"},
                      "own_by": {"key": {"id": "3"}}},
  "POST /admin/projects": {"body": {"name": "Permission check"}, "type": "form"}
 },
 "not_app": {"GET /_ignition/health-check": "a development package; not served in production"}
}
```

- `routes`: a command that prints every route, as JSON (Laravel's `--json`, or a list of `"METHOD /path"`) or one
  `METHOD /path` per line. It runs in the app's folder with the app's environment, like every command here.
- `who`: each kind of user. `login` like the scanner's saved login (a command that prints the header's value last, the
  header, a check page that answers 2xx with it; login.md), `null` for the user who is not logged in. `deny`: what a
  refusal looks like to this user (`status`, and `location` for a redirect); `same_as_missing: true` when the app
  answers a refused id exactly like an id that exists nowhere (fx then compares the two answers, each without
  the values its caller sent: "No feature with id 5" tells nothing the caller did not say). When it differs by
  part of the app (the web sends to the login page, the API answers 401), a list of rules, each with the `prefix`
  of its routes and one last rule without a prefix: never widen one rule to cover both (`[302, 401]` lets a page
  that answers 401 by mistake through). `csrf`: the page to
  read the token from and the form field (or `header`), when writes need one. `headers`: what every call sends.
- `seed`: a script **in the factory** that adds what the project's seed lacks (an account per role, a second owner with
  its own data and key), through the app's own commands (artisan, tinker, its admin API). It runs on the job's
  throw-away stack after the project's seed. Never change the project's code for it. When the app has no way to
  create a role's account, answer `ask`.
- `snapshot` / `restore`: save the job's database and put it back (`mariadb-dump` to a file in the stack and back,
  `pg_dump`, a copy of the SQLite file). `fingerprint`: print one line that changes when any data changes, leaving
  out what changes on its own (sessions, logs, rate-limit counters, last-used and last-login times): fx checks that a
  refused write changed nothing and an allowed one changed something.
- Methods: GET, HEAD, OPTIONS and QUERY are called as reads; every other method as a write (the data must stay as
  it was when refused). A 501 is the test stack's web server not knowing the method: the cell is "not checked", not
  a mismatch.
- `requests`: for each route with an id, `own` (data the users with `own` own), `other` (data none of them owns),
  `missing` (ids that exist nowhere), and `own_by` where a user owns other data; a value that is no part of
  the route goes into the query string of that kind of data only (`"other": {"case": "13", "run": "1290"}`), so a
  link that needs `?run=` of its own result gets the right one, never one `allow_status` wide enough for both; for each POST, PUT and PATCH a
  `body` that is **valid**, so a refusal is never hidden behind "invalid data" (422); `type` `json` or `form`;
  `allow_status` when an allowed call answers something unusual; `no_change: "<why>"` for an allowed write that changes
  no data (a logout). Values come from the seed data.
- `not_app`: routes that are not the app's (a development package), each with why. The user confirms each.
- `none: "<why>"` alone instead of all this, for an app that serves no route of its own (it only calls another API).

## Try it

With a job whose app runs (`fx app up <job>`): `fx permissions try <job>` calls every cell and prints what is not as
the table says, the routes without a row and the rows without a route. A mismatch where the code is wrong is a
finding, not yours to fix: keep the table as it should be and report it. Fix the technical part until every other
line is gone. `fx app down <job>` when done. At init there is no app: fx selftest runs it on the base branch.

## Job mode

The gate named routes of the job without a row, or rows without a route. Read the ticket (`issue.md`, `spec.md`,
`qa-log.md`): when it says who may use the route, write the rows in the job's staged logic
(`jobs/<KEY>/logic/<module>/README.md`, the whole file as it should be after the job) and the route's requests in
the job's own `jobs/<KEY>/permissions.json` (same shape, only what the job adds; fx keeps it with the job until its
pull request is merged). When it does not say, answer `ask`. Never take who may call a route from what the code does:
the builder wrote that code.

## Answer

```
rows: <n> routes, users <names>
rule: <one rule in plain words, such as "a viewer sees only the projects assigned to them">
doubt: <METHOD /route>: <what the code does, what you wrote, the question for the user>
ask: <what you need from the user, in plain words>
```

At most 8 `rule:` lines, all `doubt:` lines (the user reads them, not the whole table), `ask:` only when you cannot go
on. Then nothing else.
