#!/usr/bin/env python3
"""gate.py — the Gate between Alertmanager and the alert-investigation Agent.

Receives Alertmanager v4 webhook notifications and turns each Alert Group into
exactly one Incident Issue in the Incidents Repo, deterministically and without
a model call. Deduplication is a hidden HTML-comment marker in the issue body
carrying a hash of the group key; no per-group labels. A local index maps that
hash to the issue number, because GitHub's issue listing does not show a create
for a second or more and an Alertmanager retry inside that window would
otherwise open a second Incident Issue.

Decision per notification, in order:

  * resolved, open Incident Issue      -> "resolved at" comment, never close
  * resolved, no open Incident Issue   -> log and drop
  * firing,   open Incident Issue      -> "still firing at" comment
  * firing,   no open Incident Issue,
    Run Budget remaining               -> create the Incident Issue, consume one
                                          slot, forward the signed Investigation
                                          prompt to Hermes
  * firing,   no open Incident Issue,
    Run Budget exhausted               -> create a Bare Issue, forward nothing

The Run Budget is a per-UTC-day counter in a JSON file on the PVC; it and the
Incident Issue index are the only things the Gate writes to disk. Stdlib only:
runs on the slim python image as non-root with a read-only root filesystem.
"""
import hashlib
import hmac
import http.client
import json
import os
import signal
import string
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

DEFAULT_INCIDENTS_REPO = "s0len/solen-ops-incidents"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_RUN_BUDGET_PER_DAY = 10
DEFAULT_PROMETHEUS_URL = "http://prometheus-operated.observability.svc.cluster.local:9090"
DEFAULT_VICTORIALOGS_URL = "http://victoria-logs-server.observability.svc.cluster.local:9428"
DEFAULT_ALERTMANAGER_URL = "http://alertmanager-operated.observability.svc.cluster.local:9093"
HERMES_TIMESTAMP_HEADER = "X-Webhook-Timestamp"
HERMES_SIGNATURE_HEADER = "X-Webhook-Signature-V2"
HERMES_REQUEST_ID_HEADER = "X-Request-ID"
HERMES_PROMPT_FIELD = "prompt"
METRIC_PREFIX = "alert_agent_gate_"
HEARTBEAT_ABSENT_SECONDS = 1e9
MARKER_PREFIX = "<!-- alert-agent:group="
MARKER_SUFFIX = " -->"
MARKER_HASH_CHARS = 24
NEW_ISSUE_LABELS = ("needs-triage",)
BARE_ISSUE_LABEL = "uninvestigated"
TITLE_MAX_CHARS = 200
MAX_REQUEST_BYTES = 8 * 1024 * 1024
ISSUES_PER_PAGE = 100
MAX_ISSUE_PAGES = 10
INDEX_MAX_ENTRIES = 512
INDEX_MAX_AGE_SECONDS = 30 * 86400
USER_AGENT = "alert-agent-gate"
WEBHOOK_PATH = "/webhook"
HEALTH_PATHS = ("/healthz", "/health")
METRICS_PATH = "/metrics"
IDENTIFYING_LABELS = ("namespace", "pod", "container", "node", "instance", "job", "severity")


# --------------------------------------------------------------------------- logging

def log(event: str, **fields: Any) -> None:
    """Emit one JSON line to stdout."""
    record = {"ts": now_iso(), "event": event}
    record.update(fields)
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def now_iso(now: Optional[datetime] = None) -> str:
    """UTC timestamp in the form Alertmanager itself uses, to the second."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class Config:
    port: int = 8080
    github_api_url: str = DEFAULT_API_URL
    github_token: str = ""
    incidents_repo: str = DEFAULT_INCIDENTS_REPO
    github_timeout: float = 15.0
    run_budget_per_day: int = DEFAULT_RUN_BUDGET_PER_DAY
    budget_state_file: str = ""
    incident_index_file: str = ""
    hermes_webhook_url: str = ""
    hermes_webhook_secret: str = ""
    hermes_timeout: float = 10.0
    heartbeat_file: str = ""
    prometheus_url: str = DEFAULT_PROMETHEUS_URL
    victorialogs_url: str = DEFAULT_VICTORIALOGS_URL
    alertmanager_url: str = DEFAULT_ALERTMANAGER_URL
    fake_now: str = ""

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            port=int(env.get("PORT", "8080")),
            github_api_url=env.get("GITHUB_API_URL", DEFAULT_API_URL).rstrip("/"),
            github_token=env.get("GITHUB_TOKEN", ""),
            incidents_repo=env.get("GITHUB_INCIDENTS_REPO", DEFAULT_INCIDENTS_REPO),
            github_timeout=float(env.get("GITHUB_TIMEOUT_SECONDS", "15")),
            run_budget_per_day=int(env.get("RUN_BUDGET_PER_DAY", str(DEFAULT_RUN_BUDGET_PER_DAY))),
            budget_state_file=env.get("BUDGET_STATE_FILE", ""),
            incident_index_file=env.get("INCIDENT_INDEX_FILE", ""),
            hermes_webhook_url=env.get("HERMES_WEBHOOK_URL", ""),
            hermes_webhook_secret=env.get("HERMES_WEBHOOK_SECRET", ""),
            hermes_timeout=float(env.get("HERMES_TIMEOUT_SECONDS", "10")),
            heartbeat_file=env.get("HEARTBEAT_FILE", ""),
            prometheus_url=env.get("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL).rstrip("/"),
            victorialogs_url=env.get("VICTORIALOGS_URL", DEFAULT_VICTORIALOGS_URL).rstrip("/"),
            alertmanager_url=env.get("ALERTMANAGER_URL", DEFAULT_ALERTMANAGER_URL).rstrip("/"),
            fake_now=env.get("GATE_FAKE_NOW", ""),
        )

    def clock(self) -> Callable[[], datetime]:
        """Real UTC time, or the fixed instant in GATE_FAKE_NOW (tests only)."""
        if not self.fake_now:
            return lambda: datetime.now(timezone.utc)
        fixed = datetime.fromisoformat(self.fake_now.replace("Z", "+00:00"))
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=timezone.utc)
        return lambda: fixed


# --------------------------------------------------------------------------- payload

class MalformedPayload(ValueError):
    """The request body is not an Alertmanager v4 webhook notification."""


@dataclass(frozen=True)
class Notification:
    """The parts of an Alertmanager v4 webhook notification the Gate acts on."""

    group_key: str
    status: str
    alerts: list
    group_labels: dict
    common_labels: dict
    common_annotations: dict
    external_url: str
    receiver: str
    version: str

    @property
    def firing(self) -> bool:
        return self.status == "firing"

    @property
    def alertname(self) -> str:
        for labels in (self.group_labels, self.common_labels, *(a.get("labels", {}) for a in self.alerts)):
            name = labels.get("alertname")
            if name:
                return str(name)
        return "unknown-alert"

    @property
    def summary(self) -> str:
        for annotations in (self.common_annotations, *(a.get("annotations", {}) for a in self.alerts)):
            summary = annotations.get("summary")
            if summary:
                return " ".join(str(summary).split())
        return ""


def parse_notification(raw: bytes) -> Notification:
    """Validate and decode a webhook body; raise MalformedPayload otherwise."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MalformedPayload(f"body is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedPayload("body must be a JSON object")

    group_key = data.get("groupKey")
    if not isinstance(group_key, str) or not group_key:
        raise MalformedPayload("groupKey must be a non-empty string")

    status = data.get("status")
    if status not in ("firing", "resolved"):
        raise MalformedPayload("status must be 'firing' or 'resolved'")

    alerts = data.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise MalformedPayload("alerts must be a non-empty list")
    for alert in alerts:
        if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
            raise MalformedPayload("every alert must be an object with a labels object")

    def as_dict(key: str) -> dict:
        value = data.get(key, {})
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MalformedPayload(f"{key} must be an object")
        return value

    return Notification(
        group_key=group_key,
        status=status,
        alerts=alerts,
        group_labels=as_dict("groupLabels"),
        common_labels=as_dict("commonLabels"),
        common_annotations=as_dict("commonAnnotations"),
        external_url=str(data.get("externalURL") or ""),
        receiver=str(data.get("receiver") or ""),
        version=str(data.get("version") or ""),
    )


# --------------------------------------------------------------------------- marker

def group_hash(group_key: str) -> str:
    return hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:MARKER_HASH_CHARS]


def group_marker(group_key: str) -> str:
    """The hidden marker that ties an Incident Issue to its Alert Group."""
    return f"{MARKER_PREFIX}{group_hash(group_key)}{MARKER_SUFFIX}"


# --------------------------------------------------------------------------- rendering

def md_code(value: Any) -> str:
    text = " ".join(str(value).split()) or " "
    return "`" + text.replace("`", "‘").replace("|", "∣") + "`"


def md_table(mapping: dict, headers: tuple) -> list:
    lines = [f"| {headers[0]} | {headers[1]} |", "| --- | --- |"]
    for key in sorted(mapping):
        lines.append(f"| {md_code(key)} | {md_code(mapping[key])} |")
    return lines


def short_labels(labels: dict) -> str:
    parts = [f'{k}={md_code(labels[k])}' for k in IDENTIFYING_LABELS if labels.get(k)]
    return " ".join(parts)


def render_issue_title(n: Notification) -> str:
    summary = n.summary
    title = f"{n.alertname}: {summary}" if summary else f"{n.alertname} ({len(n.alerts)} alerts)"
    if len(title) > TITLE_MAX_CHARS:
        title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return title


def render_alert(index: int, alert: dict) -> list:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    lines = [f"### {index}. {labels.get('alertname', 'alert')}", ""]
    meta = [f"status {md_code(alert.get('status', 'unknown'))}", f"startsAt {md_code(alert.get('startsAt', ''))}"]
    ends_at = alert.get("endsAt") or ""
    if ends_at and not ends_at.startswith("0001-"):
        meta.append(f"endsAt {md_code(ends_at)}")
    lines.append(" · ".join(meta))
    if alert.get("generatorURL"):
        lines.append("")
        lines.append(f"[Prometheus expression]({alert['generatorURL']})")
    if labels:
        lines.append("")
        lines.extend(md_table(labels, ("Label", "Value")))
    if annotations:
        lines.append("")
        lines.extend(md_table(annotations, ("Annotation", "Value")))
    lines.append("")
    return lines


def render_issue_body(n: Notification, marker: str, now: str) -> str:
    """Human-readable rendering of the Alert Group, with the marker at the end."""
    group = ", ".join(f'{k}={md_code(v)}' for k, v in sorted(n.group_labels.items())) or md_code(n.group_key)
    lines = [
        f"## Alert Group {group}",
        "",
        f"Opened by the Gate at {md_code(now)} for a {md_code(n.status)} notification with "
        f"{len(n.alerts)} alert(s). Group key {md_code(n.group_key)}.",
    ]
    if n.external_url:
        lines.append("")
        lines.append(f"[Alertmanager]({n.external_url})")
    if n.common_labels:
        lines.append("")
        lines.append("## Common labels")
        lines.append("")
        lines.extend(md_table(n.common_labels, ("Label", "Value")))
    if n.common_annotations:
        lines.append("")
        lines.append("## Common annotations")
        lines.append("")
        lines.extend(md_table(n.common_annotations, ("Annotation", "Value")))
    lines.append("")
    lines.append("## Alerts")
    lines.append("")
    for index, alert in enumerate(n.alerts, start=1):
        lines.extend(render_alert(index, alert))
    lines.append(marker)
    return "\n".join(lines) + "\n"


def render_alert_list(n: Notification) -> list:
    lines = []
    for alert in n.alerts:
        labels = alert.get("labels") or {}
        entry = f"- {md_code(labels.get('alertname', 'alert'))} {short_labels(labels)}".rstrip()
        lines.append(entry)
    return lines


def render_still_firing_comment(n: Notification, now: str) -> str:
    count = len(n.alerts)
    lines = [f"Still firing at {now} — {count} alert{'s' if count != 1 else ''} in this Alert Group.", ""]
    lines.extend(render_alert_list(n))
    return "\n".join(lines) + "\n"


def render_resolved_comment(n: Notification, now: str) -> str:
    count = len(n.alerts)
    lines = [
        f"Resolved at {now} — {count} alert{'s' if count != 1 else ''} resolved. "
        "The Incident Issue stays open until a human triages it.",
        "",
    ]
    lines.extend(render_alert_list(n))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- github

class GitHubError(Exception):
    """GitHub could not be used; `unreachable` separates no-answer from a bad answer."""

    def __init__(self, message: str, *, unreachable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.unreachable = unreachable
        self.status = status


class GitHubClient:
    """The few Issues API calls the Gate needs, over urllib."""

    def __init__(self, api_url: str, token: str, repo: str, timeout: float = 15.0):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.repo = repo
        self.timeout = timeout

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"GitHub returned {exc.code} for {method} {path}", status=exc.code) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise GitHubError(f"GitHub unreachable for {method} {path}: {exc}", unreachable=True) from exc
        try:
            return json.loads(payload.decode("utf-8")) if payload else None
        except ValueError as exc:
            raise GitHubError(f"GitHub returned non-JSON for {method} {path}") from exc

    def get_issue(self, number: int) -> Optional[dict]:
        """One issue by number, or None when it is gone.

        Unlike the listing this read is consistent immediately after a create,
        which is what makes the Incident Issue index trustworthy.
        """
        try:
            issue = self._request("GET", f"/repos/{self.repo}/issues/{number}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return issue if isinstance(issue, dict) else None

    def find_incident_issue(self, marker: str) -> Optional[dict]:
        """The oldest open issue whose body contains `marker`, or None.

        Lists open issues oldest-first and filters locally: exact match, no
        dependency on the search index catching up, no per-group labels. The
        listing lags a create by a second or more, so the Incident Issue index
        is asked first and this stays the fallback.
        """
        for page in range(1, MAX_ISSUE_PAGES + 1):
            items = self._request(
                "GET",
                f"/repos/{self.repo}/issues",
                params={
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": ISSUES_PER_PAGE,
                    "page": page,
                },
            )
            if not isinstance(items, list):
                raise GitHubError("GitHub issue listing was not a list")
            for item in items:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                if marker in (item.get("body") or ""):
                    return item
            if len(items) < ISSUES_PER_PAGE:
                return None
        log("issue_listing_capped", pages=MAX_ISSUE_PAGES, repo=self.repo)
        return None

    def create_incident_issue(self, title: str, body: str, labels: list) -> dict:
        issue = self._request(
            "POST",
            f"/repos/{self.repo}/issues",
            body={"title": title, "body": body, "labels": list(labels)},
        )
        if not isinstance(issue, dict) or "number" not in issue:
            raise GitHubError("GitHub did not return the created issue")
        return issue

    def comment(self, issue_number: int, body: str) -> Any:
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            body={"body": body},
        )


# --------------------------------------------------------------------------- incident index

class IncidentIndex:
    """Group hash -> Incident Issue number, so deduplication does not wait for
    GitHub's issue listing.

    `GET /repos/{repo}/issues` is not read-after-write consistent: measured
    against the real API, a created issue was still absent from the listing
    over a second later, while `GET /repos/{repo}/issues/{number}` returned it
    on the first attempt. An Alertmanager retry inside that window used to open
    a second Incident Issue. Every hit here is therefore re-read by number
    before it is trusted, and a hit that is gone, closed, a pull request or no
    longer carrying the marker is dropped so the listing decides instead.

    Same discipline as the Run Budget: the file is authoritative and re-read on
    every question, writes go to a sibling temp file and are renamed into
    place, an unreadable file is logged and treated as empty. Bounded to
    INDEX_MAX_ENTRIES entries and INDEX_MAX_AGE_SECONDS of age, oldest evicted
    first; an evicted group is simply found through the listing again. Without
    a state file the index lives in memory only.
    """

    def __init__(self, state_file: str = ""):
        self.state_file = state_file
        self._lock = threading.Lock()
        self._memory: dict = {}

    def _load(self) -> dict:
        if not self.state_file:
            return dict(self._memory)
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return dict(self._memory)
        except (OSError, ValueError) as exc:
            log("incident_index_unreadable", file=self.state_file, error=str(exc))
            return dict(self._memory)
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            log("incident_index_invalid", file=self.state_file)
            return dict(self._memory)
        return {
            key: {"issue": entry["issue"], "at": entry["at"]}
            for key, entry in entries.items()
            if isinstance(entry, dict) and isinstance(entry.get("at"), str)
            and isinstance(entry.get("issue"), int) and not isinstance(entry["issue"], bool)
            and entry["issue"] > 0
        }

    def _store(self, entries: dict) -> None:
        self._memory = dict(entries)
        if not self.state_file:
            return
        temp = f"{self.state_file}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"entries": entries}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_file)
        except OSError as exc:
            log("incident_index_write_failed", file=self.state_file, error=str(exc))

    @staticmethod
    def _prune(entries: dict, now: datetime) -> dict:
        # now_iso is fixed-width UTC, so lexical order is chronological order.
        cutoff = now_iso(now - timedelta(seconds=INDEX_MAX_AGE_SECONDS))
        fresh = {key: entry for key, entry in entries.items() if entry["at"] >= cutoff}
        if len(fresh) <= INDEX_MAX_ENTRIES:
            return fresh
        newest = sorted(fresh.items(), key=lambda item: (item[1]["at"], item[0]), reverse=True)
        return dict(newest[:INDEX_MAX_ENTRIES])

    def lookup(self, key: str) -> Optional[int]:
        entry = self._load().get(key)
        return int(entry["issue"]) if entry else None

    def remember(self, key: str, issue_number: int, now: datetime) -> None:
        with self._lock:
            entries = self._load()
            entries[key] = {"issue": int(issue_number), "at": now_iso(now)}
            self._store(self._prune(entries, now))

    def forget(self, key: str) -> None:
        with self._lock:
            entries = self._load()
            if entries.pop(key, None) is not None:
                self._store(entries)


# --------------------------------------------------------------------------- run budget

class RunBudget:
    """The per-UTC-day Investigation budget, persisted as one small JSON file.

    The file is authoritative and re-read on every question, so a restart in the
    same UTC day continues the count and an edit by hand takes effect at once.
    Writes go to a sibling temp file and are renamed into place. Without a
    state file the count lives in memory only.
    """

    def __init__(self, limit: int, state_file: str = ""):
        self.limit = max(0, int(limit))
        self.state_file = state_file
        self._lock = threading.Lock()
        self._memory = {"date": "", "used": 0}

    @staticmethod
    def day(now: datetime) -> str:
        return now.astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        if not self.state_file:
            return dict(self._memory)
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return dict(self._memory)
        except (OSError, ValueError) as exc:
            log("budget_state_unreadable", file=self.state_file, error=str(exc))
            return dict(self._memory)
        if (isinstance(data, dict) and isinstance(data.get("date"), str)
                and isinstance(data.get("used"), int) and data["used"] >= 0):
            return {"date": data["date"], "used": data["used"]}
        log("budget_state_invalid", file=self.state_file)
        return dict(self._memory)

    def _store(self, state: dict) -> None:
        self._memory = dict(state)
        if not self.state_file:
            return
        temp = f"{self.state_file}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_file)
        except OSError as exc:
            log("budget_state_write_failed", file=self.state_file, error=str(exc))

    def used(self, now: datetime) -> int:
        state = self._load()
        return state["used"] if state["date"] == self.day(now) else 0

    def remaining(self, now: datetime) -> int:
        return max(0, self.limit - self.used(now))

    def consume(self, now: datetime) -> int:
        """Count one Investigation against today and return today's total."""
        with self._lock:
            used = self.used(now) + 1
            self._store({"date": self.day(now), "used": used})
            return used


# --------------------------------------------------------------------------- investigation prompt

INVESTIGATION_PROMPT = string.Template("""\
You are the alert-investigation Agent for the solen-ops Kubernetes cluster (Talos Linux, Flux CD, Rook-Ceph, VolSync). Alertmanager has forwarded an Alert Group that has been firing past the Floor. Your one task is an Investigation: a read-only run that produces a Diagnosis and posts it as ONE comment on the Incident Issue below. Nothing else.

## Incident Issue
- Incidents Repo: $incidents_repo
- Issue: #$issue_number ($issue_url)
- Alertname: $alertname
- Group key: $group_key
- Notification received by the Gate: $received_at
- Alertmanager: $external_url

## Alerts in this group ($alert_count)

$alerts

## Step 1: read the Runbooks first
Clone the Incidents Repo shallowly into this run's own directory (`gh repo clone $incidents_repo /tmp/incidents-$issue_number -- --depth 1`; if that path already exists from an earlier attempt, use it as it is), then read `runbooks/$alertname.md` under it if it exists and EVERY file under its `runbooks/patterns/`. Runbooks hold this cluster's incident history as hypotheses and how to check them. Start from their hypotheses instead of rediscovering them; a node-wide symptom described in a pattern Runbook must be recognised as one.

## Step 2: gather evidence, read-only
- kubectl read verbs only: get, describe, logs, top, events, api-resources, explain. Secrets are not readable and must not be attempted.
- PromQL against Prometheus at $prometheus_url, for example: curl -s '$prometheus_url/api/v1/query' --data-urlencode 'query=<expr>'
- LogsQL against VictoriaLogs at $victorialogs_url, for example: curl -s '$victorialogs_url/select/logsql/query' --data-urlencode 'query=<logsql>' --data-urlencode 'limit=100'
- The Alertmanager API at $alertmanager_url, for example: curl -s '$alertmanager_url/api/v2/alerts' (co-firing alerts) and '$alertmanager_url/api/v2/silences'
- Each alert's generatorURL above carries the exact expression that fired; query it and its neighbours over the firing window.
- Your commands are screened before they run. Inline interpreter scripts are refused: never `python3 -c`, `sh -c`, `bash -c`, `perl -e` or any `-c`/`-e` form, and never pipe into one. There is no `jq`. Shape JSON with the tools that are allowed instead: `kubectl -o jsonpath=...` or `-o custom-columns=...`, `curl ... | head -n`, or `curl ... -o /tmp/x.json` and then read the file.
- A refused command is not the end of the Investigation. Note it, gather what you can by another route, and record in the Diagnosis what you could not check and why.

## Rules that are never broken
- NEVER exec, restart, delete, apply, patch, edit, scale, drain, cordon, silence, label, annotate or otherwise change anything in the cluster, in Alertmanager or in any repository. No kubectl exec/cp/apply/patch/edit/delete/scale/rollout/drain/cordon, no flux suspend/resume/reconcile, no git push, no pull request, no write to any Runbook.
- If the Diagnosis needs an exec, a physical check (cable, disk, UPS, switch) or anything only the owner can do, add the label needs-info to the Incident Issue (`gh issue edit $issue_number --repo $incidents_repo --add-label needs-info`) and state exactly what the owner should run or look at and which result would confirm or refute the hypothesis.
- Never paste anything that looks like a credential, token or private key into the comment.
- Stop after a reasonable number of checks. A Diagnosis with honest gaps beats a run that never posts.

## Step 3: post the Diagnosis
Post exactly ONE comment on the Incident Issue (`gh issue comment $issue_number --repo $incidents_repo --body-file <file>`), in Markdown, with these sections in this order:

### Verified evidence
What you observed. Every item is followed by the exact command or query that produced it and the relevant excerpt of its output. Only things you actually ran and saw.

### Unverified hypotheses
Likely causes you could not confirm, each with why it is plausible and what would confirm or refute it.

### Checks a human must run
Anything that needs an exec, a physical inspection or access you do not have. If this section is not empty, the needs-info label must be on the issue.

Cite every command and query verbatim next to the evidence it produced so the owner can reproduce it. If a Runbook should gain something from this Investigation, propose it in ONE sentence at the end of the comment under the heading "Runbook proposal"; never write to a Runbook yourself. Do not open, close, edit or relabel any issue beyond adding needs-info as described, and do not post more than one comment.
""")


def render_prompt_alert(index: int, alert: dict) -> str:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    lines = [f"### {index}. {labels.get('alertname', 'alert')} ({alert.get('status', 'unknown')})",
             f"startsAt: {alert.get('startsAt', '')}"]
    ends_at = alert.get("endsAt") or ""
    if ends_at and not ends_at.startswith("0001-"):
        lines.append(f"endsAt: {ends_at}")
    if alert.get("generatorURL"):
        lines.append(f"generatorURL: {alert['generatorURL']}")
    lines.append("labels:")
    lines.extend(f"  {key}: {labels[key]}" for key in sorted(labels))
    if annotations:
        lines.append("annotations:")
        lines.extend(f"  {key}: {' '.join(str(annotations[key]).split())}" for key in sorted(annotations))
    return "\n".join(lines)


def render_investigation_prompt(issue: dict, n: Notification, now: str, incidents_repo: str, services: dict) -> str:
    """The complete Investigation prompt; Hermes substitutes nothing but the whole text."""
    alerts = "\n\n".join(render_prompt_alert(i, a) for i, a in enumerate(n.alerts, start=1))
    return INVESTIGATION_PROMPT.safe_substitute(
        incidents_repo=incidents_repo,
        issue_number=issue["number"],
        issue_url=issue.get("html_url") or f"https://github.com/{incidents_repo}/issues/{issue['number']}",
        alertname=n.alertname,
        group_key=n.group_key,
        received_at=now,
        external_url=n.external_url or "(not given)",
        alert_count=len(n.alerts),
        alerts=alerts,
        prometheus_url=services["prometheus"],
        victorialogs_url=services["victorialogs"],
        alertmanager_url=services["alertmanager"],
    )


# --------------------------------------------------------------------------- forwarder

class HermesForwarder:
    """Signs an Investigation prompt with Hermes' generic V2 scheme and posts it.

    Signature: hex HMAC-SHA256 over ``"<timestamp>.<body>"`` with the shared
    route secret, timestamp in unix seconds, sent as X-Webhook-Signature-V2 and
    X-Webhook-Timestamp (gateway/platforms/webhook.py in hermes-agent). The
    timestamp is real wall-clock time because Hermes rejects one more than
    300 s from its own clock. Any 2xx is success; Hermes answers 202 and runs
    the Investigation asynchronously.
    """

    def __init__(self, url: str, secret: str, timeout: float, metrics: "Metrics",
                 incidents_repo: str, services: dict, wall_clock: Callable[[], float] = time.time):
        self.url = url
        self.secret = secret
        self.timeout = timeout
        self.metrics = metrics
        self.incidents_repo = incidents_repo
        self.services = services
        self.wall_clock = wall_clock

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def sign(self, timestamp: str, body: bytes) -> str:
        return hmac.new(self.secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()

    def forward(self, issue: dict, n: Notification, now: datetime) -> bool:
        number = int(issue["number"])
        prompt = render_investigation_prompt(issue, n, now_iso(now), self.incidents_repo, self.services)
        body = json.dumps({HERMES_PROMPT_FIELD: prompt}).encode("utf-8")
        timestamp = str(int(self.wall_clock()))
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                HERMES_TIMESTAMP_HEADER: timestamp,
                HERMES_SIGNATURE_HEADER: self.sign(timestamp, body),
                HERMES_REQUEST_ID_HEADER: f"{USER_AGENT}/{number}/{timestamp}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                response.read()
        except urllib.error.HTTPError as exc:
            return self._failed(number, n, f"Hermes returned {exc.code}")
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            return self._failed(number, n, f"Hermes unreachable: {exc}")
        if not 200 <= status < 300:
            return self._failed(number, n, f"Hermes returned {status}")
        self.metrics.inc("forwards_total")
        log("investigation_forwarded", issue=number, alertname=n.alertname, hermes_status=status, prompt_bytes=len(body))
        return True

    def _failed(self, number: int, n: Notification, error: str) -> bool:
        self.metrics.inc("forward_failures_total")
        log("forward_failed", issue=number, alertname=n.alertname, error=error)
        return False


# --------------------------------------------------------------------------- metrics

COUNTERS = {
    "notifications_received_total": "Alertmanager notifications received on the webhook.",
    "issues_created_total": "Incident Issues created, Bare Issues included.",
    "bare_issues_total": "Bare Issues created: Run Budget exhausted or forwarding disabled.",
    "comments_total": "Still-firing and resolved comments posted on Incident Issues.",
    "forwards_total": "Investigation prompts Hermes accepted.",
    "forward_failures_total": "Investigation prompts Hermes did not accept.",
    "github_errors_total": "Notifications that failed on a GitHub call.",
}


class Metrics:
    """Counters and callback gauges rendered in the Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in COUNTERS}
        self._gauges: list = []

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def gauge(self, name: str, help_text: str, read: Callable[[], float]) -> None:
        self._gauges.append((name, help_text, read))

    @staticmethod
    def _format(value: Any) -> str:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)

    def render(self) -> str:
        with self._lock:
            counters = dict(self._counters)
        lines = []
        for name in COUNTERS:
            full = METRIC_PREFIX + name
            lines += [f"# HELP {full} {COUNTERS[name]}", f"# TYPE {full} counter", f"{full} {counters[name]}"]
        for name, help_text, read in self._gauges:
            full = METRIC_PREFIX + name
            lines += [f"# HELP {full} {help_text}", f"# TYPE {full} gauge", f"{full} {self._format(read())}"]
        return "\n".join(lines) + "\n"


def heartbeat_age_seconds(path: str, wall_clock: Callable[[], float] = time.time) -> float:
    if not path:
        return HEARTBEAT_ABSENT_SECONDS
    try:
        return max(0.0, wall_clock() - os.stat(path).st_mtime)
    except OSError:
        return HEARTBEAT_ABSENT_SECONDS


# --------------------------------------------------------------------------- the gate

@dataclass(frozen=True)
class Outcome:
    action: str
    issue_number: Optional[int] = None
    forwarded: Optional[bool] = None
    issue: Optional[dict] = None

    def as_json(self) -> dict:
        data = {"action": self.action}
        if self.issue_number is not None:
            data["issue"] = self.issue_number
        if self.forwarded is not None:
            data["forwarded"] = self.forwarded
        return data


class Gate:
    """Turns one notification into one deterministic GitHub action.

    Ordering for a new Alert Group: create the Incident Issue, then consume a
    Run Budget slot, then forward. A failed GitHub call therefore never burns
    a slot, and a forward that fails still leaves a traceable issue.
    """

    def __init__(
        self,
        github: GitHubClient,
        index: IncidentIndex,
        run_budget: RunBudget,
        forwarder: HermesForwarder,
        metrics: Metrics,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.github = github
        self.index = index
        self.run_budget = run_budget
        self.forwarder = forwarder
        self.metrics = metrics
        self.clock = clock
        self._lock = threading.Lock()

    def handle(self, n: Notification) -> Outcome:
        self.metrics.inc("notifications_received_total")
        marker = group_marker(n.group_key)
        now = self.clock()
        stamp = now_iso(now)
        context = {"group_key": n.group_key, "alertname": n.alertname, "status": n.status, "alerts": len(n.alerts)}
        try:
            with self._lock:
                outcome = self._decide(n, marker, now, stamp, context)
        except GitHubError:
            self.metrics.inc("github_errors_total")
            raise
        if outcome.issue is None:
            return outcome
        forwarded = self.forwarder.forward(outcome.issue, n, now)
        return replace(outcome, forwarded=forwarded, issue=None)

    def _find_incident_issue(self, n: Notification, marker: str, now: datetime,
                             context: dict) -> Optional[dict]:
        """The index first, the listing second; both are confirmed by number.

        The listing lags a write in both directions, so it can report a
        just-closed Incident Issue as open. Only the by-number read is
        consistent, so it decides either way.
        """
        key = group_hash(n.group_key)
        number = self.index.lookup(key)
        if number is not None:
            indexed = self.github.get_issue(number)
            reason = self._not_the_incident_issue(indexed, marker)
            if reason is None:
                log("incident_index_hit", issue=number, **context)
                return indexed
            self.index.forget(key)
            log("incident_index_dropped", issue=number, reason=reason, **context)
        listed = self.github.find_incident_issue(marker)
        if listed is None:
            return None
        found = int(listed["number"])
        confirmed = self.github.get_issue(found)
        reason = self._not_the_incident_issue(confirmed, marker)
        if reason is not None:
            log("incident_listing_rejected", issue=found, reason=reason, **context)
            return None
        self.index.remember(key, found, now)
        return confirmed

    @staticmethod
    def _not_the_incident_issue(issue: Optional[dict], marker: str) -> Optional[str]:
        """Why an indexed issue must not absorb this notification, or None."""
        if issue is None:
            return "gone"
        if issue.get("state") != "open":
            return "closed"
        if "pull_request" in issue:
            return "pull_request"
        if marker not in (issue.get("body") or ""):
            return "marker_absent"
        return None

    def _decide(self, n: Notification, marker: str, now: datetime, stamp: str, context: dict) -> Outcome:
        issue = self._find_incident_issue(n, marker, now, context)
        if issue is not None:
            number = int(issue["number"])
            if n.firing:
                self.github.comment(number, render_still_firing_comment(n, stamp))
                self.metrics.inc("comments_total")
                log("still_firing", issue=number, **context)
                return Outcome("still-firing", number)
            self.github.comment(number, render_resolved_comment(n, stamp))
            self.metrics.inc("comments_total")
            log("resolved", issue=number, **context)
            return Outcome("resolved", number)
        if not n.firing:
            log("resolved_without_incident_issue", **context)
            return Outcome("dropped")
        return self._open_incident_issue(n, marker, now, stamp, context)

    def _open_incident_issue(self, n: Notification, marker: str, now: datetime, stamp: str, context: dict) -> Outcome:
        remaining = self.run_budget.remaining(now)
        investigate = self.forwarder.enabled and remaining > 0
        labels = list(NEW_ISSUE_LABELS)
        if not investigate:
            labels.append(BARE_ISSUE_LABEL)
        issue = self.github.create_incident_issue(render_issue_title(n), render_issue_body(n, marker, stamp), labels)
        number = int(issue["number"])
        self.index.remember(group_hash(n.group_key), number, now)
        self.metrics.inc("issues_created_total")
        if investigate:
            used = self.run_budget.consume(now)
            log("incident_issue_created", issue=number, url=issue.get("html_url"),
                budget_used=used, budget_limit=self.run_budget.limit, **context)
            return Outcome("created", number, issue=issue)
        self.metrics.inc("bare_issues_total")
        reason = "forwarding_disabled" if not self.forwarder.enabled else "run_budget_exhausted"
        log("bare_issue_created", issue=number, url=issue.get("html_url"), reason=reason,
            budget_used=self.run_budget.used(now), budget_limit=self.run_budget.limit, **context)
        return Outcome("created-bare", number)


# --------------------------------------------------------------------------- http

class GateServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple, gate: Gate):
        super().__init__(address, GateHandler)
        self.gate = gate


class GateHandler(BaseHTTPRequestHandler):
    server_version = USER_AGENT
    sys_version = ""
    protocol_version = "HTTP/1.1"
    server: GateServer

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _send(self, status: int, body: Any, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def do_GET(self) -> None:
        path = self._path()
        if path in HEALTH_PATHS:
            self._send(200, {"status": "ok"})
        elif path == METRICS_PATH:
            self._send(200, self.server.gate.metrics.render().encode("utf-8"), "text/plain; version=0.0.4")
        elif path == WEBHOOK_PATH:
            self._send(405, {"error": "POST a notification here"})
        else:
            self._send(404, {"error": "not found"})

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send(413, {"error": "payload too large"})
            return None
        return self.rfile.read(length)

    def do_POST(self) -> None:
        path = self._path()
        raw = self._read_body()
        if raw is None:
            return
        if path != WEBHOOK_PATH:
            self._send(404, {"error": "not found"})
            return
        try:
            notification = parse_notification(raw)
        except MalformedPayload as exc:
            log("malformed_payload", error=str(exc), bytes=len(raw), peer=self.client_address[0])
            self._send(400, {"error": str(exc)})
            return
        if notification.version != "4":
            log("unexpected_payload_version", version=notification.version)
        try:
            outcome = self.server.gate.handle(notification)
        except GitHubError as exc:
            status = 503 if exc.unreachable else 502
            log("github_error", error=str(exc), github_status=exc.status, response=status,
                group_key=notification.group_key, alertname=notification.alertname)
            self._send(status, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            log("internal_error", error=repr(exc), group_key=notification.group_key)
            self._send(500, {"error": "internal error"})
            return
        self._send(200, outcome.as_json())


def make_server(config: Config, host: str = "0.0.0.0") -> GateServer:
    clock = config.clock()
    github = GitHubClient(config.github_api_url, config.github_token, config.incidents_repo, config.github_timeout)
    metrics = Metrics()
    index = IncidentIndex(config.incident_index_file)
    budget = RunBudget(config.run_budget_per_day, config.budget_state_file)
    services = {"prometheus": config.prometheus_url, "victorialogs": config.victorialogs_url,
                "alertmanager": config.alertmanager_url}
    forwarder = HermesForwarder(config.hermes_webhook_url, config.hermes_webhook_secret, config.hermes_timeout,
                                metrics, config.incidents_repo, services)
    metrics.gauge("run_budget_limit", "Investigations allowed per UTC day.", lambda: budget.limit)
    metrics.gauge("run_budget_used", "Investigations forwarded so far today (UTC).", lambda: budget.used(clock()))
    metrics.gauge("run_budget_remaining", "Investigations left today (UTC).", lambda: budget.remaining(clock()))
    metrics.gauge("heartbeat_age_seconds",
                  f"Age of the Hermes heartbeat file; {HEARTBEAT_ABSENT_SECONDS:g} when absent.",
                  lambda: heartbeat_age_seconds(config.heartbeat_file))
    metrics.gauge("heartbeat_file_present", "1 when the Hermes heartbeat file can be read, else 0.",
                  lambda: int(heartbeat_age_seconds(config.heartbeat_file) != HEARTBEAT_ABSENT_SECONDS))
    return GateServer((host, config.port), Gate(github, index, budget, forwarder, metrics, clock))


def main() -> int:
    config = Config.from_env()
    if not config.github_token:
        log("startup_warning", warning="GITHUB_TOKEN is empty; GitHub calls will be unauthenticated")
    if not config.hermes_webhook_url:
        log("startup_warning", warning="HERMES_WEBHOOK_URL is empty; every new Alert Group becomes a Bare Issue")
    elif not config.hermes_webhook_secret:
        log("startup_warning", warning="HERMES_WEBHOOK_SECRET is empty; Hermes will reject every forward")
    if not config.budget_state_file:
        log("startup_warning", warning="BUDGET_STATE_FILE is empty; the Run Budget resets on restart")
    if not config.incident_index_file:
        log("startup_warning",
            warning="INCIDENT_INDEX_FILE is empty; the Incident Issue index resets on restart")
    if config.fake_now:
        log("startup_warning", warning=f"GATE_FAKE_NOW={config.fake_now}; the clock is frozen (tests only)")
    server = make_server(config)

    def shutdown(signum: int, _frame: Any) -> None:
        log("shutdown", signal=signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log("startup", port=config.port, incidents_repo=config.incidents_repo, github_api_url=config.github_api_url,
        hermes_webhook_url=config.hermes_webhook_url, run_budget_per_day=config.run_budget_per_day,
        budget_state_file=config.budget_state_file, incident_index_file=config.incident_index_file,
        heartbeat_file=config.heartbeat_file,
        prometheus_url=config.prometheus_url, victorialogs_url=config.victorialogs_url,
        alertmanager_url=config.alertmanager_url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
