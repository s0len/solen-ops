#!/usr/bin/env bash
# Hermes has no config.yaml surface for cron jobs: they live in
# $HERMES_HOME/cron/jobs.json and are created with `hermes cron create`.
# This script is that declaration, applied idempotently — run it on every pod
# start; existing jobs (matched by name) are left untouched.
#
# Needs the two job scripts and hermes-fix-prompt.md from this directory
# reachable at $SRC_DIR.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
HEARTBEAT_SCHEDULE="${HEARTBEAT_SCHEDULE:-0 4 * * *}"
PRUNE_SCHEDULE="${PRUNE_SCHEDULE:-0 5 * * *}"
FIX_SCHEDULE="${FIX_SCHEDULE:-*/5 * * * *}"
# Top of the tier ladder in hermes-config.yaml; Investigations run one down.
# Both axes are pinned because the drift guard only skips runs on UNPINNED ones.
FIX_MODEL="${FIX_MODEL:-gpt-5.6-sol}"
FIX_PROVIDER="${FIX_PROVIDER:-openai-codex}"

scripts_dir="${HERMES_HOME}/scripts"
jobs_file="${HERMES_HOME}/cron/jobs.json"

mkdir -p "${scripts_dir}" "${HERMES_HOME}/cron" "${HERMES_HOME}/heartbeat"
install -m 0755 "${SRC_DIR}/hermes-heartbeat.sh" "${scripts_dir}/hermes-heartbeat.sh"
install -m 0755 "${SRC_DIR}/hermes-prune.sh" "${scripts_dir}/hermes-prune.sh"

# The Gate mounts $HERMES_HOME/heartbeat as a subPath. If kubelet ever creates
# that directory before this runs, it lands root-owned and the heartbeat job
# fails silently, which reads as a dead login. Warn rather than exit: the Gate
# shares this pod and must not be held down by a broken heartbeat.
[ -w "${HERMES_HOME}/heartbeat" ] || \
    echo "[bootstrap] WARNING: ${HERMES_HOME}/heartbeat is not writable by uid $(id -u); the heartbeat job will fail" >&2

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

# ADR-0001: this is the one job that writes, and the only thing it writes is a
# pull request. "One at a time" needs no flag — `hermes cron create` has none.
# The scheduler's in-flight guard (cron/scheduler.py::try_register_running_job)
# skips a due fire while the previous run of the SAME job id is still running
# ("Job 'fix' already running — skipping"), unconditionally and for every job.
# Do NOT reach for cron.max_parallel_jobs: 1 instead — that serialises ALL jobs
# onto one worker, so a long Fix run would delay the heartbeat that proves the
# login is alive.
# Toolset comes from platform_toolsets.cron: [terminal]; `hermes cron create`
# exposes no per-job toolset flag.
if job_exists fix; then
    echo "[bootstrap] cron job 'fix' already present"
else
    echo "[bootstrap] creating cron job 'fix'"
    hermes cron create "${FIX_SCHEDULE}" "$(cat "${SRC_DIR}/hermes-fix-prompt.md")" \
        --name fix \
        --model "${FIX_MODEL}" \
        --provider "${FIX_PROVIDER}" \
        --deliver local
fi
