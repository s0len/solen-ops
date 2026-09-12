"""Remediator tests: the real Catalogue in, the exact argv out.

The catalogue under `remediations/` is loaded here the way the pod loads it —
converted to JSON first, by PyYAML if it is available and otherwise by the
`yq` image the init container uses — so these tests fail if a shipped entry
stops parsing, loses a precondition or grows a command the Remediator is not
allowed to run.

Everything that leaves the process is faked and recorded: kubectl is a script
of canned results keyed by the command line, GitHub and Alertmanager are
in-process objects. The assertions are on what the Remediator would have done:
which entry it chose, which commands it rendered, in which order, and what it
refused.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "remediator" / "app" / "scripts"
GATE_DIR = REPO_ROOT / "app" / "scripts"
CATALOGUE_DIR = REPO_ROOT / "remediations"
CATALOGUE_FILES = sorted(p for p in CATALOGUE_DIR.glob("*.yaml") if p.name != "kustomization.yaml")
YQ_IMAGE = "docker.io/mikefarah/yq:4.53.6"

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(GATE_DIR))
import remediate  # noqa: E402
# The Gate renders the Incident Issue bodies these tests hand the Remediator,
# so the parsing under test is parsing the real thing rather than a fixture
# that agrees with it.
import gate  # noqa: E402


def render_catalogue(destination: Path) -> Path:
    """The catalogue as JSON, the way the init container renders it."""
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        documents = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in CATALOGUE_FILES]
        destination.write_text(json.dumps(documents), encoding="utf-8")
        return destination
    if shutil.which("docker") is None:
        raise unittest.SkipTest("neither PyYAML nor docker is available to render the catalogue")
    with tempfile.TemporaryDirectory() as staging:
        for path in CATALOGUE_FILES:
            shutil.copy(path, Path(staging) / path.name)
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{staging}:/catalogue:ro",
             "-v", f"{destination.parent}:/work", "--entrypoint", "sh", YQ_IMAGE,
             "-c", f"yq ea -o=json -I=0 '[.]' /catalogue/*.yaml > /work/{destination.name}"],
            check=True, capture_output=True,
        )
    return destination


def iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRunner(remediate.Runner):
    """kubectl as a lookup table. Anything unscripted is a loud failure.

    A value may be a list of results, consumed in order and then repeating the
    last one, for a command whose answer changes because a step changed it.
    """

    def __init__(self, responses: dict, dry_run: bool = False):
        super().__init__("kubectl-not-really", dry_run=dry_run)
        self.responses = responses
        self.calls = []
        self.rendered = []

    def run(self, argv, timeout_seconds=remediate.DEFAULT_STEP_TIMEOUT, mutating=False):
        if not argv or argv[0] != remediate.ALLOWED_ARGV0:
            raise remediate.BindingError(f"bad argv0: {argv}")
        printed = " ".join(argv)
        # `rendered` is everything it asked to run, `calls` only what it really
        # ran, so a dry run can be told from a real one.
        self.rendered.append(printed)
        if mutating and self.dry_run:
            return remediate.CommandResult(printed=printed, returncode=0,
                                           stdout="[dry run: not executed]", stderr="")
        self.calls.append(printed)
        if printed not in self.responses:
            return remediate.CommandResult(printed=printed, returncode=1, stdout="",
                                           stderr="no canned response for this command")
        scripted = self.responses[printed]
        if isinstance(scripted, list):
            stdout, code = scripted[0] if len(scripted) == 1 else scripted.pop(0)
        else:
            stdout, code = scripted
        return remediate.CommandResult(printed=printed, returncode=code, stdout=stdout, stderr="")


class FakeGitHub:
    """The Issues API as a recorder, with the real client's `dry_run` gate.

    `comments`, `removed` and `added` are what the Remediator asked for;
    `sent` is only what a real `GitHubClient` would have put on the wire, which
    under `dry_run` is nothing at all.
    """

    def __init__(self, issues, dry_run=False):
        self.issues = issues
        self.dry_run = dry_run
        self.comments = []
        self.removed = []
        self.added = []
        self.sent = []

    def _send(self, kind, number, payload):
        if not self.dry_run:
            self.sent.append((kind, number, payload))

    def open_issues_with_label(self, label):
        return [i for i in self.issues if label in i.get("labels", [])]

    def comment(self, number, body):
        self.comments.append((number, body))
        self._send("comment", number, body)

    def remove_label(self, number, label):
        self.removed.append((number, label))
        self._send("remove_label", number, label)

    def add_label(self, number, label):
        self.added.append((number, label))
        self._send("add_label", number, label)


class FakeLedger(remediate.Ledger):

    def __init__(self):
        self.records = []
        self.saves = 0

    def load(self):
        pass

    def save(self):
        self.saves += 1


def alert(alertname, **labels):
    return {"labels": dict(labels, alertname=alertname), "status": {"state": "active"}}


TRIGGER = remediate.DEFAULT_TRIGGER_LABEL


def gate_body(alerts, group_key=None):
    """The Incident Issue body the Gate itself writes for these alerts.

    The real group key carries a `|` from the route's alertname matcher, which
    the Gate's own `md_code` rewrites on the way into the prose — so it is here
    too, to keep the marker the only thing the Remediator reads.
    """
    first = (alerts[0]["labels"] if alerts else {})
    alertname = first.get("alertname", "alert")
    group_key = group_key or (
        '{}/{alertname!~"Watchdog|InfoInhibitor"}:{alertname="%s", job="%s"}'
        % (alertname, first.get("job", "kube-state-metrics"))
    )
    notification = gate.Notification(
        group_key=group_key,
        status="firing",
        alerts=[dict(item, status="firing", startsAt="2026-09-11T06:00:00Z") for item in alerts],
        group_labels={"alertname": alertname},
        common_labels={},
        common_annotations={},
        external_url="https://alertmanager.example",
        receiver="alert-agent",
        version="4",
    )
    return gate.render_issue_body(notification, gate.group_marker(group_key), "2026-09-11T06:01:00Z")


def issue(number, title, labels=(TRIGGER,), alerts=(), body=None):
    """An Incident Issue as the Gate filed it: title, trigger label, real body."""
    return {
        "number": number,
        "title": title,
        "labels": list(labels),
        "body": gate_body(list(alerts)) if body is None else body,
    }


class CatalogueLoads(unittest.TestCase):
    """The shipped catalogue is the fixture; nothing here is invented."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = render_catalogue(Path(cls.tmp.name) / "catalogue.json")
        cls.entries = remediate.load_catalogue(str(path))
        cls.by_id = {entry.identifier: entry for entry in cls.entries}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_shipped_file_is_loaded(self):
        failures = {entry.failure for entry in self.entries}
        self.assertEqual(failures, {path.stem for path in CATALOGUE_FILES})

    def test_the_four_known_failures_are_present(self):
        self.assertEqual(
            sorted(entry.alertname for entry in self.entries),
            ["CephMgrModuleCrash", "KubeJobFailed", "KubeJobNotCompleted",
             "VolSyncBackupStale", "etcdDatabaseHighFragmentationRatio"],
        )

    def test_etcd_defrag_ships_disabled(self):
        self.assertFalse(self.by_id["etcd-database-fragmentation/run-defrag-cronjob"].enabled)

    def test_every_entry_declares_a_blast_radius_and_a_budget(self):
        for entry in self.entries:
            with self.subTest(entry=entry.identifier):
                self.assertGreater(len(entry.blast_radius.split()), 20)
                self.assertGreaterEqual(entry.max_runs, 1)
                self.assertGreaterEqual(entry.window_hours, 1)
                self.assertTrue(entry.preconditions)
                self.assertTrue(entry.steps)

    def test_no_step_or_check_can_run_anything_but_kubectl(self):
        for entry in self.entries:
            for command in list(entry.steps) + list(entry.evidence) + [
                check for check in list(entry.preconditions) + list(entry.verify) if check.argv
            ]:
                argv = command.argv
                with self.subTest(entry=entry.identifier, argv=argv):
                    self.assertEqual(argv[0], "kubectl")
                    index = 1
                    while argv[index] in remediate.NAMESPACE_FLAGS:
                        index += 2
                    self.assertIn(argv[index], remediate.ALLOWED_VERBS)

    def test_no_verification_or_evidence_command_mutates(self):
        """`steps` is the only field that may change anything."""
        for entry in self.entries:
            commands = [check.argv for check in entry.verify if check.argv]
            commands += [step.argv for step in entry.evidence]
            for argv in commands:
                index = 1
                while argv[index] in remediate.NAMESPACE_FLAGS:
                    index += 2
                with self.subTest(entry=entry.identifier, argv=argv):
                    self.assertIn(argv[index], remediate.CHECK_VERBS)
                    self.assertNotIn("delete", remediate.CHECK_VERBS)
                    self.assertNotIn("create", remediate.CHECK_VERBS)

    def test_no_precondition_mutates(self):
        mutating = {"delete", "create", "exec"}
        for entry in self.entries:
            for check in entry.preconditions:
                if check.argv is None:
                    continue
                verbs = [element for element in check.argv if element in mutating]
                with self.subTest(entry=entry.identifier, check=check.identifier):
                    # `exec` is a read here: the admission policy pins the
                    # command to `ceph crash|health|osd|pg|status`.
                    self.assertNotIn("delete", verbs)
                    self.assertNotIn("create", verbs)


class CatalogueRefusesBadEntries(unittest.TestCase):

    def load(self, document):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump([document], handle)
            path = handle.name
        try:
            return remediate.load_catalogue(path)
        finally:
            os.unlink(path)

    def base(self, **overrides):
        document = {
            "version": 1,
            "failure": "example",
            "entries": [{
                "id": "example/thing",
                "alertname": "Example",
                "blast_radius": "one object",
                "max_runs": {"count": 1, "window_hours": 1},
                "preconditions": [{"id": "p", "run": ["kubectl", "get", "pods"], "expect": {"not_empty": True}}],
                "steps": [{"run": ["kubectl", "delete", "job", "x"]}],
            }],
        }
        document["entries"][0].update(overrides)
        return document

    def test_a_non_kubectl_command_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(steps=[{"run": ["bash", "-c", "rm -rf /"]}]))

    def test_an_unlisted_verb_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(steps=[{"run": ["kubectl", "apply", "-f", "-"]}]))

    def test_patching_the_run_ledger_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(steps=[{
                "run": ["kubectl", "-n", "observability", "patch", "configmap", "alert-remediator-ledger"],
            }]))

    def test_a_misspelled_enabled_key_is_refused_rather_than_defaulted(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(enable=False))

    def test_an_unknown_expectation_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(preconditions=[
                {"id": "p", "run": ["kubectl", "get", "pods"], "expect": {"contains": "x"}},
            ]))

    def test_a_verification_that_deletes_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(verify=[{
                "id": "v", "run": ["kubectl", "delete", "job", "x"], "expect": {"not_empty": False},
            }]))

    def test_a_precondition_that_deletes_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(preconditions=[{
                "id": "p", "run": ["kubectl", "delete", "pvc", "x"], "expect": {"not_empty": False},
            }]))

    def test_evidence_that_creates_is_refused(self):
        with self.assertRaises(remediate.CatalogueError):
            self.load(self.base(evidence=[{
                "run": ["kubectl", "-n", "kube-system", "create", "job", "x", "--from=cronjob/etcd-defrag"],
            }]))

    def test_a_step_may_still_delete(self):
        """The restriction is on the read-only fields, not on the catalogue."""
        entries = self.load(self.base(steps=[{"run": ["kubectl", "delete", "job", "x"]}]))
        self.assertEqual(entries[0].steps[0].argv, ["kubectl", "delete", "job", "x"])

    def test_a_duplicate_entry_id_is_refused(self):
        document = self.base()
        document["entries"].append(dict(document["entries"][0]))
        with self.assertRaises(remediate.CatalogueError):
            self.load(document)


class Bindings(unittest.TestCase):

    def entry(self, bind):
        return remediate.Entry(
            identifier="x/y", failure="x", alertname="A", enabled=True, match={}, bind=bind,
            blast_radius="", max_runs=1, window_hours=1, preconditions=[], steps=[], verify=[], evidence=[],
        )

    def test_labels_become_bindings(self):
        bindings = remediate.build_bindings(
            self.entry({"ns": "{namespace}", "rs": "{name}"}),
            {"namespace": "security", "name": "kanidm"}, "202609111200",
        )
        self.assertEqual(bindings["ns"], "security")
        self.assertEqual(bindings["rs"], "kanidm")
        self.assertEqual(bindings["run_stamp"], "202609111200")

    def test_strip_prefix_derives_the_replicationsource_from_the_job(self):
        bindings = remediate.build_bindings(
            self.entry({"rs": {"from": "{job_name}", "strip_prefix": "volsync-src-"}}),
            {"job_name": "volsync-src-kanidm"}, "202609111200",
        )
        self.assertEqual(bindings["rs"], "kanidm")

    def test_strip_prefix_refuses_a_value_without_it(self):
        with self.assertRaises(remediate.BindingError):
            remediate.build_bindings(
                self.entry({"rs": {"from": "{job_name}", "strip_prefix": "volsync-src-"}}),
                {"job_name": "beets-import"}, "202609111200",
            )

    def test_a_label_that_is_not_a_safe_token_never_reaches_a_command(self):
        with self.assertRaises(remediate.BindingError):
            remediate.build_bindings(
                self.entry({"ns": "{namespace}"}),
                {"namespace": "security; rm -rf /"}, "202609111200",
            )

    def test_a_shell_looking_label_is_dropped_rather_than_bound(self):
        bindings = remediate.build_bindings(
            self.entry({}), {"namespace": "security", "instance": "$(whoami)"}, "202609111200",
        )
        self.assertIn("namespace", bindings)
        self.assertNotIn("instance", bindings)

    def test_an_unbound_placeholder_is_an_error_not_an_empty_string(self):
        with self.assertRaises(remediate.BindingError):
            remediate.render_argv(["kubectl", "get", "pvc", "{nope}"], {"ns": "x"})


class Expectations(unittest.TestCase):

    def test_absent_jsonpath_output_counts_as_zero(self):
        self.assertTrue(remediate.evaluate({"integer_at_most": 0}, "")[0])
        self.assertFalse(remediate.evaluate({"integer_at_least": 1}, "")[0])

    def test_older_than_hours(self):
        self.assertTrue(remediate.evaluate({"older_than_hours": 1}, iso(-2))[0])
        self.assertFalse(remediate.evaluate({"older_than_hours": 1}, iso(-0.1))[0])

    def test_newer_than_capture(self):
        captures = {"failed_at": iso(-48)}
        self.assertTrue(remediate.evaluate({"newer_than_capture": "failed_at"}, iso(-1), captures=captures)[0])
        self.assertFalse(remediate.evaluate({"newer_than_capture": "failed_at"}, iso(-72), captures=captures)[0])

    def test_a_missing_capture_fails_closed(self):
        self.assertFalse(remediate.evaluate({"newer_than_capture": "failed_at"}, iso(-1), captures={})[0])

    def test_pluck_list_rejects_an_unacceptable_id(self):
        payload = json.dumps([{"crash_id": "2026-09-11T08:30:00.000000Z_" + "a" * 36}])
        items, _ = remediate.pluck_list(payload, "crash_id", r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z_[0-9a-f-]{36}$")
        self.assertEqual(len(items), 1)
        bad = json.dumps([{"crash_id": "; reboot"}])
        self.assertIsNone(remediate.pluck_list(bad, "crash_id", r"^.*$")[0])


class VolSyncGhostSnapshot(unittest.TestCase):
    """The sequence that cleared security/kanidm on 2026-09-11, end to end."""

    NS, RS = "security", "kanidm"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = render_catalogue(Path(cls.tmp.name) / "catalogue.json")
        cls.entries = remediate.load_catalogue(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def wedged_cluster(self):
        ns, rs = self.NS, self.RS
        return {
            f"kubectl -n {ns} get replicationsource {rs} -o jsonpath={{.spec.sourcePVC}}": (rs, 0),
            f"kubectl -n {ns} get pvc {rs} -o jsonpath={{.status.phase}}": ("Bound", 0),
            f"kubectl -n {ns} get pvc volsync-{rs}-src -o jsonpath={{.status.phase}}": ("Pending", 0),
            f"kubectl -n {ns} get pvc volsync-{rs}-src -o "
            f"jsonpath={{.spec.dataSource.kind}}/{{.spec.dataSource.name}}": (f"VolumeSnapshot/volsync-{rs}-src", 0),
            f"kubectl -n {ns} get volumesnapshot volsync-{rs}-src -o jsonpath={{.status.readyToUse}}": ("true", 0),
            f"kubectl -n {ns} get job volsync-src-{rs} -o jsonpath={{.status.succeeded}}": ("", 0),
            f"kubectl -n {ns} get job volsync-src-{rs} -o jsonpath={{.status.active}}": ("1", 0),
            f"kubectl -n {ns} get job volsync-src-{rs} -o jsonpath={{.status.startTime}}": (iso(-37), 0),
            f"kubectl -n {ns} delete job volsync-src-{rs} --wait=false --ignore-not-found=true":
                ("job.batch \"volsync-src-kanidm\" deleted", 0),
            f"kubectl -n {ns} delete pvc volsync-{rs}-src --wait=false --ignore-not-found=true":
                ("persistentvolumeclaim deleted", 0),
            f"kubectl -n {ns} delete volumesnapshot volsync-{rs}-src --wait=false --ignore-not-found=true": ("", 0),
            f"kubectl -n {ns} wait --for=delete volumesnapshot/volsync-{rs}-src --timeout=45s": ("deleted", 0),
            f"kubectl -n {ns} get volumesnapshot volsync-{rs}-src --ignore-not-found=true -o name": ("", 0),
        }

    def firing(self):
        return [alert("VolSyncBackupStale", namespace=self.NS, name=self.RS)]

    def remediator(self, responses, ledger=None, config=None):
        config = config or remediate.Config(github_token="t", incidents_repo="example/incidents")
        runner = FakeRunner(responses, dry_run=config.dry_run)
        github = FakeGitHub([issue(
            38, "VolSyncBackupStale: security/kanidm backup is 1d past its sync time",
            alerts=self.firing(),
        )], dry_run=config.dry_run)
        return remediate.Remediator(config, runner, github, ledger or FakeLedger(), self.entries), runner, github

    def test_it_runs_the_verified_sequence_in_order(self):
        remediator, runner, github = self.remediator(self.wedged_cluster())
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(selection.entry.identifier, "volsync-ghost-snapshot/backup-stale")
        self.assertEqual(remediator.execute(selection), "succeeded")
        mutations = [call for call in runner.calls if " delete " in call or " wait " in call]
        self.assertEqual(mutations, [
            f"kubectl -n {self.NS} delete job volsync-src-{self.RS} --wait=false --ignore-not-found=true",
            f"kubectl -n {self.NS} delete pvc volsync-{self.RS}-src --wait=false --ignore-not-found=true",
            f"kubectl -n {self.NS} delete volumesnapshot volsync-{self.RS}-src --wait=false --ignore-not-found=true",
            f"kubectl -n {self.NS} wait --for=delete volumesnapshot/volsync-{self.RS}-src --timeout=45s",
        ])
        self.assertIn(
            f"kubectl -n {self.NS} get volumesnapshot volsync-{self.RS}-src --ignore-not-found=true -o name",
            runner.calls,
        )
        self.assertEqual(github.removed, [(38, TRIGGER)])
        self.assertEqual(github.added, [])
        self.assertIn("Catalogued Remediation executed", github.comments[0][1])
        self.assertEqual([kind for kind, _, _ in github.sent], ["remove_label", "comment"])

    def test_it_never_names_the_source_pvc_in_a_delete(self):
        remediator, runner, github = self.remediator(self.wedged_cluster())
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        remediator.execute(selection)
        for call in runner.calls:
            if "delete" in call:
                self.assertNotIn(f"pvc {self.RS} ", call + " ")

    def test_an_unbound_source_pvc_blocks_everything(self):
        responses = self.wedged_cluster()
        responses[f"kubectl -n {self.NS} get pvc {self.RS} -o jsonpath={{.status.phase}}"] = ("Lost", 0)
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])
        self.assertEqual(github.removed, [])
        self.assertIn("did not run", github.comments[0][1])

    def test_a_bound_temp_pvc_is_not_a_ghost_and_blocks(self):
        responses = self.wedged_cluster()
        responses[f"kubectl -n {self.NS} get pvc volsync-{self.RS}-src -o jsonpath={{.status.phase}}"] = ("Bound", 0)
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_a_young_mover_job_blocks(self):
        responses = self.wedged_cluster()
        responses[f"kubectl -n {self.NS} get job volsync-src-{self.RS} -o jsonpath={{.status.startTime}}"] = (iso(-0.2), 0)
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_a_failing_step_hands_the_issue_to_a_human(self):
        responses = self.wedged_cluster()
        responses[f"kubectl -n {self.NS} wait --for=delete volumesnapshot/volsync-{self.RS}-src --timeout=45s"] = ("", 1)
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "failed")
        self.assertEqual(github.removed, [(38, TRIGGER)])
        self.assertEqual(github.added, [(38, "ready-for-human")])
        self.assertIn("failed part-way", github.comments[0][1])

    def test_the_budget_stops_a_third_run_in_the_window(self):
        ledger = FakeLedger()
        remediator, runner, github = self.remediator(self.wedged_cluster(), ledger=ledger)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        for _ in range(2):
            ledger.records.append(remediate.LedgerRecord(
                key=selection.key, entry=selection.entry.identifier, issue=38,
                target="", status="succeeded", started=iso(-1), finished=iso(-1),
            ))
        self.assertEqual(remediator.execute(selection), "budget")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_an_interrupted_claim_is_handed_back_rather_than_repeated(self):
        ledger = FakeLedger()
        remediator, runner, github = self.remediator(self.wedged_cluster(), ledger=ledger)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        ledger.records.append(remediate.LedgerRecord(
            key=selection.key, entry=selection.entry.identifier, issue=38,
            target="", status="running", started=iso(-4),
        ))
        self.assertEqual(remediator.execute(selection), "interrupted")
        self.assertFalse([call for call in runner.calls if "delete" in call])
        self.assertEqual(github.added, [(38, "ready-for-human")])

    def test_the_job_side_of_the_same_failure_renders_the_same_commands(self):
        mover = [alert("KubeJobNotCompleted", namespace=self.NS, job_name=f"volsync-src-{self.RS}")]
        remediator, runner, github = self.remediator(self.wedged_cluster())
        github.issues = [issue(25, "KubeJobNotCompleted: Job did not complete in time", alerts=mover)]
        selection = remediator.select(github.issues, mover, "202609112100")
        self.assertEqual(selection.entry.identifier, "volsync-ghost-snapshot/mover-job-not-completing")
        self.assertEqual(selection.bindings["rs"], self.RS)
        self.assertEqual(remediator.execute(selection), "succeeded")

    def test_the_two_entries_for_this_failure_share_one_budget(self):
        """One failure, one target, one budget — from either side of it."""
        mover = [alert("KubeJobNotCompleted", namespace=self.NS, job_name=f"volsync-src-{self.RS}")]
        ledger = FakeLedger()
        remediator, runner, github = self.remediator(self.wedged_cluster(), ledger=ledger)
        backup_stale = remediator.select(github.issues, self.firing(), "202609112100")
        github.issues = [issue(25, "KubeJobNotCompleted: Job did not complete in time", alerts=mover)]
        job_side = remediator.select(github.issues, mover, "202609112100")
        self.assertNotEqual(backup_stale.entry.identifier, job_side.entry.identifier)
        self.assertEqual(backup_stale.key, job_side.key)
        for _ in range(2):
            ledger.records.append(remediate.LedgerRecord(
                key=backup_stale.key, entry=backup_stale.entry.identifier, issue=38,
                target="", status="succeeded", started=iso(-1), finished=iso(-1),
            ))
        self.assertEqual(remediator.execute(job_side), "budget")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_a_dry_run_ends_at_the_commands_it_printed(self):
        config = remediate.Config(github_token="t", incidents_repo="example/incidents", dry_run=True)
        remediator, runner, github = self.remediator(self.wedged_cluster(), config=config)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "dry_run")
        # The preconditions are the point of a dry run, so they really ran.
        self.assertIn(
            f"kubectl -n {self.NS} get pvc volsync-{self.RS}-src -o jsonpath={{.status.phase}}",
            runner.calls,
        )
        # The steps were rendered and printed, and none of them executed.
        self.assertIn(f"kubectl -n {self.NS} delete job volsync-src-{self.RS} --wait=false "
                      f"--ignore-not-found=true", runner.rendered)
        self.assertFalse([call for call in runner.calls if " delete " in call or " wait " in call])
        # The verification never runs against a cluster nothing changed.
        self.assertNotIn(
            f"kubectl -n {self.NS} get volumesnapshot volsync-{self.RS}-src --ignore-not-found=true -o name",
            runner.calls,
        )
        self.assertEqual(github.removed, [])
        self.assertEqual(github.added, [])
        self.assertEqual(runner.dry_run, True)

    def test_a_dry_run_spends_nothing_and_says_so(self):
        config = remediate.Config(github_token="t", incidents_repo="example/incidents", dry_run=True)
        ledger = FakeLedger()
        remediator, _, github = self.remediator(self.wedged_cluster(), ledger=ledger, config=config)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        remediator.execute(selection)
        self.assertEqual(ledger.records, [])
        body = github.comments[0][1]
        self.assertIn("Catalogued Remediation, dry run", body)
        self.assertIn("None of them ran.", body)
        self.assertIn(f"`{TRIGGER}` label is still on this issue", body)
        # Nothing reaches GitHub either: the report is read in the run's log.
        self.assertEqual(github.sent, [])

    def test_the_report_names_only_the_values_a_command_could_use(self):
        firing = [alert("VolSyncBackupStale", namespace=self.NS, name=self.RS,
                        severity="warning", job="kube-state-metrics", prometheus="observability/k8s")]
        remediator, _, github = self.remediator(self.wedged_cluster())
        github.issues = [issue(38, "VolSyncBackupStale: security/kanidm is late", alerts=firing)]
        selection = remediator.select(github.issues, firing, "202609112100")
        self.assertEqual(remediator.execute(selection), "succeeded")
        headline = next(line for line in github.comments[0][1].splitlines() if line.startswith("Entry `"))
        self.assertIn("`ns`=`security`", headline)
        self.assertIn("`rs`=`kanidm`", headline)
        for label in ("severity", "prometheus", "run_stamp"):
            self.assertNotIn(label, headline)

    def test_a_job_outside_the_volsync_prefix_never_matches(self):
        beets = [alert("KubeJobNotCompleted", namespace="media", job_name="beets-import-29819010")]
        remediator, _, github = self.remediator(self.wedged_cluster())
        github.issues = [issue(25, "KubeJobNotCompleted: Job did not complete in time", alerts=beets)]
        self.assertIsNone(remediator.select(github.issues, beets, "202609112100"))


class Selection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = render_catalogue(Path(cls.tmp.name) / "catalogue.json")
        cls.entries = remediate.load_catalogue(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def remediator(self, issues):
        config = remediate.Config(github_token="t", incidents_repo="example/incidents")
        github = FakeGitHub(issues)
        return remediate.Remediator(config, FakeRunner({}), github, FakeLedger(), self.entries), github

    def test_an_alert_that_is_no_longer_firing_is_not_remediated(self):
        kanidm = [alert("VolSyncBackupStale", namespace="security", name="kanidm")]
        remediator, github = self.remediator(
            [issue(38, "VolSyncBackupStale: security/kanidm is late", alerts=kanidm)])
        self.assertIsNone(remediator.select(github.issues, [], "202609112100"))

    def test_two_matching_alerts_are_ambiguous_and_refused(self):
        alerts = [
            alert("VolSyncBackupStale", namespace="security", name="kanidm"),
            alert("VolSyncBackupStale", namespace="media", name="navidrome"),
        ]
        remediator, github = self.remediator(
            [issue(38, "VolSyncBackupStale: two sources are late", alerts=alerts)])
        self.assertIsNone(remediator.select(github.issues, alerts, "202609112100"))

    def test_a_disabled_entry_is_never_selected(self):
        alerts = [alert("etcdDatabaseHighFragmentationRatio", job="kube-etcd", namespace="kube-system")]
        remediator, github = self.remediator([
            issue(31, "etcdDatabaseHighFragmentationRatio: etcd database size in use is less than 50%",
                  alerts=alerts),
        ])
        self.assertIsNone(remediator.select(github.issues, alerts, "202609112100"))

    def test_an_uncatalogued_alertname_is_left_for_the_fix_lane(self):
        alerts = [alert("GatusEndpointDown", namespace="observability")]
        remediator, github = self.remediator(
            [issue(29, "GatusEndpointDown: The home-assistant endpoint is down", alerts=alerts)])
        self.assertIsNone(remediator.select(github.issues, alerts, "202609112100"))

    def test_the_oldest_triggered_issue_wins(self):
        stale = [alert("VolSyncBackupStale", namespace="security", name="kanidm")]
        failed = [alert("KubeJobFailed", namespace="media", job_name="beets-import-29819010", condition="true")]
        remediator, github = self.remediator([
            issue(16, "KubeJobFailed: Job failed to complete.", alerts=failed),
            issue(38, "VolSyncBackupStale: security/kanidm is late", alerts=stale),
        ])
        selection = remediator.select(github.issues, stale + failed, "202609112100")
        self.assertEqual(selection.issue["number"], 16)

    def test_the_alertname_comes_from_the_gates_title_format(self):
        self.assertEqual(
            remediate.Remediator.alertname_of({"title": "CephMgrModuleCrash: A manager module has crashed"}),
            "CephMgrModuleCrash",
        )
        self.assertEqual(
            remediate.Remediator.alertname_of({"title": "KubeJobFailed (3 alerts)"}),
            "KubeJobFailed",
        )
        self.assertIsNone(remediate.Remediator.alertname_of({"title": "Just some issue someone filed"}))


class IssueAuthorisation(unittest.TestCase):
    """The label authorises one Alert Group, not one alertname.

    Alertmanager groups by alertname here, so security/kanidm and
    media/navidrome share a single Incident Issue for VolSyncBackupStale. Only
    the alert the Gate wrote into THAT issue's body may be acted on.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = render_catalogue(Path(cls.tmp.name) / "catalogue.json")
        cls.entries = remediate.load_catalogue(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    KANIDM = staticmethod(lambda: alert("VolSyncBackupStale", namespace="security", name="kanidm"))
    NAVIDROME = staticmethod(lambda: alert("VolSyncBackupStale", namespace="media", name="navidrome"))

    def remediator(self, issues):
        config = remediate.Config(github_token="t", incidents_repo="example/incidents")
        github = FakeGitHub(issues)
        return remediate.Remediator(config, FakeRunner({}), github, FakeLedger(), self.entries), github

    def kanidm_issue(self):
        return issue(38, "VolSyncBackupStale: security/kanidm backup is 1d past its sync time",
                     alerts=[self.KANIDM()])

    def test_another_namespaces_alert_of_the_same_name_is_refused(self):
        remediator, github = self.remediator([self.kanidm_issue()])
        self.assertIsNone(remediator.select(github.issues, [self.NAVIDROME()], "202609112100"))

    def test_the_alert_the_issue_records_is_still_selected(self):
        remediator, github = self.remediator([self.kanidm_issue()])
        selection = remediator.select(github.issues, [self.KANIDM()], "202609112100")
        self.assertEqual(selection.bindings["ns"], "security")
        self.assertEqual(selection.bindings["rs"], "kanidm")

    def test_an_unauthorised_alert_cannot_be_smuggled_in_beside_the_authorised_one(self):
        remediator, github = self.remediator([self.kanidm_issue()])
        selection = remediator.select(
            github.issues, [self.NAVIDROME(), self.KANIDM()], "202609112100")
        self.assertEqual(selection.bindings["rs"], "kanidm")

    def test_an_issue_the_gate_did_not_open_authorises_nothing(self):
        hand_written = issue(38, "VolSyncBackupStale: security/kanidm is late",
                             body="Someone filed this by hand and it names kanidm.")
        remediator, github = self.remediator([hand_written])
        self.assertIsNone(remediator.select(github.issues, [self.KANIDM()], "202609112100"))

    def test_the_marker_alone_is_not_an_authorisation(self):
        """A body with the Gate's marker but no alert tables binds nothing."""
        body = "## Alert Group\n\n" + gate.group_marker("{}/{}:{alertname=\"VolSyncBackupStale\"}")
        remediator, github = self.remediator(
            [issue(38, "VolSyncBackupStale: security/kanidm is late", body=body)])
        self.assertIsNone(remediator.select(github.issues, [self.KANIDM()], "202609112100"))

    def test_the_gates_own_body_is_what_is_parsed(self):
        authorisation = remediate.parse_authorisation(self.kanidm_issue())
        self.assertEqual(authorisation.alerts, [
            {"alertname": "VolSyncBackupStale", "namespace": "security", "name": "kanidm"},
        ])
        self.assertRegex(authorisation.marker, "^[0-9a-f]{%d}$" % remediate.MARKER_HASH_CHARS)

    def test_only_the_labels_that_reach_a_command_have_to_agree(self):
        """A label that cannot change the target cannot refuse the run either."""
        recorded = alert("VolSyncBackupStale", namespace="security", name="kanidm", severity="warning")
        now_firing = alert("VolSyncBackupStale", namespace="security", name="kanidm", severity="critical")
        remediator, github = self.remediator([issue(38, "VolSyncBackupStale: late", alerts=[recorded])])
        selection = remediator.select(github.issues, [now_firing], "202609112100")
        self.assertEqual(selection.bindings["rs"], "kanidm")

    def test_the_target_labels_are_the_matchers_and_the_bindings(self):
        by_id = {entry.identifier: entry for entry in self.entries}
        self.assertEqual(
            remediate.target_labels(by_id["volsync-ghost-snapshot/backup-stale"]),
            ("alertname", "name", "namespace"),
        )
        self.assertEqual(
            remediate.target_labels(by_id["volsync-ghost-snapshot/mover-job-not-completing"]),
            ("alertname", "job_name", "namespace"),
        )
        self.assertEqual(
            remediate.target_labels(by_id["orphaned-failed-job/delete-tombstone"]),
            ("alertname", "condition", "job_name", "namespace"),
        )


class OrphanedFailedJob(unittest.TestCase):

    NS, JOB, CRONJOB = "media", "beets-import-29819010", "beets-import"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = render_catalogue(Path(cls.tmp.name) / "catalogue.json")
        cls.entries = remediate.load_catalogue(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def tombstone(self, **overrides):
        ns, job, cronjob = self.NS, self.JOB, self.CRONJOB
        responses = {
            f'kubectl -n {ns} get job {job} -o jsonpath={{.status.conditions[?(@.type=="Failed")].status}}': ("True", 0),
            f"kubectl -n {ns} get job {job} -o jsonpath={{.status.active}}": ("", 0),
            f"kubectl -n {ns} get pod -l batch.kubernetes.io/job-name={job} -o name": ("", 0),
            f'kubectl -n {ns} get job {job} -o '
            f'jsonpath={{.status.conditions[?(@.type=="Failed")].lastTransitionTime}}': (iso(-30), 0),
            f'kubectl -n {ns} get job {job} -o '
            f'jsonpath={{.metadata.ownerReferences[?(@.kind=="CronJob")].name}}': (cronjob, 0),
            f"kubectl -n {ns} get cronjob {cronjob} -o jsonpath={{.status.lastSuccessfulTime}}": (iso(-2), 0),
            f"kubectl -n {ns} delete job {job}": ('job.batch "beets-import-29819010" deleted', 0),
            f"kubectl -n {ns} get job {job} --ignore-not-found=true -o name": ("", 0),
            f"kubectl -n {ns} get cronjob {cronjob} -o jsonpath={{.spec.schedule}}": ("0 5 * * *", 0),
        }
        responses.update(overrides)
        return responses

    def remediator(self, responses):
        config = remediate.Config(github_token="t", incidents_repo="example/incidents")
        runner = FakeRunner(responses)
        github = FakeGitHub([issue(16, "KubeJobFailed: Job failed to complete.", alerts=self.firing())])
        return remediate.Remediator(config, runner, github, FakeLedger(), self.entries), runner, github

    def firing(self):
        return [alert("KubeJobFailed", namespace=self.NS, job_name=self.JOB, condition="true")]

    def test_it_deletes_only_the_tombstone(self):
        remediator, runner, github = self.remediator(self.tombstone())
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "succeeded")
        self.assertEqual(
            [call for call in runner.calls if "delete" in call],
            [f"kubectl -n {self.NS} delete job {self.JOB}"],
        )

    def test_surviving_pods_block_the_delete(self):
        responses = self.tombstone(**{
            f"kubectl -n {self.NS} get pod -l batch.kubernetes.io/job-name={self.JOB} -o name":
                ("pod/beets-import-29819010-abcde", 0),
        })
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_a_cronjob_that_has_not_recovered_blocks_the_delete(self):
        responses = self.tombstone(**{
            f"kubectl -n {self.NS} get cronjob {self.CRONJOB} -o jsonpath={{.status.lastSuccessfulTime}}": (iso(-72), 0),
        })
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_a_standalone_job_with_no_cronjob_parent_blocks_the_delete(self):
        responses = self.tombstone(**{
            f'kubectl -n {self.NS} get job {self.JOB} -o '
            f'jsonpath={{.metadata.ownerReferences[?(@.kind=="CronJob")].name}}': ("", 0),
        })
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])

    def test_a_fresh_failure_blocks_the_delete(self):
        responses = self.tombstone(**{
            f'kubectl -n {self.NS} get job {self.JOB} -o '
            f'jsonpath={{.status.conditions[?(@.type=="Failed")].lastTransitionTime}}': (iso(-1), 0),
        })
        remediator, runner, github = self.remediator(responses)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "delete" in call])


class CephCrashArchive(unittest.TestCase):

    POD = "rook-ceph-tools-8658cd6d64-pd6lh"
    CRASH = "2026-09-11T06:07:15.123456Z_0a1b2c3d-4e5f-6789-abcd-ef0123456789"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = render_catalogue(Path(cls.tmp.name) / "catalogue.json")
        cls.entries = remediate.load_catalogue(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cluster(self, crashes=None):
        crashes = [{"crash_id": self.CRASH}] if crashes is None else crashes
        return {
            "kubectl -n rook-ceph get pod -l app=rook-ceph-tools --field-selector=status.phase=Running "
            "-o jsonpath={.items[0].metadata.name}": (self.POD, 0),
            f"kubectl -n rook-ceph exec {self.POD} -- ceph crash ls-new -f json": (json.dumps(crashes), 0),
            f"kubectl -n rook-ceph exec {self.POD} -- ceph crash archive {self.CRASH}": ("", 0),
            f"kubectl -n rook-ceph exec {self.POD} -- ceph health detail": ("HEALTH_OK", 0),
        }

    def remediator(self, responses, promql):
        config = remediate.Config(github_token="t", incidents_repo="example/incidents")
        runner = FakeRunner(responses)
        github = FakeGitHub([issue(14, "CephMgrModuleCrash: A manager module has recently crashed",
                                   alerts=self.firing())])
        remediator = remediate.Remediator(config, runner, github, FakeLedger(), self.entries)
        remediator.run_check = self.patched(remediator, promql)
        return remediator, runner, github

    @staticmethod
    def patched(remediator, promql):
        """PromQL preconditions answered from a dict instead of Prometheus."""
        original = remediator.run_check

        def run_check(check, bindings):
            if check.promql is not None:
                value = promql[check.promql]
                passed, why = remediate.evaluate(check.expect, f"{value:g}", value=value, captures=remediator.captures)
                return passed, why if not passed else f"{value:g}"
            return original(check, bindings)

        return run_check

    def healthy(self):
        return {
            'max(ceph_health_detail{name="RECENT_MGR_MODULE_CRASH"})': 1.0,
            "count(ceph_osd_up == 0) or vector(0)": 0.0,
            "count(ceph_osd_in == 0) or vector(0)": 0.0,
            "max(ceph_pg_total - ceph_pg_active)": 0.0,
            "max(ceph_pg_total - ceph_pg_clean)": 0.0,
        }

    def firing(self):
        return [alert("CephMgrModuleCrash", cluster="rook-ceph", namespace="rook-ceph",
                      name="RECENT_MGR_MODULE_CRASH")]

    def test_it_archives_each_new_crash_by_id(self):
        responses = self.cluster()
        # The precondition sees the crash; the verification re-reads the list
        # after the archive and must see it empty.
        responses[f"kubectl -n rook-ceph exec {self.POD} -- ceph crash ls-new -f json"] = [
            (json.dumps([{"crash_id": self.CRASH}]), 0),
            ("[]", 0),
        ]
        remediator, runner, github = self.remediator(responses, self.healthy())
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(selection.entry.identifier, "ceph-mgr-module-crash/archive-new-crashes")
        self.assertEqual(remediator.execute(selection), "succeeded")
        self.assertIn(
            f"kubectl -n rook-ceph exec {self.POD} -- ceph crash archive {self.CRASH}",
            runner.calls,
        )

    def test_a_down_osd_blocks_archiving(self):
        promql = self.healthy()
        promql["count(ceph_osd_up == 0) or vector(0)"] = 1.0
        remediator, runner, github = self.remediator(self.cluster(), promql)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "archive" in call])

    def test_recovering_pgs_block_archiving(self):
        promql = self.healthy()
        promql["max(ceph_pg_total - ceph_pg_clean)"] = 12.0
        remediator, runner, github = self.remediator(self.cluster(), promql)
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "archive" in call])

    def test_no_new_crash_reports_blocks(self):
        remediator, runner, github = self.remediator(self.cluster(crashes=[]), self.healthy())
        selection = remediator.select(github.issues, self.firing(), "202609112100")
        self.assertEqual(remediator.execute(selection), "blocked")
        self.assertFalse([call for call in runner.calls if "archive" in call])


if __name__ == "__main__":
    unittest.main()
