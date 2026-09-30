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
3. Promotion takes the cheapest step that gains at least `promotion_delta`
   index points in the same lane, not one rung. Steep-curve models (Opus 5.5,
   Sonnet 5.5) usually raise effort; flat-curve models (Astra, Sol 6.1) switch
   model. A design/state-judgment diagnosis or a second reasoning failure goes
   straight to band S (model change: Fable on the Claude lane, Astra on the
   OpenAI lane). Author separation still applies. A step over the foreground
   latency limit runs only detached (`-b --wait`). Never raise effort for
   slowness. `max` is detached only; Codex worker `ultra` is a policy refusal,
   not an availability fallback. Tight budget disables optional promotions;
   risk floors still apply.
4. After the second reasoning failure, before any orchestrator takeover, ask
   the other vendor once, read-only: `harness-route.py --host <host> --role decide
   --tier S --author-vendor <failed worker's vendor>`, "why did this fail?". A spec or scope answer returns to that class's response; confirmed
   reasoning means the orchestrator implements directly with the same
   verification, or reports instead when it is below the failed band.
5. At most 3 worker attempts (first + 2 retries): `--attempt` counts retries,
   so the router refuses `--attempt 3`; then 4 applies. Each retry changes class, settings or inputs; never
   repeat a combination. Do not cycle through vendors.
6. A retry is one worker attempt. Always pass the failure and current state;
   change only the settings, inputs or scope the diagnosis points to. Codex
   supports `-r` through codex-run.sh; Claude needs a NEW claude-run.sh call with
   the original task, exact failure and current state INLINE. Never assume worker
   context is preserved. Recheck existing changes before any retry.

Unresolved NEEDS_INPUT goes back to the parent for an answer and a new
self-contained assignment; it is not a reason to silently take over.
