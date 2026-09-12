"""Gate tests: real Alertmanager payloads in; GitHub issues, comments, signed
Hermes forwards and metrics out.

The Gate runs as the real script in a subprocess, configured only through its
environment, and is driven purely over HTTP. GitHub is an in-process fake that
declares existing issues and records what the Gate creates and comments.
Hermes is an in-process fake that records every forward and verifies its
generic V2 signature with the shared secret. The tests assert only on what
leaves the Gate: HTTP status codes, created issues, comments, forwards, the
metrics page and the log.

The Gate internals the tests know are its persisted contracts: the marker
format, whose change would orphan every open Incident Issue, and the shape of
the Incident Issue index file, which a restarted Gate has to keep reading. The
Run Budget state file is only ever handed to the Gate as a path.
"""
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GATE_SCRIPT = Path(__file__).resolve().parents[1] / "app" / "scripts" / "gate.py"
INCIDENTS_REPO = "example/incidents"
TOKEN = "test-token"
HERMES_SECRET = "hermes-route-secret-for-tests"
COUNTER_NAMES = ("notifications_received_total", "issues_created_total", "issues_reopened_total",
                 "reopens_suppressed_total", "bare_issues_total", "comments_total", "forwards_total",
                 "forward_failures_total", "github_errors_total")
GAUGE_NAMES = ("run_budget_limit", "run_budget_used", "run_budget_remaining",
               "run_budget_noncritical_remaining", "run_budget_critical_reserve", "run_budget_critical_used",
               "heartbeat_age_seconds", "heartbeat_file_present")
ISO_UTC = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
# The close the owner performed on Incident Issue #13, four seconds before the
# Gate filed #38 for the same Alert Group.
CLOSED_AT = "2026-09-11T16:55:46Z"
# The default startsAt: the alert was already firing a day before CLOSED_AT, so
# the close the owner performed was a close on a condition still running.
STARTS_AT = "2026-09-10T11:20:03.117Z"
# A start well after CLOSED_AT: the condition cleared and came back, which is
# the only firing that reopens the Incident Issue.
NEW_EPISODE_STARTS_AT = "2026-09-11T18:30:12.400Z"
# Mirrors gate.py: how far either side of the close two clocks may disagree.
CLOCK_SKEW_SECONDS = 120
# Mirrors gate.py: the index file is a persisted contract, so its bounds are too.
INDEX_MAX_ENTRIES = 512
INDEX_MAX_AGE_DAYS = 30


def index_key_for(group_key):
    return hashlib.sha256(group_key.encode()).hexdigest()[:24]


def marker_for(group_key):
    return f"<!-- alert-agent:group={index_key_for(group_key)} -->"


# --------------------------------------------------------------------------- payload builders

def alert(alertname, status="firing", starts_at=STARTS_AT, **labels):
    """One alert as Alertmanager renders it inside a v4 notification."""
    all_labels = {"alertname": alertname, "job": "kube-state-metrics", "namespace": "media",
                  "severity": "warning", "prometheus": "observability/kube-prometheus-stack-prometheus"}
    all_labels.update(labels)
    ends_at = "2026-09-10T11:42:03.117Z" if status == "resolved" else "0001-01-01T00:00:00Z"
    return {
        "status": status,
        "labels": all_labels,
        "annotations": {
            "summary": f"Pod is crash looping ({all_labels.get('pod', 'unknown')})",
            "description": f"Pod {all_labels['namespace']}/{all_labels.get('pod', 'unknown')} "
                           "has been restarting 5.13 times / 10 minutes.",
            "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubepodcrashlooping",
        },
        "startsAt": starts_at,
        "endsAt": ends_at,
        "generatorURL": "http://prometheus.observability:9090/graph?g0.expr=max_over_time%28kube_pod_container_status_waiting_reason%7Breason%3D%22CrashLoopBackOff%22%7D%5B5m%5D%29+%3E%3D+1&g0.tab=1",
        "fingerprint": hashlib.md5(json.dumps(all_labels, sort_keys=True).encode()).hexdigest()[:16],
    }


def notification(alertname="KubePodCrashLooping", job="kube-state-metrics", status="firing", alerts=None,
                 starts_at=STARTS_AT):
    """A full Alertmanager v4 webhook notification for one Alert Group."""
    alerts = alerts if alerts is not None else [alert(alertname, status=status, pod="sonarr-0",
                                                      starts_at=starts_at)]
    common_labels = dict(alerts[0]["labels"])
    for a in alerts[1:]:
        common_labels = {k: v for k, v in common_labels.items() if a["labels"].get(k) == v}
    common_annotations = dict(alerts[0]["annotations"])
    for a in alerts[1:]:
        common_annotations = {k: v for k, v in common_annotations.items() if a["annotations"].get(k) == v}
    return {
        "receiver": "alert-agent",
        "status": status,
        "alerts": alerts,
        "groupLabels": {"alertname": alertname, "job": job},
        "commonLabels": common_labels,
        "commonAnnotations": common_annotations,
        "externalURL": "https://alertmanager.example.invalid",
        "version": "4",
        "groupKey": f'{{}}/{{alertname=~"^(?!Watchdog|InfoInhibitor$).*"}}:{{alertname="{alertname}", job="{job}"}}',
        "truncatedAlerts": 0,
    }


# --------------------------------------------------------------------------- fake github

class FakeGitHub:
    """Enough of the GitHub Issues API for the Gate: list, get by number, create, comment.

    The real listing endpoint is not read-after-write consistent — a created
    issue stayed missing from it for over a second while the read by number
    returned it at once — so `hide_new_from_listing` withholds every create
    from the listing until `release_listing`. Without that lag the fake was
    kinder than GitHub, which is how a duplicate Incident Issue reached
    production unnoticed.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def reset(self):
        with self.lock:
            self.mode = "ok"
            self.issues = {}
            self.created = []
            self.reopened = []
            self.labelled = []
            self.comments = []
            self.requests = []
            self.next_number = 1
            self.hide_new_from_listing = False
            self.hidden = set()
            self.stale_open_in_listing = set()
            self.fail_path_suffix = None

    def release_listing(self):
        """Everything created during the lag becomes visible to the listing."""
        with self.lock:
            self.hidden = set()

    def labels_on(self, number):
        with self.lock:
            return [label["name"] for label in self.issues[number]["labels"]]

    def close_issue(self, number, listing_still_open=False, closed_at=CLOSED_AT):
        """Close an issue; `listing_still_open` keeps the listing reporting it
        as open, which is what the real endpoint does for a second or more."""
        with self.lock:
            self.issues[number]["state"] = "closed"
            self.issues[number]["closed_at"] = closed_at
            if listing_still_open:
                self.stale_open_in_listing.add(number)

    def declare_issue(self, body, title="declared", state="open", labels=(), pull_request=False,
                      closed_at=CLOSED_AT):
        with self.lock:
            number = self.next_number
            self.next_number += 1
            item = {"number": number, "title": title, "body": body, "state": state,
                    "closed_at": closed_at if state == "closed" else None,
                    "labels": [{"name": l} for l in labels], "html_url": f"https://github.invalid/{INCIDENTS_REPO}/issues/{number}"}
            if pull_request:
                item["pull_request"] = {"url": f"{self.url}/repos/{INCIDENTS_REPO}/pulls/{number}"}
            self.issues[number] = item
            return number

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return None

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length) or b"{}")

            def _record(self, body=None):
                with fake.lock:
                    fake.requests.append({"method": self.command, "path": self.path,
                                          "authorization": self.headers.get("Authorization"), "body": body})

            def _gate(self):
                if fake.fail_path_suffix and urlparse(self.path).path.endswith(fake.fail_path_suffix):
                    self._json(500, {"message": "Server Error"})
                    return False
                if fake.mode == "error":
                    self._json(500, {"message": "Server Error"})
                    return False
                if fake.mode == "disconnect":
                    self.close_connection = True
                    self.connection.close()
                    return False
                return True

            def do_GET(self):
                self._record()
                if not self._gate():
                    return
                url = urlparse(self.path)
                listing = f"/repos/{INCIDENTS_REPO}/issues"
                if url.path.startswith(listing + "/"):
                    try:
                        number = int(url.path[len(listing) + 1:])
                    except ValueError:
                        self._json(404, {"message": "Not Found"})
                        return
                    with fake.lock:
                        item = fake.issues.get(number)
                    self._json(200, item) if item else self._json(404, {"message": "Not Found"})
                    return
                if url.path != listing:
                    self._json(404, {"message": "Not Found"})
                    return
                q = parse_qs(url.query)
                state = q.get("state", ["open"])[0]
                per_page = int(q.get("per_page", ["30"])[0])
                page = int(q.get("page", ["1"])[0])
                with fake.lock:
                    items = sorted(fake.issues.values(), key=lambda i: i["number"])
                    items = [i for i in items if i["number"] not in fake.hidden]
                items = [dict(i, state="open", closed_at=None) if i["number"] in fake.stale_open_in_listing
                         else i for i in items]
                if state != "all":
                    items = [i for i in items if i["state"] == state]
                start = (page - 1) * per_page
                self._json(200, items[start:start + per_page])

            def do_POST(self):
                body = self._body()
                self._record(body)
                if not self._gate():
                    return
                path = urlparse(self.path).path
                if path == f"/repos/{INCIDENTS_REPO}/issues":
                    with fake.lock:
                        number = fake.next_number
                        fake.next_number += 1
                        item = {"number": number, "title": body.get("title"), "body": body.get("body"),
                                "state": "open", "labels": [{"name": l} for l in body.get("labels", [])],
                                "html_url": f"https://github.invalid/{INCIDENTS_REPO}/issues/{number}"}
                        fake.issues[number] = item
                        fake.created.append(dict(item, labels=list(body.get("labels", []))))
                        if fake.hide_new_from_listing:
                            fake.hidden.add(number)
                    self._json(201, item)
                    return
                prefix = f"/repos/{INCIDENTS_REPO}/issues/"
                if path.startswith(prefix) and path.endswith("/labels"):
                    number = int(path[len(prefix):-len("/labels")])
                    asked = [name for name in body.get("labels", []) if isinstance(name, str)]
                    with fake.lock:
                        item = fake.issues.get(number)
                        if item is None:
                            self._json(404, {"message": "Not Found"})
                            return
                        # The real endpoint is additive and idempotent.
                        names = [label["name"] for label in item["labels"]]
                        names += [name for name in asked if name not in names]
                        item["labels"] = [{"name": name} for name in names]
                        fake.labelled.append({"issue": number, "labels": asked})
                        current = list(item["labels"])
                    self._json(200, current)
                    return
                if path.startswith(prefix) and path.endswith("/comments"):
                    number = int(path[len(prefix):-len("/comments")])
                    with fake.lock:
                        if number not in fake.issues:
                            self._json(404, {"message": "Not Found"})
                            return
                        fake.comments.append({"issue": number, "body": body.get("body")})
                    self._json(201, {"id": len(fake.comments), "body": body.get("body")})
                    return
                self._json(404, {"message": "Not Found"})

            def do_PATCH(self):
                body = self._body()
                self._record(body)
                if not self._gate():
                    return
                path = urlparse(self.path).path
                prefix = f"/repos/{INCIDENTS_REPO}/issues/"
                if not path.startswith(prefix) or body != {"state": "open"}:
                    self._json(500, {"message": "the Gate may only reopen an issue"})
                    return
                try:
                    number = int(path[len(prefix):])
                except ValueError:
                    self._json(404, {"message": "Not Found"})
                    return
                with fake.lock:
                    item = fake.issues.get(number)
                    if item is None:
                        self._json(404, {"message": "Not Found"})
                        return
                    item.update(state="open", closed_at=None)
                    fake.stale_open_in_listing.discard(number)
                    fake.reopened.append(number)
                    reopened = dict(item)
                self._json(200, reopened)

        return Handler


# --------------------------------------------------------------------------- fake hermes

class FakeHermes:
    """The Hermes webhook gateway's `investigate` route: records every forward
    and checks its generic V2 signature (hex HMAC-SHA256 of "<timestamp>.<body>",
    headers X-Webhook-Signature-V2 and X-Webhook-Timestamp) with the shared secret."""

    ROUTE_PATH = "/webhooks/investigate"
    REPLAY_WINDOW_SECONDS = 300

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{self.ROUTE_PATH}"

    def reset(self):
        with self.lock:
            self.mode = "ok"
            self.requests = []

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return None

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                timestamp = self.headers.get("X-Webhook-Timestamp", "")
                signature = self.headers.get("X-Webhook-Signature-V2", "")
                expected = hmac.new(HERMES_SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
                try:
                    fresh = abs(time.time() - int(timestamp)) <= fake.REPLAY_WINDOW_SECONDS
                except ValueError:
                    fresh = False
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                with fake.lock:
                    fake.requests.append({
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "raw": raw,
                        "body": body,
                        "signature_valid": bool(signature) and hmac.compare_digest(signature, expected),
                        "timestamp_fresh": fresh,
                    })
                    mode = fake.mode
                if mode == "error":
                    self._json(500, {"error": "gateway exploded"})
                elif mode == "disconnect":
                    self.close_connection = True
                    self.connection.close()
                elif mode == "hang":
                    time.sleep(3)
                    self._json(202, {"status": "accepted"})
                elif not fresh or not signature or not hmac.compare_digest(signature, expected):
                    self._json(401, {"error": "Invalid signature"})
                elif self.path != fake.ROUTE_PATH:
                    self._json(404, {"error": "no such route"})
                else:
                    self._json(202, {"status": "accepted", "route": "investigate", "delivery_id":
                                     self.headers.get("X-Request-ID", "")})

        return Handler


# --------------------------------------------------------------------------- the gate under test

class GateProcess:
    """The real gate.py, started as a subprocess with only its environment."""

    def __init__(self, github_url, hermes=None, **extra_env):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = tempfile.NamedTemporaryFile(prefix="gate-", suffix=".log", delete=False)
        env = dict(os.environ, PORT=str(self.port), GITHUB_API_URL=github_url,
                   GITHUB_INCIDENTS_REPO=INCIDENTS_REPO, GITHUB_TOKEN=TOKEN, GITHUB_TIMEOUT_SECONDS="5")
        for key in ("HERMES_WEBHOOK_URL", "HERMES_WEBHOOK_SECRET", "BUDGET_STATE_FILE", "RUN_BUDGET_PER_DAY",
                    "RUN_BUDGET_CRITICAL_RESERVE", "INCIDENT_INDEX_FILE", "HEARTBEAT_FILE", "GATE_FAKE_NOW",
                    "HERMES_TIMEOUT_SECONDS"):
            env.pop(key, None)
        if hermes is not None:
            env.update(HERMES_WEBHOOK_URL=hermes.url, HERMES_WEBHOOK_SECRET=HERMES_SECRET)
        env.update({key: str(value) for key, value in extra_env.items() if value is not None})
        self.process = subprocess.Popen([sys.executable, str(GATE_SCRIPT)], env=env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"gate exited early:\n{self.read_log()}")
            try:
                status, _ = self.get("/healthz")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"gate did not become healthy:\n{self.read_log()}")

    def read_log(self):
        with open(self.log.name, encoding="utf-8") as handle:
            return handle.read()

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()
        os.unlink(self.log.name)

    def _call(self, method, path, data=None, content_type="application/json"):
        request = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def get(self, path):
        return self._call("GET", path)

    def post_raw(self, body, path="/webhook"):
        return self._call("POST", path, body)

    def post(self, payload, path="/webhook"):
        status, body = self.post_raw(json.dumps(payload).encode(), path)
        return status, json.loads(body) if body else None

    def metrics(self):
        """The metrics page as {series name: value} plus the set of HELP/TYPE-declared names."""
        status, body = self.get("/metrics")
        assert status == 200, body
        values, declared = {}, {}
        for line in body.decode().splitlines():
            if line.startswith("# HELP "):
                declared.setdefault(line.split()[2], set()).add("help")
            elif line.startswith("# TYPE "):
                declared.setdefault(line.split()[2], set()).add(line.split()[3])
            elif line.strip():
                name, value = line.split()
                values[name] = float(value)
        return values, declared


# --------------------------------------------------------------------------- tests

class GateTests(unittest.TestCase):
    """Deduplication and GitHub behaviour with Hermes healthy and an ample Run Budget."""

    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub()
        cls.hermes = FakeHermes()
        cls.state_dir = tempfile.TemporaryDirectory(prefix="gate-state-")
        cls.gate = GateProcess(cls.github.url, hermes=cls.hermes,
                               BUDGET_STATE_FILE=os.path.join(cls.state_dir.name, "run-budget.json"),
                               INCIDENT_INDEX_FILE=os.path.join(cls.state_dir.name, "incident-index.json"),
                               RUN_BUDGET_PER_DAY=1000)

    @classmethod
    def tearDownClass(cls):
        cls.gate.stop()
        cls.github.stop()
        cls.hermes.stop()
        cls.state_dir.cleanup()

    def setUp(self):
        self.github.reset()
        self.hermes.reset()

    def tearDown(self):
        self.github.mode = "ok"

    # -- firing, no open Incident Issue

    def test_firing_new_alert_group_creates_one_incident_issue(self):
        payload = notification(alerts=[alert("KubePodCrashLooping", pod="sonarr-0"),
                                       alert("KubePodCrashLooping", pod="radarr-0")])
        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertEqual(len(self.github.created), 1)
        issue = self.github.created[0]
        self.assertEqual(body["issue"], issue["number"])
        self.assertEqual(issue["labels"], ["needs-triage"])
        self.assertTrue(issue["title"].startswith("KubePodCrashLooping"), issue["title"])
        self.assertIn(marker_for(payload["groupKey"]), issue["body"])
        for expected in ("sonarr-0", "radarr-0", "KubePodCrashLooping", "kube-state-metrics",
                         "2026-09-10T11:20:03.117Z", "Pod is crash looping",
                         "runbooks.prometheus-operator.dev", "prometheus.observability:9090"):
            self.assertIn(expected, issue["body"])
        self.assertEqual(self.github.comments, [])

    def test_issue_title_carries_alertname_and_summary(self):
        payload = notification("CephOSDDown", job="rook-ceph-mgr", alerts=[alert("CephOSDDown", pod="rook-ceph-osd-2")])
        payload["commonAnnotations"]["summary"] = "  An OSD  went down  "
        self.gate.post(payload)

        self.assertEqual(self.github.created[0]["title"], "CephOSDDown: An OSD went down")

    def test_two_alert_groups_create_two_incident_issues(self):
        first = notification("KubePodCrashLooping")
        second = notification("KubeDeploymentReplicasMismatch")

        self.assertEqual(self.gate.post(first)[0], 200)
        self.assertEqual(self.gate.post(second)[0], 200)

        self.assertEqual(len(self.github.created), 2)
        bodies = [i["body"] for i in self.github.created]
        self.assertIn(marker_for(first["groupKey"]), bodies[0])
        self.assertIn(marker_for(second["groupKey"]), bodies[1])
        self.assertNotIn(marker_for(second["groupKey"]), bodies[0])
        self.assertEqual(self.github.comments, [])

    def test_same_alert_group_with_alerts_added_stays_one_incident_issue(self):
        first = notification(alerts=[alert("KubePodCrashLooping", pod="sonarr-0")])
        grown = notification(alerts=[alert("KubePodCrashLooping", pod="sonarr-0"),
                                     alert("KubePodCrashLooping", pod="radarr-0"),
                                     alert("KubePodCrashLooping", pod="lidarr-0")])
        self.assertEqual(first["groupKey"], grown["groupKey"])

        status_first, body_first = self.gate.post(first)
        status_grown, body_grown = self.gate.post(grown)

        self.assertEqual((status_first, status_grown), (200, 200))
        self.assertEqual(body_first["action"], "created")
        self.assertEqual(body_grown["action"], "still-firing")
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(self.github.comments[0]["issue"], self.github.created[0]["number"])
        self.assertIn("3 alerts", self.github.comments[0]["body"])
        self.assertIn("lidarr-0", self.github.comments[0]["body"])

    # -- firing, open Incident Issue

    def test_firing_with_open_incident_issue_comments_still_firing(self):
        payload = notification(alerts=[alert("KubePodCrashLooping", pod="sonarr-0"),
                                       alert("KubePodCrashLooping", pod="radarr-0")])
        number = self.github.declare_issue(f"Earlier body\n\n{marker_for(payload['groupKey'])}\n")

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "still-firing", "issue": number})
        self.assertEqual(self.github.created, [])
        self.assertEqual(len(self.github.comments), 1)
        comment = self.github.comments[0]
        self.assertEqual(comment["issue"], number)
        self.assertRegex(comment["body"], rf"^Still firing at {ISO_UTC} — 2 alerts")

    def test_open_incident_issue_is_found_beyond_the_first_page(self):
        payload = notification()
        for i in range(130):
            self.github.declare_issue(f"unrelated issue {i}")
        number = self.github.declare_issue(marker_for(payload["groupKey"]))

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "still-firing", "issue": number})
        self.assertEqual(self.github.created, [])

    def test_a_new_episode_after_a_close_reopens_the_incident_issue_instead_of_filing_a_new_one(self):
        """The condition cleared and came back: every alert in the group began
        after the close, so the Gate reopens rather than filing a second issue
        for the same Alert Group."""
        payload = notification("VolSyncBackupStale", starts_at=NEW_EPISODE_STARTS_AT)
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.reopened, [number])
        self.assertEqual(self.github.issues[number]["state"], "open")
        self.assertEqual(len(self.github.comments), 1)
        comment = self.github.comments[0]
        self.assertEqual(comment["issue"], number)
        self.assertRegex(comment["body"], rf"^Firing again at {ISO_UTC} — 1 alert in this Alert Group")
        self.assertIn(f"closed at {CLOSED_AT}", comment["body"])

    # -- firing, closed Incident Issue, the episode the owner closed

    def test_a_firing_that_was_already_running_at_the_close_leaves_the_issue_closed(self):
        """The owner closed #13 at 16:55:46 while its alert was still firing —
        an accepted, known condition — and the Gate filed #38 at 16:55:50.
        Answering that close with a reopen instead of a fresh issue leaves the
        queue exactly as undrainable, so the close has to hold."""
        payload = notification("CephMgrModuleCrash", starts_at=STARTS_AT)
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed",
                                           labels=("needs-info",))

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "suppressed", "issue": number})
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.reopened, [])
        self.assertEqual(self.github.comments, [])
        self.assertEqual(self.github.labelled, [])
        self.assertEqual(self.github.issues[number]["state"], "closed")
        self.assertEqual(self.github.labels_on(number), ["needs-info"])
        self.assertEqual([r for r in self.github.requests if r["method"] == "PATCH"], [])

    def test_repeated_notifications_for_a_closed_condition_never_reopen_it(self):
        """Alertmanager re-notifies on repeatInterval and on every change to the
        group; not one of those may undo the close."""
        first = notification("CephHealthWarning", alerts=[alert("CephHealthWarning", pod="rook-ceph-mgr-a")])
        grown = notification("CephHealthWarning", alerts=[alert("CephHealthWarning", pod="rook-ceph-mgr-a"),
                                                          alert("CephHealthWarning", pod="rook-ceph-mgr-b")])
        number = self.github.declare_issue(marker_for(first["groupKey"]), state="closed")

        outcomes = [self.gate.post(first)[1], self.gate.post(grown)[1], self.gate.post(first)[1]]

        self.assertEqual(outcomes, [{"action": "suppressed", "issue": number}] * 3)
        self.assertEqual(self.github.issues[number]["state"], "closed")
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.comments, [])

    def test_one_alert_predating_the_close_holds_it_even_when_the_group_grew(self):
        """The group is judged by its earliest alert: a new member of a group
        whose original condition is still running is the same accepted
        condition, not a new episode."""
        payload = notification("KubePodNotReady",
                               alerts=[alert("KubePodNotReady", pod="sonarr-0", starts_at=STARTS_AT),
                                       alert("KubePodNotReady", pod="radarr-0",
                                             starts_at=NEW_EPISODE_STARTS_AT)])
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "suppressed", "issue": number})
        self.assertEqual(self.github.issues[number]["state"], "closed")
        self.assertEqual(self.github.created, [])

    def test_an_alert_the_notification_marks_resolved_does_not_hold_the_close(self):
        """The old member is gone; what is left all started after the close, so
        this is a new episode however long the group key has existed."""
        payload = notification("KubeContainerWaiting",
                               alerts=[alert("KubeContainerWaiting", pod="sonarr-0", status="resolved",
                                             starts_at=STARTS_AT),
                                       alert("KubeContainerWaiting", pod="radarr-0",
                                             starts_at=NEW_EPISODE_STARTS_AT)])
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.reopened, [number])

    def test_a_start_inside_the_clock_skew_window_reopens_rather_than_suppresses(self):
        """Prometheus and GitHub stamp from two clocks. Inside the window the
        two cannot be ordered, and an unnecessary reopen the owner can close
        again beats a firing alert buried in a closed issue."""
        closed = datetime.strptime(CLOSED_AT, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        inside = closed - timedelta(seconds=CLOCK_SKEW_SECONDS - 30)
        outside = closed - timedelta(seconds=CLOCK_SKEW_SECONDS + 30)

        for name, started, action in (("inside the window", inside, "reopened"),
                                      ("outside the window", outside, "suppressed")):
            with self.subTest(case=name):
                self.github.reset()
                payload = notification("NodeClockSkewDetected",
                                       starts_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"))
                number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

                status, body = self.gate.post(payload)

                self.assertEqual(status, 200, body)
                self.assertEqual(body, {"action": action, "issue": number})

    def test_an_unreadable_closed_at_or_start_reopens_rather_than_suppresses(self):
        """Nothing that cannot be read is allowed to swallow a firing alert."""
        cases = {
            "closed_at missing": (None, STARTS_AT),
            "closed_at unparseable": ("last Tuesday", STARTS_AT),
            "startsAt missing": (CLOSED_AT, ""),
            "startsAt unparseable": (CLOSED_AT, "whenever"),
            "startsAt is Go's zero time": (CLOSED_AT, "0001-01-01T00:00:00Z"),
        }
        for name, (closed_at, starts_at) in cases.items():
            with self.subTest(case=name):
                self.github.reset()
                payload = notification("EtcdMembersDown", starts_at=starts_at)
                number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed",
                                                   closed_at=closed_at)

                status, body = self.gate.post(payload)

                self.assertEqual(status, 200, body)
                self.assertEqual(body, {"action": "reopened", "issue": number})
                self.assertEqual(self.github.reopened, [number])
                self.assertEqual(self.github.created, [])

    def test_resolved_never_reopens_a_closed_incident_issue(self):
        payload = notification("CephMonQuorumAtRisk", status="resolved")
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "dropped"})
        self.assertEqual(self.github.issues[number]["state"], "closed")
        self.assertEqual(self.github.reopened, [])
        self.assertEqual(self.github.comments, [])
        self.assertEqual(self.github.created, [])

    def test_the_most_recently_closed_incident_issue_is_the_one_reopened(self):
        """A group that has been through several episodes has several matches;
        only the newest carries this incident's history, and an issue the owner
        drained weeks ago must stay closed."""
        payload = notification("KubeletDown", starts_at=NEW_EPISODE_STARTS_AT)
        marker = marker_for(payload["groupKey"])
        old = self.github.declare_issue(marker, state="closed", closed_at="2026-08-14T09:00:00Z")
        middle = self.github.declare_issue(marker, state="closed", closed_at="2026-09-01T09:00:00Z")
        newest = self.github.declare_issue(marker, state="closed", closed_at=CLOSED_AT)

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": newest})
        self.assertEqual(self.github.reopened, [newest])
        self.assertEqual([self.github.issues[n]["state"] for n in (old, middle)], ["closed", "closed"])
        self.assertEqual([c["issue"] for c in self.github.comments], [newest])
        self.assertIn(f"closed at {CLOSED_AT}", self.github.comments[0]["body"])

    # -- the triage label a reopened Incident Issue re-enters the queue with

    def test_a_reopened_incident_issue_carries_the_triage_label_a_new_one_would(self):
        """The Fix flow strips ready-for-agent before the close, so a reopened
        issue can carry no triage label at all and be invisible to
        `gh issue list --label needs-triage`."""
        payload = notification("CephPoolNearFull", starts_at=NEW_EPISODE_STARTS_AT)
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed", labels=())

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.labelled, [{"issue": number, "labels": ["needs-triage"]}])
        self.assertEqual(self.github.labels_on(number), ["needs-triage"])

    def test_a_reopen_adds_the_triage_label_without_clobbering_the_owners_labels(self):
        """needs-info, ready-for-human and wontfix are a human's judgement; the
        Gate owns needs-triage and adds it beside them, never over them."""
        payload = notification("CephDaemonCrash", starts_at=NEW_EPISODE_STARTS_AT)
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed",
                                           labels=("needs-info", "ready-for-human", "wontfix"))

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.labelled, [{"issue": number, "labels": ["needs-triage"]}])
        self.assertEqual(self.github.labels_on(number),
                         ["needs-info", "ready-for-human", "wontfix", "needs-triage"])

    def test_a_reopen_of_an_issue_that_kept_the_triage_label_sends_no_label_call(self):
        payload = notification("NodeNetworkReceiveErrs", starts_at=NEW_EPISODE_STARTS_AT)
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed",
                                           labels=("needs-triage", "uninvestigated"))

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.labelled, [])
        self.assertEqual(self.github.labels_on(number), ["needs-triage", "uninvestigated"])
        self.assertEqual([r["path"] for r in self.github.requests if r["path"].endswith("/labels")], [])

    def test_a_label_call_that_fails_still_leaves_the_issue_reopened_and_commented(self):
        """The issue is already open when the label is added, so raising here
        would hand the retry a "still firing" comment and lose the reopen."""
        payload = notification("NodeFilesystemFilesFillingUp", starts_at=NEW_EPISODE_STARTS_AT)
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")
        self.github.fail_path_suffix = "/labels"

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.reopened, [number])
        self.assertEqual(self.github.issues[number]["state"], "open")
        self.assertEqual([c["issue"] for c in self.github.comments], [number])
        self.assertEqual(self.github.labels_on(number), [])
        self.assertIn("reopen_labels_failed", self.gate.read_log())

    def test_an_open_incident_issue_wins_over_an_older_closed_one(self):
        payload = notification("NodeFilesystemAlmostOutOfSpace")
        marker = marker_for(payload["groupKey"])
        closed = self.github.declare_issue(marker, state="closed")
        open_issue = self.github.declare_issue(marker)

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "still-firing", "issue": open_issue})
        self.assertEqual(self.github.reopened, [])
        self.assertEqual(self.github.issues[closed]["state"], "closed")
        self.assertEqual([r for r in self.github.requests if r["method"] == "PATCH"], [])

    def test_pull_request_carrying_the_marker_is_not_an_incident_issue(self):
        payload = notification()
        self.github.declare_issue(marker_for(payload["groupKey"]), pull_request=True)

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.comments, [])

    # -- resolved

    def test_resolved_with_open_incident_issue_comments_resolved_and_keeps_it_open(self):
        payload = notification(status="resolved")
        number = self.github.declare_issue(marker_for(payload["groupKey"]))

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "resolved", "issue": number})
        self.assertEqual(self.github.created, [])
        self.assertEqual(len(self.github.comments), 1)
        self.assertRegex(self.github.comments[0]["body"], rf"^Resolved at {ISO_UTC} — 1 alert resolved")
        self.assertEqual(self.github.issues[number]["state"], "open")
        self.assertEqual([r for r in self.github.requests if r["method"] == "PATCH"], [])

    def test_resolved_without_incident_issue_is_dropped(self):
        status, body = self.gate.post(notification(status="resolved"))

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "dropped"})
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.comments, [])

    # -- malformed

    def test_malformed_payload_is_rejected_and_never_reaches_github(self):
        valid = notification()
        cases = {
            "not json": b"{not json",
            "json array": b"[]",
            "empty object": b"{}",
            "missing groupKey": json.dumps({k: v for k, v in valid.items() if k != "groupKey"}).encode(),
            "empty groupKey": json.dumps(dict(valid, groupKey="")).encode(),
            "unknown status": json.dumps(dict(valid, status="pending")).encode(),
            "alerts not a list": json.dumps(dict(valid, alerts={})).encode(),
            "no alerts": json.dumps(dict(valid, alerts=[])).encode(),
            "alert without labels": json.dumps(dict(valid, alerts=[{"annotations": {}}])).encode(),
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                status, body = self.gate.post_raw(raw)
                self.assertEqual(status, 400, body)
        self.assertEqual(self.github.requests, [])
        self.assertEqual(self.github.created, [])

    def test_unknown_paths_and_methods(self):
        self.assertEqual(self.gate.post(notification(), path="/nope")[0], 404)
        self.assertEqual(self.gate.get("/webhook")[0], 405)
        self.assertEqual(self.gate.get("/nope")[0], 404)
        self.assertEqual(self.github.requests, [])

    # -- github unavailable

    def test_github_error_returns_502_and_creates_nothing(self):
        self.github.mode = "error"

        status, body = self.gate.post(notification())

        self.assertEqual(status, 502, body)
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.comments, [])

    def test_github_unreachable_returns_503_and_creates_nothing(self):
        self.github.mode = "disconnect"

        status, body = self.gate.post(notification())

        self.assertEqual(status, 503, body)
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.comments, [])

    def test_alertmanager_retry_after_github_recovers_creates_exactly_one_issue(self):
        payload = notification()
        self.github.mode = "error"
        self.assertEqual(self.gate.post(payload)[0], 502)
        self.github.mode = "ok"

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertEqual(len(self.github.created), 1)

    # -- plumbing

    def test_health_and_metrics_endpoints(self):
        self.assertEqual(self.gate.get("/healthz")[0], 200)
        status, body = self.gate.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn(b"alert_agent_gate_notifications_received_total", body)

    def test_github_calls_carry_the_configured_token_and_repo(self):
        self.gate.post(notification())

        self.assertTrue(self.github.requests)
        for request in self.github.requests:
            self.assertEqual(request["authorization"], f"Bearer {TOKEN}")
            self.assertTrue(request["path"].startswith(f"/repos/{INCIDENTS_REPO}/issues"), request["path"])


class BudgetAndForwardTests(unittest.TestCase):
    """Run Budget and Hermes forwarding: a fresh Gate and state file per test."""

    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub()
        cls.hermes = FakeHermes()

    @classmethod
    def tearDownClass(cls):
        cls.github.stop()
        cls.hermes.stop()

    def setUp(self):
        self.github.reset()
        self.hermes.reset()
        self.state_dir = tempfile.TemporaryDirectory(prefix="gate-state-")
        self.state_file = os.path.join(self.state_dir.name, "run-budget.json")
        self.index_file = os.path.join(self.state_dir.name, "incident-index.json")
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.stop()
        self.state_dir.cleanup()

    def start_gate(self, hermes="default", **env):
        """A gate with nothing reserved for critical alerts unless the test asks
        for it, so every budget assertion here is about the limit alone."""
        hermes = self.hermes if hermes == "default" else hermes
        env.setdefault("RUN_BUDGET_CRITICAL_RESERVE", 0)
        gate = GateProcess(self.github.url, hermes=hermes, BUDGET_STATE_FILE=self.state_file,
                           INCIDENT_INDEX_FILE=self.index_file, **env)
        self.gates.append(gate)
        return gate

    def budget(self, gate):
        values, _ = gate.metrics()
        return {k: values[f"alert_agent_gate_{k}"] for k in ("run_budget_limit", "run_budget_used", "run_budget_remaining")}

    def critical_budget(self, gate):
        values, _ = gate.metrics()
        return {k: values[f"alert_agent_gate_{k}"]
                for k in ("run_budget_critical_reserve", "run_budget_critical_used")}

    def counter(self, gate, name):
        return gate.metrics()[0][f"alert_agent_gate_{name}"]

    def gauge(self, gate, name):
        return gate.metrics()[0][f"alert_agent_gate_{name}"]

    def log_events(self, gate, event):
        """Every structured log line the Gate emitted for one event name."""
        records = []
        for line in gate.read_log().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("event") == event:
                records.append(record)
        return records

    # -- new Alert Group under budget

    def test_new_alert_group_under_budget_creates_issue_consumes_one_slot_and_forwards_one_signed_prompt(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=3)
        payload = notification(alerts=[alert("KubePodCrashLooping", pod="sonarr-0"),
                                       alert("KubePodCrashLooping", pod="radarr-0")])

        status, body = gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertIs(body["forwarded"], True)
        issue = self.github.created[0]
        self.assertEqual(issue["labels"], ["needs-triage"])
        self.assertEqual(self.budget(gate), {"run_budget_limit": 3, "run_budget_used": 1, "run_budget_remaining": 2})

        self.assertEqual(len(self.hermes.requests), 1)
        forward = self.hermes.requests[0]
        self.assertEqual(forward["path"], FakeHermes.ROUTE_PATH)
        self.assertTrue(forward["signature_valid"], forward["headers"])
        self.assertTrue(forward["timestamp_fresh"], forward["headers"])
        self.assertIn("x-webhook-signature-v2", forward["headers"])
        self.assertIn("x-webhook-timestamp", forward["headers"])
        self.assertNotIn("x-webhook-signature", forward["headers"])
        self.assertEqual(forward["headers"]["content-type"], "application/json")
        self.assertIn(str(issue["number"]), forward["headers"]["x-request-id"])
        self.assertEqual(list(forward["body"]), ["prompt"])
        prompt = forward["body"]["prompt"]
        for expected in (f"#{issue['number']}", issue["html_url"], INCIDENTS_REPO, "KubePodCrashLooping",
                         payload["groupKey"], "runbooks/KubePodCrashLooping.md", "runbooks/patterns/",
                         "sonarr-0", "radarr-0", "2026-09-10T11:20:03.117Z", "Pod is crash looping",
                         "prometheus.observability:9090",
                         "prometheus-operated.observability.svc.cluster.local:9090",
                         "victoria-logs-server.observability.svc.cluster.local:9428",
                         "alertmanager-operated.observability.svc.cluster.local:9093",
                         "Verified evidence", "Unverified hypotheses", "Checks a human must run",
                         "needs-info", "NEVER exec", "ONE comment", "Runbook proposal"):
            self.assertIn(expected, prompt)
        self.assertNotRegex(prompt, r"\$\{?(issue|alert|group|incidents|received|external|prometheus|victorialogs|alertmanager)")
        self.assertEqual(self.counter(gate, "forwards_total"), 1)
        self.assertEqual(self.counter(gate, "forward_failures_total"), 0)
        self.assertEqual(self.counter(gate, "issues_created_total"), 1)
        self.assertEqual(self.counter(gate, "bare_issues_total"), 0)

    def test_service_urls_come_from_the_environment(self):
        gate = self.start_gate(PROMETHEUS_URL="http://prom.test:9090/", VICTORIALOGS_URL="http://vl.test:9428",
                               ALERTMANAGER_URL="http://am.test:9093")
        gate.post(notification())

        prompt = self.hermes.requests[0]["body"]["prompt"]
        self.assertIn("http://prom.test:9090/api/v1/query", prompt)
        self.assertIn("http://vl.test:9428/select/logsql/query", prompt)
        self.assertIn("http://am.test:9093/api/v2/alerts", prompt)

    # -- over budget

    def test_over_budget_creates_bare_issue_and_forwards_nothing(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=2)
        outcomes = [gate.post(notification(name))[1] for name in ("AlertOne", "AlertTwo", "AlertThree")]

        self.assertEqual([o["action"] for o in outcomes], ["created", "created", "created-bare"])
        self.assertNotIn("forwarded", outcomes[2])
        self.assertEqual([i["labels"] for i in self.github.created],
                         [["needs-triage"], ["needs-triage"], ["needs-triage", "uninvestigated"]])
        self.assertIn("AlertThree", self.github.created[2]["body"])
        self.assertEqual(len(self.hermes.requests), 2)
        self.assertEqual({r["body"]["prompt"].count("AlertThree") for r in self.hermes.requests}, {0})
        self.assertEqual(self.budget(gate), {"run_budget_limit": 2, "run_budget_used": 2, "run_budget_remaining": 0})
        self.assertEqual(self.counter(gate, "bare_issues_total"), 1)
        self.assertEqual(self.counter(gate, "issues_created_total"), 3)

    def test_zero_budget_means_every_issue_is_bare(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=0)

        status, body = gate.post(notification())

        self.assertEqual((status, body["action"]), (200, "created-bare"))
        self.assertEqual(self.github.created[0]["labels"], ["needs-triage", "uninvestigated"])
        self.assertEqual(self.hermes.requests, [])

    # -- the slots reserved for critical Alert Groups

    def test_a_critical_alert_group_takes_a_reserved_slot_after_the_general_budget_is_spent(self):
        """On 2026-09-10 a synthetic acceptance fixture spent a slot and
        VolSyncBackupStale at severity=critical — 38 hours with no on-site
        backup of the identity provider — was filed as a Bare Issue."""
        gate = self.start_gate(RUN_BUDGET_PER_DAY=3, RUN_BUDGET_CRITICAL_RESERVE=1)
        warnings = [gate.post(notification(name))[1] for name in ("WarningOne", "WarningTwo", "WarningThree")]
        critical = notification("VolSyncBackupStale",
                                alerts=[alert("VolSyncBackupStale", severity="critical", pod="kanidm-0")])

        status, body = gate.post(critical)

        self.assertEqual([o["action"] for o in warnings], ["created", "created", "created-bare"])
        self.assertEqual((status, body["action"]), (200, "created"))
        self.assertIs(body["forwarded"], True)
        self.assertEqual(self.github.created[3]["labels"], ["needs-triage"])
        self.assertEqual(len(self.hermes.requests), 3)
        self.assertIn("VolSyncBackupStale", self.hermes.requests[2]["body"]["prompt"])
        self.assertEqual(self.budget(gate), {"run_budget_limit": 3, "run_budget_used": 3, "run_budget_remaining": 0})
        self.assertEqual(self.critical_budget(gate),
                         {"run_budget_critical_reserve": 1, "run_budget_critical_used": 1})

        spent = notification("CephOSDDown", alerts=[alert("CephOSDDown", severity="critical", pod="rook-ceph-osd-2")])
        self.assertEqual(gate.post(spent)[1]["action"], "created-bare")
        self.assertIn("run_budget_exhausted", gate.read_log())

    def test_a_warning_is_refused_at_the_reserve_boundary_while_a_slot_remains(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=3, RUN_BUDGET_CRITICAL_RESERVE=1)
        under = [gate.post(notification(name))[1] for name in ("WarningOne", "WarningTwo")]

        status, body = gate.post(notification("WarningThree"))

        self.assertEqual([o["action"] for o in under], ["created", "created"])
        self.assertEqual((status, body["action"]), (200, "created-bare"))
        self.assertEqual(self.github.created[2]["labels"], ["needs-triage", "uninvestigated"])
        self.assertEqual(len(self.hermes.requests), 2)
        self.assertEqual(self.budget(gate), {"run_budget_limit": 3, "run_budget_used": 2, "run_budget_remaining": 1})
        self.assertIn("run_budget_critical_reserve", gate.read_log())

    def test_the_noncritical_remaining_gauge_and_log_show_the_reserve_boundary(self):
        """run_budget_remaining reads 1 while every warning is already being
        filed bare, so on its own it tells an operator nothing they can act on."""
        gate = self.start_gate(RUN_BUDGET_PER_DAY=3, RUN_BUDGET_CRITICAL_RESERVE=1)
        self.assertEqual(self.gauge(gate, "run_budget_noncritical_remaining"), 2)

        for name in ("WarningOne", "WarningTwo"):
            self.assertEqual(gate.post(notification(name))[1]["action"], "created")

        self.assertEqual(self.gauge(gate, "run_budget_remaining"), 1)
        self.assertEqual(self.gauge(gate, "run_budget_noncritical_remaining"), 0)
        self.assertEqual(gate.post(notification("WarningThree"))[1]["action"], "created-bare")

        bare = self.log_events(gate, "bare_issue_created")
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0]["reason"], "run_budget_critical_reserve")
        self.assertEqual(bare[0]["budget_remaining"], 1)
        self.assertEqual(bare[0]["budget_noncritical_remaining"], 0)

        critical = notification("VolSyncBackupStale",
                                alerts=[alert("VolSyncBackupStale", severity="critical", pod="kanidm-0")])
        self.assertEqual(gate.post(critical)[1]["action"], "created")
        self.assertEqual(self.gauge(gate, "run_budget_remaining"), 0)
        self.assertEqual(self.gauge(gate, "run_budget_noncritical_remaining"), 0)

    def test_a_group_mixing_severities_is_treated_as_critical(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=2, RUN_BUDGET_CRITICAL_RESERVE=2)
        mixed = notification("CephOSDDown", alerts=[alert("CephOSDDown", pod="rook-ceph-osd-2"),
                                                    alert("CephOSDDown", pod="rook-ceph-osd-5", severity="critical")])
        self.assertNotIn("severity", mixed["commonLabels"])

        warning = gate.post(notification("KubePodCrashLooping"))[1]
        status, body = gate.post(mixed)

        self.assertEqual(warning["action"], "created-bare")
        self.assertEqual((status, body["action"]), (200, "created"))
        self.assertIs(body["forwarded"], True)
        self.assertEqual(self.budget(gate), {"run_budget_limit": 2, "run_budget_used": 1, "run_budget_remaining": 1})
        self.assertEqual(self.critical_budget(gate),
                         {"run_budget_critical_reserve": 2, "run_budget_critical_used": 1})

    def test_a_budget_state_file_written_before_the_reserve_keeps_counting(self):
        """The old file has no `critical` key; its slots are the ones the old
        code handed out with no severity in the decision, so they count as
        non-critical rather than crashing or resetting the day."""
        Path(self.state_file).write_text(json.dumps({"date": "2026-09-10", "used": 2}) + "\n")
        gate = self.start_gate(RUN_BUDGET_PER_DAY=3, RUN_BUDGET_CRITICAL_RESERVE=1,
                               GATE_FAKE_NOW="2026-09-10T12:00:00Z")

        self.assertEqual(self.budget(gate), {"run_budget_limit": 3, "run_budget_used": 2, "run_budget_remaining": 1})
        self.assertEqual(self.critical_budget(gate),
                         {"run_budget_critical_reserve": 1, "run_budget_critical_used": 0})
        self.assertEqual(gate.post(notification("WarningOne"))[1]["action"], "created-bare")

        critical = notification("VolSyncBackupStale",
                                alerts=[alert("VolSyncBackupStale", severity="critical", pod="kanidm-0")])
        self.assertEqual(gate.post(critical)[1]["action"], "created")

        with open(self.state_file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"date": "2026-09-10", "used": 3, "critical": 1})
        self.assertNotIn("budget_state_invalid", gate.read_log())

    # -- repeats and resolutions

    def test_still_firing_and_resolved_never_touch_the_budget_or_hermes(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=2)
        firing = notification()
        resolved = notification(status="resolved")
        number = self.github.declare_issue(marker_for(firing["groupKey"]))

        self.assertEqual(gate.post(firing)[1], {"action": "still-firing", "issue": number})
        self.assertEqual(gate.post(firing)[1], {"action": "still-firing", "issue": number})
        self.assertEqual(gate.post(resolved)[1], {"action": "resolved", "issue": number})
        self.assertEqual(gate.post(notification("Other", status="resolved"))[1], {"action": "dropped"})

        self.assertEqual(len(self.github.comments), 3)
        self.assertEqual(self.hermes.requests, [])
        self.assertEqual(self.budget(gate), {"run_budget_limit": 2, "run_budget_used": 0, "run_budget_remaining": 2})
        self.assertEqual(self.counter(gate, "comments_total"), 3)
        self.assertEqual(self.counter(gate, "notifications_received_total"), 4)

    def test_a_suppressed_reopen_is_counted_and_touches_neither_the_budget_nor_hermes(self):
        """The suppression has to be visible, or the Gate quietly swallowing a
        firing alert looks exactly like the Gate never being notified."""
        gate = self.start_gate(RUN_BUDGET_PER_DAY=5)
        payload = notification("VolSyncBackupStale")
        number = self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

        self.assertEqual(gate.post(payload)[1], {"action": "suppressed", "issue": number})
        self.assertEqual(gate.post(payload)[1], {"action": "suppressed", "issue": number})

        self.assertEqual(self.counter(gate, "reopens_suppressed_total"), 2)
        self.assertEqual(self.counter(gate, "issues_reopened_total"), 0)
        self.assertEqual(self.counter(gate, "issues_created_total"), 0)
        self.assertEqual(self.counter(gate, "comments_total"), 0)
        self.assertEqual(self.budget(gate)["run_budget_used"], 0)
        self.assertEqual(self.hermes.requests, [])
        suppressed = self.log_events(gate, "reopen_suppressed")
        self.assertEqual([r["reason"] for r in suppressed], ["started_before_close"] * 2)
        self.assertEqual(suppressed[0]["closed_at"], CLOSED_AT)
        self.assertEqual(suppressed[0]["earliest_starts_at"], "2026-09-10T11:20:03Z")
        self.assertEqual(suppressed[0]["issue"], number)

        episode = notification("VolSyncBackupStale", starts_at=NEW_EPISODE_STARTS_AT)
        self.assertEqual(gate.post(episode)[1], {"action": "reopened", "issue": number})
        self.assertEqual(self.counter(gate, "reopens_suppressed_total"), 2)
        self.assertEqual(self.counter(gate, "issues_reopened_total"), 1)
        self.assertEqual(self.budget(gate)["run_budget_used"], 0)
        self.assertEqual(self.hermes.requests, [])
        self.assertEqual(self.log_events(gate, "incident_issue_reopened")[0]["reason"], "started_after_close")

    # -- rollover and persistence

    def test_budget_rolls_over_at_utc_midnight_and_not_before(self):
        late = self.start_gate(RUN_BUDGET_PER_DAY=1, GATE_FAKE_NOW="2026-09-10T23:59:58Z")
        self.assertEqual(late.post(notification("First"))[1]["action"], "created")
        self.assertEqual(late.post(notification("Second"))[1]["action"], "created-bare")
        late.stop()
        self.gates.remove(late)

        last_second = self.start_gate(RUN_BUDGET_PER_DAY=1, GATE_FAKE_NOW="2026-09-10T23:59:59Z")
        self.assertEqual(last_second.post(notification("Third"))[1]["action"], "created-bare")
        self.assertEqual(self.budget(last_second), {"run_budget_limit": 1, "run_budget_used": 1, "run_budget_remaining": 0})
        last_second.stop()
        self.gates.remove(last_second)

        midnight = self.start_gate(RUN_BUDGET_PER_DAY=1, GATE_FAKE_NOW="2026-09-11T00:00:00Z")
        self.assertEqual(self.budget(midnight), {"run_budget_limit": 1, "run_budget_used": 0, "run_budget_remaining": 1})
        self.assertEqual(midnight.post(notification("Fourth"))[1]["action"], "created")
        self.assertEqual(midnight.post(notification("Fifth"))[1]["action"], "created-bare")

        self.assertEqual([len(r["body"]["prompt"]) > 0 for r in self.hermes.requests], [True, True])
        self.assertIn("First", self.hermes.requests[0]["body"]["prompt"])
        self.assertIn("Fourth", self.hermes.requests[1]["body"]["prompt"])

    def test_budget_survives_a_gate_restart_within_the_same_day(self):
        first = self.start_gate(RUN_BUDGET_PER_DAY=2)
        self.assertEqual(first.post(notification("First"))[1]["action"], "created")
        first.stop()
        self.gates.remove(first)
        self.assertTrue(os.path.exists(self.state_file))

        second = self.start_gate(RUN_BUDGET_PER_DAY=2)
        self.assertEqual(self.budget(second), {"run_budget_limit": 2, "run_budget_used": 1, "run_budget_remaining": 1})
        self.assertEqual(second.post(notification("Second"))[1]["action"], "created")
        self.assertEqual(second.post(notification("Third"))[1]["action"], "created-bare")
        self.assertEqual(len(self.hermes.requests), 2)

    def test_budget_without_a_state_file_still_counts_within_the_process(self):
        gate = GateProcess(self.github.url, hermes=self.hermes, RUN_BUDGET_PER_DAY=1,
                           RUN_BUDGET_CRITICAL_RESERVE=0)
        self.gates.append(gate)

        self.assertEqual(gate.post(notification("First"))[1]["action"], "created")
        self.assertEqual(gate.post(notification("Second"))[1]["action"], "created-bare")
        self.assertIn("BUDGET_STATE_FILE is empty", gate.read_log())

    # -- hermes unavailable

    def test_hermes_error_leaves_the_issue_counts_a_failure_and_answers_alertmanager_with_success(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=5)
        self.hermes.mode = "error"

        status, body = gate.post(notification())

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertIs(body["forwarded"], False)
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.created[0]["labels"], ["needs-triage"])
        self.assertEqual(self.counter(gate, "forward_failures_total"), 1)
        self.assertEqual(self.counter(gate, "forwards_total"), 0)
        self.assertEqual(self.budget(gate)["run_budget_used"], 1)
        self.assertIn("forward_failed", gate.read_log())

        self.hermes.mode = "ok"
        status, body = gate.post(notification())
        self.assertEqual((status, body["action"]), (200, "still-firing"))
        self.assertEqual(len(self.hermes.requests), 1)

    def test_hermes_unreachable_or_hanging_still_answers_alertmanager_with_success(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=5, HERMES_TIMEOUT_SECONDS=1)
        for mode, name in (("disconnect", "Dropped"), ("hang", "Hung")):
            with self.subTest(mode=mode):
                self.hermes.mode = mode
                status, body = gate.post(notification(name))
                self.assertEqual(status, 200, body)
                self.assertEqual((body["action"], body["forwarded"]), ("created", False))
        self.assertEqual(self.counter(gate, "forward_failures_total"), 2)
        self.assertEqual(len(self.github.created), 2)

        down = GateProcess(self.github.url, BUDGET_STATE_FILE=self.state_file, RUN_BUDGET_PER_DAY=5,
                           RUN_BUDGET_CRITICAL_RESERVE=0,
                           HERMES_WEBHOOK_URL="http://127.0.0.1:9/webhooks/investigate", HERMES_WEBHOOK_SECRET="x")
        self.gates.append(down)
        status, body = down.post(notification("Refused"))
        self.assertEqual((status, body["action"], body["forwarded"]), (200, "created", False))
        self.assertEqual(self.counter(down, "forward_failures_total"), 1)

    # -- github failure

    def test_github_create_failure_leaves_the_budget_unchanged(self):
        gate = self.start_gate(RUN_BUDGET_PER_DAY=1)
        self.github.mode = "error"
        self.assertEqual(gate.post(notification("First"))[0], 502)
        self.assertEqual(self.budget(gate)["run_budget_used"], 0)
        self.assertEqual(self.counter(gate, "github_errors_total"), 1)
        self.assertEqual(self.hermes.requests, [])

        self.github.mode = "ok"
        self.assertEqual(gate.post(notification("First"))[1]["action"], "created")
        self.assertEqual(gate.post(notification("Second"))[1]["action"], "created-bare")
        self.assertEqual(self.budget(gate)["run_budget_used"], 1)
        self.assertEqual(len(self.hermes.requests), 1)

    # -- forwarding disabled

    def test_forwarding_disabled_when_hermes_url_is_empty(self):
        gate = self.start_gate(hermes=None, RUN_BUDGET_PER_DAY=5)

        status, body = gate.post(notification())

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created-bare")
        self.assertEqual(self.github.created[0]["labels"], ["needs-triage", "uninvestigated"])
        self.assertEqual(self.hermes.requests, [])
        self.assertEqual(self.budget(gate)["run_budget_used"], 0)
        self.assertEqual(self.counter(gate, "forward_failures_total"), 0)
        self.assertIn("HERMES_WEBHOOK_URL is empty", gate.read_log())
        self.assertIn("forwarding_disabled", gate.read_log())

    # -- metrics

    def test_metrics_expose_every_series_with_help_and_type(self):
        heartbeat = os.path.join(self.state_dir.name, "heartbeat.txt")
        gate = self.start_gate(RUN_BUDGET_PER_DAY=7, HEARTBEAT_FILE=heartbeat)

        values, declared = gate.metrics()
        for name in COUNTER_NAMES:
            full = f"alert_agent_gate_{name}"
            self.assertEqual(declared.get(full), {"help", "counter"}, full)
            self.assertEqual(values[full], 0, full)
        for name in GAUGE_NAMES:
            full = f"alert_agent_gate_{name}"
            self.assertEqual(declared.get(full), {"help", "gauge"}, full)
        self.assertEqual(values["alert_agent_gate_run_budget_limit"], 7)
        self.assertEqual(values["alert_agent_gate_heartbeat_file_present"], 0)
        self.assertGreaterEqual(values["alert_agent_gate_heartbeat_age_seconds"], 1e9)

        Path(heartbeat).write_text("ok\n")
        values, _ = gate.metrics()
        self.assertEqual(values["alert_agent_gate_heartbeat_file_present"], 1)
        self.assertLess(values["alert_agent_gate_heartbeat_age_seconds"], 60)

        two_days = time.time() - 2 * 86400
        os.utime(heartbeat, (two_days, two_days))
        values, _ = gate.metrics()
        self.assertGreater(values["alert_agent_gate_heartbeat_age_seconds"], 86400 * 1.9)
        self.assertLess(values["alert_agent_gate_heartbeat_age_seconds"], 86400 * 2.1)

        gate.post(notification())
        values, _ = gate.metrics()
        self.assertEqual(values["alert_agent_gate_notifications_received_total"], 1)
        self.assertEqual(values["alert_agent_gate_issues_created_total"], 1)
        self.assertEqual(values["alert_agent_gate_forwards_total"], 1)
        self.assertEqual(values["alert_agent_gate_run_budget_used"], 1)
        self.assertEqual(values["alert_agent_gate_run_budget_remaining"], 6)
        self.assertEqual(gate.get("/healthz")[0], 200)


class IncidentIndexTests(unittest.TestCase):
    """One Incident Issue per Alert Group even while GitHub's listing lags.

    Every test here runs with `hide_new_from_listing`, the live bug in
    miniature: the Gate has just created an issue that `GET /issues` still does
    not return, and Alertmanager retries within seconds.
    """

    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub()
        cls.hermes = FakeHermes()

    @classmethod
    def tearDownClass(cls):
        cls.github.stop()
        cls.hermes.stop()

    def setUp(self):
        self.github.reset()
        self.hermes.reset()
        self.state_dir = tempfile.TemporaryDirectory(prefix="gate-index-")
        self.index_file = os.path.join(self.state_dir.name, "incident-index.json")
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.stop()
        self.state_dir.cleanup()

    def start_gate(self, **env):
        gate = GateProcess(self.github.url, hermes=self.hermes, RUN_BUDGET_PER_DAY=1000,
                           BUDGET_STATE_FILE=os.path.join(self.state_dir.name, "run-budget.json"),
                           INCIDENT_INDEX_FILE=self.index_file, **env)
        self.gates.append(gate)
        return gate

    def entries(self):
        with open(self.index_file, encoding="utf-8") as handle:
            return json.load(handle)["entries"]

    # -- the duplicate that reached production

    def test_immediate_retry_while_the_listing_lags_creates_one_incident_issue(self):
        gate = self.start_gate()
        self.github.hide_new_from_listing = True
        payload = notification()

        created = gate.post(payload)[1]
        status, body = gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(created["action"], "created")
        self.assertEqual(body, {"action": "still-firing", "issue": created["issue"]})
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(len(self.hermes.requests), 1)

    # -- the comment on a closed issue that reached production

    def test_a_stale_listing_cannot_hide_that_the_incident_issue_was_closed(self):
        """The listing reports a just-closed issue as open; only the by-number
        read is consistent, so it must decide. Live on 2026-09-11 the Gate
        trusted the listing and commented on an issue that stayed closed, where
        nobody would see it. The comment now lands on a reopened issue, and a
        Gate that still trusted the listing would never send the PATCH."""
        gate = self.start_gate()
        payload = notification(starts_at=NEW_EPISODE_STARTS_AT)
        number = self.github.declare_issue(f"earlier\n\n{marker_for(payload['groupKey'])}")
        self.github.close_issue(number, listing_still_open=True)

        status, body = gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": number})
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.github.reopened, [number])
        self.assertEqual(self.github.issues[number]["state"], "open")
        self.assertRegex(self.github.comments[0]["body"], rf"^Firing again at {ISO_UTC}")
        self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], number)

    def test_a_closed_incident_issue_is_reopened_when_the_index_has_forgotten_it(self):
        """The index prunes; the listing is the only memory left, and it has to
        carry the reopen on its own."""
        gate = self.start_gate()
        payload = notification(starts_at=NEW_EPISODE_STARTS_AT)
        created = gate.post(payload)[1]
        self.github.close_issue(created["issue"])
        Path(self.index_file).write_text(json.dumps({"entries": {}}))

        status, body = gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": created["issue"]})
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.reopened, [created["issue"]])
        self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], created["issue"])

    def test_an_open_issue_found_only_through_the_listing_is_still_used(self):
        """The index knows nothing after a restart, so the listing must still
        work: its hit is confirmed by number, not discarded."""
        gate = self.start_gate()
        declared = self.github.declare_issue(body=f"stale\n\n{marker_for(notification()['groupKey'])}")

        status, body = gate.post(notification())

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "still-firing", "issue": declared})
        self.assertEqual(self.github.created, [])
        self.assertEqual(len(self.github.comments), 1)

    def test_resolved_immediately_after_creation_comments_instead_of_being_dropped(self):
        gate = self.start_gate()
        self.github.hide_new_from_listing = True

        created = gate.post(notification())[1]
        status, body = gate.post(notification(status="resolved"))

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "resolved", "issue": created["issue"]})
        self.assertEqual(len(self.github.created), 1)
        self.assertRegex(self.github.comments[0]["body"], rf"^Resolved at {ISO_UTC}")
        self.assertEqual(self.github.issues[created["issue"]]["state"], "open")

    def test_the_listing_catching_up_does_not_change_the_answer(self):
        gate = self.start_gate()
        self.github.hide_new_from_listing = True
        payload = notification()
        created = gate.post(payload)[1]
        self.github.release_listing()

        self.assertEqual(gate.post(payload)[1], {"action": "still-firing", "issue": created["issue"]})
        self.assertEqual(len(self.github.created), 1)

    # -- what the index costs and skips

    def test_an_index_hit_reads_the_issue_by_number_and_never_lists(self):
        gate = self.start_gate()
        payload = notification()
        number = gate.post(payload)[1]["issue"]
        self.github.requests.clear()

        gate.post(payload)

        self.assertEqual([r["path"] for r in self.github.requests if r["method"] == "GET"],
                         [f"/repos/{INCIDENTS_REPO}/issues/{number}"])

    def test_an_issue_found_through_the_listing_is_indexed(self):
        gate = self.start_gate()
        payload = notification()
        number = self.github.declare_issue(marker_for(payload["groupKey"]))

        self.assertEqual(gate.post(payload)[1], {"action": "still-firing", "issue": number})

        self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], number)

    # -- surviving a restart

    def test_the_index_survives_a_gate_restart(self):
        first = self.start_gate()
        self.github.hide_new_from_listing = True
        created = first.post(notification())[1]
        first.stop()
        self.gates.remove(first)
        self.assertEqual(self.entries()[index_key_for(notification()["groupKey"])]["issue"], created["issue"])

        second = self.start_gate()
        status, body = second.post(notification())

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "still-firing", "issue": created["issue"]})
        self.assertEqual(len(self.github.created), 1)

    # -- entries that are only trusted after the by-number read

    def test_an_indexed_issue_that_was_closed_is_reopened_without_asking_the_listing(self):
        """The index hit is kept once the by-number read confirms it: closed is
        no longer a reason to drop it. With the listing blind to the issue, a
        Gate that fell through to it would file a second one."""
        gate = self.start_gate()
        self.github.hide_new_from_listing = True
        payload = notification(starts_at=NEW_EPISODE_STARTS_AT)
        created = gate.post(payload)[1]
        self.github.close_issue(created["issue"])
        self.github.requests.clear()

        status, body = gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"action": "reopened", "issue": created["issue"]})
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.reopened, [created["issue"]])
        self.assertEqual([r["path"] for r in self.github.requests if r["method"] == "GET"],
                         [f"/repos/{INCIDENTS_REPO}/issues/{created['issue']}"])
        self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], created["issue"])
        self.assertNotIn("incident_index_dropped", gate.read_log())

    def test_a_suppressed_firing_keeps_its_index_entry_and_files_nothing(self):
        """Leaving the close alone must not become a fall-through to a create:
        with the listing blind, the index is all that stands between a
        suppressed notification and a duplicate Incident Issue."""
        gate = self.start_gate()
        self.github.hide_new_from_listing = True
        payload = notification()
        created = gate.post(payload)[1]
        self.github.close_issue(created["issue"])

        outcomes = [gate.post(payload)[1] for _ in range(3)]

        self.assertEqual(outcomes, [{"action": "suppressed", "issue": created["issue"]}] * 3)
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.reopened, [])
        self.assertEqual(self.github.issues[created["issue"]]["state"], "closed")
        self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], created["issue"])

    def test_an_indexed_issue_that_is_gone_falls_back_to_the_listing(self):
        gate = self.start_gate()
        payload = notification()
        created = gate.post(payload)[1]
        del self.github.issues[created["issue"]]
        number = self.github.declare_issue(marker_for(payload["groupKey"]))

        self.assertEqual(gate.post(payload)[1], {"action": "still-firing", "issue": number})
        self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], number)

    # -- an index the Gate cannot read

    def test_an_empty_or_corrupt_index_falls_back_to_the_listing_and_says_so(self):
        for name, content in (("truncated", "{not json"), ("empty", ""),
                              ("wrong shape", '{"entries": 7}'), ("not an object", "[]")):
            with self.subTest(index=name):
                self.github.reset()
                Path(self.index_file).write_text(content)
                gate = self.start_gate()
                payload = notification()
                number = self.github.declare_issue(marker_for(payload["groupKey"]))

                self.assertEqual(gate.post(payload)[1], {"action": "still-firing", "issue": number})
                self.assertEqual(self.github.created, [])
                self.assertRegex(gate.read_log(), r"incident_index_(unreadable|invalid)")
                self.assertEqual(self.entries()[index_key_for(payload["groupKey"])]["issue"], number)

    def test_an_unwritable_index_leaves_the_gate_working_through_the_listing(self):
        os.mkdir(self.index_file)
        gate = self.start_gate()
        payload = notification()

        self.assertEqual(gate.post(payload)[1]["action"], "created")
        self.assertEqual(gate.post(payload)[1]["action"], "still-firing")
        self.assertEqual(len(self.github.created), 1)
        self.assertRegex(gate.read_log(), r"incident_index_(unreadable|write_failed)")

    # -- bounded growth

    def test_the_index_cannot_grow_without_bound(self):
        frozen = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        overflow = INDEX_MAX_ENTRIES + 200
        seeded = {f"{i:024x}": {"issue": 10_000 + i,
                                "at": (frozen - timedelta(minutes=overflow - i)).strftime("%Y-%m-%dT%H:%M:%SZ")}
                  for i in range(overflow)}
        expired = {f"a{i:023x}": {"issue": 900_000 + i,
                                  "at": (frozen - timedelta(days=INDEX_MAX_AGE_DAYS + 1 + i)).strftime(
                                      "%Y-%m-%dT%H:%M:%SZ")}
                   for i in range(5)}
        Path(self.index_file).write_text(json.dumps({"entries": dict(seeded, **expired)}))

        gate = self.start_gate(GATE_FAKE_NOW=frozen.strftime("%Y-%m-%dT%H:%M:%SZ"))
        payload = notification()
        gate.post(payload)

        entries = self.entries()
        self.assertEqual(len(entries), INDEX_MAX_ENTRIES)
        self.assertIn(index_key_for(payload["groupKey"]), entries)
        for key in expired:
            self.assertNotIn(key, entries)
        self.assertNotIn(f"{0:024x}", entries)
        self.assertIn(f"{overflow - 1:024x}", entries)
        self.assertEqual(gate.post(payload)[1]["action"], "still-firing")


if __name__ == "__main__":
    unittest.main()
