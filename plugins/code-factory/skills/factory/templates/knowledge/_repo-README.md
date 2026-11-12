<!-- FACTORY_TODO: /code-factory:factory init writes the knowledge of the repository {repo} from its code, its history and an interview with the team, then deletes this line. -->
# Repository {repo}

The knowledge of the repository {repo}. Citations name it: `{repo}:<path>` or `{repo}:<path>:<from>-<to>`.

| File | What it holds | Read by |
|---|---|---|
| architecture.md | Layers, folders, the module map (every main module, what it owns, key files), patterns to follow | spec, builder, code, approver, judge |
| style.md | How code here is written, per role (controller, model, component…), with real excerpts (`{repo}:path:lines`) from the newest code; older patterns under "Legacy (do not copy)" | spec, builder, code, ui, judge |
| testing.md | Test kinds and folders, factories/fixtures/seeders, how to build related test data, how to run one test | spec, builder, code, judge |
| stack.md | How to build and review in this stack: commands, idioms, pitfalls | builder, reviewers, judge |
| api.md | API surface: routes or operations, auth, request and response shape | spec, builder, code, security |
| ui.md | Design system, components, layouts, accessibility, languages | spec, builder, ux, ui |
| environment.md | Setup, running the app, test accounts, checks, timings, known flaky tests | builder, reviewers |
