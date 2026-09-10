#!/usr/bin/env bash
# Hermes has no config.yaml surface for cron jobs: they live in
# $HERMES_HOME/cron/jobs.json and are created with `hermes cron create`.
# This script is that declaration, applied idempotently — run it on every pod
# start; existing jobs (matched by name) are left untouched.
#
# Needs the two scripts from this directory reachable at $SRC_DIR.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
HEARTBEAT_SCHEDULE="${HEARTBEAT_SCHEDULE:-0 4 * * *}"
PRUNE_SCHEDULE="${PRUNE_SCHEDULE:-0 5 * * *}"

scripts_dir="${HERMES_HOME}/scripts"
jobs_file="${HERMES_HOME}/cron/jobs.json"

mkdir -p "${scripts_dir}" "${HERMES_HOME}/cron" "${HERMES_HOME}/heartbeat"
install -m 0755 "${SRC_DIR}/hermes-heartbeat.sh" "${scripts_dir}/hermes-heartbeat.sh"
install -m 0755 "${SRC_DIR}/hermes-prune.sh" "${scripts_dir}/hermes-prune.sh"

job_exists() {
    [ -f "${jobs_file}" ] || return 1
    JOBS_FILE="${jobs_file}" JOB_NAME="$1" python3 - <<'PY'
import json, os, sys
try:
    with open(os.environ["JOBS_FILE"], encoding="utf-8-sig") as fh:
        jobs = json.load(fh)
except Exception:
    sys.exit(1)
if isinstance(jobs, dict):
    jobs = jobs.get("jobs", [])
name = os.environ["JOB_NAME"]
sys.exit(0 if any(isinstance(j, dict) and j.get("name") == name for j in jobs) else 1)
PY
}

if job_exists heartbeat; then
    echo "[bootstrap] cron job 'heartbeat' already present"
else
    echo "[bootstrap] creating cron job 'heartbeat'"
    hermes cron create "${HEARTBEAT_SCHEDULE}" \
        "Run this one command with the terminal tool, then reply with the single word ok and stop: bash ${scripts_dir}/hermes-heartbeat.sh" \
        --name heartbeat \
        --deliver local
fi

if job_exists prune; then
    echo "[bootstrap] cron job 'prune' already present"
else
    echo "[bootstrap] creating cron job 'prune'"
    hermes cron create "${PRUNE_SCHEDULE}" \
        --name prune \
        --script hermes-prune.sh \
        --no-agent \
        --deliver local
fi
