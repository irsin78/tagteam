# Mission operation

A mission is the unit of work that completes one purpose agreed with the user.
Create a folder `docs/missions/<purpose>/` per purpose and carry the
investigation, design, implementation, verification and fixes for that purpose
forward as records inside it. Implementation code is edited at its original
location in the project. The execution rules for both hosts are defined
authoritatively in [mission-artifacts.md](../../.claude/rules/mission-artifacts.md).

## From start to completion

1. **Clarify the purpose**: write the problem, the desired outcome, the scope
   and exclusions, and the constraints in `intent.md`. If only a discussion or a
   read-only review was requested, keep to that scope.
2. **Investigate and write the spec**: check the existing code and
   requirements, and make the behaviour to build, the important choices, the
   completion criteria and the verification method concrete in `spec.md`. Do
   not start implementing on the purpose alone.
3. **User confirmation of the spec**: present a spec concrete enough for the
   user to judge the result, and get it confirmed. Record the confirmed scope
   and the conversation/date or review reference in `spec.md`. If the same spec
   was already confirmed in conversation, record that decision instead of
   asking for approval again. An agent review's PASS, a status value in a
   document, and time elapsed without a reply are not user confirmation.
4. **Continuous execution**: within the confirmed spec, make the implementation
   plan concrete and carry implementation, verification and fixes forward. When
   a step, a delegation or a review ends, check the result and proceed to the
   next item. Do not end the turn or wait for the user's "continue" merely
   because a progress report was given.
5. **Completion**: confirm the confirmed completion criteria with the actual
   changes and verification evidence. Record the result and important
   limitations, and update the relevant manual when behaviour or usage changed.
   If required verification cannot be performed, leave the work incomplete.
   Commits and pushes follow the existing scope of approval.

When the spec changes, an action falls outside the approved scope, or an
important problem needs the user's choice, lay out the options and their impact
and get confirmation. Work that does not depend on that answer may continue.
Respect a user's stop request immediately, and when the necessary permission,
environment or information is missing, record the current state and the
conditions for resuming. Do not blindly retry the same failure.

## Small changes that need no mission documents

A small, limited-impact, grounded standalone change may proceed on the spec
confirmed in conversation alone, and no new mission folder is needed for it.
"Grounded" here means that the actual target code and interfaces have been read
so that the scope of the change and the verification method are known; a short
request or the impression that only one file is involved is not grounding.

- Work already in progress as a mission continues inside that mission. Do not
  move an ongoing record into the conversation on the strength of this
  exception.
- Work that spans several sessions, needs its scope or completion criteria
  re-confirmed, or is hard to undo keeps intent, spec and state as persistent
  records.
- A spec confirmation already received in conversation stays valid. Do not ask
  for approval again because it is being written down, and do not stop
  automatically at the end of every step.
- When a small change grows while in progress, do not extend it on the spot;
  re-organise the remaining implementation and judge whether a mission record is
  needed.

## Recovering a multi-item mission from an early stop

When executing a confirmed spec across several items, register it from the
project root with the current session ID shown in the `HARNESS MISSION SESSION`
output of UserPromptSubmit. Do not register simple questions, spec drafts or a
single small fix.

```sh
python .claude/scripts/harness-session.py mission --session <id> --state active --mission docs/missions/<purpose>
```

The existing Stop hook reads only that session's marker under
`.claude/.mission-open/`. If it is still active, it turns the stop back once so
that the actual remaining work continues. On the next stop it passes with a
`MISSION_INCOMPLETE` warning. That means automatic recovery is used up; it is
not a completion verdict. It does not search whole mission or conversation logs
and does not call a separate model. The existing mission documents are the
authoritative source for per-item progress, acceptance criteria and verification
evidence.

After the completion evidence is recorded, use the same command with
`--state complete`. For a user stop use `--state paused --reason "user stop request"`;
for an actual decision, permission or mandatory-dependency wait use
`--state needs-input --reason "the decision needed and the resume condition"`;
for a replaced request use `--state switched --reason "the changed request"`,
and tell the user the reason. Registering and releasing do not replace approval
or verification, and committing or reporting some items is not a reason to
release.

New user input releases only that session's previous marker, so that an old
mission is not forced onto a stop or a change of topic. Re-register when
continuing the existing mission; a simple status question does not cancel the
existing work. Markers of other sessions and delegates are unaffected. After
registering, re-registering before the next request does not reset the
automatic recovery count.

`.stop-gate` remains a separate verification-script gate. Releasing a mission
does not release the verification gate. In an environment without hooks,
`python .claude/scripts/harness-session.py finish --session <id>` can check the
registration state, but that command cannot automatically resume a turn that
has already ended.

There is no guarantee against an unregistered marker, a wrong completion claim,
untrusted hooks, or an app/API interruption. An unreadable marker is reported as
`MISSION_UNKNOWN` and does not hold the conversation. Markers are local
temporary state, so they are excluded from Git, and a new session re-registers
based on the existing mission documents. An existing copy must also merge the
UserPromptSubmit wiring of both hosts, and Codex must re-trust the changed
`.codex/hooks.json` in `/hooks`.

Host contracts: [Claude Stop and UserPromptSubmit](https://code.claude.com/docs/en/hooks),
[Codex Hooks](https://learn.chatgpt.com/docs/hooks). Whether the real app wires
and trusts the hooks automatically, and the inspection of hook JSON input and
output, are separate matters.

## Folder and documents

```text
docs/missions/
  <purpose>/
    intent.md       # why, and what is to be achieved
    spec.md         # behaviour, constraints and completion criteria confirmed with the user
    plan.md         # when needed: work order, dependencies, ownership, checks
    state.md        # when needed: current situation, next work, blockers, verification evidence
```

Use a short folder name that identifies the purpose. Distinguish with an issue
number or similar only on a collision. Sub-tasks, bug fixes and session
switches needed for the current purpose use the same folder. An independent
purpose with its own completion criteria is handled as a new mission, and new
work the user did not request is not started merely because it was discovered.
A related defect found after completion may continue in the same mission at
the user's request when it falls within the existing purpose and spec.

Large work may add `design.md`, `roadmap.md`, per-phase sub-folders and so on.
The number of documents is not limited, and a fixed document set is not forced.
Per-phase material references the parent `intent.md` and `spec.md` and does not
require separate approval, a stop or a session restart. The same fact is not
duplicated across documents; choose a reference document and link to it.

The simple starting format for `intent.md` and `spec.md` is as follows. The
level of detail of each item and the completion check commands are decided by
the adopting project; short work is recorded briefly.

```markdown
# Intent: <purpose>

## Problem and desired outcome
<why it is needed and what should improve>

## Scope and exclusions
<what this purpose includes and what it does not>

## Constraints
<conditions set by the user and existing decisions>
```

```markdown
# Spec: <purpose>

## Behaviour and important decisions
<what result must come out for which input or situation>

## Completion criteria and verification
<the observable result and how it is confirmed>

## Open items
<content that needs the user's judgment; "none" if there is none>

## User confirmation
Status: draft
Confirmed scope and evidence: <record of the conversation/date or review reference after the user confirmed>
```

`plan.md` holds the actual work order, file scope, dependencies, ownership and
verification method. Reversible implementation choices and plan changes within
the confirmed spec are handled by the agent. `state.md` records current/next/
blockers and the necessary verification evidence. Git projects record the base
HEAD and uncommitted changes; non-Git projects record the baseline state and
change history of the relevant files. Instead of appending a log every time,
update it to the latest summary the next person can continue from. When no
separate state file is needed, a short progress or completion note in the spec
is enough.

## Delegation, review and resume

The app that was started stays the orchestrator and hands the relevant
`intent.md`, the confirmed `spec.md`, the plan that is needed and the actual
verification evidence to implementers and reviewers. The principles of
cross-verification by author and of checks proportional to risk still apply.
Confirm not only that the implementation plan was followed but that the
original purpose and spec were met. An agent review is not mandated for every
document.

An external plan review follows the conditions in
[plan-check-gate.md](../../.claude/rules/plan-check-gate.md). That review does
not replace the user's spec confirmation, and the review-round limit does not
mean stopping other approved work. Simply writing up an already confirmed spec
needs no re-approval.

The same mission continues after compaction, a session change or a delegation.
A new session reads the host instructions, the current purpose and confirmed
spec, and the plan and state it needs, and compares them with the actual HEAD
and uncommitted files. A spec changed by the user's follow-up instruction is
also passed to the relevant workers. Do not read the whole mission history every
time or pull all of it into the root instructions. Ask the user for the
necessary information only when the relevant mission cannot be identified, and
do not make a global current-work pointer mandatory.

## Preserving records and existing material

Intent, spec, plan, state, and the summaries of necessary decisions and
verification are preserved as the project's persistent records. Git projects
track them together with the code; non-Git projects keep the same documents and
follow the project's backup and sharing practice. Do not initialise Git in order
to use missions. Raw logs and temporary material go into each mission's `logs/`
or `scratch/` or the existing local log folder, excluded from tracking. Keep
only the summarised verification evidence that may be shared in the repository.
Completed missions are preserved as history; the authoritative source for
current usage stays in the manual.

New work does not create `.planning/` or use it as progress state. An existing
`.planning/` is left as untracked past material. Only when switching an
incomplete purpose, move the necessary intent, decisions and verification
evidence into that mission and make the latest spec and its confirmation status
clear. Do not record subsequent progress in both places at once. Do not move
old raw logs and personal settings into tracking as they are, and do not turn a
spec without an approval record into an approved one.

When adopting the template, copy this guide and the shared rules but exclude
the template repository's actual mission folders. The adopting project creates
its own per-purpose folders.

## Official guidance and its scope

Anthropic's [capturing intent](https://academy.claude.com/courses/ai-native-sdlc-playbook/capture-intent),
[design](https://academy.claude.com/courses/ai-native-sdlc-playbook/requirements-and-design)
and [implementation plans and autonomous execution](https://academy.claude.com/courses/ai-native-sdlc-playbook/plan-mode)
were consulted. The original links intent, design and plan and lays out a
procedure in which a person approves the spec and the implementation plan. The
official example location is the repository's `intent/`; `docs/missions/` is
this template's choice.

This template takes the user's spec confirmation as the moment execution is
approved, and writes the detailed plan within that scope autonomously. A project
that also needs plan approval presents the plan at spec confirmation or puts that
approval condition in its project policy. Documents alone change neither CLI
permissions nor session lifetime limits, and no hook-based mission state manager
is added.
