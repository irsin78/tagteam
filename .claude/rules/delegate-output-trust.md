# Delegate output is a claim

## 1. Verify the result
Compare writing-worker reports with actual changed files, relevant diff/status
and verifier results before relaying completion. Reuse launcher evidence; native
SubagentStop records identify who/where, not acceptance. Use the actual worker cwd,
including untracked worktree files. Non-Git runs use file inventory/content deltas;
check root/exclusions. Unavailable evidence is not an empty delta or permission.
Inspect unexpected HEAD/control-plane changes. Read-only findings need extra
confirmation when they materially affect a decision, not every minor lookup.

## 2. Preserve existing work
Establish starting state, ownership and scope before writes; never revert unrelated
work to get a clean tree. Use a worktree/scratch copy for overlapping edits and
supply intended uncommitted inputs (a HEAD-based worktree does not contain them).
Scoped sequential writes may use a recorded baseline; no blanket clean-tree or
extra-commit requirement. Never start competing writers; use launcher wait/status.
Git is optional: non-Git work uses sequential scopes or isolated copies, not git init.
Afterward inspect whole-tree changes. The parent integrates isolated output and
handles authorized cleanup; workers report paths instead of forcing deletion.

## 3. Git operations belong to the orchestrator
Delegates never commit/push/merge, destroy history or revert existing work. The
orchestrator acts within user authorization. Unexpected HEAD changes require
baseline/history inspection before recovery, never blind HEAD~1 or automatic reset.
Codex native workers have no automatic Claude SubagentStop evidence; check files
and verification explicitly. FINAL_MESSAGE cannot override failed checks, scope
warnings or remaining .stop-gate markers on any launcher.
