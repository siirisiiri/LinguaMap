#!/bin/bash
# Overnight supervisor: fetch + classify the Americas, Africa, Asia, and
# Australia/Oceania. Restarts only if the pipeline process dies before
# writing finished_at. Country JSON files are the real checkpoint.
set -u
ROOT="/Users/aryandaga/Documents/LinguaMap"
cd "$ROOT"
mkdir -p logs
export PYTHONUNBUFFERED=1
LOG="logs/americas_africa_pipeline.log"
STATUS="logs/americas_africa_status.json"

echo "[supervisor] starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exec >>"$LOG" 2>&1
echo "[supervisor] logging to $LOG"

while true; do
  python3 run_world_pipeline.py \
    --continents north-america,central-america,south-america,africa,asia,australia-oceania \
    --status-file "$STATUS"
  code=$?
  if python3 - "$STATUS" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)
status = json.loads(path.read_text())
raise SystemExit(0 if status.get("finished_at") else 1)
PY
  then
    echo "[supervisor] pipeline finished with exit ${code} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
  fi
  echo "[supervisor] crashed/exited ${code} without finished_at; restarting in 45s at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sleep 45
done
