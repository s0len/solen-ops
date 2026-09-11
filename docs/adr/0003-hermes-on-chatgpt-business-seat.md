---
status: accepted
date: 2026-09-10
---

# Run the alert Agent on Hermes Agent, paid through a ChatGPT Business seat via device-code login

The Agent's model is paid through the owner's existing ChatGPT Business (formerly Team) standard seat rather than an API key. OpenAI grants nothing for third-party harnesses in its terms, but publicly endorses pi, OpenClaw and OpenCode on ChatGPT subscriptions and documents unattended ChatGPT-login use of its own CLI; no enforcement against harnesses is known. Anthropic prohibits the equivalent by contract, which is why this Agent is not on a Claude subscription. This path is tolerated, not contractual, and Business-seat eligibility for Hermes is undocumented; the owner accepts that.

The Runtime is Hermes Agent, chosen for its built-in webhook adapter, cron scheduler, toolset restriction, unattended deny mode and official image, at the price of a weekly release cadence with no stable tag and a Codex login that only works through a device-code OAuth flow with a refresh token on writable storage. The alternative, Codex CLI with an admin-issued Business access token and a hand-written receiver, is OpenAI's own documented pattern and remains the fallback if Hermes cannot hold the login or the seat is rejected.

## Consequences

- The OpenAI login is the owner's own identity on a shared seat; a heavy Agent day consumes the owner's window.
- The refresh token's lifetime is unpublished, so a dead login is detected by a daily heartbeat and a Prometheus alert, not by Hermes itself.
- Hermes upgrades are merged manually after reading release notes; automerge is never enabled for this image.
- A Gate sidecar the owner writes signs webhooks, deduplicates by group key and enforces the daily Run Budget, because Hermes does none of these.

## Revisited 2026-09-11: the credential mechanism stays as it is

The first credential was invalidated after about thirty minutes and sixteen model calls, twice, and a second login behaved the same way. Upstream has open issues for Hermes' credential pool replaying an already-consumed refresh token, which invalidates the chain server-side, and for the pool never recovering from an external auth file. A survey of 44 public Kubernetes deployments of this image found none using the device-code pool: nine route through a LiteLLM gateway, five use a static API key, and the single Codex user runs the Codex app-server runtime.

**That runtime was investigated and rejected for v2026.9.7, on three grounds established by reading the shipped code rather than documentation.** `model.openai_runtime: codex_app_server` is read at exactly one place, inside the credential-pool resolution path, so it does not replace the pool but depends on a live entry in it; the pool skips entries marked dead, so the setting silently disengages at the moment the credential fails and Hermes reverts to the previous mode. On that runtime the official CLI executes commands and Hermes only answers approval requests, and an unattended session has no approver, so every tool call returns a decline and the Agent could run nothing. The `approvals.deny` globs are reachable only from Hermes' own terminal guard chain and would no longer govern execution, while the smoke gate would still pass all of its block assertions, making the safety layer falsely green.

The decision therefore stands unchanged, with the risk now visible rather than silent: `AlertAgentHeartbeatStale` pages within 26 hours of the login dying, and the login runbook makes the repair a one-command job. Re-examine on a newer Hermes, where the runtime may be wired to the OAuth path; the fallbacks remain a static API key or a LiteLLM gateway, both of which end the refresh-chain failure at the cost of a separate bill.
