---
status: accepted
date: 2026-11-02
---

# Renovate automerges by default; what a rollback cannot undo waits for the owner

In the nine weeks before this decision Renovate opened about 48 changes a week and the owner merged them by hand in sweeps: 292 of 400 merges landed within a minute of the previous one. Every upgrade that broke something in that period (home-assistant 2026.9, navidrome 0.64, vacuula 1.1, actions-runner-controller 0.15, bazarr) was merged by hand and caught afterwards by Flux and Alertmanager, never in review. Manual merging bought timing, not scrutiny.

So minor, patch and digest updates now automerge through a pull request once CI is green, at most one per hourly Renovate run, between 07:00 and 22:00 Stockholm time. Releases whose datasource carries a date wait three days first; most ghcr images carry none and merge as soon as CI passes. The safety net for an ordinary app is the HelmRelease's wait and rollback plus the Flux alerts, the same net that caught every failure before.

A Helm rollback restores manifests, not data, and some components take the whole cluster down at once. Those never automerge and arrive on Saturdays for a manual review: Nextcloud and its photo backup, CloudNativePG, Rook-Ceph, VolSync, kopiur, OpenEBS, Cilium, CoreDNS, Flux, Envoy Gateway with the Gateway API CRDs, external-dns, cert-manager, kanidm, External Secrets, Talos and Kubernetes, qBittorrent (private trackers whitelist client versions), and every major. The Hermes image stays manual as ADR-0003 decided.

## Consequences

- Protection is a list in `.renovate/autoMerge.json5`, so a new app automerges unless it is added there. A component that holds data or serves the whole cluster is added to the list in the commit that introduces it.
- Branch automerge is gone. CI triggers only on pull requests, so a branch pushed to main ran no checks at all; Gateway API v1.7.0 reached main that way on 2026-10-29.
- A ruleset on main requires `Flux Local - Success` and `Image Pull - Success` from GitHub Actions, and only repository admins bypass it. The owner still commits straight to main; Renovate cannot merge past a red check.
- `platformAutomerge` is off: GitHub-native automerge ignores `automergeSchedule` and merges every PR the moment it turns green.
- ADR-0001 is unchanged for the Agent. Its Fix PRs are never merged automatically; only Renovate's lane moved.
