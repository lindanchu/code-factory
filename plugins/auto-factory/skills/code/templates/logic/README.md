<!-- FACTORY_TODO: /auto-factory:code init writes the logic of every feature from the code, the issues and the team, then deletes this line. -->
# Feature logic: <project name>

> This folder is the source of truth for the business logic of <system> (Jira project <KEY>).
> Every change of logic is checked against the file concerned here and recorded in it.
> It lives in the factory repository, never in the project's repository.

**Owner:** <name>

## Folder structure
```
logic/
├── README.md     list of modules, conventions
├── history.md    what changed when (fx adds a row for each change; no agent reads it to work)
├── shared.md     rules shared by the whole system
└── <module>/
    ├── README.md     its feature table, module-wide rules and the parameter table
    ├── open.md       the module's open points, conflicts and questions
    ├── history.md    older history moved out of the module's files
    └── <feature>.md  one file per feature
```

## Modules
| Module | Name |
| ------ | ---- |
<!-- index:end -->

Each module's README.md lists its features in its feature table. fx keeps both tables (`fx logic index`): one row
per module here, one per feature file there, so neither grows past the file limit.

## Conventions
- Folder and file names: lowercase English, hyphenated.
- Module-wide rules go in `<module>/README.md`, system-wide rules in `shared.md`.
- Every parameter (durations, limits, lengths…) is written in one place only, normally the parameter table
  of the module README; other files link to it.
- A limit that can stop tests or scanners (calls per minute, failed logins before a lock, a captcha, messages sent
  per hour…) is read by the code from an environment variable, named in the table's Env column with the value the
  factory's test stack runs with in Test value. One the code does not read so is MISSING, and stops every job until
  it is fixed.
- Who may call each route is in the `## Permissions` section of its module README (yes, no or own per kind of
  user). The factory calls every cell on its test stack in every gate; the user confirmed the table, and a new route
  gets its rows from its ticket.
- UI text is quoted verbatim, in its original language.
- Each rule cites the code that implements it (`path:lines`) and the test that covers it (or "no test").
- Every file stays short (12 KB by default, `learning.file_max_kb`): an agent reads the whole file. A longer feature
  is split into sub-features, each a feature file of its own.
- A change of logic: update the file and move a settled open point into the rules. No file keeps a change history:
  fx adds a row to [history.md](history.md) for every change it applies.
