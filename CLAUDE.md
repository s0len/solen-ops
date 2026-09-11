# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Home Assistant Notes

- When creating automations, do NOT include the `id` field - it requires a server restart to take effect. Let HA auto-generate the ID.

## Project Overview

This is a Kubernetes home operations repository for a self-hosted bare-metal cluster running on Talos Linux. It uses GitOps principles with Flux CD to manage all deployments declaratively.

## Task Runner

`.tasks/ops` is the cluster task runner (available as `ops` on PATH via mise's `_.path`). Run `.tasks/ops --help` for the command list; run it with no arguments for the interactive menu.

## Key Files

- `config.yaml` - Generated Talos cluster config (don't edit directly)

## Conventions

- Apps are disabled by commenting out their `ks.yaml` reference in the namespace's `kustomization.yaml`

## Git Workflow

- **Commit straight to `main`** — no PRs, no feature branches. This is a personal cluster repo; the owner reviews changes by reading the diff before committing, not via PR review.
- **Always push immediately after committing** — Flux only reconciles what's on `origin/main`. A commit that hasn't been pushed has zero effect on the cluster.
- For urgent fixes, offer to `flux reconcile` after pushing to skip the polling interval.
- If working from a worktree on a non-main branch, push the commit straight to main with `git push origin HEAD:main` (fast-forward) rather than opening a PR.

**The one exception (ADR-0001):** the unattended alert Agent (`kubernetes/apps/observability/alert-agent`, machine account `solen-ops-agent`) proposes every change as a pull request from an `agent/incident-<n>` branch, because nobody reviewed its diff before it was written. Its PRs are never merged automatically and nothing in this repository automerges. Leave that as it is — a Fix PR is not a mistake to "correct" into a commit on main, and the Agent must never be given a shortcut past the owner's review.

## Agent skills

### Issue tracker

Issues live in the **private** repo `s0len/solen-ops-incidents`, never here (this repo is public). Every `gh issue` command needs `--repo s0len/solen-ops-incidents`. PRs stay in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` plus `docs/adr/` at the repo root, created lazily by `/domain-modeling`. See `docs/agents/domain.md`.
