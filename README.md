# tagteam

A small harness for one developer who works with several coding-agent CLIs on the same project. One session, the
*orchestrator*, splits the work. Worker sessions design, implement and read. A reviewer running on another vendor's model
checks the result. Only the orchestrator commits. Supported CLIs and their roles: [manual section
1](docs/manual.md#1-install-and-update).

What it adds to a project:

- One instruction file, `AGENTS.md`, read by every supported orchestrator CLI.
- A router that picks model and effort for each role from a grade table, so that a worker never silently runs on the
  wrong model.
- A launcher that runs another vendor's CLI as a worker and checks which model really answered.
- One hook that enforces the rules text cannot enforce: workers do not commit, push or open sessions, and a session is
  never opened without model and effort.
- Two skills: `cycle` (how the orchestrator runs a mission) and `model-update` (how a changed model list is applied).

It does not ship a service, a UI or a message relay (scope and known gaps: [docs/design.md](docs/design.md)).

## Before you install: warnings

- **Install and update overwrite harness-owned files**, in a plain run as well: `.claude/agents`, `.claude/skills`,
  `.claude/tagteam/` (including `models.json`) and `.codex/agents`; they also replace the `hooks` and
  `subagentPromptCacheTtl` keys in `.claude/settings.json`. Customizations there are lost. **Commit or back up your project
  first.** Put per-machine changes in `.claude/tagteam/models.local.json`.
- Kept as they are: `models.local.json`, `bench.local.json`, the machine-local `.codex/hooks.json`, other keys of
  `settings.json`, and your `AGENTS.md` from the heading `## Project policy` down. An existing `AGENTS.md` without that
  heading is kept whole under a new one. Full rules: [manual section 1](docs/manual.md#1-install-and-update).
- The git check in the launcher and the role hook are accident checks, not a security boundary ([design section
  5](docs/design.md#5-enforcement)).

## Requirements

- Python 3.9 or newer (standard library only) and git.
- At least one supported orchestrator CLI; the others are optional, and a vendor whose CLI is missing is skipped by the
  router. Which CLI can do what: [manual section 1](docs/manual.md#1-install-and-update).
- Windows is the verified platform. macOS was measured once and two defects found there were fixed (re-check pending);
  Linux is not yet verified; see [docs/platform/](docs/platform/windows.md).

## Quick start

Clone this repository, then install into your project (commands are written `python3`; on Windows use `python`):

    python3 install.py <project dir>
    python3 install.py <project dir> --check
    python3 install.py <project dir> --update

`--check` looks for the CLIs on PATH and feeds fake payloads to the installed hook; `--update` refreshes an existing
install. Then open the project in your orchestrator CLI and give it a mission; it follows `AGENTS.md` and the `cycle`
procedure. Fill
in the `## Project policy` section at the bottom of the installed `AGENTS.md` (verification commands, conventions, protected
scope, push mode).

Some CLIs need a step of their own before they can be the orchestrator: trusting the project's hooks (a CLI may skip new
or changed hooks silently until you do) and, in a sandbox, permission for git writes and `run.py` launches
([manual section 3.3](docs/manual.md#33-codex-as-the-orchestrator),
[hook trust and moved projects](docs/manual.md#12-moving-a-project)).
`python3 install.py <project dir> --check --host <cli>` checks files and hook handling (not trust); the optional `--live`
also runs the CLI once and uses tokens.

A weekly check of the vendors' model lists is optional: `python3 install.py <project dir> --schedule`.

## What is in this repository

| Path | Installed as | Purpose |
|---|---|---|
| `install.py` | (not installed) | install, update, check, schedule |
| `AGENTS.md.template`, `CLAUDE.md.template` | `AGENTS.md`, `CLAUDE.md` | the instruction file and a one-line import of it |
| `harness/claude/` | `.claude/` | hook settings, worker agent definitions |
| `harness/skills/` | `.claude/skills/` | the `cycle` and `model-update` procedures |
| `harness/codex/` | `.codex/` | hook settings, worker agent definitions |
| `harness/tagteam/` | `.claude/tagteam/` | `route.py`, `run.py`, `role_guard.py`, `models_collect.py`, `models.json` |
| `harness/tagteam/tests/`, `tests/` | (not installed) | unit tests |
| `docs/` | (not installed) | manual, design, platform notes |

## Documentation

| Read | For |
|---|---|
| [docs/manual.md](docs/manual.md) | install and update, daily use, router, launcher (exit codes, read-only limits), model updates, recovery |
| [docs/design.md](docs/design.md) | principles, scope, enforcement and its limits, grading, known gaps |
| [docs/platform/windows.md](docs/platform/windows.md), [macos.md](docs/platform/macos.md), [linux.md](docs/platform/linux.md) | platform notes |
| [AGENTS.md.template](AGENTS.md.template) | the rules themselves and the term table |

## Benchmark data

Optional. With a benchmark API key in the environment variable `TAGTEAM_AA_API_KEY`, install writes model scores and
prices to `.claude/tagteam/bench.local.json` (machine-local, git-ignored), and the weekly collector proposes updates.
Without a key, routes follow the `defaults` lists and the model order in `models.json`. Data source and its attribution
terms: [manual section 8](docs/manual.md#8-keeping-the-model-list-current).

## License

See LICENSE.
