#!/usr/bin/env python
"""Record native worker metadata only; the parent verifies actual outputs.

Claude's SubagentStop and, with `--host codex`, Codex's SubagentStop call it;
each record names its host.

No repository scan, memory hashing or completion claim. External launchers
supply their own before/after evidence. Missing metadata is not success.
"""
import json
import os
import sys
from datetime import datetime, timezone


def host_from_argv(argv):
    """`--host codex` / `--host=codex`; absent or unknown means claude (as session_preflight.py)."""
    for i, value in enumerate(argv):
        if value.startswith('--host='):
            host = value.split('=', 1)[1]
        elif value == '--host' and i + 1 < len(argv):
            host = argv[i + 1]
        else:
            continue
        return host if host in ('claude', 'codex') else 'claude'
    return 'claude'


def main(argv=None):
    try:
        from evidence import append_line, project_root
        data = json.load(sys.stdin)
        folder = os.path.join(project_root(data), '.claude')
        if not os.path.isdir(folder):
            return 0
        entry = {key: data.get(key) for key in
                 ('agent_id', 'agent_type', 'session_id', 'stop_reason')}
        entry.update(host=host_from_argv(sys.argv[1:] if argv is None else argv),
                     ts=datetime.now(timezone.utc).isoformat(),
                     cwd=data.get('cwd') or os.getcwd(),
                     evidence_kind='invocation', verification='parent-required')
        append_line(os.path.join(folder, '.delegation-log.jsonl'),
                    json.dumps(entry, ensure_ascii=True), 200, line_bytes=2000)
    except Exception:
        print('HARNESS NOTICE: native worker metadata unavailable; inspect actual outputs', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
