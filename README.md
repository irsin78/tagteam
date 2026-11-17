# tagteam

A small harness for one developer who works with Claude Code and Codex (and
optionally Gemini through the `agy` CLI) on the same project. One session, the
*orchestrator*, splits the work. Worker sessions design, implement and read. A
reviewer from another vendor checks the result. Only the orchestrator commits.

What it adds to a project:

- One instruction file, `AGENTS.md`, read by both Claude Code and Codex.
- A router that picks model and effort for each role from a grade table, so that a
  worker never silently runs on the wrong model.
- A launcher that runs another vendor's CLI as a worker and checks which model
  really answered.
- One hook that enforces the rules text cannot enforce: workers do not commit,
  push or open sessions, and a session is never opened without model and effort.
- Two skills: `cycle` (how the orchestrator runs a mission) and `model-update`
  (how a changed model list is applied).

It does not ship a service, a UI or a message relay. See [docs/design.md](docs/design.md)
for the scope and the known gaps.

## Requirements

- Python 3.9 or newer (standard library only) and git.
- Claude Code. Codex (`codex`) and `agy` are optional; a vendor whose CLI is missing is
  skipped by the router.
- The session pool described in the manual uses the session tool of the Claude Code
  desktop app. Without it, native subagents and the launcher still work.
- Windows is the verified platform. macOS and Linux are not yet verified; see
  [docs/platform/](docs/platform/windows.md).

## Quick start

Clone this repository, then install into your project:

    python install.py <project dir>
    python install.py <project dir> --check

`--check` looks for the CLIs on PATH and feeds fake payloads to the installed hook.
Then open the project in Claude Code and give it a mission; the orchestrator loads the
`cycle` skill itself. Fill in the `## Project policy` section at the bottom of the
installed `AGENTS.md` (verification commands, conventions, protected scope, push mode).

Update later with `python install.py <project dir> --update`.

**Before you install or update, commit or back up your project.** Install and update
overwrite files the harness owns, in a plain run as well: `.claude/agents`,
`.claude/skills`, `.claude/tagteam/` (including `models.json`) and `.codex/agents`, and
they replace the `hooks` and `subagentPromptCacheTtl` keys in `.claude/settings.json`.
Customizations there are lost; put per-machine changes in
`.claude/tagteam/models.local.json`. Kept as they are: `models.local.json`,
`bench.local.json`, the machine-local `.codex/hooks.json`, other keys of
`settings.json`, and your `AGENTS.md` from the heading `## Project policy` down. If an
existing `AGENTS.md` has no such heading, the whole file is kept under a new
`## Project policy` heading below the template text, and the installer says so.

A weekly check of the vendors' model lists is optional:
`python install.py <project dir> --schedule`.

## What is in this repository

| Path | Installed as | Purpose |
|---|---|---|
| `install.py` | (not installed) | install, update, check, schedule |
| `AGENTS.md.template`, `CLAUDE.md.template` | `AGENTS.md`, `CLAUDE.md` | the instruction file and its Claude Code import |
| `harness/claude/` | `.claude/` | hook settings, worker agent definitions, skills |
| `harness/codex/` | `.codex/` | hook settings, worker agent definitions |
| `harness/tagteam/` | `.claude/tagteam/` | `route.py`, `run.py`, `role_guard.py`, `models_collect.py`, `models.json` |
| `harness/tagteam/tests/`, `tests/` | (not installed) | unit tests |
| `docs/` | (not installed) | manual, design, platform notes |

## Documentation

- [docs/manual.md](docs/manual.md): install, daily use, router, launcher, model
  updates, recovery.
- [docs/design.md](docs/design.md): principles, scope, enforcement, grading, known gaps.
- [docs/platform/windows.md](docs/platform/windows.md),
  [macos.md](docs/platform/macos.md), [linux.md](docs/platform/linux.md): platform notes.
- [AGENTS.md.template](AGENTS.md.template): the rules themselves and the term table.

## Benchmark data

Optional. With an Artificial Analysis API key in the environment variable
`TAGTEAM_AA_API_KEY`, install writes scores and prices from the free Data API to
`.claude/tagteam/bench.local.json` (machine-local, git-ignored), and the weekly
collector proposes updates. Without a key, routes follow the `defaults` lists and the
model order in `models.json`. Model benchmark data: [Artificial Analysis](https://artificialanalysis.ai/).

## License

See LICENSE.
