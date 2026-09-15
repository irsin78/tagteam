#!/usr/bin/env python
"""Record native worker metadata only; the parent verifies actual outputs.

No repository scan, memory hashing or completion claim. External launchers
supply their own before/after evidence. Missing metadata is not success.
"""
import json
import os
import sys
from datetime import datetime, timezone


def main():
    try:
        from evidence import append_line, project_root
        data = json.load(sys.stdin)
        folder = os.path.join(project_root(data), '.claude')
        if not os.path.isdir(folder):
            return 0
        entry = {key: data.get(key) for key in
                 ('agent_id', 'agent_type', 'session_id', 'stop_reason')}
        entry.update(ts=datetime.now(timezone.utc).isoformat(),
                     cwd=data.get('cwd') or os.getcwd(),
                     evidence_kind='invocation', verification='parent-required')
        append_line(os.path.join(folder, '.delegation-log.jsonl'),
                    json.dumps(entry, ensure_ascii=True), 200, line_bytes=2000)
    except Exception:
        print('HARNESS NOTICE: native worker metadata unavailable; inspect actual outputs', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
