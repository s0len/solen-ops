#!/usr/bin/env bash
# Hermes has no config.yaml surface for cron jobs: they live in
# $HERMES_HOME/cron/jobs.json and are created with `hermes cron create`.
# This script is that declaration, RECONCILED on every pod start: a missing job
# is created, and an existing job whose schedule or pinned model/provider/effort
# has drifted from the declaration below is brought back into line. Creating
# only what is missing was not enough — a retune of the flags here would leave
# the live job on its old pins for as long as the volume survives.
#
# Needs the two job scripts and hermes-fix-prompt.md from this directory
# reachable at $SRC_DIR.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
HEARTBEAT_SCHEDULE="${HEARTBEAT_SCHEDULE:-0 4 * * *}"
PRUNE_SCHEDULE="${PRUNE_SCHEDULE:-0 5 * * *}"
FIX_SCHEDULE="${FIX_SCHEDULE:-*/5 * * * *}"
# Rare path, small model, maximum thinking. `max` is in gpt-5.6's wire ladder
# (none/low/medium/high/xhigh/max), so it reaches the wire unclamped.
# Both inference axes are pinned because the drift guard only skips runs on
# UNPINNED ones, and a pinned axis carries no snapshot to go stale.
FIX_MODEL="${FIX_MODEL:-gpt-5.6-luna}"
FIX_PROVIDER="${FIX_PROVIDER:-openai-codex}"
FIX_EFFORT="${FIX_EFFORT:-max}"
# The heartbeat stays UNPINNED on model on purpose: it exists to prove the login
# behind model.default still answers, so it has to follow it. Effort is pinned
# to the floor instead — one daily "reply ok" proves nothing extra for thinking
# tokens. `low` is that floor because model.default is gpt-6-astra, whose ladder
# omits `none`; a `none` request would clamp UP to low anyway.
HEARTBEAT_EFFORT="${HEARTBEAT_EFFORT:-low}"

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

# Prints exactly one verdict line for the named job:
#   missing                        no job by that name
#   ok <id>                        stored shape already matches the declaration
#   edit <id> <axis>[,<axis>...]   schedule/model/provider/effort differ
#   recreate <id> <reason>         edit cannot fix it; remove and create again
job_state() {
    # job_state <name> <schedule> <model> <provider> <effort>
    JOBS_FILE="${jobs_file}" HERMES_HOME="${HERMES_HOME}" JOB_NAME="$1" \
    WANT_SCHEDULE="$2" WANT_MODEL="$3" WANT_PROVIDER="$4" WANT_EFFORT="$5" python3 - <<'PY'
import json, os, sys


def norm(value):
    return value.strip().lower() if isinstance(value, str) and value.strip() else ""


want = {
    "schedule": norm(os.environ.get("WANT_SCHEDULE")),
    "model": norm(os.environ.get("WANT_MODEL")),
    "provider": norm(os.environ.get("WANT_PROVIDER")),
    "effort": norm(os.environ.get("WANT_EFFORT")),
}

try:
    with open(os.environ["JOBS_FILE"], encoding="utf-8-sig") as fh:
        jobs = json.load(fh)
except Exception:
    jobs = []
if isinstance(jobs, dict):
    jobs = jobs.get("jobs", [])
job = next(
    (j for j in jobs if isinstance(j, dict) and j.get("name") == os.environ["JOB_NAME"]), None)
if job is None:
    print("missing")
    raise SystemExit(0)

job_id = str(job.get("id") or "")
schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
have = {
    "schedule": norm(schedule.get("expr")),
    "model": norm(job.get("model")),
    "provider": norm(job.get("provider")),
    "effort": norm(job.get("reasoning_effort")),
}
differs = sorted(axis for axis in want if want[axis] != have[axis])

# A job left unpinned on model carries a creation-time model_snapshot, and the
# scheduler's fail-closed drift guard SKIPS the run — no inference call, no
# heartbeat file, so it reads as a dead login — once that snapshot no longer
# matches the resolved default. update_job only re-snapshots when an inference
# axis actually changes, so `hermes cron edit` cannot clear a stale snapshot on
# a job that must stay unpinned. Re-creation is the only way to re-take it.
stale = ""
if not want["model"] and not have["model"] and not job.get("no_agent"):
    snapshot = norm(job.get("model_snapshot"))
    current = ""
    try:
        sys.path.insert(0, "/opt/hermes")
        from cron.jobs import _resolve_default_model_snapshot

        current = norm(_resolve_default_model_snapshot())
    except Exception:
        current = ""
    if snapshot and current and snapshot != current:
        stale = "model_snapshot '%s' -> '%s'" % (snapshot, current)

if stale:
    print("recreate %s %s" % (job_id, stale))
elif differs:
    print("edit %s %s" % (job_id, ",".join(differs)))
else:
    print("ok %s" % job_id)
PY
}

create_heartbeat() {
    hermes cron create "${HEARTBEAT_SCHEDULE}" \
        "Run this one command with the terminal tool, then reply with the single word ok and stop: bash ${scripts_dir}/hermes-heartbeat.sh" \
        --name heartbeat \
        --reasoning-effort "${HEARTBEAT_EFFORT}" \
        --deliver local
}

create_prune() {
    hermes cron create "${PRUNE_SCHEDULE}" \
        --name prune \
        --script hermes-prune.sh \
        --no-agent \
        --deliver local
}

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
create_fix() {
    hermes cron create "${FIX_SCHEDULE}" "$(cat "${SRC_DIR}/hermes-fix-prompt.md")" \
        --name fix \
        --model "${FIX_MODEL}" \
        --provider "${FIX_PROVIDER}" \
        --reasoning-effort "${FIX_EFFORT}" \
        --deliver local
}

# `hermes cron edit` and `remove` match on job id, never on name: cron/jobs.py's
# _with_job compares job["id"] only. The id comes back from job_state.
reconcile_job() {
    # reconcile_job <name> <create-fn> <schedule> <model> <provider> <effort>
    local name="$1" create_fn="$2" schedule="$3" model="$4" provider="$5" effort="$6"
    local state verb job_id detail
    state="$(job_state "${name}" "${schedule}" "${model}" "${provider}" "${effort}")"
    verb="${state%% *}"
    job_id="$(echo "${state}" | cut -d' ' -f2)"
    detail="$(echo "${state}" | cut -d' ' -f3-)"

    case "${verb}" in
        missing)
            echo "[bootstrap] creating cron job '${name}'"
            "${create_fn}"
            ;;
        ok)
            echo "[bootstrap] cron job '${name}' already present and in step"
            ;;
        edit)
            echo "[bootstrap] repinning cron job '${name}' (${detail})"
            hermes cron edit "${job_id}" \
                --schedule "${schedule}" \
                --model "${model}" \
                --provider "${provider}" \
                --reasoning-effort "${effort}"
            ;;
        recreate)
            echo "[bootstrap] recreating cron job '${name}' (${detail})"
            hermes cron remove "${job_id}"
            "${create_fn}"
            ;;
        *)
            echo "[bootstrap] ERROR: unreadable state for cron job '${name}': ${state}" >&2
            return 1
            ;;
    esac
}

# An empty model/provider/effort argument declares "unpinned": `hermes cron edit`
# reads an empty string as "clear this pin", which is the same shape `create`
# leaves behind when the flag is omitted.
reconcile_job heartbeat create_heartbeat "${HEARTBEAT_SCHEDULE}" "" "" "${HEARTBEAT_EFFORT}"
reconcile_job prune create_prune "${PRUNE_SCHEDULE}" "" "" ""
reconcile_job fix create_fix "${FIX_SCHEDULE}" "${FIX_MODEL}" "${FIX_PROVIDER}" "${FIX_EFFORT}"
