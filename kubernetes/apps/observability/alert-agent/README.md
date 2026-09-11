# alert-agent

The alert-investigation Agent: one pod in `observability` with two containers.

- **Gate** (`app/scripts/gate.py`, stdlib Python) receives Alertmanager
  notifications, deduplicates them against the Incident Issues in the private
  Incidents Repo, enforces the daily Run Budget, and forwards the ones that earn
  an Investigation to Hermes as a signed webhook. It never calls a model.
- **Hermes** (derived image, `image/Dockerfile`) runs the Investigation and the
  `fix` cron against a ChatGPT Business seat. It reads the cluster; it never
  changes it.

Both share one `ceph-block` PVC, but not evenly. Hermes mounts the whole volume
as its home (`/opt/data`): the OpenAI credential, its env file, `state.db` with
every session transcript, cron output and the heartbeat file. The Gate mounts
two subdirectories and nothing else — `heartbeat/` read-only for the file whose
age it exports, and `gate/` read-write for the Run Budget and the incident
index. Under the pod's `fsGroup: 65534` a whole-volume mount would make Hermes'
credential group-readable by the Gate.

Vocabulary is `CONTEXT.md`; the decisions behind this app are ADR-0001 to
ADR-0003 and the parent spec in the Incidents Repo.

## Layout

| Path | What it is |
| --- | --- |
| `app/scripts/gate.py` | The Gate. Shipped in a ConfigMap with Flux substitution disabled. |
| `app/resources/hermes-config.yaml` | The Hermes config **template**. |
| `app/resources/hermes-config-render.sh` | Init step: substitutes the route secret and writes `/opt/data/config.yaml`. |
| `app/resources/hermes-cron-bootstrap.sh` | Idempotent `heartbeat` + `prune` + `fix` cron declarations. |
| `app/resources/hermes-fix-prompt.md` | The `fix` job's prompt, passed to `hermes cron create` verbatim. |
| `app/resources/hermes-heartbeat.sh` | Written by the `heartbeat` job's model turn. |
| `app/resources/hermes-prune.sh` | The `prune` job. No model call. |
| `app/resources/hermes.env.example` | Non-secret env, and which keys come from Secrets. |
| `app/rbac.yaml` | The Agent's read-only ClusterRole and its binding. |
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

The `fix` cron pins the top tier on the job itself, in
`app/resources/hermes-cron-bootstrap.sh` (`FIX_MODEL`, `FIX_PROVIDER`), so the
two tiers move independently. Re-run the smoke script after either change.

Both axes are pinned on purpose. The scheduler's drift guard skips a run whose
**unpinned** model or provider has resolved differently since the job was
created — sensible for spend, but it would silently stop Fix runs the next time
`model.default` moves. A pinned axis never counts as drift. Changing `FIX_MODEL`
does **not** re-create an existing job: the bootstrap only creates jobs that are
missing. Move a live job with `hermes cron edit <id> --model <name> --provider
openai-codex`, or delete it and let the next pod start recreate it.

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
- `fix` — every five minutes, top tier, terminal toolset, output to local files.
  It takes the oldest open Incident Issue labelled `ready-for-agent`, re-verifies
  the Diagnosis read-only, and opens ONE pull request here (ADR-0001). It is the
  only job that writes anything, and the only thing it writes is a branch and a
  pull request. The prompt is `app/resources/hermes-fix-prompt.md`; it is stored
  verbatim in `jobs.json` at creation, so **editing the file does not change a
  job that already exists** — `hermes cron remove fix` and restart the pod, or
  `hermes cron edit`, to pick up a reworded prompt. The run is idempotent
  against being cut off: it looks for an open pull request on
  `agent/incident-<n>` before doing anything, so a run that dies between
  `gh pr create` and removing the label reconciles instead of duplicating.

**"One at a time" is not a setting.** `hermes cron create` has no overlap flag
and `jobs.json` has no field for one. The scheduler dedupes per job id instead:
`try_register_running_job` refuses a due fire while the previous run of the same
job is still in flight and logs `Job 'fix' already running — skipping`. That is
unconditional, applies to every job, and is what the smoke script asserts.
`cron.max_parallel_jobs: 1` exists but is the wrong tool — it serialises *all*
jobs onto one worker, so a long Fix run would hold up the heartbeat that proves
the login is alive. A run wedged in-flight for longer than
`cron.inflight_max_minutes` (default 30) is force-released by the stale sweep.

## What actually stops a write

In descending order of trust:

1. **RBAC** (`app/rbac.yaml`) — get, list and watch, cluster-wide, on
   everything except Secrets, plus `pods/log`. The only layer that holds
   against a determined prompt injection. RBAC has no deny, so the core group
   is enumerated without `secrets` and every other API group is listed by
   name; a new API group is invisible to the Agent until it is added there.
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

The Fix run's write lane is deliberately narrow: `git push -u origin <branch>`
and `gh pr create` are allowed, while `*git*push*main*`, `*git*push*--force*`,
`*gh*pr*merge*` (which also catches `--auto`) and `*gh*workflow*run*` are not.
Two globs had to be narrowed to their write verbs to keep that lane usable —
they match the WHOLE command string, so the blanket `*gh*release*` refused
`gh pr create --title "...HelmRelease..."` and `*gh*secret*` refused
`...ExternalSecret...`. That is the `*gh*auth*` lesson again: an over-broad glob
does not fail loudly, it fails as an unrelated-looking refusal in the one run
that mattered. The prompt also keeps every piece of prose in a file
(`--body-file`, `commit -F`, `gh pr create --fill`) so free text never reaches a
command line where an ordinary word can trip a rule.

git authenticates from `$HERMES_HOME/home/.config/gh/hosts.yml` plus the
`!gh auth git-credential` helper in `$HERMES_HOME/home/.gitconfig`, both written
by `hermes-config-render.sh`. `GITHUB_TOKEN` is scrubbed out of every tool
subprocess (it is on Hermes' provider blocklist), so the helper is the only path
— and both files are on the deny list so the Agent cannot read its own token.

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
seeding `auth.json` onto the PVC. The runbook is `docs/agent-login.md` in the
Incidents Repo. Two traps it exists for: be signed in to the workspace account
before opening the device page, and run the login through
`/command/s6-setuidgid hermes` — `kubectl exec` lands as uid 0 and a root-owned
`auth.json` is unreadable to the gateway's uid 10000.

## Why Hermes runs as uid 0

`main-wrapper.sh` in the image refuses any uid that is neither 0 nor 10000, so
the pod's `runAsUser: 65534` cannot apply to the Hermes containers. They carry
a container-level `runAsNonRoot: false`, `runAsUser: 0` instead, and s6-overlay
bootstraps the volume and then drops every real process to uid 10000 with
`s6-setuidgid`. That drop is why `capabilities: {drop: ["ALL"]}` must not be
copied from the Gate here — it kills the boot and leaves `config.yaml`
root-owned — and why `allowPrivilegeEscalation` is `true`. The Gate stays on
the pod default: uid 65534, all capabilities dropped.

The root filesystem is read-only for all three Hermes containers, with
emptyDirs at `/run` (s6's supervision tree) and `/tmp`. Neither is optional: an
`/run` that is missing or `noexec` fails the boot.
