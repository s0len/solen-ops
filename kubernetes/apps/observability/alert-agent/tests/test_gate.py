"""Gate tests: real Alertmanager payloads in, GitHub issues and comments out.

The Gate runs as the real script in a subprocess, configured only through its
environment, and is driven purely over HTTP. GitHub is an in-process fake that
declares existing issues and records what the Gate creates and comments. The
tests assert only on what leaves the Gate: HTTP status codes, created issues,
comments.

The one piece of Gate internals the tests know is the marker format, because
it is a persisted contract: changing it would orphan every open Incident Issue.
"""
import hashlib
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GATE_SCRIPT = Path(__file__).resolve().parents[1] / "app" / "scripts" / "gate.py"
INCIDENTS_REPO = "example/incidents"
TOKEN = "test-token"
ISO_UTC = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def marker_for(group_key):
    return f"<!-- alert-agent:group={hashlib.sha256(group_key.encode()).hexdigest()[:24]} -->"


# --------------------------------------------------------------------------- payload builders

def alert(alertname, status="firing", **labels):
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
        "startsAt": "2026-09-10T11:20:03.117Z",
        "endsAt": ends_at,
        "generatorURL": "http://prometheus.observability:9090/graph?g0.expr=max_over_time%28kube_pod_container_status_waiting_reason%7Breason%3D%22CrashLoopBackOff%22%7D%5B5m%5D%29+%3E%3D+1&g0.tab=1",
        "fingerprint": hashlib.md5(json.dumps(all_labels, sort_keys=True).encode()).hexdigest()[:16],
    }


def notification(alertname="KubePodCrashLooping", job="kube-state-metrics", status="firing", alerts=None):
    """A full Alertmanager v4 webhook notification for one Alert Group."""
    alerts = alerts if alerts is not None else [alert(alertname, status=status, pod="sonarr-0")]
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
    """Enough of the GitHub Issues API for the Gate: list, create, comment."""

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
            self.comments = []
            self.requests = []
            self.next_number = 1

    def declare_issue(self, body, title="declared", state="open", labels=(), pull_request=False):
        with self.lock:
            number = self.next_number
            self.next_number += 1
            item = {"number": number, "title": title, "body": body, "state": state,
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
                if url.path != f"/repos/{INCIDENTS_REPO}/issues":
                    self._json(404, {"message": "Not Found"})
                    return
                q = parse_qs(url.query)
                state = q.get("state", ["open"])[0]
                per_page = int(q.get("per_page", ["30"])[0])
                page = int(q.get("page", ["1"])[0])
                with fake.lock:
                    items = sorted(fake.issues.values(), key=lambda i: i["number"])
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
                    self._json(201, item)
                    return
                prefix = f"/repos/{INCIDENTS_REPO}/issues/"
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
                self._record(self._body())
                self._json(500, {"message": "the Gate must never edit or close an issue"})

        return Handler


# --------------------------------------------------------------------------- the gate under test

class GateProcess:
    """The real gate.py, started as a subprocess with only its environment."""

    def __init__(self, github_url):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = tempfile.NamedTemporaryFile(prefix="gate-", suffix=".log", delete=False)
        env = dict(os.environ, PORT=str(self.port), GITHUB_API_URL=github_url,
                   GITHUB_INCIDENTS_REPO=INCIDENTS_REPO, GITHUB_TOKEN=TOKEN, GITHUB_TIMEOUT_SECONDS="5")
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


# --------------------------------------------------------------------------- tests

class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.github = FakeGitHub()
        cls.gate = GateProcess(cls.github.url)

    @classmethod
    def tearDownClass(cls):
        cls.gate.stop()
        cls.github.stop()

    def setUp(self):
        self.github.reset()

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

    def test_closed_incident_issue_does_not_absorb_a_new_firing(self):
        payload = notification()
        self.github.declare_issue(marker_for(payload["groupKey"]), state="closed")

        status, body = self.gate.post(payload)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.comments, [])

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


if __name__ == "__main__":
    unittest.main()
