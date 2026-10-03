# Retry / escalation policy (implementation)

Orchestrator only, not auto-loaded; the starting host stays in charge (session-role.md).

1. Diagnose before rerunning. A timeout reports a deadline, not its cause:
   inspect onboarding reads, API/tool waits and task progress first.
   Confirmed authentication/quota failures are availability conditions even
   when a CLI returns exit 1. Inspect partial changes first; use the matrix
   fallback or request reauthentication, never repeat a revoked-token call.
   Record confirmed exhaustion with `python .claude/scripts/harness-session.py budget --session <id> --exhausted <vendor>`; with `HARNESS_SESSION_ID=<id>` the router skips that vendor.
   The exhaustion wordings the launchers recognise are listed in their comments and must be replaced with captured CLI output when a real exhaustion is observed.
   Do not infer CLI errors from quoted task/tool output in merged logs.
   Sandbox, dependencies, quoting and unreachable MCP are infrastructure failures. Repair
   the environment and rerun at the same model/effort; do not promote.
   If the same infrastructure failure remains after a targeted repair,
   report it instead of repeatedly launching workers.
2. Classify the failure from run logs and verifier output. The class is the
   router's `--retry-reason` value; only `reasoning` promotes.

   | Class | Observable evidence | Response |
   |---|---|---|
   | `infra` | sandbox refusal, missing dependency, broken quoting, unreachable MCP | repair, same settings |
   | `availability` | auth/quota error, unsupported-model 400 | matrix fallback, `budget --exhausted` |
   | `spec` | NEEDS_INPUT, or the worker guessed what the spec omits | supply inputs, same settings |
   | `scope` | timeout after partial progress, only part of the change landed | split the task, same settings |
   | `knowledge` | misuse of an unfamiliar API or library | supply references or a web reader, same settings |
   | `reasoning` | spec understood but approach wrong, repeated verifier failure | promote (3) |

   Get the retry route with `harness-route.py --host <host> --role <role>
   --retry-from <worker_id> --retry-reason <class> --attempt N`.
3. A reasoning retry below A raises the floor one band in the same vendor,
   choosing the cheapest eligible row at or above that band in the latency
   class; if none is fast enough, use the cheapest such row detached. If the
   vendor has no higher ordinary row, or A fails, use its S lane at foreground
   effort (Fable for Claude, Astra for OpenAI). A failed S foreground effort
   steps to that lane's detached effort. After detached S fails, or when no
   further S effort exists, implementation and writing may take one cross-lane S
   attempt at foreground effort, only when the other vendor is an artifact author.
   Advice and review roles never waive author separation. That single attempt's
   result must be reviewed by a different vendor than the implementer. A further
   reasoning retry from an author's cross-lane S row is refused; the orchestrator
   decides under 4.
   When the other lane is already independent, this retry is refused; choose it
   through normal `--tier S` / `--worker` routing, with the orchestrator enforcing
   rule 5's attempt limit.
   `selection_policy.cross_lane_s: false` disables this final exception. A step
   over the requested latency limit runs detached (`-b --wait`). Never raise effort for
   slowness. `max` is detached only; Codex worker `ultra` is a policy refusal,
   not an availability fallback. Tight budget disables optional promotions;
   risk floors still apply.
4. After the second reasoning failure, before any orchestrator takeover, ask
   the other vendor once, read-only: resolve `python .claude/scripts/harness-route.py
   --host <host> --role decide --tier S --author-vendor <failed worker's vendor>`, then
   run the returned launcher with `-s read-only` and the question "why did this fail?". A spec or scope answer returns to that class's response; confirmed
   reasoning means the orchestrator implements directly with the same
   verification, or reports instead when it is below the failed band.
5. At most 3 attempts within a lane (first + 2 retries), plus the single cross-lane
   S attempt: `--attempt` counts retries, so `--attempt 3` is accepted only for
   that final cross-lane S route; later attempts are refused and 4 applies. Each
   retry changes class, settings or inputs; never repeat a combination. Do not
   cycle through vendors; the single cross-lane S attempt in 3 is the only
   reasoning-retry exception.
6. A retry is one worker attempt. Always pass the failure and current state;
   change only the settings, inputs or scope the diagnosis points to. Codex
   supports `-r` through codex-run.sh; Claude needs a NEW claude-run.sh call with
   the original task, exact failure and current state INLINE. Never assume worker
   context is preserved. Recheck existing changes before any retry.

Unresolved NEEDS_INPUT goes back to the parent for an answer and a new
self-contained assignment; it is not a reason to silently take over.

Run records: Set optional `HARNESS_TASK` to a task slug (1–64 ASCII letters,
digits, dots, underscores or hyphens) to link delegated attempts and direct work.
Set `HARNESS_ASSESS` to all five comma-separated ratings, for example
`open=1,tangle=2,precedent=0,verifier=1,consequence=1` (each 0, 1 or 2).
The router's `--assess` uses these ratings to choose the automatic start band
and records them in JSON; see delegation-matrix.md, "Assess before routing".
After diagnosis, use `python .claude/scripts/harness-session.py outcome --run <id>
--class <class> [--accepted yes|no] [--note TEXT]` to record or replace the outcome.
For direct work, use `python .claude/scripts/harness-session.py direct --task <slug>
--result done|failed --elapsed-s N [--assess STR]` with optional model, tokens,
rework, interventions, verify and note fields. Records share the launcher's tree
state directory; `harness-stats.sh` summarizes them alongside delegated reports.
