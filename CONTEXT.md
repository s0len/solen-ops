# solen-ops

Home Kubernetes cluster operations, managed declaratively through Flux. This glossary covers the alert-investigation agent and the vocabulary around alerts and their resolution.

## Language

### Alerting

**Alert**:
A single firing condition emitted by Prometheus and delivered through Alertmanager. Identified by its label set.
_Avoid_: alarm, notification, event

**Alert Group**:
The set of alerts Alertmanager bundles under one group key (today alertname plus job). The unit that gets one Incident Issue.
_Avoid_: incident, batch

**Floor**:
The minimum time an Alert Group must have been firing before it is investigated. Self-healing and flapping alerts never reach it.
_Avoid_: delay, debounce

**Known-benign Alert**:
An alert whose cause is understood and accepted; it never opens an Incident Issue. The list is explicit, never inferred.
_Avoid_: denylisted, ignored, noise

### Investigation

**Agent**:
The unattended system that investigates Alert Groups, opens Incident Issues, and proposes Fix PRs. Distinct from the Runtime it runs on and the Model Provider it pays.
_Avoid_: bot, assistant

**Runtime**:
The software harness the Agent executes in (tools, memory, scheduling).
_Avoid_: framework, agent (for the harness alone)

**Model Provider**:
The account and API the Agent's model calls are billed to.
_Avoid_: subscription, backend

**Investigation**:
A read-only run over one Alert Group that produces a Diagnosis. It may read cluster state, metrics and logs; it never changes anything.
_Avoid_: analysis, debugging, triage

**Diagnosis**:
The Agent's written explanation of an Alert Group's likely cause, with the evidence it rests on and what it could not verify.
_Avoid_: root cause (unless verified), finding

**Triage**:
The human step of deciding what happens to an Incident Issue, expressed as a label. Only a human triages.
_Avoid_: review, approval

**Gate**:
The deterministic step between Alertmanager and the Agent that decides whether an Alert Group becomes an Investigation, a Bare Issue, or a comment on an existing Incident Issue. It applies the Floor, the Run Budget and deduplication; it never reasons.
_Avoid_: sidecar, proxy, receiver

**Run Budget**:
The maximum number of Investigations per day. Beyond it, Alert Groups still produce Bare Issues, never nothing.
_Avoid_: rate limit, quota

**Runbook**:
Knowledge about a specific alert or failure pattern, kept in the Incidents Repo, that the Agent reads before investigating. Only humans merge changes to it.
_Avoid_: memory, notes, knowledge base

### Tracking and resolution

**Incidents Repo**:
The private repository that holds Incident Issues and Runbooks, kept apart from this public repository so evidence never becomes world-readable.
_Avoid_: issues repo, ops repo

**Incident Issue**:
The one GitHub issue in the Incidents Repo opened for an Alert Group. Repeat firings of the same group comment on it; they never open a second one. It is never closed automatically.
_Avoid_: ticket, alert issue, bug

**Bare Issue**:
An Incident Issue filed with the alert payload and no Diagnosis, because the Run Budget was exhausted. It is labelled as uninvestigated.
_Avoid_: stub, placeholder

**Fix PR**:
A pull request authored by the Agent against this repository proposing a change intended to be a Resolution, linked to its Incident Issue. It is never merged automatically.
_Avoid_: patch, auto-fix, remediation

**Resolution**:
Something reviewed on the main branch that removes an alert's cause: a change to a file, a silence with a recorded reason, or a Catalogued Remediation that ran. An improvised live cluster action is never a Resolution.
_Avoid_: fix (for improvised live actions), workaround

**Catalogued Remediation**:
A live cluster action written out in full in this repository — its exact commands, its preconditions, its blast radius — and merged before it is ever run. The commands are the artefact a human reviews; running them is not a judgement anyone makes afterwards.
_Avoid_: auto-remediation, self-healing, runbook automation

**Remediation Catalogue**:
The set of Catalogued Remediations, one file per known failure. An entry is added only after a human has run the sequence by hand and seen it work.
_Avoid_: playbook, action library

**Remediator**:
The deterministic executor that runs a Catalogued Remediation. It matches an Incident Issue to an entry and runs that entry unchanged; it never composes a command, never calls a model, and never changes a file. Distinct from the Agent, which reasons and cannot act.
_Avoid_: healer, operator, fixer

**Precondition**:
A read-only check an entry must pass before any of its commands run, re-proving on live state the diagnosis the entry was written for. All of them must pass; the first failure stops the run.
_Avoid_: guard, assertion, sanity check

**Blast Radius**:
The sentence in an entry naming everything its commands can touch and what is lost if the diagnosis is wrong. It is quoted back onto the Incident Issue whenever the entry runs.
_Avoid_: impact, scope

**Run Ledger**:
The record of which entries have run, against which target and when. It enforces each entry's maximum runs per window and is what makes an interrupted run visible rather than silent.
_Avoid_: history, audit log, state
