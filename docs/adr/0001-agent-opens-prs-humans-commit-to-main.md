---
status: accepted
date: 2026-09-10
---

# The alert Agent opens pull requests; humans commit to main; live actions are never a Resolution

Humans commit straight to main because the owner reviews the diff before committing. An unattended Agent cannot be reviewed that way, so it is the one actor that must propose changes as pull requests, never merged automatically, in the same lane Renovate already uses. Flux only reconciles main, so a pull request is the one place a machine-authored change can sit and be read without touching the cluster.

The Agent's cluster access is read-only, cluster-wide, and it never execs, restarts or applies. A live action leaves no trace in git and is exactly what GitOps exists to prevent; several documented incidents here (krbd blocklist fencing, raw-OSD activation deadlock) are cases where the obvious live action is wrong. A Resolution is therefore a merged change on main, or a silence with a recorded reason. Actions outside the cluster (TrueNAS, UniFi, Talos nodes) may be advised in an Incident Issue, never performed.

## Consequences

- The Agent has its own ServiceAccount with a read-only ClusterRole; no cluster-admin identity is reused.
- Fix PRs run the existing PR checks (flux-local diff, image-pull) before a human reads them.
- If a Diagnosis needs an exec or a physical check, the Agent applies `needs-info` and waits for the human.
