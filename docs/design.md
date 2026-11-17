# tagteam design

Why the harness is shaped as it is, what it leaves out on purpose, and where it is
known to be incomplete. Operation is in [manual.md](manual.md); the rules agents follow
are in [AGENTS.md.template](../AGENTS.md.template), which also holds the term table.

## 1. Problem

A developer who uses several coding agents (Claude Code, Codex, optionally Gemini
through `agy`) runs into the same failures:

- A delegated session inherits the orchestrator's model by default, or does not run at
  all, so the model in use is not the model that was meant.
- Workers commit, push or start further workers on their own.
- A review by the same vendor that wrote the change shares its blind spots.
- Model names, efforts and availability change every few weeks, and per-model notes in
  prompts go stale.
- Instructions that only say "do not" are followed unevenly.

tagteam addresses these with a small number of mechanisms and keeps everything else
as plain text a person can read in a few minutes.

## 2. Principles

1. **One instruction file.** `AGENTS.md` is read by Codex directly and by Claude Code
   through an `@AGENTS.md` import. Skills hold procedures; they do not redefine terms.
2. **Enforce in code what text cannot guarantee.** Model and effort on new sessions,
   no worker commits, no worker delegation: one hook. Everything else is guidance.
3. **Honest about strength.** The hook and the launcher are accident checks. They catch
   the plain forms of a mistake; they are not a security boundary (section 5).
4. **Measure, then rely.** Platform behaviour used by the design (what a hook sees,
   whether a turn may open sessions, how the cache behaves) was tested on real
   installs. Statements the tests could not confirm are listed as gaps (section 10).
5. **Grades per model and effort.** The same model is a different tool at `low` and at
   `max`. A grade names a (model, effort) pair, and the router picks within a grade.
6. **No language model in the update path.** Collecting model lists is a script. A
   skill, run by the orchestrator, applies changes to the committed files.
7. **Fail open on internal errors, fail closed on rules.** A bug in the hook must not
   lock a session; a rule violation is denied.
8. **Rules that compensate for a model are labelled and removable** (section 8).

## 3. Scope

Does:

- Installs one rule file, one hook, two skills, worker definitions and four scripts into
  a project, and updates them without touching project policy or machine-local files.
- Chooses model and effort per role, substitutes another vendor when one is exhausted,
  and keeps review independent of the author's vendor.
- Runs a worker through another vendor's CLI and verifies the model that answered.
- Proposes model-list and benchmark updates on a weekly schedule.

Does not (by decision):

- No daemon, server, GUI or message relay between sessions. Codex sessions could not
  message each other in the tested version.
- No automatic change to `models.json`. A person or the orchestrator applies a proposal.
- No cost accounting. `grading.caps` are relative ceilings used to order candidates,
  not budgets.
- No sandbox or security boundary against a hostile worker.
- No adapter for CLIs of other companies as workers (section 10).
- No multi-user coordination beyond the `Push mode` setting in AGENTS.md.
- No uninstaller.

## 4. Components

| Component | Source | Installed at | Role |
|---|---|---|---|
| Rules | `AGENTS.md.template` | `AGENTS.md` | roles, cycle, verification, terms; `## Project policy` is project-owned |
| Claude import | `CLAUDE.md.template` | `CLAUDE.md` | `@AGENTS.md` |
| Hook | `harness/tagteam/role_guard.py` | `.claude/tagteam/` | role record and denials (Claude settings and Codex hooks both call it) |
| Router | `harness/tagteam/route.py` | `.claude/tagteam/` | model, effort and launch method per role |
| Launcher | `harness/tagteam/run.py` | `.claude/tagteam/` | one worker through another vendor's CLI, model check, exhaustion detection |
| Collector | `harness/tagteam/models_collect.py` | `.claude/tagteam/` | model lists, benchmark values, proposal file |
| Data | `harness/tagteam/models.json` | `.claude/tagteam/` | vendors, roles, models, grades, defaults |
| Skills | `harness/claude/skills/` | `.claude/skills/` | `cycle`, `model-update` |
| Worker definitions | `harness/claude/agents`, `harness/codex/agents` | `.claude/agents`, `.codex/agents` | fixed model and effort, limited tools |
| Installer | `install.py` | not installed | install, update, check, schedule |

The scripts use only the Python standard library and need Python 3.9 or newer. They
share one state folder (`<git common dir>/tagteam/`, else `<project>/.tagteam/`),
whose location only `role_guard.py` computes; the other scripts import it from there.

## 5. Enforcement

| Rule | Mechanism | What it does not stop |
|---|---|---|
| New session needs `model` and `effort` | PreToolUse on `mcp__ccd_session__start_session`: deny unless both are set; a call carrying only a `proposal_id` is allowed | any session opened through another tool |
| Workers do not commit or push | PreToolUse on shell tools: deny when the command matches `git commit` or `git push` in plain forms, including after `git -C <path>` or other leading options | aliases, variables, scripts, other tools |
| Workers do not delegate | PreToolUse: deny `start_session`, `Agent`, `spawn_agent` for a worker | a worker using another route to a CLI |
| Launcher workers do not launch workers | `run.py` refuses when `HARNESS_DELEGATE_RUN=1` | a session worker (the hook covers it, not the launcher) |
| Worker is identified | any line of the first prompt starting with `HARNESS ROLE: DELEGATE`, or `HARNESS_DELEGATE_RUN=1`, or running as a subagent; recorded once per session | tasks sent later by message cannot add the marker |
| Read-only reading | the `reader` definition lists no edit tools and tells the worker to run only read-only commands | its Bash tool is limited by instruction, not by the hook |

Notes:

- The hook runs for every session of the project, workers included, because the
  checkout is shared. The first prompt decides the role, and the launcher adds the
  marker itself. A background paragraph that the desktop app prepends to a worker's
  first prompt is why the marker may sit on any line.
- Internal errors in the hook exit 0 with a warning on stderr (fail open). A missing or
  broken Python installation is a different failure; `install.py --check` reports it.
- Claude Code settings reference the hook through `${CLAUDE_PROJECT_DIR}`. Codex hook
  commands run in the session's working directory, so the installer writes the guard's
  absolute path into `.codex/hooks.json` once and then keeps the file. Codex ties its
  trust to that path and to the hook definition, so the file is not rewritten on update.
- Review independence rests on the router (`--author-vendor`) and the orchestrator's
  duty to use it. There is no hook for it.

## 6. Roles and sessions

The orchestrator is the session you commanded; every other session is a worker. Three
measured facts shaped the cycle:

- A turn started by a worker's report cannot open sessions, but it can message an
  existing one. Hence the **session pool**: open the sessions a phase needs on a user
  turn, reuse them by message.
- A message sent to a session does not pass the prompt hook. Hence the marker belongs
  in the first prompt.
- The prompt cache of a reused session lasts one hour (`subagentPromptCacheTtl` is set
  to `1h` for subagents), and a fresh session costs on the order of 90,000 tokens
  before any work. Hence one session per role, kept for the mission.

Independent units may run in parallel in separate git worktrees; without git, one at a
time. The mission folder (`intent.md`, `spec.md`, `status.md`, design documents) is the
record that survives compaction or a new orchestrator session.

## 7. Grading and routing

### 7.1 Grades

`models.json` stores, for each model, a grade for every effort it offers. The rule used
to produce them is applied to the Artificial Analysis index at that effort:

| Grade | Index at least | Relative cost ceiling per task (USD) |
|---|---|---|
| S | 54 | 10 |
| A | 48 | 4 |
| B | 30 | 2 |
| C | below B | 0.2 |

The floors and ceilings are `grading.floors` and `grading.caps`, our own settings. The
benchmark values themselves live only in the machine-local `bench.local.json` and are
not committed. `grades` are the committed result of applying the floors, so routing
works without benchmark data; the collector reports `grade_changed` when fresh values
would give another grade.

### 7.2 Roles to grades

| Role | Grade | Read-only | Why |
|---|---|---|---|
| design | A | no | judgement is the product |
| implement | B | no | bounded units with a brief and checks |
| review | A | yes | needs the author's grade, other vendor |
| read | C | yes | bulk reading; conclusions carry `path:line` evidence |
| translate | B | no | vendors google, openai, claude in that order |

### 7.3 Choosing

The router goes through vendors in the host's order (or a role's own `vendors`
list, which wins), then among that vendor's
candidates at the role grade in a fixed order of preference (known cost within the
ceiling and highest index, then highest index without cost, then lowest cost above the
ceiling, then `defaults`, then the order in `models.json`; manual section 5.1). Only a
benchmark value at exactly the candidate's effort is used, because values at other
efforts would skew a comparison.

The order of models in `models.json` carries meaning: earlier means stronger within a
vendor. The router uses it to keep a native Claude session from outranking its
orchestrator and as the last tie-break. When the orchestrator's model or effort is
exceeded, the launch moves to `run.py`.

A candidate is usable only if the plan allows the vendor, the model is not hidden, and
the vendor CLI lists it. The `available` lists are written by the collector;
a vendor with no list is assumed to offer everything, which keeps an install usable
before its first collection.

### 7.4 Exhaustion

A usage-limit message from a run, or a manual mark, writes the vendor and an `until`
time to `exhausted.json`. While active, the router takes the same grade from the next
vendor and says so in the notice. If no other vendor can review, the review is reported
as incomplete (router exit 3) instead of relaxing independence.

## 8. Model-compensation rules

A rule written for one model's repeated mistake ends with
`[fix: <reason> / remove when <condition>]`. They are listed by
`grep -rn "\[fix:" AGENTS.md .claude .codex`. Handling:

- Add a rule only after the mistake has been seen, and add the removal condition at the
  same time.
- After a model change (the `model-update` skill lists them when the default route
  moves), test each condition. Delete a rule whose condition holds; keep the rest.
- A rule that fits no single model is not a compensation rule and has no tag.

The aim is that old caution does not accumulate: a rule nobody can explain is not kept
"just in case".

## 9. Model list maintenance

Weekly, a scheduled script asks each vendor CLI for its model list (Codex
`debug models`, `agy models`, Claude Code `/model`) and, with an Artificial Analysis
API key, fetches scores and prices for the registered models. It writes a proposal and
nothing else. The next orchestrator session sees one line about it and loads the
`model-update` skill, which applies each change kind by a fixed rule: automatic for
availability and benchmark values, held for deletions and for any change of a default
route that lacks two agreeing sources, a probe run and passing route tests.

Reasons: the update path needs no model and costs nothing between sessions; the
collector cannot change a route by itself; and the only committed file that changes is
`models.json`.

Claude Code lists model families, not versions, so Claude availability is known per
family. Data from Artificial Analysis must be credited; the README and the collector
carry the notice.

## 10. Known gaps

| Gap | Effect | Status |
|---|---|---|
| No `run.py` adapter for CLIs of other companies (for example xAI) | such models cannot be launched as workers; the collector can still compare their benchmarks through `aa_creator` | open |
| Claude `/model` output differs by plan | availability was verified on one plan only | open |
| macOS and Linux | installer, scheduler and hook not run there | not verified; checklists in [platform/macos.md](platform/macos.md), [platform/linux.md](platform/linux.md) |
| Git detection of commits is textual | a worker can evade the hook with indirection | accepted; review the diff |
| The hook does not know the vendor of a reviewer | independence depends on the orchestrator using the router | accepted |
| Codex sessions cannot message each other, and cannot commit in the Windows sandbox | the Codex host uses its own subagents and the launcher | accepted |
| `start_session` is the Claude Code desktop app's session tool | the session pool needs it; elsewhere, native subagents and the launcher remain | accepted |
| Worktrees do not initialize submodules | a brief must say so | documented in the `cycle` skill |
| Hook recovery needs git | a broken hook is repaired outside Claude Code | see manual section 9.1 |

Windows measurements are in [platform/windows.md](platform/windows.md).
