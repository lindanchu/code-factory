### `/auto-factory:code install`

Part of the factory orchestrator's rules, kept apart from `SKILL.md` (in this same folder) so that a job does not carry it: everything in `SKILL.md` still applies. `${CLAUDE_SKILL_DIR}` here is the folder of this file and of `SKILL.md`, the same path `SKILL.md`'s commands use.


Check this machine for everything the factory uses and install what is
missing, with the user's consent. Run it right after installing the plugin,
whenever a tool is reported missing, and at the start of every `/auto-factory:code init`.
It works in any folder; inside a factory repository it also knows the
project's host (GitHub or GitLab) and whether UI checks are on.

1. `python3 ${CLAUDE_SKILL_DIR}/fx install`. It returns `tools`, one per tool:
   `name`, `need` (required, recommended, optional, or when it matters; the
   tool of every scanner in `security.required` is required, so gitleaks by
   default, also before a factory repository exists),
   `purpose`, `ok`, `version`, `detail`, `auto` (how fx can install it now
   without administrator rights: brew, download, pip, pipx, npm; empty when it
   cannot), `manual` (commands the user runs themselves) and, for gh and glab,
   `login`. Also `platform`, `tools_bin`, `jira.token_env` and, on Windows,
   `windows`. fx talks to S3 itself, so **Baselines on S3** needs no tool; `aws`
   is listed only with a `baselines` block, and required only for an AWS profile
   without static keys (SSO, a role).
2. Show the user one short table: tool, status (version, or what is wrong),
   what it is for, and whether it is needed. Translate `purpose`.
3. Jira: if `jira.token_env` is false, check whether your own tools include an
   Atlassian (Jira) connection. If neither, tell the user that Jira links need
   the Atlassian connection in Claude Code (or `JIRA_EMAIL` and `JIRA_API_TOKEN`
   in the environment); local tasks work without it.
4. If tools with `auto` are missing, ask with AskUserQuestion (`multiSelect`,
   at most 4 options per question, so split into several questions) which to
   install; required ones first, then recommended ones, all marked
   `(Recommended)`, each option saying what the tool is for. Then
   `python3 ${CLAUDE_SKILL_DIR}/fx install run --names <the chosen ones, comma-separated>`
   (long timeout: Playwright downloads a browser).
5. Show what was installed (`results`) and the new table. For what is still
   missing (no `auto`, or the install failed) and for every `login` that is not
   `ok`, give the user the exact commands from `manual` and `login.command` to
   run in their own terminal: fx never runs commands that need an
   administrator password and never handles tokens. Offer to check again
   (`fx install`) when they are done.
6. If `path_hint` is set, mention it once: fx finds the tools it installed by
   itself; the user only needs it to use them in their own terminal.
7. **The sandbox** for colleagues' merge requests and for jobs (/auto-factory:review runs nothing
   of a merge request without it, and a job stops before its setup): `python3 ${CLAUDE_SKILL_DIR}/fx
   sandbox status`. Not `ok` → tell the user in plain words: a Linux VM of its own (`fx-sandbox`,
   Lima; about 6 GB of memory while it runs, more with several jobs at once, 60 GB of disk, the
   first set-up takes 10 to 20 minutes) where each review and each job runs in a box that sees none
   of this machine's files or keys and reaches only the package registries, and only while it
   installs. Ask
   with AskUserQuestion whether to set it up now. Lima itself is the user's to install (`install`
   in the output); then `fx sandbox install`. Never install either without their yes. It is done
   once per machine:
   - macOS: `brew install lima`.
   - Linux and WSL (Windows 11's WSL2 has the nested virtualization it needs): first check
     `ls -l /dev/kvm`; missing on WSL → `nestedVirtualization=true` under `[wsl2]` in
     `%UserProfile%\.wslconfig`, then `wsl --shutdown`. Then give the user these to run in their
     own terminal (`sudo` asks for their password): `sudo apt-get install -y qemu-system-x86
     qemu-utils`; Lima's release tarball from https://github.com/lima-vm/lima/releases
     (`lima-<version>-Linux-x86_64.tar.gz`; download it yourself and check it against the
     release's `SHA256SUMS` first), `sudo tar -C /usr/local -xzf <it>`; `sudo usermod -aG kvm $USER`.
   - The new `kvm` group reaches only terminals opened after it: `fx sandbox install` says so when
     this one cannot use `/dev/kvm`. Have the user run `fx sandbox install` in a new terminal, or
     on WSL restart it (`wsl --shutdown`, which also ends this session) and run it yourself.
   - The VM does not start by itself after the machine (or WSL) restarts: `fx sandbox status`
     says `limactl start fx-sandbox`, which you may run once this terminal can use `/dev/kvm`.

Never install anything the user did not choose. A missing optional tool is
fine: the gate skips that check and says so. A missing tool that `need` calls
required for a scanner fails every gate: tell the user, and that the way out
is removing the scanner from `security.required` in `factory.json`.
