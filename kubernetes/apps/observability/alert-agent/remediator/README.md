# alert-remediator

The Remediator: a CronJob in `observability` that runs a **Catalogued
Remediation** — a live cluster action whose exact commands the owner read and
merged as a diff before it ever ran. It is the second half of ADR-0004; the
first half is the catalogue itself, in `../remediations/`.

It is not the Agent. There is no model in this process. `app/scripts/remediate.py`
is stdlib Python that matches an Incident Issue to a catalogue entry and runs
that entry unchanged.

**It is live.** It was enabled on main on 2026-09-12 in
`feat(alert-agent): Enable the Remediator`: `./alert-agent/remediator/ks.yaml`
is no longer commented out in
`kubernetes/apps/observability/kustomization.yaml`, the CronJob
`alert-remediator` runs `3-59/5 * * * *` in `observability` with `DRY_RUN`
false, and `ready-for-remediation` exists in the Incidents Repo. A run can
therefore delete an object today.

What stands between a firing alert and that delete is not the kustomization any
more; it is the four conditions below, all of which must hold: a human has put
`ready-for-remediation` on an open Incident Issue; the alert is still firing,
with exactly one member matching the entry, and that member is one the Gate
recorded in that issue's body; the catalogue entry it matches is
`enabled: true`; and every one of that entry's preconditions passes. Miss any
of them and the run does nothing and says why. `etcd-database-fragmentation`
is still `enabled: false`, for the credential reason set out further down, so
the label on an `etcdDatabaseHighFragmentationRatio` issue is a no-op.

Vocabulary is `CONTEXT.md`; the decision is ADR-0004, which amends ADR-0001.

## The four conditions

A run does at most one thing, and only when all four hold:

1. an open Incident Issue in the Incidents Repo carries `ready-for-remediation`
   — a human put it there;
2. the alert is **still firing** in Alertmanager, neither silenced nor
   inhibited; exactly **one** alert matches the entry's label matchers; and
   that alert is one **this** Incident Issue records. That one alert supplies
   every templated value, so no name is ever read out of prose. Two matches is
   an ambiguous target and is refused;
3. an enabled catalogue entry matches the alertname taken from the issue title;
4. every precondition passes, in order, stopping at the first failure.

Then it claims the issue in the Run Ledger, removes the label, runs the
commands, verifies, and comments the whole transcript. On a failed step it also
applies `ready-for-human`.

## What one label authorises

The title says which alertname, and nothing more. What the human's label
authorises is one **Alert Group**: the Gate stamps a hidden
`<!-- alert-agent:group=… -->` marker at the end of every Incident Issue body
and renders every alert in that group above it as a `| Label | Value |` table.
The Remediator reads those tables and acts only on an alert whose target labels
— the entry's own matchers and everything its bindings interpolate — match one
of them.

That matters because Alertmanager groups by `alertname` here, so
`security/kanidm` and `media/navidrome` share one Incident Issue for
`VolSyncBackupStale`. Without this, a label applied while reading about kanidm
would drive a delete against navidrome the moment kanidm resolved and navidrome
fired. A live alert the issue does not record is logged as
`alert_not_authorised` and nothing runs; an issue that carries no Gate marker at
all — anything filed by hand — authorises nothing.

Labels outside that set are deliberately not compared: a `severity` that
changed between the issue being written and now cannot change which object a
command names, so it is not a reason to refuse.

## Layout

| Path | What it is |
| --- | --- |
| `../remediations/*.yaml` | The catalogue. One file per known failure. |
| `../remediations/kustomization.yaml` | Generates the catalogue ConfigMap; a base of `app/`. |
| `app/scripts/remediate.py` | The executor. Stdlib only. |
| `app/rbac.yaml` | Its ServiceAccount's ClusterRole and the two namespaced Roles. |
| `app/rbac-etcd-defrag.yaml` | The kube-system grant, **out of** the kustomization. |
| `app/validatingadmissionpolicy.yaml` | What narrows the three write verbs to specific names and commands. |
| `app/ledger.yaml` | The Run Ledger ConfigMap, seeded once with `ssa: IfNotPresent`. |
| `../tests/test_remediate.py` | Its tests, run by `gate-tests.yaml` on any change here. |

Tests: `python3 -m unittest discover -s kubernetes/apps/observability/alert-agent/tests`.
They load the real shipped catalogue, so an entry that stops parsing, loses a
precondition or grows a command the executor may not run fails CI.

## Catalogue schema

```yaml
version: 1                    # must be 1
failure: volsync-ghost-snapshot   # the file's own stem
summary: >-                   # what the failure is, for a human
entries:
  - id: volsync-ghost-snapshot/backup-stale
    alertname: VolSyncBackupStale     # matched against the Incident Issue title
    enabled: true
    match:                    # regular expressions over the live alert's labels
      namespace: "^[a-z0-9-]+$"
    bind:                     # the only values a command may be templated on
      ns: "{namespace}"
      rs: {from: "{job_name}", strip_prefix: volsync-src-}
    notes: >-                 # free text for a reader; ignored by the executor
    blast_radius: >-          # quoted onto the Incident Issue on every run
    max_runs: {count: 2, window_hours: 24}
    preconditions: [...]      # read-only; all must pass
    steps: [...]              # what actually runs
    verify: [...]             # read-only; proves the result
    evidence: [...]           # read-only; output pasted onto the issue
```

A **check** (`preconditions`, `verify`) is an `id`, a `describe`, exactly one of
`run` (an argv list) or `promql` (an instant query that must return exactly one
series), and an `expect`. It may `capture` its stdout under a name, or
`capture_list` a field out of a JSON array. `equals` and `matches` are themselves
templated, so a check can assert one object names another.

Expectations: `equals`, `matches`, `not_empty`, `integer_equals`,
`integer_at_least`, `integer_at_most`, `older_than_hours`, `newer_than_capture`,
`value_equals`, `value_at_least`, `value_at_most`, `list_length_at_least`,
`list_length_at_most`. Empty jsonpath output counts as the integer 0, because
that is what kubectl prints for an absent numeric field.

A **step** is a `describe`, a `run`, an optional `for_each` naming a captured
list (each item arrives as `{item}`), and an optional `timeout_seconds`.

## What actually stops a write

In descending order of trust:

1. **RBAC** (`app/rbac.yaml`) — three write verbs on three kinds:
   `delete persistentvolumeclaims`, `delete jobs`, `delete volumesnapshots`,
   plus `create pods/exec` in rook-ceph and `get`/`patch` on the single named
   Run Ledger ConfigMap. No Secrets at any level, no `patch` or `update` on
   anything else, no `create` outside the kube-system file below.
2. **The ValidatingAdmissionPolicies** — RBAC cannot say "only objects named
   `volsync-<something>-src`", so these do. They apply to this one
   ServiceAccount and are inert for every other actor, the owner included.
   A PVC or VolumeSnapshot delete must match `^volsync-[a-z0-9-]{1,53}-src$`;
   a Job delete must be a `volsync-src-` mover Job or a Job a CronJob owns and
   has already Completed or Failed, which is what keeps an in-flight
   `etcd-defrag-<stamp>` and its os:admin credential out of reach; the
   only Job that may be created is `kube-system/etcd-defrag-<12 digits>` owned by
   the `etcd-defrag` CronJob; and an exec must be into `rook-ceph-tools-*` with
   a command that is exactly `ceph status`, `ceph health [detail]` or
   `ceph crash archive|info|ls|ls-new|stat`.
3. **The executor** — `argv[0]` must be the literal string `kubectl`, checked
   both when the catalogue loads and again before every execution; there is no
   shell anywhere, so no argument is ever word-split or expanded; the kubectl
   verb must be one of get, describe, logs, wait, delete, exec, create, which is
   why no entry can reach `patch` and therefore its own Run Ledger; in
   `preconditions`, `verify` and `evidence` it is narrowed again to get,
   describe, logs, wait, exec, so only `steps` can carry a mutation; and every
   value substituted into a command, whether an alert label or a capture, must
   match `^[A-Za-z0-9][A-Za-z0-9._:+-]{0,190}$`.
4. **The catalogue's own preconditions** — the only layer that knows what the
   failure is supposed to look like. The VolSync entry refuses to run unless the
   source PVC is `Bound`; the Ceph entry refuses unless every OSD is up and every
   PG is `active+clean`; the Job entry refuses unless the pods are already gone
   and the parent CronJob has succeeded since.

Layers 1 and 2 were verified against a throwaway k3s v1.34.1 cluster with the
real manifests and an impersonated ServiceAccount, not by reading them. Deleting
the application's own PVC, deleting a standalone Job, deleting a running
CronJob-owned Job, reading a Secret, patching
any other ConfigMap, creating a Job under any other name, `ceph osd out`,
`ceph pg repair`, `ceph health mute`, `ceph crash rm`, `rbd rm` and a shell in
the toolbox are all refused; the catalogue's own commands are all allowed. That
probe is also how `ceph osd` and `ceph pg` came off the exec allowlist: they had
been added for read-only status commands, and `ceph osd out 0` sailed through.

## The etcd entry, and the credential it does not have

`etcd-database-fragmentation.yaml` ships `enabled: false`. `talosctl etcd defrag`
needs a Talos ServiceAccount with the `os:admin` role — the credential that can
also wipe a node's disks — and nothing unattended here holds that. The
`etcd-defrag` CronJob in kube-system already does, narrowed to one script that
only defragments, that refuses to start if a member reports no leader or an
active etcd alarm, and that does one member at a time with a settle between them,
leader last. The entry therefore starts that Job instead of running talosctl.

Enabling it is deliberately two edits, not one: `enabled: true` in the catalogue
file, and uncommenting `# - ./rbac-etcd-defrag.yaml` in `app/kustomization.yaml`.
The reason is that `create job --from=cronjob` is in general a way to run
whatever a CronJob's container runs, and this container holds an os:admin Talos
identity.

## Its own label, not the Fix lane's

The Remediator reads `ready-for-remediation`; the Hermes `fix` cron reads
`ready-for-agent`. They are separate on purpose, and not only to avoid a race.

Sharing one label cannot be made safe with a cron offset. `*/5` fires at
`:00, :05, :10…` and `3-59/5` at `:03, :08, :13…`, so whichever schedule you
pick, a label applied in the gap between two firings reaches whichever lane runs
next — there is no offset under which one lane always claims first. The offset
that remains is only there so the two runs do not start in the same second.

The second reason is the one that matters more: "turn this into a pull request I
will review" and "run these commands against the cluster now" are different
decisions, and they need different labels. Adding `ready-for-remediation` to
the Incidents Repo was part of enabling this app; it is there now, described as
authorising one catalogued live action rather than a pull request.

## Logs and state

The executor writes one JSON line per decision to stdout, so
`kubectl -n observability logs job/<name>` says which entry it chose, which
precondition failed, and what it refused. Nothing is written to disk.

The Run Ledger is a ConfigMap and is meant to be read:

```sh
kubectl -n observability get configmap alert-remediator-ledger \
  -o jsonpath='{.data.ledger\.json}' | python3 -m json.tool
```

Each record is an entry, a target, an issue number, a status and two timestamps.
Its key is the catalogue **file** plus the target, not the entry, so the two
entries that catalogue the same VolSync wedge from different sides share one
`max_runs` budget against one ReplicationSource instead of getting two.
`running` means a run claimed that entry and never reported back; the next run
that matches it hands the issue to a human instead of repeating the sequence.
`blocked` records a precondition failure that was reported on the issue, which is
how a failing precondition comments once per window rather than every five
minutes.

## Adding an entry

An entry is added the way a Runbook is: after the owner has run the sequence by
hand and watched it work. Then, in order — write the file in `../remediations/`,
name it in that directory's `kustomization.yaml`, add the verbs it needs to
`app/rbac.yaml`, narrow those verbs in `app/validatingadmissionpolicy.yaml`,
and add a test to `tests/test_remediate.py` that proves both the sequence it
renders and at least one precondition that blocks it.

Set `DRY_RUN: "true"` in the HelmRelease for the first run of a new entry. A dry
run selects an entry exactly as a real one does and runs its **preconditions for
real** — they are read-only, and proving them against the live cluster is the
whole point — then renders the steps, prints them, and stops. Nothing is
executed, the `verify` checks are not run at all (verifying a cluster no command
touched would only re-report the failure it started with), the Run Ledger is not
written and no label is spent. The run ends at the outcome `dry run` and exits 0.
`DRY_RUN` writes nothing to GitHub either, so the report is printed in the run's
own log as `dry_run_comment` rather than commented on the issue — read it with
`kubectl -n observability logs job/<name>`. Because it spends nothing, the next
cron tick does it again: a dry run is something you switch on, read once, and
switch off.
