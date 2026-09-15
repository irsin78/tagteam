# Maintaining the harness template

This document is the guide used in the template repository when the harness
itself is modified. Ordinary work in a consuming project follows that project's
verification and release procedure. Installed files and optional features are
listed under [Copy targets](harness-manual.md#copy-targets).

## Relevant regression checks

Select the existing checks for the paths you changed. The list below is what is
available, not a checklist to run in full every time. The checks use stub CLIs
and temporary workspaces without model calls; the local-read check uses a
loopback HTTP server.

```sh
python -B -m unittest discover -s .claude/hooks -p 'test_*.py'
python -B .claude/hooks/test_stop_gate.py
python -B -m unittest discover -s .claude/scripts -p 'test_*.py'
bash .claude/scripts/test_launchers.sh --list
bash .claude/scripts/test_launchers.sh --group codex,claude
```

The launcher groups are `core`, `codex`, `claude`, `agy`, `evidence`,
`lifecycle` and `nongit`. Check the paths you changed; without an argument every
group runs. When the shared execution contract or a cross-group dependency
changes, run everything. Reuse a passing result whose inputs are unchanged, and
do not report a passing selected group as a full pass.

A change in guard behaviour follows the
[focused mutation recipe](../.claude/skills/verify-safety-guard/SKILL.md).
Wording cleanup is not a guard behaviour change. Required verification and
review conditions follow the
[verification rules](../.claude/rules/verification-tiering.md).

## Changing the installation layout

When the copy table changes, confirm host routing and installation dependencies
in a temporary adoption copy containing only those files. Keep the files shared
by hooks and launchers and the `test_host_routes.py` used by the installation
checks, and include `gen-codex-hooks.sh` only when the Codex hooks are
installed. Do not require features that were not selected.
`--template-dir` / `-TemplateDir` compares only the differences of files that
are actually installed and does not count an unselected optional feature as
DRIFT. Whether a file is mandatory is checked against the copy table and the
installation checks together. When a CLI integration is uncertain, run only that
path for real and compare the output and the report.

If you changed the registration or connection checks, run
`python -m unittest discover -s .claude/scripts -p test_install_checks.py` to
confirm missing and added events and the Claude connection regression. This
maintenance check reads only a temporary registration file and is not run
recursively from a consumer's installation check. `test_host_routes.py` checks
the installed connections; the test file above verifies separately that the
check catches omissions.

The authoritative source for current execution values and benchmark evidence is
[model bindings](../.claude/model-bindings.json). Figures from measurement
history are not used as the current version's speed or protection guarantee.

## Behaviour evaluation (observing real runs)

Some effects of instruction and policy changes do not show up in regression
checks. Choosing between delegation and direct handling, scope estimation,
continuing within a confirmed spec, and unfounded completion claims can only be
seen by observing real runs. Only when such a change is made, pick a few
representative tasks and run them. This is not a mandatory step for every change
or every commit.

- Keep an identical baseline copy per host and give the same task description.
  Keep the grading criteria and hidden checks outside the worker's write scope.
- Observe actual behaviour, not only the result code: which route it took,
  whether it implemented directly, where it stopped, which checks it ran, and
  whether the report matches the actual changes. Record the CLI version, model
  and effort, changed files, check results and elapsed time per run.
- Record failures and runs that could not be executed as results too. After
  fixing a configuration problem, re-run only the affected items.
- Keep code regression results, behaviour observed in real runs and
  expectations not yet confirmed apart. Do not claim model superiority or a
  speed gain from a handful of samples.
- Keep raw logs and experimental projects outside the template; summarise only
  the conclusions that led to a policy change in the related mission.

## Checking context and completion cost

When instructions change substantially or a consumer reports repeated delays,
compare on the same representative tasks. Distinguish the total volume of
installation documents, the instructions loaded at host start, and additional
reads during work. Claude rules without a path condition are loaded
automatically, so renaming them or moving them within the same folder does not
reduce load cost. Keep detailed procedures in the existing manual and skills and
read only the section that is needed. For Codex, check the entry instructions'
explicit read paths and the actual additional reads separately.

From the existing records, check the delay until the first task-related
command, total completion time, and retries and rework. Record whether the spec,
model, effort and verification scope are the same; when there is no token value,
report line and character counts only as volume proxies. TIMING only separates
launcher processing from CLI time; it does not fully measure the start-up
context or the parent's review cost. Usage counts are not a quality metric. There is no
fixed line-count cap, no per-commit consumer-run record and no per-task
performance check.
