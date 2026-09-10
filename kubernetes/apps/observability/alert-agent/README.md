# alert-agent

The alert-investigation Agent: one pod in `observability` with two containers.

- **Gate** (`app/scripts/gate.py`, stdlib Python) receives Alertmanager
  notifications, deduplicates them against the Incident Issues in the private
  Incidents Repo, enforces the daily Run Budget, and forwards the ones that earn
  an Investigation to Hermes as a signed webhook. It never calls a model.
- **Hermes** (derived image, `image/Dockerfile`) runs the Investigation and the
  `fix` cron against a ChatGPT Business seat. It reads the cluster; it never
  changes it.

Both share one `ceph-block` PVC: Hermes' home (`/opt/data`), the Gate's budget
state, the heartbeat file, session transcripts and cron output.

Vocabulary is `CONTEXT.md`; the decisions behind this app are ADR-0001 to
ADR-0003 and the parent spec in the Incidents Repo.

## Layout

| Path | What it is |
| --- | --- |
| `app/scripts/gate.py` | The Gate. Shipped in a ConfigMap with Flux substitution disabled. |
| `app/resources/hermes-config.yaml` | The Hermes config **template**. |
| `app/resources/hermes-config-render.sh` | Init step: substitutes the route secret and writes `/opt/data/config.yaml`. |
| `app/resources/hermes-cron-bootstrap.sh` | Idempotent `heartbeat` + `prune` cron declarations. |
| `app/resources/hermes-heartbeat.sh` | Written by the `heartbeat` job's model turn. |
| `app/resources/hermes-prune.sh` | The `prune` job. No model call. |
| `app/resources/hermes.env.example` | Non-secret env, and which keys come from Secrets. |
| `image/Dockerfile` | Hermes plus `gh` and `kubectl`. |
| `smoke/smoke.sh` | The Hermes upgrade gate. |
| `tests/test_gate.py` | The Gate's tests (`python3 -m unittest discover -s tests`). |

## Running the smoke script

```sh
kubernetes/apps/observability/alert-agent/smoke/smoke.sh
```

It needs Docker and nothing else — no cluster, no OpenAI login. It renders the
config the way the pod's init container does, starts the gateway on a throwaway
data dir with a random route secret, and asserts:

- exactly one platform connects — the webhook on `0.0.0.0:8644` with only the
  `investigate` route — and neither the `api_server` listener nor the kanban
  dispatcher is running;
- the turn cap, secret redaction and `approvals.mode: manual` are live in the
  **effective** config, read back through Hermes' own loaders, and the resolved
  toolset surface really is `[terminal]` for both webhook and cron;
- every one of 57 write-shaped commands is refused and every one of 24 the
  Agent actually needs is allowed — decided by `check_all_command_guards`, the
  function the terminal tool calls, with a webhook run's session environment;
- a correctly signed `{"prompt": …}` is accepted with 202 and starts a run;
- an unsigned and a wrongly signed one are rejected with 401;
- the cron bootstrap creates both jobs and is a no-op on a second run;
- prune deletes back-dated cron output, keeps fresh output, and never touches
  the gateway's session routing map.

A signed run is accepted and then fails on authentication — that is expected.
Every assertion is on acceptance, never on a model reply.

`--with-login <dir>` (where `<dir>/auth.json` is a live Codex credential) adds
one real prompt. Never run that in CI, and never commit the credential.

### It gates every Hermes image bump

Renovate tracks the Hermes base tag with automerge disabled. Before merging a
bump, run:

```sh
kubernetes/apps/observability/alert-agent/smoke/smoke.sh
```

With no `--image` the script derives the tag from `image/Dockerfile` with the
same expression the Hermes Image workflow uses and pulls it first, so it always
tests what CI pushed for the Dockerfile on the branch. `--image <ref>` pins a
specific ref and skips the pull.

A green run is the required check. A red one means the new release moved a
setting this app depends on; read the failing assertion before touching the
config. The script exits non-zero on any failure and prints the gateway log.

## Changing model tiers

The tier ladder available to the seat, strongest first:
`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`
(`hermes_cli/codex_models.py::DEFAULT_CODEX_MODELS`).

Investigations run one tier down from the top. Changing that is one line in
`app/resources/hermes-config.yaml`:

```yaml
model:
  default: "gpt-5.6-terra"
```

The `fix` cron pins the top tier on the job itself (`hermes cron edit <id>
--model gpt-5.6-sol`), so the two tiers move independently. Re-run the smoke
script after either change.

## Cron jobs

Hermes has no config-file surface for cron jobs; they live in
`/opt/data/cron/jobs.json` and are created with `hermes cron create`.
`app/resources/hermes-cron-bootstrap.sh` is that declaration, applied
idempotently — safe to run on every pod start.

- `heartbeat` — daily, default model, one terminal turn that runs
  `hermes-heartbeat.sh` and writes `/opt/data/heartbeat/last`. The Gate exports
  that file's age; a dead login stops the write and the age alert fires. It is
  deliberately **not** a `--script` job: a script would run whether or not the
  model answered, and would prove nothing about the login.
- `prune` — daily, `--no-agent`, deletes cron output older than thirty days. No
  model call. Session transcripts are **not** files: they live in `state.db` and
  are retained by `sessions.auto_prune` + `sessions.retention_days: 30`. The one
  thing under `/opt/data/sessions` is the gateway's live routing map, which the
  script must never age out.

## What actually stops a write

In descending order of trust:

1. **RBAC** (#9) — a read-only ClusterRole with no access to Secrets. The only
   layer that holds against a determined prompt injection.
2. **`approvals.deny` globs** — fnmatch rules checked before yolo and before
   `approvals.mode`, so they hold even if the mode is loosened. This is the
   load-bearing in-process layer: with `*kubectl*rollout*` removed, the smoke
   matrix shows `kubectl -n x rollout restart deploy/x` sails straight through.
   They are **not** a sandbox — `bash helper.sh` is never content-scanned, so a
   helper the Agent writes in one turn and runs in the next bypasses every glob.
   RBAC is the control for that case.
3. **`approvals.mode: manual`** — a webhook session has no human and no
   `/approve` channel, so a command the dangerous-pattern detector flags fails
   closed instead of being waved through by the default `smart` guardian LLM.

`approvals.unattended_mode` and `approvals.cron_mode` are set to `deny` and kept
for correctness, but they do **not** govern terminal commands in this runtime:
`check_all_command_guards` skips the unattended branch whenever `HERMES_EXEC_ASK`
is set, which `start_gateway()` does process-wide. Do not treat them as the
enforcement.

`security.tirith_enabled` is off. The scanner self-installs a downloaded binary
onto the volume that also holds the OpenAI refresh token, and its URL heuristics
flag plain-HTTP endpoints — which is every in-cluster service the Investigation
reads. The Cilium policy bounds egress far more tightly than a URL heuristic can.

The `api_server` platform cannot be disabled from `config.yaml`: it is enabled
from `API_SERVER_KEY` after the file is read. `hermes.env.example` pins a
deliberately short sentinel, and the render script pins the same value into
`$HERMES_HOME/.env` because that file is loaded with `override=True` and would
otherwise resurrect a generated key from an older PVC.

## Logs

Hermes writes only WARNING and above to stdout. INFO — the listener line, the
turn-cap line, accepted and rejected webhooks — goes to
`/opt/data/logs/gateway.log` on the PVC. `kubectl logs` alone will not show a
healthy Investigation starting.

## The login

The seat is authenticated once with a device-code flow inside the pod, or by
seeding `auth.json` onto the PVC. The runbook lives in the Incidents Repo,
ticket #9. Being signed in to the workspace account before opening the device
page avoids the invalid-state failure.
