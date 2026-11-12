<!-- FACTORY_TODO: /code-factory:factory init writes these files from the repositories, their history and an interview with the team, then deletes this line. -->
# Project knowledge

Every factory agent reads this index first, then the files listed for its role: the shared files below, and the files of each repository it works on, in `knowledge/<repo>/` (each with its own index, README.md).
Keep every file short, concrete, true, and pointed at real paths. Never write secret values (name the variable).
`fx knowledge check` verifies every cited path, line range and excerpt against the code.
How `fx knowledge check` and `fx logic check` read citations: a project path in backticks is `<repo>:<path>` or `<repo>:<path>:<from>-<to>` (the repository's name from factory.json, then the path from that repository's root); a project of one repository may leave out `<repo>:`. The path is checked to exist and the line range to fit the file; a fenced code block that starts within 3 lines after such a citation is an excerpt of that file and must appear in it in that order (indentation, blank lines and `...` lines are ignored). URL routes (`/api/x`), commands and globs are not checked; write identifiers like `res.setHeader` without a slash.

## Shared: the project as a whole

| File | What it holds | Read by |
|---|---|---|
| overview.md | What the system is, who uses it, its repositories and how they fit together, how it fits with other systems | everyone |
| domain.md | Glossary, entities and their relations, where each lives in the code | everyone |
| rules.md | Business rules and invariants, as testable statements | everyone |
| conventions.md | Naming, errors, i18n, logging, commits, PRs | builder, reviewers, judge |
| security.md | Roles and permissions, sensitive data, auth flows, do-not-log list | intake, spec, builder, security, approver |
| definition-of-done.md | What "done" means here; what always needs a human | everyone |

## Per repository: `knowledge/<repo>/`

| Repository | Index |
|---|---|
| (one row per repository: its name, and knowledge/<repo>/README.md) | |
