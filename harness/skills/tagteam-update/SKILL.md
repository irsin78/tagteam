---
name: tagteam-update
description: "Orchestrator procedure for updating a project's installed tagteam harness from its tagteam checkout - refuses while a worker runs, shows the new commits, moves the checkout by fast-forward only, runs install.py --update and --check, and commits the installer's output. Use it only when the user asks to update tagteam (or after a session-start line says an update is available and the user agrees). Do not use it in a worker, on your own initiative, or to change the harness by hand."
disable-model-invocation: true
---

# Update the installed tagteam harness

Terms are defined only in AGENTS.md (section 6). The user's instructions override this skill. The user calling this skill is
the approval for steps 1 to 9; ask again only where a step says so. Commands use `python3` (on Windows `python`); `<project>` is
the main work tree of this project, `<host>` is `claude`, `codex` or `agy`. Every command is written to work in sh and in
PowerShell: keep `'@{u}'` and each revision range in single quotes (PowerShell parses an unquoted `@{u}` before git runs). Codex and agy: the checkout is outside the workspace,
so its git and `install.py` commands each need an escalation request or an approval (hosts.md in the skill `cycle`).

## Steps

0. **Worker check.** If this session is a worker, change nothing; report "tagteam update requested" in NOTES and stop.
   Work in the main work tree: the machine-local record (step 1) is not in a linked worktree.
1. **Read the install record.** `.claude/tagteam/install.json` gives the installed template (`template.commit`, `describe`);
   `.claude/tagteam/install.local.json` gives `checkout`, the folder that holds `install.py` (relative: from `<project>`).
   If a file is missing, `checkout` has no `install.py` or `commit` is null, ask the user for the checkout path once and
   stop if they do not know it. Never clone. A project installed before these records existed is updated by hand once
   (`python3 <checkout>/install.py <project> --update`); this skill covers the updates after it.
2. **Nothing may be running.** Run `python3 .claude/tagteam/run.py --running`.
   Exit 9 (a launcher run is going) or 13 (a record or process could not be judged, so one may be going): stop, show the
   lines and say what to do (wait; or check the listed `runs/<id>`: a reused pid or a damaged record also blocks).
   Any other non-zero exit: stop and show the output. Exit 0 covers launcher runs only. List your own session pool
   (connected sessions on Claude Code desktop, open subagents on Codex) and ask the user once whether any other session works in
   this project or in the checkout; a yes ends the update. Both answers must be clear before step 3.
3. **Clean trees.** In `<project>`: `git status --porcelain -- .claude .codex .agents AGENTS.md CLAUDE.md .gitignore` must print
   nothing and `git diff --cached --quiet` must exit 0 (an empty index: the commit takes the index, so an unrelated staged file would
   go in). `git -C <checkout> status --porcelain` must print nothing. This keeps the update commit to the installer's output.
   Otherwise stop and name what is in the way.
4. **Show what changes.** `git -C <checkout> fetch --tags`. The target is `'@{u}'`: `git -C <checkout> rev-parse --verify '@{u}'`
   must succeed (else stop and ask). Stop and ask if `git -C <checkout> merge-base --is-ancestor HEAD '@{u}'` or
   `git -C <checkout> merge-base --is-ancestor <commit> '@{u}'` exits non-zero (local commits, a diverged branch, a reverted
   install). Show `git -C <checkout> log --oneline '<commit>..@{u}'` and `git -C <checkout> diff --stat '<commit>..@{u}'`, and say whether
   `harness/codex/hooks.json`, `harness/claude/settings.json` or `AGENTS.md.template` changed. No new commits: say the
   installed version is current and stop. Do not ask again before step 5.
5. **Move the checkout.** `git -C <checkout> merge --ff-only '@{u}'`; stop if it cannot fast-forward. If Project policy in
   AGENTS.md names its own way to update the checkout, follow that. A checkout that is a submodule of `<project>`
   also changes the submodule pointer, which belongs in step 8's commit.
6. **Install.** `python3 <checkout>/install.py <project> --update --yes`. On a non-zero exit stop and show the output and
   any backup path it printed; offer `git checkout -- <path>` for tracked files and the backup for `settings.json`.
   Do not undo anything yourself.
7. **Verify.** Show `git diff --stat` and say what changed: `AGENTS.md` only above Project policy, `.claude/settings.json`
   only tagteam's handlers, added or removed skills and agents. Pass on the installer's notice about other hooks.
   Run `python3 <checkout>/install.py <project> --check --host <host>` for every host the project uses: `claude` (for Claude
   Code desktop too), and `codex` if `.codex/hooks.json` exists, `agy` if `.agents/hooks.json` exists. If the Codex hooks differ, show the
   `--repair-hooks` preview, apply it with `--yes` only if the user agrees, tell them to re-trust the project and its hooks
   in Codex, and run the checks again. Show every WARN line to the user. Then judge:
   - An `ERROR` or `FAILED` line or a non-zero exit: stop. Show it, commit nothing, push nothing, say how to undo (step 6).
   - **Exempt commit** (AGENTS.md section 4): every host's check exited 0 and its `handler (<host>)` and `hook commands (<host>)`
     lines say `ok` (these lines exist for `claude` and `agy`; `codex` has the handler line only), none says `unverified`.
     `--live` is not required and WARN lines do not block.
   - Otherwise (a handler or hook-command line says `unverified`): the update may still be committed, but that commit is
     **not exempt**: tell the user so, and have it reviewed as usual (AGENTS.md section 4).
8. **Commit.** Only after step 7 passed (no ERROR, FAILED or non-zero exit). List the changed files with
   `git status --porcelain -uall -- .claude .codex .agents AGENTS.md CLAUDE.md .gitignore`, add exactly those paths (and the
   submodule pointer, if any) by explicit path, then check `git diff --cached --name-only` names exactly that set and nothing
   else; if not, stop and name the difference. `install.local.json` and backups are ignored files and stay out. Commit as
   `tagteam: update to <describe>` and copy the WARN lines into the message body (an exempt commit says so: "check passed
   on <hosts>"). The commit holds installer output and nothing else: a hand edit goes in its own commit, reviewed as usual.
   Push mode `solo` in Project policy: push to the existing upstream and say so; `team` or none: ask the user before pushing.
9. **Restart.** Tell the user this session still holds the old skills and instructions; new sessions use the updated harness.
