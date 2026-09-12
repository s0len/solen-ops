#!/usr/bin/env python3
"""remediate.py — the Remediator: runs a Catalogued Remediation, never an improvised one.

One invocation does at most one thing. It takes the oldest open Incident Issue
carrying the human's trigger label, matches it to an entry in the Remediation
Catalogue, re-checks every precondition read-only, and only then runs that
entry's commands exactly as they are written in git.

Four conditions must all hold before a single command runs:

  1. an open Incident Issue carries the trigger label — a human put it there;
  2. the alert is STILL FIRING in Alertmanager, exactly one alert matches the
     entry's label matchers, and that alert is one the Gate recorded in THAT
     issue's body; that one alert supplies every templated value, so nothing is
     ever composed from prose and no other namespace's alert of the same name
     can inherit the authorisation;
  3. a catalogue entry matches its alertname and is enabled;
  4. every precondition passes.

There is no model in this process. The only inputs that reach a command are
label values from Alertmanager and captures from the entry's own read-only
precondition commands, each validated against a strict charset and substituted
into an argv list that is never handed to a shell. argv[0] must be the literal
string `kubectl`; nothing else can ever be executed.

The Run Ledger is a ConfigMap. It is what makes `max_runs` real, what stops a
second run re-executing an entry a previous run already claimed, and what keeps
a failing precondition from commenting every five minutes.

Stdlib only, so it runs on the slim python image as non-root with a read-only
root filesystem. The catalogue arrives as JSON, converted from the reviewed
YAML by the init container, because the slim image has no YAML parser.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DEFAULT_INCIDENTS_REPO = "s0len/solen-ops-incidents"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_ALERTMANAGER_URL = "http://alertmanager-operated.observability.svc.cluster.local:9093"
DEFAULT_PROMETHEUS_URL = "http://prometheus-operated.observability.svc.cluster.local:9090"
DEFAULT_CATALOGUE_FILE = "/work/catalogue.json"
DEFAULT_KUBECTL = "/opt/kubectl/bin/kubectl"
DEFAULT_LEDGER_NAMESPACE = "observability"
DEFAULT_LEDGER_NAME = "alert-remediator-ledger"
# Deliberately NOT `ready-for-agent`. That label is the Fix lane's, and two
# lanes reading one label race for it: no cron offset can make one of them
# always win, because a label applied between the two firings reaches whichever
# runs next. A label of its own also means the owner authorises a live cluster
# action explicitly rather than as a side effect of asking for a pull request.
DEFAULT_TRIGGER_LABEL = "ready-for-remediation"
DEFAULT_HANDBACK_LABEL = "ready-for-human"
CATALOGUE_PATH = "kubernetes/apps/observability/alert-agent/remediations"
LEDGER_KEY = "ledger.json"
LEDGER_MAX_RUNS = 200
LEDGER_MAX_AGE_DAYS = 90
STALE_CLAIM_HOURS = 1
USER_AGENT = "alert-remediator"
CATALOGUE_VERSION = 1

# argv[0] of every catalogue command. The Remediator can execute this and
# nothing else: no shell, no interpreter, no binary the catalogue names itself.
ALLOWED_ARGV0 = "kubectl"
# Checked at catalogue load, under RBAC and the ValidatingAdmissionPolicy rather
# than instead of them. `create` is here only for the etcd entry's
# `create job --from=cronjob`; `patch` is deliberately absent, so no catalogue
# entry can reach the Run Ledger the Remediator keeps its own budget in.
ALLOWED_VERBS = ("get", "describe", "logs", "wait", "delete", "exec", "create")
# What a precondition, a verification or an evidence command may be: `steps` is
# the only field that may mutate anything. `wait` only watches. `exec` is here
# because the Ceph entry reads `ceph crash ls-new` through the toolbox, and an
# exec is bounded by the ValidatingAdmissionPolicy rather than by this tuple.
CHECK_VERBS = ("get", "describe", "logs", "wait", "exec")
NAMESPACE_FLAGS = ("-n", "--namespace")
BIND_KEYS = ("from", "strip_prefix", "strip_suffix")

# The Gate stamps this marker at the end of every Incident Issue body and
# renders every alert in the Alert Group as a `| Label | Value |` table above
# it. Those tables are the only machine-readable record of what the human was
# looking at when they applied the trigger label, and they are what binds a
# remediation to one target rather than to an alertname.
MARKER_PREFIX = "<!-- alert-agent:group="
MARKER_SUFFIX = " -->"
MARKER_HASH_CHARS = 24
GROUP_MARKER_RE = re.compile(
    re.escape(MARKER_PREFIX) + r"([0-9a-f]{%d})" % MARKER_HASH_CHARS + re.escape(MARKER_SUFFIX)
)
ALERT_HEADING_RE = re.compile(r"^###\s+\d+\.\s")
LABEL_TABLE_HEADER = "| Label | Value |"
LABEL_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`(.*)`\s*\|\s*$")
ISSUE_BODY_MAX = 200000

# Every value substituted into a command, whether it came from an Alertmanager
# label or from a capture, must match this. Kubernetes object names are a
# subset; `:` and `+` are here for Ceph crash ids, which carry an RFC3339
# timestamp.
VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,190}$")
# A placeholder is lowercase with underscores, which no kubectl jsonpath
# expression can look like: `{.status.phase}` starts with a dot.
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
ALERTNAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)(?::| \()")
FAILURE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ISSUE_TITLE_MAX = 200
OUTPUT_MAX_CHARS = 4000
COMMENT_MAX_CHARS = 60000
DEFAULT_STEP_TIMEOUT = 120
MAX_STEP_TIMEOUT = 900
MAX_FOR_EACH_ITEMS = 20

EXPECTATIONS = (
    "equals",
    "matches",
    "not_empty",
    "integer_equals",
    "integer_at_least",
    "integer_at_most",
    "older_than_hours",
    "newer_than_capture",
    "value_equals",
    "value_at_least",
    "value_at_most",
    "list_length_at_least",
    "list_length_at_most",
)
TEMPLATED_EXPECTATIONS = ("equals", "matches")

DOCUMENT_KEYS = ("version", "failure", "summary", "entries")
ENTRY_KEYS = (
    "id", "alertname", "enabled", "match", "bind", "notes", "blast_radius",
    "max_runs", "preconditions", "steps", "verify", "evidence",
)
CHECK_KEYS = ("id", "describe", "run", "promql", "expect", "capture", "capture_list", "timeout_seconds")
STEP_KEYS = ("describe", "run", "for_each", "timeout_seconds")
MAX_RUNS_KEYS = ("count", "window_hours")
CAPTURE_LIST_KEYS = ("name", "json_pluck", "item_matches")


# --------------------------------------------------------------------------- logging

def log(event: str, **fields: Any) -> None:
    """Emit one JSON line to stdout."""
    record = {"ts": now_iso(), "event": event}
    record.update(fields)
    print(json.dumps(record, default=str, sort_keys=True), flush=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(moment: Optional[datetime] = None) -> str:
    return (moment or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(text: str) -> Optional[datetime]:
    """RFC3339 as Kubernetes and Alertmanager emit it, or None."""
    candidate = (text or "").strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clip(text: str, limit: int = OUTPUT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters]"


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class Config:
    github_token: str
    incidents_repo: str = DEFAULT_INCIDENTS_REPO
    api_url: str = DEFAULT_API_URL
    alertmanager_url: str = DEFAULT_ALERTMANAGER_URL
    prometheus_url: str = DEFAULT_PROMETHEUS_URL
    catalogue_file: str = DEFAULT_CATALOGUE_FILE
    kubectl: str = DEFAULT_KUBECTL
    ledger_namespace: str = DEFAULT_LEDGER_NAMESPACE
    ledger_name: str = DEFAULT_LEDGER_NAME
    trigger_label: str = DEFAULT_TRIGGER_LABEL
    handback_label: str = DEFAULT_HANDBACK_LABEL
    dry_run: bool = False

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        source = os.environ if env is None else env
        token = source.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        return cls(
            github_token=token,
            incidents_repo=source.get("GITHUB_INCIDENTS_REPO", DEFAULT_INCIDENTS_REPO),
            api_url=source.get("GITHUB_API_URL", DEFAULT_API_URL),
            alertmanager_url=source.get("ALERTMANAGER_URL", DEFAULT_ALERTMANAGER_URL),
            prometheus_url=source.get("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL),
            catalogue_file=source.get("CATALOGUE_FILE", DEFAULT_CATALOGUE_FILE),
            kubectl=source.get("KUBECTL_PATH", DEFAULT_KUBECTL),
            ledger_namespace=source.get("LEDGER_NAMESPACE", DEFAULT_LEDGER_NAMESPACE),
            ledger_name=source.get("LEDGER_NAME", DEFAULT_LEDGER_NAME),
            trigger_label=source.get("TRIGGER_LABEL", DEFAULT_TRIGGER_LABEL),
            handback_label=source.get("HANDBACK_LABEL", DEFAULT_HANDBACK_LABEL),
            dry_run=source.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes"),
        )


# --------------------------------------------------------------------------- catalogue

class CatalogueError(ValueError):
    """The catalogue on disk is not one this version can run."""


@dataclass(frozen=True)
class Check:
    identifier: str
    describe: str
    argv: Optional[list]
    promql: Optional[str]
    expect: dict
    capture: Optional[str]
    capture_list: Optional[dict]
    timeout_seconds: int


@dataclass(frozen=True)
class Step:
    describe: str
    argv: list
    for_each: Optional[str]
    timeout_seconds: int


@dataclass(frozen=True)
class Entry:
    identifier: str
    failure: str
    alertname: str
    enabled: bool
    match: dict
    bind: dict
    blast_radius: str
    max_runs: int
    window_hours: int
    preconditions: list
    steps: list
    verify: list
    evidence: list


def _reject_unknown(mapping: Any, allowed: tuple, where: str) -> None:
    """A key this version does not know is an error, never a silent default.

    `enable: false` for `enabled: false` would otherwise ship an entry the
    reviewer believed was off.
    """
    if not isinstance(mapping, dict):
        raise CatalogueError(f"{where}: expected a mapping")
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise CatalogueError(f"{where}: unknown key(s) {unknown}")


def _require(mapping: Any, key: str, kind: type, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise CatalogueError(f"{where}: missing `{key}`")
    value = mapping[key]
    if kind is int and isinstance(value, bool):
        raise CatalogueError(f"{where}: `{key}` must be {kind.__name__}")
    if not isinstance(value, kind):
        raise CatalogueError(f"{where}: `{key}` must be {kind.__name__}")
    return value


def _parse_argv(raw: Any, where: str, verbs: tuple = ALLOWED_VERBS) -> list:
    """An argv list the Remediator is willing to execute, or an exception.

    The verb is whatever comes first after argv[0] once a leading `-n <ns>` has
    been stepped over, so `kubectl -n x delete job y` and `kubectl delete job y`
    are both read correctly and `kubectl apply -f -` is refused at load time
    rather than at run time. `verbs` narrows that further for the fields that
    are read-only by contract.
    """
    if not isinstance(raw, list) or not raw:
        raise CatalogueError(f"{where}: `run` must be a non-empty list")
    for element in raw:
        if not isinstance(element, str):
            raise CatalogueError(f"{where}: every `run` element must be a string")
    argv = list(raw)
    if argv[0] != ALLOWED_ARGV0:
        raise CatalogueError(f"{where}: `run` must start with `{ALLOWED_ARGV0}`, not `{argv[0]}`")
    index = 1
    while index < len(argv) and argv[index] in NAMESPACE_FLAGS:
        index += 2
    if index >= len(argv):
        raise CatalogueError(f"{where}: `run` names no kubectl verb")
    if argv[index] not in verbs:
        raise CatalogueError(f"{where}: kubectl verb `{argv[index]}` is not one of {verbs}")
    return argv


def _parse_expect(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise CatalogueError(f"{where}: `expect` must be a non-empty mapping")
    for key in raw:
        if key not in EXPECTATIONS:
            raise CatalogueError(f"{where}: unknown expectation `{key}`")
    return dict(raw)


def _parse_timeout(raw: Any, where: str) -> int:
    if raw is None:
        return DEFAULT_STEP_TIMEOUT
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1 or raw > MAX_STEP_TIMEOUT:
        raise CatalogueError(f"{where}: `timeout_seconds` must be 1..{MAX_STEP_TIMEOUT}")
    return raw


def _parse_check(raw: Any, where: str) -> Check:
    identifier = _require(raw, "id", str, where)
    where = f"{where}[{identifier}]"
    _reject_unknown(raw, CHECK_KEYS, where)
    argv = _parse_argv(raw["run"], where, CHECK_VERBS) if "run" in raw else None
    promql = raw.get("promql")
    if (argv is None) == (promql is None):
        raise CatalogueError(f"{where}: exactly one of `run` and `promql` is required")
    if promql is not None and not isinstance(promql, str):
        raise CatalogueError(f"{where}: `promql` must be a string")
    capture = raw.get("capture")
    if capture is not None and not (isinstance(capture, str) and NAME_RE.match(capture)):
        raise CatalogueError(f"{where}: `capture` must be a lowercase identifier")
    capture_list = raw.get("capture_list")
    if capture_list is not None:
        _reject_unknown(capture_list, CAPTURE_LIST_KEYS, f"{where}.capture_list")
        name = _require(capture_list, "name", str, where)
        if not NAME_RE.match(name):
            raise CatalogueError(f"{where}: `capture_list.name` must be a lowercase identifier")
        _require(capture_list, "json_pluck", str, where)
        re.compile(_require(capture_list, "item_matches", str, where))
    return Check(
        identifier=identifier,
        describe=raw.get("describe", ""),
        argv=argv,
        promql=promql,
        expect=_parse_expect(raw.get("expect"), where),
        capture=capture,
        capture_list=capture_list,
        timeout_seconds=_parse_timeout(raw.get("timeout_seconds"), where),
    )


def _parse_step(raw: Any, where: str, verbs: tuple = ALLOWED_VERBS) -> Step:
    _reject_unknown(raw, STEP_KEYS, where)
    argv = _parse_argv(_require(raw, "run", list, where), where, verbs)
    for_each = raw.get("for_each")
    if for_each is not None and not (isinstance(for_each, str) and NAME_RE.match(for_each)):
        raise CatalogueError(f"{where}: `for_each` must be a lowercase identifier")
    return Step(
        describe=raw.get("describe", ""),
        argv=argv,
        for_each=for_each,
        timeout_seconds=_parse_timeout(raw.get("timeout_seconds"), where),
    )


def parse_entry(raw: Any, failure: str) -> Entry:
    identifier = _require(raw, "id", str, failure)
    where = f"{failure}[{identifier}]"
    _reject_unknown(raw, ENTRY_KEYS, where)
    match = raw.get("match") or {}
    if not isinstance(match, dict):
        raise CatalogueError(f"{where}: `match` must be a mapping")
    for label, pattern in match.items():
        if not isinstance(pattern, str):
            raise CatalogueError(f"{where}: matcher `{label}` must be a regular expression string")
        re.compile(pattern)
    bind = raw.get("bind") or {}
    if not isinstance(bind, dict):
        raise CatalogueError(f"{where}: `bind` must be a mapping")
    for name, spec in bind.items():
        if not NAME_RE.match(str(name)):
            raise CatalogueError(f"{where}: binding name `{name}` is not a lowercase identifier")
        if isinstance(spec, dict):
            unknown = set(spec) - set(BIND_KEYS)
            if unknown:
                raise CatalogueError(f"{where}: binding `{name}` has unknown key(s) {sorted(unknown)}")
            _require(spec, "from", str, where)
            for key in ("strip_prefix", "strip_suffix"):
                if key in spec and not isinstance(spec[key], str):
                    raise CatalogueError(f"{where}: binding `{name}`.{key} must be a string")
        elif not isinstance(spec, str):
            raise CatalogueError(f"{where}: binding `{name}` must be a string or a mapping")
    max_runs = _require(raw, "max_runs", dict, where)
    _reject_unknown(max_runs, MAX_RUNS_KEYS, f"{where}.max_runs")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CatalogueError(f"{where}: `enabled` must be a boolean")
    return Entry(
        identifier=identifier,
        failure=failure,
        alertname=_require(raw, "alertname", str, where),
        enabled=enabled,
        match=match,
        bind=bind,
        blast_radius=_require(raw, "blast_radius", str, where),
        max_runs=_require(max_runs, "count", int, where),
        window_hours=_require(max_runs, "window_hours", int, where),
        preconditions=[
            _parse_check(item, f"{where}.preconditions")
            for item in _require(raw, "preconditions", list, where)
        ],
        steps=[_parse_step(item, f"{where}.steps") for item in _require(raw, "steps", list, where)],
        verify=[_parse_check(item, f"{where}.verify") for item in (raw.get("verify") or [])],
        evidence=[
            _parse_step(item, f"{where}.evidence", CHECK_VERBS)
            for item in (raw.get("evidence") or [])
        ],
    )


def load_catalogue(path: str) -> list:
    """Every entry in the JSON the init container rendered from the catalogue.

    A malformed document is fatal rather than partially loaded: half a
    catalogue is a catalogue whose blast radius nobody reviewed.
    """
    with open(path, "r", encoding="utf-8") as handle:
        documents = json.load(handle)
    if not isinstance(documents, list):
        raise CatalogueError(f"{path}: expected a list of catalogue documents")
    entries = []
    seen = set()
    for document in documents:
        _reject_unknown(document, DOCUMENT_KEYS, path)
        failure = _require(document, "failure", str, path)
        if not FAILURE_RE.match(failure):
            raise CatalogueError(f"{path}: `failure` must be the file's own stem, saw {failure!r}")
        version = _require(document, "version", int, failure)
        if version != CATALOGUE_VERSION:
            raise CatalogueError(f"{failure}: catalogue version {version} is not {CATALOGUE_VERSION}")
        for raw in _require(document, "entries", list, failure):
            entry = parse_entry(raw, failure)
            if entry.identifier in seen:
                raise CatalogueError(f"duplicate catalogue entry id `{entry.identifier}`")
            seen.add(entry.identifier)
            entries.append(entry)
    return entries


# --------------------------------------------------------------------------- templating

class BindingError(ValueError):
    """A value could not be substituted, or was not safe to substitute."""


def substitute(text: str, bindings: dict) -> str:
    """Replace `{name}` with a bound value, refusing anything unbound or unsafe.

    Never `str.format`: that reaches attributes and indexes of whatever it was
    handed. Every replacement is re-checked against VALUE_RE here as well as
    where it was bound, because this is the last point before argv.
    """
    def replace(hit: "re.Match") -> str:
        name = hit.group(1)
        if name not in bindings:
            raise BindingError(f"unbound placeholder `{{{name}}}`")
        value = str(bindings[name])
        if not VALUE_RE.match(value):
            raise BindingError(f"value for `{{{name}}}` is not a safe token: {value!r}")
        return value

    return PLACEHOLDER_RE.sub(replace, text)


def render_argv(argv: list, bindings: dict) -> list:
    return [substitute(element, bindings) for element in argv]


def render_expect(expect: dict, bindings: dict) -> dict:
    """Substitute into the two expectations that compare against a name.

    A `matches` regular expression is left readable because no quantifier can
    look like a placeholder: `{0,61}` does not start with a letter.
    """
    return {
        key: substitute(value, bindings) if key in TEMPLATED_EXPECTATIONS and isinstance(value, str) else value
        for key, value in expect.items()
    }


def build_bindings(entry: Entry, labels: dict, run_stamp: str) -> dict:
    """Alert labels plus the entry's derived names. Nothing else gets in.

    Label values are filtered rather than rejected, so an alert carrying an
    `instance` of `192.168.10.40:2381` alongside a clean `namespace` is still
    usable: the unusable label is simply not available to bind against.
    """
    bindings = {"run_stamp": run_stamp}
    for name, value in labels.items():
        if NAME_RE.match(str(name)) and VALUE_RE.match(str(value)):
            bindings[str(name)] = str(value)
    for name, spec in entry.bind.items():
        if isinstance(spec, dict):
            value = substitute(str(spec["from"]), bindings)
            prefix = spec.get("strip_prefix")
            if prefix is not None:
                if not value.startswith(prefix):
                    raise BindingError(f"binding `{name}`: {value!r} does not start with {prefix!r}")
                value = value[len(prefix):]
            suffix = spec.get("strip_suffix")
            if suffix is not None:
                if not value.endswith(suffix):
                    raise BindingError(f"binding `{name}`: {value!r} does not end with {suffix!r}")
                value = value[: len(value) - len(suffix)]
        else:
            value = substitute(str(spec), bindings)
        if not VALUE_RE.match(value):
            raise BindingError(f"binding `{name}` resolved to an unsafe token: {value!r}")
        bindings[name] = value
    return bindings


# --------------------------------------------------------------------------- command runner

@dataclass
class CommandResult:
    printed: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout if self.stdout.strip() else self.stderr).strip()


class Runner:
    """Runs argv lists. No shell, ever, and no argv[0] but kubectl.

    Under `dry_run` only the calls declared `mutating` are faked; reads run for
    real, which is the point of a dry run. What keeps that honest is that the
    Remediator stops after the steps in a dry run rather than verifying a
    cluster nothing changed.
    """

    def __init__(self, kubectl: str, dry_run: bool = False):
        self.kubectl = kubectl
        self.dry_run = dry_run

    def run(self, argv: list, timeout_seconds: int = DEFAULT_STEP_TIMEOUT, mutating: bool = False) -> CommandResult:
        if not argv or argv[0] != ALLOWED_ARGV0:
            raise BindingError(f"refusing to run argv that does not start with `{ALLOWED_ARGV0}`: {argv}")
        printed = " ".join(argv)
        if mutating and self.dry_run:
            return CommandResult(printed=printed, returncode=0, stdout="[dry run: not executed]", stderr="")
        try:
            completed = subprocess.run(
                [self.kubectl] + argv[1:],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(printed=printed, returncode=124, stdout="", stderr=f"timed out after {timeout_seconds}s")
        except OSError as exc:
            return CommandResult(printed=printed, returncode=127, stdout="", stderr=str(exc))
        return CommandResult(
            printed=printed,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


# --------------------------------------------------------------------------- http

class HttpError(Exception):
    """An HTTP dependency could not be used."""


def http_json(url: str, *, method: str = "GET", body: Optional[dict] = None,
              headers: Optional[dict] = None, timeout: float = 20.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    merged = {"Accept": "application/json", "User-Agent": USER_AGENT}
    merged.update(headers or {})
    if data is not None:
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise HttpError(f"{method} {url} returned {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise HttpError(f"{method} {url} unreachable: {exc}") from exc
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise HttpError(f"{method} {url} returned non-JSON") from exc


class GitHubClient:
    """The four Issues API calls the Remediator needs, over urllib."""

    def __init__(self, api_url: str, token: str, repo: str, dry_run: bool = False):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.repo = repo
        self.dry_run = dry_run

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
        }

    def open_issues_with_label(self, label: str) -> list:
        """Open issues carrying `label`, oldest first, pull requests dropped."""
        url = f"{self.api_url}/repos/{self.repo}/issues?" + urllib.parse.urlencode(
            {"state": "open", "labels": label, "sort": "created", "direction": "asc", "per_page": 100}
        )
        items = http_json(url, headers=self._headers())
        if not isinstance(items, list):
            raise HttpError("GitHub issue listing was not a list")
        return [item for item in items if isinstance(item, dict) and "pull_request" not in item]

    def comment(self, number: int, body: str) -> None:
        if self.dry_run:
            log("dry_run_comment", issue=number, body=body)
            return
        http_json(
            f"{self.api_url}/repos/{self.repo}/issues/{number}/comments",
            method="POST",
            body={"body": clip(body, COMMENT_MAX_CHARS)},
            headers=self._headers(),
        )

    def remove_label(self, number: int, label: str) -> None:
        if self.dry_run:
            log("dry_run_remove_label", issue=number, label=label)
            return
        try:
            http_json(
                f"{self.api_url}/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(label)}",
                method="DELETE",
                headers=self._headers(),
            )
        except HttpError as exc:
            if "returned 404" not in str(exc):
                raise

    def add_label(self, number: int, label: str) -> None:
        if self.dry_run:
            log("dry_run_add_label", issue=number, label=label)
            return
        http_json(
            f"{self.api_url}/repos/{self.repo}/issues/{number}/labels",
            method="POST",
            body={"labels": [label]},
            headers=self._headers(),
        )


def firing_alerts(alertmanager_url: str) -> list:
    """Alerts active right now, neither silenced nor inhibited.

    A silenced alert is the owner saying "leave this alone", so it is not a
    remediation target even when its Incident Issue carries the trigger label.
    """
    url = alertmanager_url.rstrip("/") + "/api/v2/alerts?" + urllib.parse.urlencode(
        {"active": "true", "silenced": "false", "inhibited": "false"}
    )
    alerts = http_json(url)
    if not isinstance(alerts, list):
        raise HttpError("Alertmanager did not return a list of alerts")
    return [alert for alert in alerts if isinstance(alert, dict)]


def promql_scalar(prometheus_url: str, expression: str) -> tuple:
    """(value, detail) for an instant query that must return exactly one series."""
    url = prometheus_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    payload = http_json(url)
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise HttpError(f"Prometheus rejected the query: {expression}")
    result = ((payload.get("data") or {}).get("result")) or []
    if len(result) != 1:
        return None, f"{len(result)} series returned, exactly 1 required"
    try:
        return float(result[0]["value"][1]), "ok"
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "the series carried no readable value"


# --------------------------------------------------------------------------- expectations

def as_int(text: str) -> Optional[int]:
    """kubectl jsonpath prints nothing for an absent numeric field; that is 0."""
    candidate = (text or "").strip()
    if not candidate:
        return 0
    try:
        return int(candidate)
    except ValueError:
        return None


def evaluate(expect: dict, observed: str, *, value: Optional[float] = None,
             items: Optional[list] = None, captures: Optional[dict] = None) -> tuple:
    """(passed, why). Every expectation in the mapping must hold."""
    captures = captures or {}
    text = (observed or "").strip()
    for key, wanted in expect.items():
        if key == "equals":
            if text != str(wanted):
                return False, f"expected {str(wanted)!r}, saw {text!r}"
        elif key == "matches":
            if not re.search(str(wanted), text):
                return False, f"expected a match for /{wanted}/, saw {text!r}"
        elif key == "not_empty":
            if bool(wanted) != bool(text):
                return False, f"expected {'non-empty' if wanted else 'empty'}, saw {text!r}"
        elif key in ("integer_equals", "integer_at_least", "integer_at_most"):
            number = as_int(text)
            if number is None:
                return False, f"expected an integer, saw {text!r}"
            if key == "integer_equals" and number != int(wanted):
                return False, f"expected exactly {wanted}, saw {number}"
            if key == "integer_at_least" and number < int(wanted):
                return False, f"expected at least {wanted}, saw {number}"
            if key == "integer_at_most" and number > int(wanted):
                return False, f"expected at most {wanted}, saw {number}"
        elif key == "older_than_hours":
            moment = parse_rfc3339(text)
            if moment is None:
                return False, f"expected an RFC3339 timestamp, saw {text!r}"
            age = now() - moment
            if age < timedelta(hours=float(wanted)):
                return False, f"expected older than {wanted}h, saw {age}"
        elif key == "newer_than_capture":
            mine = parse_rfc3339(text)
            theirs = parse_rfc3339(str(captures.get(str(wanted), "")))
            if mine is None or theirs is None:
                return False, f"expected two RFC3339 timestamps, saw {text!r} and capture `{wanted}`"
            if mine <= theirs:
                return False, f"expected later than capture `{wanted}` ({theirs}), saw {mine}"
        elif key in ("value_equals", "value_at_least", "value_at_most"):
            if value is None:
                return False, f"no scalar value to compare: {text}"
            if key == "value_equals" and value != float(wanted):
                return False, f"expected exactly {wanted}, saw {value:g}"
            if key == "value_at_least" and value < float(wanted):
                return False, f"expected at least {wanted}, saw {value:g}"
            if key == "value_at_most" and value > float(wanted):
                return False, f"expected at most {wanted}, saw {value:g}"
        elif key in ("list_length_at_least", "list_length_at_most"):
            if items is None:
                return False, "no captured list to measure"
            if key == "list_length_at_least" and len(items) < int(wanted):
                return False, f"expected at least {wanted} item(s), saw {len(items)}"
            if key == "list_length_at_most" and len(items) > int(wanted):
                return False, f"expected at most {wanted} item(s), saw {len(items)}"
    return True, "ok"


def pluck_list(stdout: str, field_name: str, pattern: str) -> tuple:
    """(items, why). A JSON list of objects in, one validated field out."""
    try:
        parsed = json.loads(stdout or "[]")
    except ValueError:
        return None, "output was not JSON"
    if not isinstance(parsed, list):
        return None, "output was not a JSON list"
    compiled = re.compile(pattern)
    items = []
    for element in parsed:
        if not isinstance(element, dict) or field_name not in element:
            return None, f"an element carried no `{field_name}`"
        candidate = str(element[field_name])
        if not compiled.match(candidate) or not VALUE_RE.match(candidate):
            return None, f"`{field_name}` value {candidate!r} is not an acceptable token"
        items.append(candidate)
    if len(items) > MAX_FOR_EACH_ITEMS:
        return None, f"{len(items)} items exceeds the cap of {MAX_FOR_EACH_ITEMS}"
    return items, "ok"


# --------------------------------------------------------------------------- run ledger

@dataclass
class LedgerRecord:
    key: str
    entry: str
    issue: int
    target: str
    status: str
    started: str
    finished: str = ""

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "entry": self.entry,
            "issue": self.issue,
            "target": self.target,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
        }


class Ledger:
    """Entry-and-target -> what has run, in a ConfigMap the owner can just read.

    It is the only state the Remediator keeps, and it is what makes `max_runs`
    real: without it a CronJob firing every five minutes would re-run an entry
    for as long as its alert kept firing. The claim record is written before the
    first command, so a run interrupted mid-sequence is visible afterwards as
    `running` rather than invisible.
    """

    def __init__(self, runner: Runner, namespace: str, name: str):
        self.runner = runner
        self.namespace = namespace
        self.name = name
        self.records: list = []

    def load(self) -> None:
        result = self.runner.run(["kubectl", "-n", self.namespace, "get", "configmap", self.name, "-o", "json"])
        if not result.ok:
            raise HttpError(f"cannot read the Run Ledger: {result.output}")
        try:
            document = json.loads(result.stdout)
            parsed = json.loads((document.get("data") or {}).get(LEDGER_KEY) or "{}")
        except ValueError:
            log("ledger_unreadable", action="starting from an empty ledger")
            parsed = {}
        self.records = []
        for item in (parsed.get("runs") or []):
            try:
                self.records.append(LedgerRecord(**item))
            except TypeError:
                continue

    def save(self) -> None:
        cutoff = now() - timedelta(days=LEDGER_MAX_AGE_DAYS)
        kept = [r for r in self.records if (parse_rfc3339(r.started) or now()) >= cutoff][-LEDGER_MAX_RUNS:]
        patch = json.dumps({"data": {LEDGER_KEY: json.dumps({"version": 1, "runs": [r.to_json() for r in kept]}, sort_keys=True)}})
        result = self.runner.run(
            ["kubectl", "-n", self.namespace, "patch", "configmap", self.name, "--type", "merge", "-p", patch],
            mutating=True,
        )
        if not result.ok:
            raise HttpError(f"cannot write the Run Ledger: {result.output}")
        self.records = kept

    def runs_in_window(self, key: str, window_hours: int) -> int:
        cutoff = now() - timedelta(hours=window_hours)
        return sum(
            1 for r in self.records
            if r.key == key
            and r.status in ("running", "succeeded", "failed", "interrupted")
            and (parse_rfc3339(r.started) or now()) >= cutoff
        )

    def reported_recently(self, key: str, window_hours: int) -> bool:
        cutoff = now() - timedelta(hours=window_hours)
        return any(
            r.key == key and r.status == "blocked" and (parse_rfc3339(r.started) or now()) >= cutoff
            for r in self.records
        )

    def stale_claim(self, key: str) -> Optional[LedgerRecord]:
        cutoff = now() - timedelta(hours=STALE_CLAIM_HOURS)
        for record in self.records:
            if record.key == key and record.status == "running" and (parse_rfc3339(record.started) or now()) < cutoff:
                return record
        return None

    def append(self, record: LedgerRecord) -> LedgerRecord:
        self.records.append(record)
        return record


def target_of(entry: Entry, bindings: dict) -> str:
    """The bound names that identify what an entry would act on, and only those.

    `bindings` also carries every alert label that passed VALUE_RE, which is
    most of the alert; the entry's own `bind` names are the subset that can
    reach a command.
    """
    return ",".join(f"{name}={bindings[name]}" for name in sorted(entry.bind) if name in bindings)


def ledger_key(entry: Entry, bindings: dict) -> str:
    """One key per FAILURE per target.

    Keyed on the catalogue file rather than the entry id because one failure
    can be catalogued from more than one side — the same wedged VolSync sync
    raises both VolSyncBackupStale and KubeJobNotCompleted, and both entries
    run the same sequence against the same objects. Keying per entry would give
    each of them its own `max_runs` and quietly double the budget the owner
    reviewed.
    """
    return f"{entry.failure}|{target_of(entry, bindings)}"


# --------------------------------------------------------------------------- rendering

def render_table(rows: list, headers: tuple) -> list:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "∣") for cell in row) + " |")
    return lines


def render_check_table(rows: list) -> list:
    return render_table(
        [(f"`{name}`", verdict, clip(detail, 200)) for name, verdict, detail in rows],
        ("Check", "Result", "Observed"),
    )


def render_transcript(results: list) -> list:
    lines = ["```console"]
    for result in results:
        lines.append(f"$ {result.printed}")
        body = clip(result.output)
        if body:
            lines.append(body)
        if not result.ok:
            lines.append(f"[exit {result.returncode}]")
    lines.append("```")
    return lines


def render_report(entry: Entry, bindings: dict, alert_labels: dict, marker: str, checks: list, steps: list,
                  verifications: list, evidence: list, outcome: str, trigger_label: str) -> str:
    headline = {
        "succeeded": "Catalogued Remediation executed",
        "failed": "Catalogued Remediation failed part-way",
        "blocked": "Catalogued Remediation did not run",
        "dry_run": "Catalogued Remediation, dry run",
    }[outcome]
    target = ", ".join(
        f"`{name}`=`{bindings[name]}`" for name in sorted(entry.bind) if name in bindings
    )
    lines = [
        f"## {headline}",
        "",
        f"Entry `{entry.identifier}`, from `{CATALOGUE_PATH}/{entry.failure}.yaml`, matched against an alert "
        f"firing in Alertmanager right now that is one of the alerts this Incident Issue records "
        f"(Alert Group `{marker}`), on {target or 'no bound values'}.",
        "",
        f"**Blast radius, as reviewed:** {entry.blast_radius}",
        "",
        "### Preconditions",
        "",
    ]
    lines.extend(render_check_table(checks))
    if outcome == "blocked":
        lines += [
            "",
            f"Nothing ran. The `{trigger_label}` label is still on this issue, so the next run re-checks; "
            "this comment is written once per window, not every five minutes.",
        ]
        return "\n".join(lines) + "\n"
    lines += ["", "### Commands", ""]
    if outcome == "dry_run":
        lines += ["These were rendered and printed. None of them ran.", ""]
    lines.extend(render_transcript(steps))
    if verifications:
        lines += ["", "### Verification", ""]
        lines.extend(render_check_table(verifications))
    if evidence:
        lines += ["", "### Evidence", ""]
        lines.extend(render_transcript(evidence))
    lines += ["", "### Alert labels this was matched on", ""]
    lines.extend(render_table([(f"`{k}`", f"`{v}`") for k, v in sorted(alert_labels.items())], ("Label", "Value")))
    if outcome == "dry_run":
        lines += [
            "",
            f"`DRY_RUN` is set, so nothing was executed, the Run Ledger was not written and the "
            f"`{trigger_label}` label is still on this issue. The preconditions above are the real ones, "
            "run against the cluster as it is right now; the verifications are not run at all, because "
            "verifying a cluster no command touched would only report the failure it started with. Set "
            "`DRY_RUN` back to `false` to let this entry act.",
        ]
    elif outcome == "succeeded":
        lines += [
            "",
            f"The `{trigger_label}` label has been removed. No file in `solen-ops` changed and nothing was "
            "merged. This Incident Issue stays open until a human triages it; if the alert clears, the Gate "
            "says so in a `Resolved at` comment.",
        ]
    else:
        lines += [
            "",
            "**A command failed after the remediation had started, so the cluster may be part-way through "
            "this sequence.** Read the transcript above before triggering anything else.",
        ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- the remediator

@dataclass(frozen=True)
class Authorisation:
    """What one Incident Issue actually authorises.

    `marker` is the Gate's own hidden tie between this issue and one Alert
    Group. `alerts` is every alert label set the Gate rendered into the body,
    which is what the human was reading when they applied the trigger label.
    An issue with no marker was not opened by the Gate and authorises nothing.

    The marker is not recomputed from the group key printed above it: the Gate
    renders that key through `md_code`, which rewrites `|` and collapses
    whitespace, so the prose is not the byte string that was hashed.
    """

    marker: str
    alerts: list

    def covers(self, labels: dict, names: tuple) -> bool:
        """True when the live alert is one of the alerts this issue records.

        Compared on `names` only — the labels the entry matches on and binds
        from, which are the only ones that can reach a command. A label that
        cannot change what runs cannot make a target a different target.
        """
        return any(
            all(str(recorded.get(name, "")) == str(labels.get(name, "")) for name in names)
            for recorded in self.alerts
        )


def parse_authorisation(issue: dict) -> Optional[Authorisation]:
    """The Alert Group marker and the alert label tables out of an issue body."""
    body = (issue.get("body") or "")[:ISSUE_BODY_MAX]
    hit = GROUP_MARKER_RE.search(body)
    if hit is None:
        return None
    recorded: list = []
    current: Optional[dict] = None
    reading = False
    for line in body.splitlines():
        if ALERT_HEADING_RE.match(line):
            current = {}
            recorded.append(current)
            reading = False
            continue
        if current is None:
            continue
        if line.strip() == LABEL_TABLE_HEADER:
            reading = True
            continue
        if not reading:
            continue
        row = LABEL_ROW_RE.match(line)
        if row is not None:
            current[row.group(1)] = row.group(2)
        elif not line.startswith("|"):
            reading = False
    return Authorisation(marker=hit.group(1), alerts=[labels for labels in recorded if labels])


def target_labels(entry: Entry) -> tuple:
    """Every alert label an entry's target is derived from.

    Its matchers, plus every label its bindings interpolate, plus the
    alertname. These are exactly the labels that decide which object the
    commands name.
    """
    names = set(entry.match)
    for spec in entry.bind.values():
        source = spec["from"] if isinstance(spec, dict) else spec
        names.update(PLACEHOLDER_RE.findall(str(source)))
    names.discard("run_stamp")
    names.add("alertname")
    return tuple(sorted(names))


@dataclass
class Selection:
    issue: dict
    entry: Entry
    alert: dict
    bindings: dict
    key: str
    marker: str


class Remediator:

    def __init__(self, config: Config, runner: Runner, github: GitHubClient, ledger: Ledger, entries: list):
        self.config = config
        self.runner = runner
        self.github = github
        self.ledger = ledger
        self.entries = entries
        self.captures: dict = {}

    # -- selection

    @staticmethod
    def alertname_of(issue: dict) -> Optional[str]:
        """The Gate titles an Incident Issue `<alertname>: <summary>`."""
        hit = ALERTNAME_RE.match((issue.get("title") or "")[:ISSUE_TITLE_MAX])
        return hit.group(1) if hit else None

    @staticmethod
    def matching_alerts(entry: Entry, alerts: list) -> list:
        found = []
        for alert in alerts:
            labels = alert.get("labels") or {}
            if labels.get("alertname") != entry.alertname:
                continue
            if all(re.search(pattern, str(labels.get(label, ""))) for label, pattern in entry.match.items()):
                found.append(alert)
        return found

    def select(self, issues: list, alerts: list, run_stamp: str) -> Optional[Selection]:
        """The oldest triggered issue with exactly one live alert it authorises.

        The title supplies the alertname and nothing else. What the human's
        label authorises is one Alert Group, and the Gate wrote that group's
        alerts into the issue body, so a live alert of the right name against a
        target this issue never mentioned is refused rather than remediated.
        """
        for issue in issues:
            alertname = self.alertname_of(issue)
            if not alertname:
                log("issue_skipped", issue=issue.get("number"), reason="no alertname in the title")
                continue
            authorisation = parse_authorisation(issue)
            if authorisation is None or not authorisation.alerts:
                log("issue_skipped", issue=issue.get("number"),
                    reason="the body carries no Gate Alert Group marker and alert labels, so it "
                           "authorises no target")
                continue
            for entry in self.entries:
                if entry.alertname != alertname:
                    continue
                if not entry.enabled:
                    log("entry_disabled", issue=issue.get("number"), entry=entry.identifier)
                    continue
                firing = self.matching_alerts(entry, alerts)
                names = target_labels(entry)
                candidates = [a for a in firing if authorisation.covers(a.get("labels") or {}, names)]
                if len(firing) != len(candidates):
                    log("alert_not_authorised", issue=issue.get("number"), entry=entry.identifier,
                        group=authorisation.marker, on=list(names),
                        authorised=[{n: str(r.get(n, "")) for n in names} for r in authorisation.alerts],
                        firing=[{n: str((a.get("labels") or {}).get(n, "")) for n in names}
                                for a in firing if a not in candidates],
                        reason="a live alert of this name is on a target this Incident Issue does not record")
                if len(candidates) != 1:
                    log("entry_skipped", issue=issue.get("number"), entry=entry.identifier,
                        reason=f"{len(candidates)} live alerts this issue authorises match, exactly 1 required")
                    continue
                try:
                    bindings = build_bindings(entry, candidates[0].get("labels") or {}, run_stamp)
                except BindingError as exc:
                    log("entry_skipped", issue=issue.get("number"), entry=entry.identifier, reason=str(exc))
                    continue
                return Selection(
                    issue=issue,
                    entry=entry,
                    alert=candidates[0],
                    bindings=bindings,
                    key=ledger_key(entry, bindings),
                    marker=authorisation.marker,
                )
        return None

    # -- checks

    def scope(self, bindings: dict) -> dict:
        return {**bindings, **{k: v for k, v in self.captures.items() if isinstance(v, str)}}

    def run_check(self, check: Check, bindings: dict) -> tuple:
        """(passed, observed) for one precondition or verification. Read-only by construction."""
        if check.promql is not None:
            try:
                value, detail = promql_scalar(self.config.prometheus_url, check.promql)
            except HttpError as exc:
                return False, str(exc)
            if value is None:
                return False, detail
            passed, why = evaluate(check.expect, f"{value:g}", value=value, captures=self.captures)
            return passed, why if not passed else f"{value:g}"
        try:
            scope = self.scope(bindings)
            argv = render_argv(check.argv, scope)
            expect = render_expect(check.expect, scope)
        except BindingError as exc:
            return False, str(exc)
        result = self.runner.run(argv, timeout_seconds=check.timeout_seconds)
        if not result.ok:
            return False, f"exit {result.returncode}: {clip(result.output, 200)}"
        items = None
        if check.capture_list:
            items, why = pluck_list(result.stdout, check.capture_list["json_pluck"], check.capture_list["item_matches"])
            if items is None:
                return False, why
            self.captures[check.capture_list["name"]] = items
        passed, why = evaluate(expect, result.stdout, items=items, captures=self.captures)
        if not passed:
            return False, why
        if check.capture:
            captured = result.stdout.strip()
            if not VALUE_RE.match(captured):
                return False, f"capture `{check.capture}` is not a safe token: {captured!r}"
            self.captures[check.capture] = captured
        return True, f"{len(items)} item(s)" if items is not None else clip(result.stdout.strip() or "(empty)", 200)

    def run_checks(self, checks: list, bindings: dict) -> tuple:
        """Stops at the first failure: a later check may assume an earlier capture."""
        rows = []
        for check in checks:
            passed, observed = self.run_check(check, bindings)
            rows.append((check.identifier, "pass" if passed else "**FAIL**", observed))
            if not passed:
                return False, rows
        return True, rows

    # -- execution

    def run_steps(self, steps: list, bindings: dict) -> tuple:
        results = []
        for step in steps:
            targets = [None]
            if step.for_each:
                captured = self.captures.get(step.for_each)
                if not isinstance(captured, list):
                    results.append(CommandResult(printed=" ".join(step.argv), returncode=125, stdout="",
                                                 stderr=f"`for_each` capture `{step.for_each}` is not a list"))
                    return False, results
                targets = captured
            for item in targets:
                scope = self.scope(bindings)
                if item is not None:
                    scope["item"] = item
                try:
                    argv = render_argv(step.argv, scope)
                except BindingError as exc:
                    results.append(CommandResult(printed=" ".join(step.argv), returncode=125, stdout="", stderr=str(exc)))
                    return False, results
                result = self.runner.run(argv, timeout_seconds=step.timeout_seconds, mutating=True)
                results.append(result)
                if not result.ok:
                    return False, results
        return True, results

    def collect_evidence(self, steps: list, bindings: dict) -> list:
        results = []
        for step in steps:
            try:
                argv = render_argv(step.argv, self.scope(bindings))
            except BindingError as exc:
                results.append(CommandResult(printed=" ".join(step.argv), returncode=125, stdout="", stderr=str(exc)))
                continue
            results.append(self.runner.run(argv, timeout_seconds=step.timeout_seconds))
        return results

    # -- the run

    def _block(self, selection: Selection, body: str) -> None:
        """Record one blocked attempt and say so once per window, not every run."""
        if self.ledger.reported_recently(selection.key, selection.entry.window_hours):
            return
        self.ledger.append(LedgerRecord(
            key=selection.key, entry=selection.entry.identifier, issue=int(selection.issue["number"]),
            target=selection.key.split("|", 1)[1], status="blocked", started=now_iso(), finished=now_iso(),
        ))
        self.ledger.save()
        self.github.comment(int(selection.issue["number"]), body)

    def execute(self, selection: Selection) -> str:
        entry, bindings = selection.entry, selection.bindings
        number = int(selection.issue["number"])
        self.captures = {}

        stale = self.ledger.stale_claim(selection.key)
        if stale is not None:
            stale.status = "interrupted"
            stale.finished = now_iso()
            self.ledger.save()
            self.github.remove_label(number, self.config.trigger_label)
            self.github.add_label(number, self.config.handback_label)
            self.github.comment(number, (
                "## Catalogued Remediation needs a human\n\n"
                f"A previous run claimed entry `{entry.identifier}` at {stale.started} and never reported back, "
                "so the cluster may be part-way through that sequence. Nothing ran this time. The "
                f"`{self.config.trigger_label}` label has been replaced with `{self.config.handback_label}`.\n"
            ))
            log("interrupted_claim", issue=number, entry=entry.identifier, started=stale.started)
            return "interrupted"

        used = self.ledger.runs_in_window(selection.key, entry.window_hours)
        if used >= entry.max_runs:
            log("budget_exhausted", issue=number, entry=entry.identifier, used=used, allowed=entry.max_runs)
            self._block(selection, (
                "## Catalogued Remediation did not run\n\n"
                f"Entry `{entry.identifier}` has already run {used} time(s) on this target in the last "
                f"{entry.window_hours}h, which is the maximum recorded in "
                f"`{CATALOGUE_PATH}/{entry.failure}.yaml`. A failure this entry cannot hold down is not one it "
                "should keep papering over; this needs a human.\n"
            ))
            return "budget"

        passed, check_rows = self.run_checks(entry.preconditions, bindings)
        if not passed:
            log("preconditions_failed", issue=number, entry=entry.identifier, checks=check_rows)
            self._block(selection, render_report(
                entry, bindings, selection.alert.get("labels") or {}, selection.marker,
                check_rows, [], [], [], "blocked", self.config.trigger_label,
            ))
            return "blocked"

        # A dry run stops here, after the preconditions it came to prove and
        # before anything is claimed. Verifying afterwards would only re-read a
        # cluster no command touched and report the failure it started with, so
        # it is not run at all and the report says so.
        if self.config.dry_run:
            rendered, step_results = self.run_steps(entry.steps, bindings)
            self.github.comment(number, render_report(
                entry, bindings, selection.alert.get("labels") or {}, selection.marker,
                check_rows, step_results, [], [], "dry_run", self.config.trigger_label,
            ))
            log("dry_run_complete", issue=number, entry=entry.identifier,
                target=target_of(entry, bindings), rendered=rendered,
                commands=[r.printed for r in step_results])
            return "dry_run"

        record = self.ledger.append(LedgerRecord(
            key=selection.key, entry=entry.identifier, issue=number,
            target=selection.key.split("|", 1)[1], status="running", started=now_iso(),
        ))
        self.ledger.save()
        self.github.remove_label(number, self.config.trigger_label)
        log("executing", issue=number, entry=entry.identifier, target=record.target)

        ok, step_results = self.run_steps(entry.steps, bindings)
        verify_rows = []
        if ok and entry.verify:
            ok, verify_rows = self.run_checks(entry.verify, bindings)
        # Collected whether or not it worked: a log is worth most on the run
        # that failed, and a command that cannot run now is itself evidence.
        evidence = self.collect_evidence(entry.evidence, bindings)

        record.status = "succeeded" if ok else "failed"
        record.finished = now_iso()
        self.ledger.save()

        if not ok:
            self.github.add_label(number, self.config.handback_label)
        self.github.comment(number, render_report(
            entry, bindings, selection.alert.get("labels") or {}, selection.marker,
            check_rows, step_results, verify_rows, evidence,
            "succeeded" if ok else "failed", self.config.trigger_label,
        ))
        log("executed", issue=number, entry=entry.identifier, outcome=record.status)
        return record.status


# --------------------------------------------------------------------------- main

def main() -> int:
    try:
        config = Config.from_env()
    except ValueError as exc:
        log("config_error", error=str(exc))
        return 2

    runner = Runner(config.kubectl, dry_run=config.dry_run)
    try:
        entries = load_catalogue(config.catalogue_file)
    except (OSError, ValueError) as exc:
        log("catalogue_error", error=str(exc))
        return 2
    log("catalogue_loaded",
        entries=[e.identifier for e in entries],
        enabled=[e.identifier for e in entries if e.enabled])

    github = GitHubClient(config.api_url, config.github_token, config.incidents_repo, dry_run=config.dry_run)
    ledger = Ledger(runner, config.ledger_namespace, config.ledger_name)
    try:
        ledger.load()
        issues = github.open_issues_with_label(config.trigger_label)
    except HttpError as exc:
        log("dependency_error", error=str(exc))
        return 1

    if not issues:
        log("nothing_to_do", reason=f"no open Incident Issue carries `{config.trigger_label}`")
        return 0

    try:
        alerts = firing_alerts(config.alertmanager_url)
    except HttpError as exc:
        log("dependency_error", error=str(exc))
        return 1

    remediator = Remediator(config, runner, github, ledger, entries)
    selection = remediator.select(issues, alerts, now().strftime("%Y%m%d%H%M"))
    if selection is None:
        log("nothing_to_do",
            reason="no enabled catalogue entry matches a live alert that a triggered Incident Issue "
                   "authorises",
            issues=[i.get("number") for i in issues])
        return 0

    try:
        outcome = remediator.execute(selection)
    except (HttpError, BindingError) as exc:
        log("execution_error", issue=selection.issue.get("number"), entry=selection.entry.identifier, error=str(exc))
        return 1
    # A part-way remediation exits non-zero on purpose: the failed Job is what
    # makes it visible to Prometheus as well as to the Incident Issue.
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
