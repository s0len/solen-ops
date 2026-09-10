#!/usr/bin/env bash
# Required check before merging any Renovate bump of the Hermes image.
# Starts the derived image the way the pod runs it — render the config, then the
# gateway, one shared data dir — and proves: only the webhook listener comes up,
# a signed Investigation is accepted, an unsigned one is rejected, the turn cap
# and the deny-approvals are live in the EFFECTIVE config, the cron bootstrap is
# idempotent, and prune deletes only what is older than thirty days.
#
# Needs no OpenAI login: a signed run is accepted and then fails on auth, and
# every assertion is on acceptance, never on a model reply.
#
#   ./smoke.sh                          run every assertion
#   ./smoke.sh --image <ref>            test another image (Renovate's bump)
#   ./smoke.sh --with-login <auth-dir>  also send a real prompt (needs a live
#                                       auth.json; never run in CI)
set -uo pipefail

AUTH_DIR=""
BOOT_TIMEOUT="${SMOKE_BOOT_TIMEOUT:-300}"
EXPECTED_MAX_TURNS=30
HERMES_RUNTIME_UID=10000

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_RESOURCES="$(cd "${SMOKE_DIR}/../app/resources" && pwd)"
DOCKERFILE="${SMOKE_DIR}/../image/Dockerfile"

# Same expression the Hermes Image workflow uses, so a Renovate bump of the base
# tag moves the gate with it instead of silently retesting the old image.
image_from_dockerfile() {
    local version
    version="$(grep -oE '^FROM [^ ]*/hermes-agent:v[^@ ]+' "${DOCKERFILE}" | cut -d: -f2)"
    [ -n "${version}" ] || { echo "smoke: no hermes-agent FROM tag in ${DOCKERFILE}" >&2; exit 2; }
    echo "ghcr.io/s0len/hermes-agent:${version}"
}
IMAGE="${HERMES_IMAGE:-$(image_from_dockerfile)}"
IMAGE_PINNED="${HERMES_IMAGE:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; IMAGE_PINNED="$2"; shift 2 ;;
        --with-login) AUTH_DIR="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | cut -c 3-; exit 0 ;;
        *) echo "smoke: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

FAILURES=0
CONTAINER="hermes-smoke-$$"
WORKDIR="$(mktemp -d)"
DATA_DIR="${WORKDIR}/data"
GATEWAY_LOG="${DATA_DIR}/logs/gateway.log"
mkdir -p "${DATA_DIR}"
chmod 777 "${WORKDIR}" "${DATA_DIR}"

cleanup() {
    docker rm -f "${CONTAINER}" >/dev/null 2>&1
    chmod -R u+rwX "${WORKDIR}" >/dev/null 2>&1
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

pass() { echo "  ok    $1"; }
fail() { echo "  FAIL  $1" >&2; FAILURES=$((FAILURES + 1)); }
step() { echo; echo "== $1"; }

trim() { printf '%s' "$1" | tr '\n' '|' | cut -c1-400; }

require() {
    # require <description> <expected-substring> <actual>
    case "$3" in
        *"$2"*) pass "$1" ;;
        *) fail "$1 — expected '$2', got: $(trim "$3")" ;;
    esac
}

refute() {
    case "$3" in
        *"$2"*) fail "$1 — found '$2' where it must not appear" ;;
        *) pass "$1" ;;
    esac
}

step "image ${IMAGE}"
[ -f "${APP_RESOURCES}/hermes-config.yaml" ] || {
    echo "smoke: no config template in ${APP_RESOURCES}" >&2; exit 2
}
# The derived tag is mutable, so a cached local copy would hide what CI pushed.
if [ -z "${IMAGE_PINNED}" ]; then
    if docker pull "${IMAGE}" >/dev/null 2>&1; then
        pass "pulled the tag the Dockerfile names"
    else
        echo "  note: could not pull ${IMAGE}; using the local copy" >&2
    fi
fi
SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# The pod's init container: template mounted read-only, rendered onto the PVC.
step "config render"
render_args=(
    --rm
    -e "API_SERVER_KEY=disabled"
    -v "${DATA_DIR}:/opt/data"
    -v "${APP_RESOURCES}:/opt/config-template:ro"
)
if docker run "${render_args[@]}" "${IMAGE}" \
        bash /opt/config-template/hermes-config-render.sh > "${WORKDIR}/render-nosecret.log" 2>&1; then
    fail "render succeeded with no HERMES_WEBHOOK_SECRET; it must refuse"
else
    pass "render refuses to run without HERMES_WEBHOOK_SECRET"
fi
if ! docker run "${render_args[@]}" -e "HERMES_WEBHOOK_SECRET=${SECRET}" "${IMAGE}" \
        bash /opt/config-template/hermes-config-render.sh > "${WORKDIR}/render.log" 2>&1; then
    echo "smoke: config render failed" >&2
    tail -20 "${WORKDIR}/render.log" >&2
    exit 1
fi
[ -f "${DATA_DIR}/config.yaml" ] && pass "rendered /opt/data/config.yaml" \
    || { fail "render produced no config.yaml"; exit 1; }
if grep -q 'HERMES_WEBHOOK_SECRET' "${DATA_DIR}/config.yaml"; then
    fail "rendered config still carries the placeholder"
else
    pass "placeholder substituted"
fi
owner="$(docker run --rm -v "${DATA_DIR}:/opt/data" "${IMAGE}" \
    stat -c '%u %a' /opt/data/config.yaml 2>/dev/null | tail -1)"
require "rendered config is owned by the hermes runtime user, mode 0640" \
    "${HERMES_RUNTIME_UID} 640" "${owner}"
require "render pinned the api_server sentinel on the volume" \
    "pinned API_SERVER_KEY" "$(cat "${WORKDIR}/render.log")"

if [ -n "${AUTH_DIR}" ]; then
    [ -f "${AUTH_DIR}/auth.json" ] || { echo "smoke: no auth.json in ${AUTH_DIR}" >&2; exit 2; }
    cp "${AUTH_DIR}/auth.json" "${DATA_DIR}/auth.json"
    chmod 600 "${DATA_DIR}/auth.json"
    echo "  using the login in ${AUTH_DIR}"
fi

step "gateway startup"
docker rm -f "${CONTAINER}" >/dev/null 2>&1
if ! docker run -d --name "${CONTAINER}" \
        -p 127.0.0.1:0:8644 \
        -e "TZ=UTC" \
        -e "API_SERVER_KEY=disabled" \
        -v "${DATA_DIR}:/opt/data" \
        -v "${APP_RESOURCES}:/opt/smoke/resources:ro" \
        -v "${SMOKE_DIR}:/opt/smoke/bin:ro" \
        "${IMAGE}" gateway run >/dev/null; then
    echo "smoke: docker run failed" >&2
    exit 1
fi

# INFO never reaches the container's stdout; the gateway logs it to a file on
# the data volume, which is where a pod's operator has to look too.
deadline=$(( $(date +%s) + BOOT_TIMEOUT ))
listening=0
while [ "$(date +%s)" -lt "${deadline}" ]; do
    if [ -f "${GATEWAY_LOG}" ] && grep -aq "\[webhook\] Listening on" "${GATEWAY_LOG}"; then
        listening=1
        break
    fi
    if [ -z "$(docker ps -q --filter "name=^${CONTAINER}$")" ]; then
        echo "  FAIL  container exited before the webhook listener came up" >&2
        docker logs "${CONTAINER}" 2>&1 | tail -40 >&2
        exit 1
    fi
    sleep 2
done
if [ "${listening}" -ne 1 ]; then
    echo "  FAIL  webhook listener did not come up within ${BOOT_TIMEOUT}s" >&2
    docker logs "${CONTAINER}" 2>&1 | tail -40 >&2
    exit 1
fi
pass "webhook listener up"

listen_line="$(grep -a "\[webhook\] Listening on" "${GATEWAY_LOG}" | tail -1)"
require "binds 0.0.0.0:8644 with only the investigate route" "0.0.0.0:8644 — routes: investigate" "${listen_line}"

# api_server is enabled from API_SERVER_KEY after config.yaml is read, so the
# count in the log is the only place a second listener shows up.
platform_line="$(grep -a "Gateway running with" "${GATEWAY_LOG}" | tail -1)"
require "exactly one platform is connected" "Gateway running with 1 platform(s)" "${platform_line}"
refute "no api_server listener" "API server listening" "$(cat "${GATEWAY_LOG}")"
refute "no kanban dispatcher loop" "kanban dispatcher: embedded" "$(cat "${GATEWAY_LOG}")"

step "effective configuration"
budget_line="$(grep -a "Agent budget: max_iterations=" "${GATEWAY_LOG}" | tail -1)"
require "gateway bridged the turn cap" "max_iterations=${EXPECTED_MAX_TURNS} " "${budget_line}"
redaction_line="$(grep -a "Secret redaction:" "${GATEWAY_LOG}" | tail -1)"
require "secret redaction on" "Secret redaction: ENABLED" "${redaction_line}"

refute "gateway did not warn about a below-floor config version" \
    "predates version" "$(docker logs "${CONTAINER}" 2>&1)"

# The same session env start_gateway() and the webhook adapter give a real run,
# so the probe's verdicts are the ones an Investigation would get.
probe_out="$(docker exec -w /opt/hermes \
    -e HERMES_SESSION_PLATFORM=webhook -e HERMES_EXEC_ASK=1 \
    "${CONTAINER}" python /opt/smoke/bin/effective_config.py 2>&1)"
probe_rc=$?
echo "${probe_out}" | grep -aE "^(FAIL|must_allow|effective_config_failures)"
if [ "${probe_rc}" -eq 0 ]; then
    pass "approvals, toolsets, deny globs and the route resolve as configured"
    pass "every command the Agent needs is allowed and every write-shaped one is denied"
else
    fail "effective config probe (exit ${probe_rc})"
fi

step "webhook authentication"
BASE_URL="http://$(docker port "${CONTAINER}" 8644 | head -1)"
printf '%s' '{"prompt": "smoke test: reply with the single word ok and stop."}' > "${WORKDIR}/body.json"

signed="$(python3 "${SMOKE_DIR}/post.py" "${BASE_URL}/webhooks/investigate" "${SECRET}" "${WORKDIR}/body.json")"
require "signed POST accepted" "202 " "${signed}"
require "signed POST reports accepted" '"status": "accepted"' "${signed}"

unsigned="$(python3 "${SMOKE_DIR}/post.py" "${BASE_URL}/webhooks/investigate" "-" "${WORKDIR}/body.json")"
require "unsigned POST rejected" "401 " "${unsigned}"

wrong="$(python3 "${SMOKE_DIR}/post.py" "${BASE_URL}/webhooks/investigate" "not-the-secret" "${WORKDIR}/body.json")"
require "wrongly signed POST rejected" "401 " "${wrong}"

sleep 5
accepted_line="$(grep -a "\[webhook\] POST event=" "${GATEWAY_LOG}" | tail -1)"
require "gateway log records the run start" "route=investigate prompt_len=" "${accepted_line}"
rejected_line="$(grep -a "Invalid signature for route" "${GATEWAY_LOG}" | tail -1)"
require "gateway log records the rejection" "Invalid signature for route investigate" "${rejected_line}"

step "cron declarations"
first="$(docker exec -e SRC_DIR=/opt/smoke/resources "${CONTAINER}" bash /opt/smoke/resources/hermes-cron-bootstrap.sh 2>&1)"
require "bootstrap creates heartbeat" "creating cron job 'heartbeat'" "${first}"
require "bootstrap creates prune" "creating cron job 'prune'" "${first}"
second="$(docker exec -e SRC_DIR=/opt/smoke/resources "${CONTAINER}" bash /opt/smoke/resources/hermes-cron-bootstrap.sh 2>&1)"
require "re-running is a no-op for heartbeat" "cron job 'heartbeat' already present" "${second}"
require "re-running is a no-op for prune" "cron job 'prune' already present" "${second}"
refute "re-running creates nothing" "creating cron job" "${second}"

jobs="$(docker exec "${CONTAINER}" cat /opt/data/cron/jobs.json 2>&1)"
require "heartbeat job persisted" '"name": "heartbeat"' "${jobs}"
require "prune job persisted" '"name": "prune"' "${jobs}"
require "prune job runs no agent" '"no_agent": true' "${jobs}"
require "heartbeat job writes the Gate's heartbeat file" "hermes-heartbeat.sh" "${jobs}"

step "prune"
if ! docker exec "${CONTAINER}" bash /opt/smoke/bin/seed_prune_fixtures.sh > "${WORKDIR}/seed.log" 2>&1; then
    fail "could not seed the back-dated prune fixtures"
    cat "${WORKDIR}/seed.log" >&2
fi
prune_out="$(docker exec "${CONTAINER}" bash /opt/data/scripts/hermes-prune.sh 2>&1)"
require "prune reports what it deleted" "older than 30 days" "${prune_out}"
survivors="$(docker exec "${CONTAINER}" find /opt/data/sessions /opt/data/cron/output -type f 2>&1)"
refute "40-day-old cron output deleted" "old-output.md" "${survivors}"
require "fresh cron output kept" "fresh-output.md" "${survivors}"
require "the gateway's session routing map is never pruned" "sessions.json" "${survivors}"
retention="$(docker exec -w /opt/hermes "${CONTAINER}" hermes config get sessions.retention_days 2>&1 | tail -1)"
require "state.db transcripts retained for thirty days" "30" "${retention}"

if [ -n "${AUTH_DIR}" ]; then
    step "live model run (--with-login)"
    python3 "${SMOKE_DIR}/post.py" "${BASE_URL}/webhooks/investigate" "${SECRET}" "${WORKDIR}/body.json" > /dev/null
    sleep 60
    if grep -aqE "Response for webhook:investigate|agent run complete|assistant" "${GATEWAY_LOG}"; then
        pass "the seat answered a real prompt"
    else
        fail "no model reply within 60s — check the login"
    fi
fi

echo
if [ "${FAILURES}" -eq 0 ]; then
    echo "smoke: PASS"
    exit 0
fi
echo "smoke: FAIL (${FAILURES} assertion(s)) — last 60 gateway log lines:" >&2
tail -60 "${GATEWAY_LOG}" >&2 2>/dev/null
exit 1
