#!/usr/bin/env bash
# Monitor the Sonnet 500q LongMemEval run and report progress.
# Usage: ./scripts/monitor_sonnet_run.sh
#
# Run once for a snapshot. Loop: watch -n 60 ./scripts/monitor_sonnet_run.sh

REPO="/home/max/git/yadgar"
JSONL="$REPO/benchmarks/results/longmemeval_v5.26.0_s_full_hypotheses.jsonl"
LOG="/tmp/yadgar-v5.26.0-sonnet-full-run.log"

echo "=== Sonnet 500q run monitor ==="
echo "Time: $(date)"
echo ""

# Process check
PID=$(pgrep -f "run_longmemeval.py.*sonnet" | head -1)
if [ -n "$PID" ]; then
    echo "Status: RUNNING (PID $PID)"
else
    echo "Status: NOT RUNNING"
fi

# Progress
if [ -f "$JSONL" ]; then
    DONE=$(wc -l < "$JSONL")
    echo "Progress: $DONE / 500 questions complete"
    if [ "$DONE" -gt 0 ]; then
        PCT=$(echo "scale=1; $DONE * 100 / 500" | bc)
        echo "Percent: $PCT%"
        # Estimate remaining time based on ~55s/question
        REMAINING=$(( (500 - DONE) * 55 ))
        HOURS=$(( REMAINING / 3600 ))
        MINS=$(( (REMAINING % 3600) / 60 ))
        echo "Est. remaining: ~${HOURS}h ${MINS}m (at 55s/q)"
    fi
else
    echo "JSONL not found yet"
fi

# Last log line (non-warning)
echo ""
echo "Last log activity:"
grep -v "^WARNING" "$LOG" 2>/dev/null | tail -5

# Check for errors
ERRORS=$(grep -c "ERROR on" "$LOG" 2>/dev/null || echo 0)
if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo "ERRORS detected: $ERRORS"
    grep "ERROR on" "$LOG" | tail -5
fi

# When done
if [ -f "$JSONL" ] && [ "$(wc -l < "$JSONL")" -ge 500 ]; then
    echo ""
    echo "=== RUN COMPLETE ==="
    echo "Run aggregation + commit steps:"
    echo "  cd $REPO"
    echo "  python3 scripts/aggregate_sonnet_results.py"
    echo "  (then commit, merge, push per MIGRATION_NOTES.md)"
fi
