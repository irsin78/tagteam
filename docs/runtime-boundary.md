# Harness runtime boundary

This is the current contract. Older measurements describe the version they were
taken on and are not evidence of broader protection or of current performance.

## Default features

| Point of execution | Responsibility | Not part of the responsibility |
|---|---|---|
| SessionStart | Per-platform guidance and minimal session metadata | Network, usage lookup, server status, whole-file survey |
| UserPromptSubmit | Releasing this session's previous mission marker and announcing the current session ID | Classifying or refusing user requests, changing another session's state |
| PreToolUse | Clearly dangerous options of direct commands, delegate role, structured write targets | General shell interpretation, detecting every bypass, proving arbitrary code safe |
| SubagentStop (Claude) | Minimal record: delegate, working path, session | Output inspection, hashing all memory, completion verdict |
| Stop | Confirming the selected verification command; one continuation request for a registered incomplete mission | A mandatory closing ritual for every task, automatic proof of acceptance, endless resumption |
| External delegation launchers | Passing the role, checking explicit options, run state, change evidence, requested verification | Inferring user intent, blocking or undoing every external side effect |

Claude's Bash and PowerShell tool calls and its structured edits are wired to
the same guard. Codex uses the same guard, and the Stop behaviour answers in
that host's format. PreToolUse's `--host` argument is accepted for
compatibility with existing wiring; the direct-check verdict and the JSON
response are common to both hosts. The number of Python files or tests is not a
simplification target: the aim is fewer mandatory conditions, less unnecessary
execution, and less duplicated implementation of the shared checks that are
needed.

## Limits of the direct-command check

The guard inspects directly named Git and model-CLI options, a delegate setting
grant variables, explicitly named file edit targets and some basic shell writes.
It handles ordinary command separation, quoting and environment-variable
prefixes, but it does not reproduce shell execution semantics.

Variable evaluation, command substitution, heredocs, the inside of shell or
interpreter programs, script files, encodings, aliases and indirect path
references are outside the check. Only complete direct-command tokens before a
substitution or heredoc are inspected. The `commit` in
`git commit -m "$(...)"` is checked, but the argument's contents are not
interpreted for execution semantics and `git$(...)` is not guessed to be git.
Commands after an opaque construct, as in `echo "$(date)"; git push`, are also
outside this check. PowerShell-specific syntax is not interpreted generally
either. That the hook allowed such input does not mean its safety was
confirmed.

The host's permissions, and the isolation the project chose where needed, limit
execution. Launchers check the arguments they receive themselves. Change
detection is after the fact; it cannot undo an external action already
performed or replace isolation.

## Project protection policy

The adopting project states the files to protect, external actions and approval
conditions in its Project policy. That description alone changes neither host
permissions nor the guard's denials, so confirm them together at installation.

Claude's Read deny applies to tool paths; the optional WSL configuration's
denyRead applies to OS paths. Check that home credential paths are covered by
the broader directory exclusions on the WSL side, but do not make the two lists
textually identical. A project's `.env` and similar files are a separate scope.

- The default Claude `Read` deny for `.env.*` also covers `.env.example` and
  `.env.template`. If a public example must be readable, a person checks its
  contents and the secret-file scope, then narrows the original deny to what is
  needed. Do not recommend the exception of leaving the broad deny in place and
  adding an allow. The local read tool also excludes the `.env` family in code,
  so this configuration change does not permit it there.
- The direct Git command guard does not allow force pushes, including
  `--force-with-lease`, even for the orchestrator. It is currently unsupported,
  and neither the Project policy nor a general approval creates an exception.
  The delegate's commit/push prohibition also stays.
- A project's additional protection and release checks are attached to the
  existing rules. When required work conflicts with the default protection, the
  user decides the policy and support scope; the agent does not relax or bypass
  it automatically.

## Approval and completion

An approved orchestrator's harness edit needs no separate time-limited file. A
delegate's configuration edit is allowed only when the orchestrator has passed
an already approved scope to the launcher as HARNESS_ALLOW_CONTROL_PLANE=1. That
input is not itself evidence of user approval. A delegate cannot widen its own
permissions or commit and push; the actual scope of user approval is confirmed
by the orchestrator.

Ordinary work is judged complete by the actual output and the required
verification results. Only work that needs a completion hook, because it runs
unattended or the project requires it, writes the verification script path to
.claude/.stop-gate and uses harness-session.py finish. No separate completion
command is mandated for work without a marker.

A confirmed multi-item mission additionally registers a per-session marker
under `.claude/.mission-open/`. If the session ends while it is still active,
Stop asks once to continue. On the next end it leaves an incomplete warning and
passes. New user input releases only that session's previous marker, and an
orchestrator continuing the mission re-registers it. Stops, waits for a
decision and replaced requests record a reason and pass. Delegates and other
sessions are unaffected by the marker. A missing marker, a wrong completion
claim and inactive hooks are not detected. It reads neither the whole mission
nor the conversation log, and it does not replace the verification script gate.
Usage and limits follow
[mission operation](missions/README.md#recovering-a-multi-item-mission-from-an-early-stop).

The .delegation-log.jsonl of a native delegation only says who ran where. The
parent of a writing task confirms the actual changed files and verification
results. When an external launcher's Git or non-Git change evidence is
available, reuse it. Missing evidence or a failed check is not read as "no
change" or as success.

## Restricted file reads by a local model

Invoke it as follows from the selected project root. Git is not required.

~~~json
[
  {"path": "README.md", "start": 1, "end": 80},
  {"path": "src/app.py", "contains": "def "}
]
~~~

Save the above as inputs.json and write the question to answer in prompt.txt.

~~~sh
bash .claude/scripts/local-run.sh -i inputs.json -p prompt.txt
~~~

path is project-relative; start/end are inclusive 1-based line numbers;
contains is a literal search string, not a regular expression. start defaults
to 1 and an omitted end means to the end of the file. Large files are passed up
to the input character limit and flagged with INPUT_TRUNCATED.

- There is no arbitrary-command input (`-c`). It runs no commands or programs.
- Paths containing `..` components or outside the project, symbolic links and junctions, the .git and .env
  families, and the Read-excluded paths of the project settings.json are
  excluded from the input.
- The same file-access check applies to the input declaration and the question
  file.
- The read policy restricts explicit file selection; it is not an OS sandbox.
  It does not guarantee isolation against concurrent path swaps or hard links
  under other names.
- The address and model are declared in vendors.local.endpoint of
  model-bindings.local.json. One network request is made per use, and HTTP
  redirects to another address are not followed. Even when the server's name
  says local it is a LAN transfer, so confirm the project's data boundary.
- The original text is not copied into the caller's output or the request log.
  Only the summary and brief run results are stored under .claude/local-logs.
  The summary may still contain input content.
- Exit code 0 is a normal response, 1 an empty or incomplete response, 2 a
  declaration or connectivity availability problem, 4 an input or
  configuration error. A summary used for a required judgment is confirmed by
  the parent.

## Shared launcher parts and optional tools

Codex hook admission uses the installed engine's effective `hooks/list` trust
and enabled state for all configured project handlers; missing or unknown evidence
refuses the run. Codex/Claude require GNU timeout before foreground or detached
admission. A native Claude child still holds the active-writer slot if its parent
exits, subject to the existing PID/start-time identity check.

Claude's `-v` verifier bytes stay in parent memory. Changing/removing the original
fails integrity; execution consumes the captured bytes, not a writable snapshot.
The UTF-8 verifier runs from the project root through Bash `-c` with stdin closed;
use root-relative paths, not `BASH_SOURCE`. Invocation/argument-size errors fail
verification. This is not same-user OS isolation, nor
does it freeze files/dependencies the verifier reads.

The local read path does not take a workspace snapshot. A report's
`CHANGED: not measured` is not an observation of no change. When change
evidence is needed, confirm it separately in the adopting project.

codex-run.sh, claude-run.sh and agy-run.sh keep the per-app invocation,
permissions and result interpretation. launcher-common.sh shares configuration
change evidence, the common time limit and per-phase timing;
workspace-evidence.sh shares the before/after comparison of working files.
run-state.sh tracks the current run and prevents overlapping writes. A writing
run holds the OS lock only from reading the existing records to registering
`starting`. A registration race for a new run is returned as exit 5, and the
lock is not held while the model runs. The detached child of a scheduled run
waits up to 10 seconds for the registration lock and then takes over under the
same RUN_ID. A timeout is exit 5; errors such as a state store without lock
support are exit 4. Read-only runs are not subject to the lock. The protection
is limited to launchers using the same state folder and tree key; writes by
other tools are not blocked. The configuration hash reads the same file list in
one Python process. Run records are read in one batch too, skipping finished
history, and only starting or running candidates go through the existing PID
check and re-confirmation. Check targets, Codex hook-trust normalisation and
protection of the current run are not skipped for speed. The report's `TIMING`
and the usage of the optional regression checks follow the manual.

| Optional tool | When to use it |
|---|---|
| check-windows-aliases.ps1 / check-posix.sh | Installation, environment changes, diagnosing execution problems |
| check-codex-sandbox.sh | When a Windows Codex shell-execution diagnostic is needed. Incurs real model-call cost |
| gen-codex-hooks.sh | Installations that need absolute paths or a separate Python path |
| harness-stats.sh | Looking at cost and latency in existing run records |
| lane-sensitive.sh | When the project chose the hardened WSL isolation |
| harness-clean.py | Explicitly cleaning up old run records and logs |

The budget is decided in the order HARNESS_BUDGET, the local bindings' budget,
then normal. There is no automatic usage detection at start. A tight/exhausted
declaration feeds optional delegation cost and available routes; it is not a
basis for skipping required verification.

~~~sh
python .claude/scripts/harness-clean.py --days 14
python .claude/scripts/harness-clean.py --days 14 --apply
~~~

The first call is a preview. Only --apply deletes that project's old finished
records and log files, and it never deletes directories. When a run record is
running or unreadable, the logs are preserved. Starting an ordinary delegation
does not delete past logs. Evidence that is needed is kept separately under the
project's retention policy.

For commands with large output, the worker saves the log explicitly and checks
the exit code and the parts it needs. No automatic command rewriting and no
permission-request audit hook are provided. evidence.py handles the path,
concurrent writes and size limit of the default SubagentStop record and is
kept. There is no permission_log.py registration; projects that need auditing
decide it separately. Ignore entries for old logs and the maintenance cleanup
targets stay, to prevent accidental tracking.
