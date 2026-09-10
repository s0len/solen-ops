#!/usr/bin/env bash
# Invoked BY THE MODEL from the `heartbeat` cron prompt, never as a cron --script:
# a --script runs whether or not the model answered, so it would not prove the login.
# The Gate exports the mtime of the written file as alert_agent_gate_heartbeat_age_seconds.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
mkdir -p "${HERMES_HOME}/heartbeat"
date -u +%Y-%m-%dT%H:%M:%SZ > "${HERMES_HOME}/heartbeat/last"
echo "heartbeat written to ${HERMES_HOME}/heartbeat/last"
