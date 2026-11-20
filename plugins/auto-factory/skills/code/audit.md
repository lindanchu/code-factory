### `/auto-factory:code audit`

Part of the factory orchestrator's rules, kept apart from `SKILL.md` (in this same folder) so that a job does not carry it: everything in `SKILL.md` still applies. `${CLAUDE_SKILL_DIR}` here is the folder of this file and of `SKILL.md`, the same path `SKILL.md`'s commands use.


What is already vulnerable in the project's repositories. First the scanners, which cost
no usage allowance:

`python3 ${CLAUDE_SKILL_DIR}/fx audit-scan [--repo <name>] [--base [<repo>=]<branch> ...] [--no-app]`
runs every scanner over each repository's whole branch (its `base_branch` unless
`--base` names another; `--repo` for one repository) and its history, and writes
`scan.md` and one task file per rule at or above `security.block_severity` under
`jobs/_audits/`. Report `counts`, `to_fix` (repository, rule, severity, places), and what `missed`, `skipped` and
`notes` say was not checked. A secret in the history must be replaced by a human
first: say so. Then go to **Fix** below with `to_fix`.

Then ask whether to add a deep audit by agents, with Cloudflare's open-source
security-audit skill, which can find what no scanner rule describes:

1. If the `security-audit` skill is not in your list of skills, tell the user
   what it is (Cloudflare's multi-agent security auditor, MIT licence, needs
   Node.js; a full run uses many sub-agents and a noticeable share of the
   usage allowance) and ask with AskUserQuestion whether to install it:
   `npx skills add https://github.com/cloudflare/security-audit-skill --skill security-audit`
   (add `--global` if they want it for every project). After installing, the
   user must start a new conversation for the skill to appear; say so and stop.
2. Ask which profile to run (quick, standard, deep), and in a project of several
   repositories which of them. For each, open a read-only clone with
   `python3 ${CLAUDE_SKILL_DIR}/fx code open --repo <name>` and run the skill on the `path` it
   prints, following the skill's own instructions. It only reads code and
   never probes live systems.
3. Find its `findings.json` (the skill reports where it wrote its run), then
   `python3 ${CLAUDE_SKILL_DIR}/fx audit-import <path to findings.json>`. It writes
   `jobs/_audits/<time>/summary.md` and one task file per confirmed finding,
   and returns the confirmed findings sorted by severity (one import per
   repository's run). Then `python3 ${CLAUDE_SKILL_DIR}/fx code close` deletes the clones.
4. Report to the user: counts by status and severity, and the confirmed
   findings (severity, title, where), with the path of the full report.
5. **Lessons.** Draft general rules from the confirmed findings (like
   `/auto-factory:code lessons`), let the user choose with AskUserQuestion, and save the
   kept ones with `fx lessons save` (no `--jobs`).
6. **Fix.** Ask with AskUserQuestion (`multiSelect`, at most 4 per question)
   which confirmed findings the factory should fix. For each chosen one, ask
   the source branch of its repository, then
   `python3 ${CLAUDE_SKILL_DIR}/fx new --local "Fix: <title>" --issue-file <its task_file> --base <branch>`
   (with several repositories: `--repos <its repo> --base <its repo>=<branch>`) and run
   it through the pipeline, one at a time; each ends in its own pull request.
