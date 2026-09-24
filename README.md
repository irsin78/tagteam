# tagteam

A **harness template for solo developers** in which frontier coding agents
tag-team: whichever one you start orchestrates, the other implements and
cross-checks. It currently pairs Claude Code and Codex. Copy it into your
projects as the shared structure for dividing work, choosing models and
verifying results.

The goal is to complete work that meets the required quality and safety bar
with less cost, time and user intervention. Models are chosen for the tier of
the task by performance, speed and cost, and work whose delegation gain is small
is handled directly.

## How it works

**The app you start the conversation in becomes the orchestrator.** Whether it
is opened from a terminal, a desktop app or an IDE is not a routing criterion.
Projects that do not use Git run the same way.

| App you start | Orchestrates | Default implementation delegate |
|---|---|---|
| Claude Code | Claude Code | Codex |
| Codex | Codex | Claude Code |

A single model that designs, implements and verifies can wave its own
assumptions and mistakes through. To reduce that, roles are split and the
result is cross-checked from another model's perspective.

The table is the default route. The orchestrator checks the actual designer,
implementer and task risk, adjusts who is delegated to and who reviews, and
judges completion by the actual output and verification results. When a review
is needed it picks a model from a vendor other than the author's. A different
vendor alone does not guarantee correctness.

Direct handling is chosen when the actual target code and interfaces have been
read, the change closes locally and the way to verify it is known. A request
that looks short or touches one file is not evidence. When the behaviour is
unclear, several modules' contracts are involved, or several new behaviours
interact, cross-vendor delegation is used. A change is not "small" because it
fits in one file, and a directly started change that grows is not extended on
the spot: the remaining implementation is delegated.

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
Connecting the apps and tools you use follows the
[installation manual](docs/harness-manual.md#installation).

- [Dependencies](docs/dependencies.md) — what to install per platform and what breaks without it
- [Copy targets](docs/harness-manual.md#copy-targets) · [Support status](docs/harness-manual.md#support-status)
- [Design principles and responsibility boundary](docs/design-principles.md) — model selection rationale, efficiency, adoption success criteria
- [Harness manual](docs/harness-manual.md) — layout, installation, hook behaviour, operation, checks
- [Runtime boundary and optional tools](docs/runtime-boundary.md) — default hooks, local reads, diagnostics and log cleanup
