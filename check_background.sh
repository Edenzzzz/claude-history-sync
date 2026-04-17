#!/bin/bash
# Check whether the background sync daemon is alive using ps + grep,
# and print the configured jobs (interval + chats) from .sync_jobs.json.

SYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
JOBS_FILE="$SYNC_DIR/.sync_jobs.json"

if [ ! -f "$JOBS_FILE" ]; then
    echo "No jobs file at $JOBS_FILE"
    exit 1
fi

PID=$(python3 -c "import json; print(json.load(open('$JOBS_FILE')).get('_daemon', {}).get('pid', ''))" 2>/dev/null)

if [ -z "$PID" ]; then
    echo "No daemon PID recorded in $JOBS_FILE"
    STATUS=1
else
    MATCH=$(ps -aux | grep -v grep | grep "sync_claude_history.py --background" | awk -v pid="$PID" '$2 == pid')
    if [ -n "$MATCH" ]; then
        echo "ALIVE (pid $PID)"
        echo "$MATCH"
        STATUS=0
    else
        echo "DEAD (pid $PID not found in ps -aux)"
        STATUS=1
    fi
fi

echo
echo "Jobs in $JOBS_FILE:"
python3 -c "
import json
jobs = json.load(open('$JOBS_FILE'))
by_repo = {}
for key, j in jobs.items():
    if key.startswith('_'):
        continue
    by_repo.setdefault(j.get('repo', '?'), []).append(j)
if not by_repo:
    print('  (no jobs)')
for repo, entries in by_repo.items():
    print(f'  {repo}')
    for j in entries:
        name = j.get('name') or '(untitled)'
        print(f'    - {name}  chat {j.get(\"chat_id\", \"?\")}  (interval {j.get(\"interval\", \"?\")}s)')
"

exit $STATUS
