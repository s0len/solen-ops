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
