# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker      | Meaning                                                          |
| -------------------------- | ------------------------- | ---------------------------------------------------------------- |
| `needs-triage`             | `needs-triage`            | Maintainer needs to evaluate this issue                           |
| `needs-info`               | `needs-info`              | Waiting on reporter for more information                          |
| `ready-for-agent`          | `ready-for-agent`         | Fully specified: the Fix lane may open ONE pull request for it    |
| —                          | `ready-for-remediation`   | Authorises ONE live, destructive cluster action — see below       |
| `ready-for-human`          | `ready-for-human`         | Requires human implementation                                     |
| `wontfix`                  | `wontfix`                 | Will not be actioned                                              |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## The two labels that make something happen

Both are one-shot triggers: the lane that consumes one removes it, and re-applying it is how you ask again. They are not interchangeable.

`ready-for-agent` is read by the Hermes `fix` cron every five minutes. It turns the oldest issue carrying it into a pull request against `solen-ops`. Nothing is merged, nothing touches the cluster, and the diff is reviewed before anything happens — so the cost of applying it to the wrong issue is a pull request you close.

`ready-for-remediation` is read by the Remediator (`kubernetes/apps/observability/alert-agent/remediator`, currently shipped disabled). It authorises that issue's catalogued command sequence to **run against the live cluster now** — deletes included. There is no diff, no review step and no undo: applying it is the review. Before applying it, read the entry it will run under `kubernetes/apps/observability/alert-agent/remediations/`, and in particular its `blast_radius`, which is quoted back onto the issue on every run. The Remediator binds the run to the alerts recorded in that issue's body and re-proves every precondition first, but neither of those is a substitute for having read what it will do.

Edit the right-hand column to match whatever vocabulary you actually use.
