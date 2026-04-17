#!/bin/bash
# Check whether the background sync daemon is alive using ps + grep.

SYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
JOBS_FILE="$SYNC_DIR/.sync_jobs.json"

if [ ! -f "$JOBS_FILE" ]; then
    echo "No jobs file at $JOBS_FILE"
    exit 1
fi

PID=$(python3 -c "import json; print(json.load(open('$JOBS_FILE')).get('_daemon', {}).get('pid', ''))" 2>/dev/null)

if [ -z "$PID" ]; then
    echo "No daemon PID recorded in $JOBS_FILE"
    exit 1
fi

MATCH=$(ps -aux | grep -v grep | grep "sync_claude_history.py --background" | awk -v pid="$PID" '$2 == pid')

if [ -n "$MATCH" ]; then
    echo "ALIVE (pid $PID)"
    echo "$MATCH"
    exit 0
else
    echo "DEAD (pid $PID not found in ps -aux)"
    exit 1
fi
