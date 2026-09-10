---
status: accepted
date: 2026-09-10
---

# Incident Issues and Runbooks live in a separate private repository

This repository is public. Incident Issues carry pod and node names, log excerpts and whatever evidence the Agent pastes, and Runbooks are the distilled incident history of the cluster. Relying on a language model to redact that is not something to build on, and making this repository private would break the Renovate and label workflows it depends on.

Issues and Runbooks therefore live in `s0len/solen-ops-incidents`, private. Fix PRs still target this repository and close their Incident Issue across repos. The cost is one cross-repo link per issue and a second repository to install labels and credentials on.

## Considered options

- Keep everything here and instruct the Agent to redact: rejected, redaction by a model is unreliable and the failure is permanent once indexed.
- Make this repository private: rejected, it breaks existing automation and the repository is public on purpose.
