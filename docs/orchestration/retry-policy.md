# Retry / escalation policy (implementation)

Orchestrator-only reference outside automatic rules loading; the starting host stays in charge (session-role.md).

1. Diagnose before rerunning. A timeout reports a deadline, not its cause:
   inspect onboarding reads, API/tool waits and task progress first.
   Confirmed authentication/quota failures are availability conditions even
   when a CLI returns exit 1. Inspect partial changes first; use the matrix
   fallback or request reauthentication, never repeat a revoked-token call.
   Do not infer CLI errors from quoted task/tool output in merged logs.
   Sandbox, dependencies, quoting and unreachable MCP are infrastructure failures. Repair
   the environment and rerun at the same model/effort; do not promote.
   If the same infrastructure failure remains after a targeted repair,
   report it instead of repeatedly launching workers.
2. After the first implementation failure, choose one correction at the
   original settings OR one justified promotion below, not both.
   Codex supports `-r`
   through codex-run.sh. Claude uses a NEW claude-run.sh invocation with
   the original task plus the exact failure and current state INLINE;
   claude-run.sh does not implement `-r`. Never assume worker context is
   preserved. Recheck existing changes before either kind of retry.
3. Only diagnosed reasoning insufficiency allows ONE promotion. From an
   explicit C task, use the host route's default B entry (omit --tier).
   Otherwise move one step on that host's implementation ladder
   (`harness-route.py --step N`). Never
   raise effort because a run was slow. `max` is detached only;
   Codex worker `ultra` is a policy refusal, not an availability fallback.
   Tight budget disables optional promotions; risk floors still apply.
4. After two real implementation failures the orchestrator implements
   directly with the same verification. Do not cycle through vendors.
   If the current main model already failed that task or cannot meet its
   risk floor, stop and report. Availability fallback is separate (matrix).

Unresolved NEEDS_INPUT goes back to the parent for an answer and a new
self-contained assignment; it is not a reason to silently take over.
