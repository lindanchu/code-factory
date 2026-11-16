---
name: review
description: Review a colleague's merge request against its Jira ticket with the software factory - a checklist from the ticket, the code run in a throw-away sandbox, reviewers for the requirements, the code, security and the screens, and the findings posted only after the user confirms. Only when the user types /auto-factory:review with a merge request link or a Jira key, or with nothing to choose one.
argument-hint: [<MR link or Jira key>]
disable-model-invocation: true
---

# Review of a merge request

Arguments: `$ARGUMENTS`

This command is the software factory's review of a colleague's merge request. Its rules are the factory's own, kept
in one file with the code command, so that a review and a job never follow two versions of the same rule.

1. Read the whole of the code command's file: `SKILL.md` in the folder next to this one named `code`, that is
   `${CLAUDE_SKILL_DIR}/../code/SKILL.md` (installed with install.sh as personal skills, the folder is named
   `auto-factory-code` instead). It is long: read it in parts, to the end, before the first step.
2. Be the orchestrator it describes, for its command **/auto-factory:review**, with the arguments
   `review $ARGUMENTS`.
3. That file names its own folder with the variable CLAUDE_SKILL_DIR, written with a dollar sign and braces. It is the
   folder of that file: the path of this folder with its last part changed from `review` to `code` (or
   `auto-factory-code`). Write it that way, without `..`, so that fx runs as `python3 <that folder>/fx`, the command
   the factory's permission rules allow.
