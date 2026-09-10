#!/usr/bin/env bash
# Run INSIDE the container by smoke.sh: one cron output file the prune job must
# delete, one it must keep, and the gateway's live routing map, which it must
# never touch even when the file is old.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
old_dir="${HERMES_HOME}/cron/output/smoke-job"
mkdir -p "${HERMES_HOME}/sessions" "${old_dir}"

touch "${old_dir}/old-output.md"
touch -d '40 days ago' "${old_dir}/old-output.md"
touch "${old_dir}/fresh-output.md"

touch "${HERMES_HOME}/sessions/sessions.json"
touch -d '40 days ago' "${HERMES_HOME}/sessions/sessions.json"
