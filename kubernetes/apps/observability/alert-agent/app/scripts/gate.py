#!/usr/bin/env python3
"""gate.py — the Gate between Alertmanager and the alert-investigation Agent.

Receives Alertmanager v4 webhook notifications and turns each Alert Group into
exactly one Incident Issue in the Incidents Repo, deterministically and without
a model call. Deduplication is a hidden HTML-comment marker in the issue body
carrying a hash of the group key; no per-group labels.

Decision per notification, in order:

  * resolved, open Incident Issue      -> "resolved at" comment, never close
  * resolved, no open Incident Issue   -> log and drop
  * firing,   open Incident Issue      -> "still firing at" comment
  * firing,   no open Incident Issue   -> create the Incident Issue

Stdlib only: runs on the slim python image as non-root with a read-only root
filesystem, so nothing here is installed and nothing is written to disk.
"""
import hashlib
import http.client
import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

DEFAULT_INCIDENTS_REPO = "s0len/solen-ops-incidents"
DEFAULT_API_URL = "https://api.github.com"
MARKER_PREFIX = "<!-- alert-agent:group="
MARKER_SUFFIX = " -->"
MARKER_HASH_CHARS = 24
NEW_ISSUE_LABELS = ("needs-triage",)
BARE_ISSUE_LABEL = "uninvestigated"
TITLE_MAX_CHARS = 200
MAX_REQUEST_BYTES = 8 * 1024 * 1024
ISSUES_PER_PAGE = 100
MAX_ISSUE_PAGES = 10
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

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            port=int(env.get("PORT", "8080")),
            github_api_url=env.get("GITHUB_API_URL", DEFAULT_API_URL).rstrip("/"),
            github_token=env.get("GITHUB_TOKEN", ""),
            incidents_repo=env.get("GITHUB_INCIDENTS_REPO", DEFAULT_INCIDENTS_REPO),
            github_timeout=float(env.get("GITHUB_TIMEOUT_SECONDS", "15")),
        )


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

    def find_incident_issue(self, marker: str) -> Optional[dict]:
        """The oldest open issue whose body contains `marker`, or None.

        Lists open issues oldest-first and filters locally: exact match, no
        dependency on the search index catching up, no per-group labels.
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


# --------------------------------------------------------------------------- seams for the next ticket

class RunBudget:
    """Seam: the per-UTC-day Investigation budget. This stub never runs out."""

    def try_consume(self, now: datetime) -> bool:
        return True


class Forwarder:
    """Seam: the signed forward of an Investigation prompt to Hermes. This stub does nothing."""

    def forward(self, issue: dict, notification: Notification, now: datetime) -> None:
        return None


class Metrics:
    """Seam: in-memory counters rendered in Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict = {}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def render(self) -> str:
        with self._lock:
            lines = [f"alert_agent_gate_{name} {value}" for name, value in sorted(self._counters.items())]
        return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------- the gate

@dataclass(frozen=True)
class Outcome:
    action: str
    issue_number: Optional[int] = None

    def as_json(self) -> dict:
        data = {"action": self.action}
        if self.issue_number is not None:
            data["issue"] = self.issue_number
        return data


class Gate:
    """Turns one notification into one deterministic GitHub action."""

    def __init__(
        self,
        github: GitHubClient,
        run_budget: Optional[RunBudget] = None,
        forwarder: Optional[Forwarder] = None,
        metrics: Optional[Metrics] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.github = github
        self.run_budget = run_budget or RunBudget()
        self.forwarder = forwarder or Forwarder()
        self.metrics = metrics or Metrics()
        self.clock = clock
        self._lock = threading.Lock()

    def handle(self, n: Notification) -> Outcome:
        self.metrics.inc("notifications_received_total")
        marker = group_marker(n.group_key)
        now = self.clock()
        stamp = now_iso(now)
        context = {"group_key": n.group_key, "alertname": n.alertname, "status": n.status, "alerts": len(n.alerts)}
        with self._lock:
            issue = self.github.find_incident_issue(marker)
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
        investigate = self.run_budget.try_consume(now)
        labels = list(NEW_ISSUE_LABELS)
        if not investigate:
            labels.append(BARE_ISSUE_LABEL)
        issue = self.github.create_incident_issue(render_issue_title(n), render_issue_body(n, marker, stamp), labels)
        number = int(issue["number"])
        self.metrics.inc("issues_created_total")
        if investigate:
            self.forwarder.forward(issue, n, now)
            log("incident_issue_created", issue=number, url=issue.get("html_url"), **context)
            return Outcome("created", number)
        self.metrics.inc("bare_issues_total")
        log("bare_issue_created", issue=number, url=issue.get("html_url"), **context)
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
    github = GitHubClient(config.github_api_url, config.github_token, config.incidents_repo, config.github_timeout)
    return GateServer((host, config.port), Gate(github))


def main() -> int:
    config = Config.from_env()
    if not config.github_token:
        log("startup_warning", warning="GITHUB_TOKEN is empty; GitHub calls will be unauthenticated")
    server = make_server(config)

    def shutdown(signum: int, _frame: Any) -> None:
        log("shutdown", signal=signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log("startup", port=config.port, incidents_repo=config.incidents_repo, github_api_url=config.github_api_url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
