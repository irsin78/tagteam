# Design principles and the scope of the template

The purpose of this project is to keep the app the user started as the
orchestrator while completing work that meets each project's required quality
and safety bar with less cost, time and user intervention. Delegation, hooks and
verification rules are means to that end. Installation and execution procedures
follow the [harness manual](harness-manual.md).

## Premises and the responsibility boundary

Solo development is the default. The template provides an execution structure
and minimal guards reusable across projects. It does not define every situation
and every domain-specific exception as a shared rule. Whether the user starts in
a terminal, a desktop app or an IDE, the app that was started orchestrates.

| What the template provides | What the adopting project decides |
|---|---|
| Separation of orchestrator and delegate roles | Per-project division of work |
| Missions per purpose and continuous execution after spec confirmation | Concrete specs, acceptance criteria and extra approval conditions |
| Default model selection, delegation and fallback | Overrides of models and task tiers |
| Preservation of existing work and control of permission expansion | Directories, data and operating environments to protect |
| How results are reported and linked to verification | Actual build, test and completion criteria |
| Bounded retries and failure reporting | Domain-specific risk and extra approval conditions |
| Configuration extension points and the adoption procedure | Project-specific exceptions |

When adding a shared rule, judge whether several projects need it and whether it
is needed to keep the harness's own role, delegation and verification contract.
If neither holds, handle it first in the adopting project's configuration. A
problem in one project does not automatically become a shared rule. The aim is
to respond through the instruction file's project items and local settings
without changing harness internals. Defects in the shared contract itself are
fixed in the template.

Per-vendor subscriptions and quotas are declared in local settings. Using
another vendor's quota is not always cheaper or faster. Local inference is
optional; the model, address and hardware are decided by the adopting
environment. A LAN server means data can leave this host, and local execution
by itself is no guarantee against information leakage or prompt injection.

## Cross-verification and role selection

A model that handles design through implementation and verification can carry
its initial assumptions all the way and miss errors. That is why, by default,
implementation is handed to another vendor and roles are split so the result is
reviewed from another perspective. A different vendor does not guarantee
correctness; the actual output and the project's acceptance checks are the
basis for judging completion.

The app that was started decides the orchestrator. Concrete roles are adjusted
by who actually authored what. If an external model produced the design,
distinguish that designer from the implementer; after direct implementation or a
fallback, request a review from a vendor other than the actual implementer.
Design review and code review examine different targets, so name the author of
each target. If several vendors co-authored, include them all. The router
selects from the declared authorship and does not itself prove the actual
authoring history.

Direct implementation applies to local changes whose actual target code,
interfaces and verification method have been confirmed. It is not decided by the
length of the request or the expected file count; when the scope grows, preserve
the existing change and switch to delegation. Verification proportional to risk
is kept. For work that needs review, the author's self-assessment does not count
as an independent review. If the required independence cannot be obtained, state
that limitation, and if the review is mandatory leave the work incomplete.

## Executing against purpose and spec

The reference for work is the intent and the spec confirmed with the user.
Large work, or work spanning several sessions, is recorded under
`docs/missions/<purpose>/`. A small, grounded, low-impact standalone change may
proceed on a spec confirmed in conversation, and existing mission work keeps its
record. With only a purpose, investigate and make the spec concrete. Once the
user has confirmed the spec, carry planning, implementation, verification and
fixes through to completion within that scope. Do not ask for re-approval
because already confirmed content was written up, or because a phase,
delegation or review finished.

Large work may add plan, state and design documents and per-phase material. The
document structure exists to support execution; a document or phase boundary is
not an automatic stopping condition. When a spec change or a new user decision
is needed, present concrete options and their impact, and continue the work that
does not depend on that decision. Explicit stops and missing mandatory
conditions are respected.

Intent, spec, and the necessary decisions and verification summaries are
preserved the project's way: Git projects track them, non-Git projects follow
their backup and sharing practice. Raw logs are kept separate. A completed
mission is the record of why something changed; the authoritative source for
current behaviour and usage is the manual. Document examples, resume and
switch procedures, and the differences from Anthropic's original guidance follow
[Mission operation](missions/README.md).

## Basis for model selection

The benchmark scores, cost and latency, sources and measurement dates in
[model-bindings.json](../.claude/model-bindings.json) are the basis for the
default bindings. There is no need to build a new data collection system or a
complex composite scoring formula for the initial choice.

The criterion is: among models likely enough to finish the task properly, the
one with the better expected cost and time to completion. When no candidate
leads on capability, price and speed at once, weigh the task's quality
requirement, risk and budget together. A low per-call price is inefficient when
retries are many, and a stronger model that finishes in one pass may be the
better deal.

| What is being judged | Role of the benchmark JSON |
|---|---|
| Capability, price and speed across models | Default evidence; check the measurement conditions and sources with it |
| Candidates per task tier and promotion order | Basis for the initial choice |
| Actual success rate in a specific project | Not guaranteed directly |
| Whether delegation or direct handling is better | Needs a separate judgment: spec writing, waiting and review cost are not included |

Set the default bindings from benchmarks and correct only what repeatedly
diverges in real use. When a model that looks suitable keeps failing, or the
start-up wait of a small delegation is excessive, check completion time,
verification results and retries in the existing launcher records. When the
records are not enough, add only the information that problem needs. Do not
settle a ranking on call frequency or a single success; keep project-specific
differences in local overrides.

The authoritative source for current models, efforts and per-host routes is the
JSON. Do not duplicate the evidence as numbers in this document. Measured
records are in the manual's
[measurement appendix](harness-manual.md#appendix-measured-route-profiles-measurement-date-per-tag-update-tags-when-remeasuring).

## Delegation and efficiency

Autonomous progress, asking the questions that are needed, handling conflicting
instructions and absorbing additional requirements are shared principles of
both hosts. Complete the work within the agreed scope, and when the user asked
only for discussion or review, keep to that scope. Detailed execution rules live
in `session-role.md`. A new model's guidance is used as evidence to complement
existing rules; behaviour principles useful for every model are distinguished
from features supported only by a specific app or model.

Efficiency means reducing cost, time, user intervention and rework to
completion while meeting the required quality and safety bar. Speed alone is
not the absolute criterion, and costs in different units are not summed into a
single number without basis.

Delegation carries costs beyond model execution: writing the spec, start-up
waiting, passing context, reviewing the result and requesting fixes.
Implementation is delegated in principle, but small, clear work whose handover
cost is hard to recover is handled directly. Related changes are grouped into
units the worker can verify on its own, and independent work is parallelised
when the gain exceeds the coordination cost. Repeated delegation without fixing
the cause of failure is stopped. File and line counts are reference signals for
judging the unit of work.

## Scope of safety and verification

The core protected targets are the harness's shared execution contract: loss of
existing work, unauthorised commits and pushes, permission expansion and
unverified completion reports. Guards are placed on controllable actions, and
when a guard did not run or verification failed, that is surfaced. Fully sealing
off every risky action and every variant of prompt injection with the harness's
own hooks is not the goal.

Verification prioritises actual outputs and repeatable checks, and its depth is
set by the impact of failure. Verify-and-fix loops need a termination condition,
and work that did not meet required verification is not reported as complete. A
condition that cannot be substituted, such as an independent review, is not
faked through a fallback; the reason for incompleteness is stated.

The benefit of a rule is judged by the harm prevented and the evidence that it
occurs; its cost by delay, false positives and maintenance burden. A rule that
frequently blocks ordinary low-risk work is also a candidate for re-review.
Reducing or merging rules is an improvement too. Keep the agreed protection and
quality bar, but choose the efficient means among those that meet it. Do not
extend the protection scope without end.

## Confirming adoption success and effect

Renaming files, writing project TODOs and per-machine settings are the normal
template adoption procedure. The existence of installation work is not itself a
failure.

| Area | Adoption success criterion |
|---|---|
| Installation | Usable through the documented copy and configuration procedure, with no modifications required beyond what is documented |
| Role selection | The app that was started orchestrates, and an invoked worker does not become an orchestrator again |
| Task completion | A representative task is delegated and the parent confirms the actual output and verification results |
| Continuous execution | No implementation before spec confirmation; after it, progress to completion without per-step re-approval |
| Resume | The same mission's purpose, spec and confirmation record are preserved and compared with the actual code state |
| Failure handling | A missing CLI or failed verification is not mistaken for success, and there are no endless retries |
| Reusability | Adoptable in another project through project settings, without editing harness internals |

Record the adopted project, platform, host and the result together. Do not
extrapolate success in one environment to all environments. Rather than setting
a numeric target for installation time up front, look for the steps that
repeatedly block real adoption and for intervention outside the documentation.

Adoption success and efficiency gain are separate. When the harness's effect
needs confirming, compare with direct handling of a similar representative task
on completion time, quota consumption, user intervention and rework. It does not
have to win on every task: identify the range of work where it is favourable and
reflect it in the direct-handling exceptions. That does not make extra
measurement or review mandatory for every task.

## Change and re-review

Every improvement proposal is classified by where it applies: the shared
contract, the host's execution mechanism, the behavioural tendencies of a model
or effort, or the roles of orchestrator and delegate. A gain on one route is not
automatically extended to another. Shared permission and evidence contracts are
shared; the strength of procedures and instructions is the minimum the route
needs. No fixed character is assumed for a particular vendor.

The release of a new model is not by itself a reason to re-review every rule.
When a model is actually adopted, failures repeat or a performance change is
observed, the affected bindings and policies are re-reviewed.

For example, when adopting a new implementation model, compare it with the
existing one on representative implementation tasks. If it is more efficient,
replace the binding; if it handles larger work reliably, review the delegation
unit; if a specific failure disappears, review the retry rule created for that
failure. Unrelated security hooks or installation procedures are not
re-reviewed wholesale. When a problem in the shared contract is observed, widen
the review to match its blast radius.

The rationale for rules and their re-review conditions live in the manual.
Detailed operating rules are reflected in both hosts' entry templates and in
`.claude/rules/`. When operation changes, align the affected rules, bindings and
manual together, but do not lift an existing permission boundary automatically
on the strength of this principle.

## Responsibilities of the execution tools

Hooks check only limited facts such as direct commands and explicitly named file
targets. They do not judge the safety of a whole shell program. No separate
approval-file ritual is added to approved work, and session start carries no
network, budget or whole-file survey. Detailed diagnostics and log cleanup are
run explicitly. Local reads take a file-selection input instead of arbitrary
commands. Behaviour and limits are summarised in the
[runtime boundary](runtime-boundary.md).
