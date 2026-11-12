<!-- FACTORY_TODO: /factory init writes these files from the repository, its history and an interview with the team, then deletes this line. -->
# Project knowledge

Every factory agent reads this index first, then the files listed for its role.
Keep every file short, concrete, true, and pointed at real paths. Never write secret values (name the variable).
`fx knowledge check` verifies every cited path, line range and excerpt against the code.
How `fx knowledge check` and `fx logic check` read citations: a project path in backticks (`<path>` or `<path>:<from>-<to>`, relative to the project root) is checked to exist and the line range to fit the file; a fenced code block that starts within 3 lines after such a citation is an excerpt of that file and must appear in it in that order (indentation, blank lines and `...` lines are ignored). URL routes (`/api/x`), commands and globs are not checked; write identifiers like `res.setHeader` without a slash.

| File | What it holds | Read by |
|---|---|---|
| overview.md | What the system is, who uses it, how it fits with other systems | everyone |
| architecture.md | Layers, folders, the module map (every main module, what it owns, key files), patterns to follow | spec, builder, code, approver, judge |
| domain.md | Glossary, entities and their relations, where each lives in the code | everyone |
| rules.md | Business rules and invariants, as testable statements | everyone |
| conventions.md | Naming, errors, i18n, logging, commits, PRs | builder, reviewers, judge |
| style.md | How code here is written, per role (controller, model, component…), with real excerpts (`path:lines`) from the newest code; older patterns under "Legacy (do not copy)" | spec, builder, code, ui, judge |
| testing.md | Test kinds and folders, factories/fixtures/seeders, how to build related test data, how to run one test | spec, builder, code, judge |
| stack.md | How to build and review in this stack: commands, idioms, pitfalls | builder, reviewers, judge |
| api.md | API surface: routes or operations, auth, request and response shape | spec, builder, code, security |
| security.md | Roles and permissions, sensitive data, auth flows, do-not-log list | intake, spec, builder, security, approver |
| ui.md | Design system, components, layouts, accessibility, languages | spec, builder, ux, ui |
| environment.md | Setup, running the app, test accounts, checks, timings, known flaky tests | builder, reviewers |
| definition-of-done.md | What "done" means here; what always needs a human | everyone |
