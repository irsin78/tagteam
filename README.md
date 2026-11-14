# tagteam

A **harness template for solo developers** pairing Claude Code and Codex:
the user's session orchestrates; workers implement and cross-check. Copy
it into your projects to divide work, choose models and verify results.

The goal is to complete work that meets the required quality and safety bar
with less cost, time and user intervention. Models are chosen for the tier of
the task by performance, speed and cost, with implementation routed to workers.

## How it works

Roles follow [session-role.md](.claude/rules/session-role.md). Whether the host
is opened from a terminal, a desktop app or an IDE is not a routing criterion.
Projects that do not use Git run the same way.

| App you start | Orchestrates | Default implementation delegate |
|---|---|---|
| Claude Code | Claude Code | Claude worker (or the design author's vendor) |
| Codex | Codex | Codex worker (or the design author's vendor) |

Optional workers are declared in the bindings' `workers` list or local `workers_local` overrides and selected only when their requirements are met.

Split roles and cross-checks help catch a model's own assumptions and mistakes.

The orchestrator adjusts this default route to actual authorship and task risk,
and judges completion by outputs and verification. Reviews use a vendor other
than the author's; vendor separation alone does not guarantee correctness.

Implementation follows [Sessions and workers](docs/orchestration/delegation-matrix.md#sessions-and-workers).
Direct work is an opt-in project setting, off by default; its scope and
grounding requirements are in
[Direct work (off by default)](docs/orchestration/delegation-matrix.md#direct-work-off-by-default).

Work proceeds with its purpose and spec recorded under
`docs/missions/<purpose>/`. Once the user confirms the spec, implementation,
verification and fixes continue to completion. A small, low-impact standalone
change may proceed on a spec confirmed in conversation alone, without creating
mission documents. The document layout and procedure follow
[Mission operation](docs/missions/README.md).

## Core principles

- **Carry confirmed specs through to the end.** No stopping at every step;
  routine implementation decisions are made autonomously. New user decisions
  and stop requests are respected.
- **Choose the model that fits the task.** Benchmarks are the default
  evidence; only what repeatedly diverges in real use is corrected.
- **Measure efficiency to completion.** Not only model execution but delegation
  preparation, waiting, review, rework and user intervention count.
- **Focus on the protection and verification that are needed.** Preserving
  existing work, controlling permissions and checking actual results are
  handled in common. Hooks check limited facts; situational judgment belongs to
  the orchestrator and the adopting project. Blocking every risk completely is
  not guaranteed.
- **Leave per-project judgment to the project.** The template provides the
  shared execution structure and minimal guards; domain risk, tests and
  specific exceptions are decided by the adopting project.
- **Reuse through documented procedure.** Renaming files and filling in
  project settings are the normal adoption steps; the aim is adoption without
  editing harness internals.
- **Improve on observed change.** When models are actually adopted, failures
  repeat or performance shifts, the affected bindings and policies are
  re-reviewed.

## Adopting it

Copy the template and shared files into the project, install the instruction
files as `CLAUDE.md` and `AGENTS.md`, and fill in the project-specific items.
Connect apps and tools using the
[installation manual](docs/harness-install.md#installation).

- [Dependencies](docs/dependencies.md) — what to install per platform and what breaks without it
- [Copy targets](docs/harness-install.md#copy-targets) · [Support status](docs/harness-install.md#support-status)
- [Design principles and responsibility boundary](docs/design-principles.md) — model selection rationale, efficiency, adoption success criteria
- [Installation](docs/harness-install.md) — copy targets, platforms, hook trust and CLI updates
- [Launchers](docs/harness-launchers.md) — execution recipes, flags, reports and exit codes
- [Harness manual](docs/harness-manual.md) — configuration, operation and diagnosis
- [Runtime boundary and optional tools](docs/runtime-boundary.md) — default hooks, local reads, diagnostics and log cleanup
