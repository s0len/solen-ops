#!/usr/bin/env bash
# Run by the `prune` cron job with --no-agent (no model call).
# Installed at $HERMES_HOME/scripts/hermes-prune.sh by hermes-cron-bootstrap.sh.
# Stdout is the job's delivered output; keep it one line so a quiet day is quiet.
#
# Cron output only. Session transcripts live in state.db, not on disk, and are
# retained by sessions.auto_prune + sessions.retention_days in the config;
# $HERMES_HOME/sessions holds the gateway's live routing map, which must not be
# aged out from under a long-running gateway.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
DAYS="${HERMES_PRUNE_DAYS:-30}"

outputs=0

if [ -d "${HERMES_HOME}/cron/output" ]; then
    outputs=$(find "${HERMES_HOME}/cron/output" -type f -name '*.md' -mtime "+${DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
    find "${HERMES_HOME}/cron/output" -mindepth 1 -type d -empty -delete 2>/dev/null || true
fi

if [ "${outputs}" -gt 0 ]; then
    echo "pruned ${outputs} cron output file(s) older than ${DAYS} days"
fi
