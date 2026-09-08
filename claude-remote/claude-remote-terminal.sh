#!/bin/bash
cd /home/dash

# Known issue: `claude --continue --remote-control` can crash-loop after a
# reboot even from a cleanly-ended session ("No deferred tool marker found
# in the resumed session."). Guard against spinning forever on that: if
# --continue exits fast several times in a row, fall back to one fresh
# (non-continue) session to break the loop, then try --continue again next
# cycle.
FAILS=0
MAX_FAILS=3
FAST_EXIT_SECS=15

while true; do
    START=$(date +%s)
    /home/dash/.local/bin/claude --continue --remote-control dash
    ELAPSED=$(( $(date +%s) - START ))

    if [ "$ELAPSED" -lt "$FAST_EXIT_SECS" ]; then
        FAILS=$((FAILS + 1))
    else
        FAILS=0
    fi

    if [ "$FAILS" -ge "$MAX_FAILS" ]; then
        echo "claude-remote-terminal.sh: --continue crash-looped ${FAILS}x quickly, falling back to a fresh session" >&2
        /home/dash/.local/bin/claude --remote-control dash
        FAILS=0
    fi

    sleep 5
done
