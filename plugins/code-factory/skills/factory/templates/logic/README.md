<!-- FACTORY_TODO: /factory init writes the logic of every feature from the code, the issues and the team, then deletes this line. -->
# Feature logic: <project name>

> This folder is the source of truth for the business logic of <system> (Jira project <KEY>).
> Every change of logic is checked against the file concerned here and recorded in it.
> It lives in the factory repository, never in the project's repository.

**Owner:** <name>

## Folder structure
```
logic/
├── README.md     index, conventions, overall log
├── shared.md     rules shared by the whole system
└── <module>/
    ├── README.md     module-wide rules and the parameter table
    └── <feature>.md  one file per feature
```

## Index
| Module | File | Feature | Ticket |
| ------ | ---- | ------- | ------ |
<!-- index:end -->

## Conventions
- Folder and file names: lowercase English, hyphenated.
- Module-wide rules go in `<module>/README.md`, system-wide rules in `shared.md`.
- Every parameter (durations, limits, lengths…) is written in one place only, normally the parameter table
  of the module README; other files link to it.
- UI text is quoted verbatim, in its original language.
- Each rule cites the code that implements it (`path:lines`) and the test that covers it (or "no test").
- A change of logic: update the file, add a row to its change history (old → new value) and to the overall
  log below; move a settled open point into the rules.

## Overall log
| Date | File | Change |
| ---- | ---- | ------ |
<!-- log:end -->
