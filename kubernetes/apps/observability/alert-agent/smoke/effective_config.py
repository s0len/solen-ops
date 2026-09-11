"""Run INSIDE the Hermes container: assert the EFFECTIVE config, not the file text.

Every value is read back through Hermes' own loaders and resolvers, and every
command verdict comes from check_all_command_guards — the function the terminal
tool actually calls — with the session env a webhook run has. A key this Hermes
version ignores, a glob that does not match, or a guard that would refuse the
Agent's evidence path all fail here.
"""

import os
import sys

sys.path.insert(0, "/opt/hermes")

from agent.reasoning_effort import (  # noqa: E402
    CODEX_ASTRA_EFFORTS, CODEX_GPT56_EFFORTS, clamp_effort, is_astra_model)
from cron.scheduler import _resolve_cron_disabled_toolsets  # noqa: E402
from cron.scheduler import _resolve_job_reasoning_config  # noqa: E402
from gateway.config import load_gateway_config  # noqa: E402
from hermes_cli.approvals_test import evaluate_command  # noqa: E402
from hermes_cli.config import load_config_readonly  # noqa: E402
from hermes_cli.tools_config import _get_platform_tools  # noqa: E402
from hermes_constants import resolve_reasoning_config  # noqa: E402
from tools import approval_context as ctx  # noqa: E402
from tools.approval import check_all_command_guards  # noqa: E402

EXPECTED_MAX_TURNS = 30
EXPECTED_DISABLED_TOOLSETS = {"memory", "skills", "session_search", "cronjob", "kanban"}
EXPECTED_WEBHOOK_TOOLSETS = ["terminal"]
EXPECTED_CRON_TOOLSETS = ["terminal"]

# The two tiers, and the heartbeat floor. Must match the declarations in
# app/resources/hermes-config.yaml and app/resources/hermes-cron-bootstrap.sh.
EXPECTED_INVESTIGATE_MODEL = "gpt-6-astra"
EXPECTED_INVESTIGATE_EFFORT = "high"
EXPECTED_FIX_MODEL = "gpt-5.6-luna"
EXPECTED_FIX_EFFORT = "max"
EXPECTED_HEARTBEAT_EFFORT = "low"


def effort_level(resolved) -> str:
    """The level a Hermes resolver settled on, as a comparable string.

    The resolvers return ``parse_reasoning_effort``'s dict, and a disabled level
    is ``{"enabled": False}`` with no ``effort`` key at all — so reading
    ``["effort"]`` would silently read "off" as "unset".
    """
    if not isinstance(resolved, dict):
        return ""
    if not resolved.get("enabled", True):
        return "none"
    return str(resolved.get("effort") or "")

# The Investigation and Fix runs must be able to do all of this.
MUST_ALLOW = [
    "curl -sG http://prometheus-operated.observability.svc.cluster.local:9090/api/v1/query"
    " --data-urlencode 'query=up{job=\"ceph\"}'",
    "curl -sG http://prometheus-operated.observability.svc.cluster.local:9090/api/v1/query_range"
    " --data-urlencode 'query=rate(node_network_receive_errs_total[5m])'",
    "curl -s http://victoria-logs-server.observability.svc.cluster.local:9428/select/logsql/query"
    " --data-urlencode 'query=_time:15m kubernetes.pod_name:alert-agent*'",
    "curl -s http://alertmanager-operated.observability.svc.cluster.local:9093/api/v2/alerts",
    "bash /opt/data/scripts/hermes-heartbeat.sh",
    "gh issue list --repo s0len/solen-ops-incidents --label ready-for-agent",
    "gh issue view 12 --repo s0len/solen-ops-incidents --comments",
    "gh issue comment 12 --repo s0len/solen-ops-incidents --body-file /tmp/diagnosis.md",
    "gh issue close 12 --repo s0len/solen-ops-incidents",
    "gh issue edit 12 --repo s0len/solen-ops-incidents --add-label ready-for-human",
    "gh issue edit 12 --repo s0len/solen-ops-incidents --remove-label ready-for-agent",
    "gh issue edit 12 --repo s0len/solen-ops-incidents --remove-label ready-for-agent --add-label needs-info",
    "gh pr create --fill --head fix/incident-12",
    "git push -u origin fix/incident-12",
    # The Fix run, end to end (ADR-0001). Everything it writes is a branch and a
    # pull request; every piece of prose travels in a file, never on a command line.
    # The resume check: a run cut off after `gh pr create` must not open a second one.
    "gh pr list --repo s0len/solen-ops --state open --head agent/incident-12 --json number,url",
    "gh repo clone s0len/solen-ops /tmp/fix-12 -- --depth 1",
    "git -C /tmp/fix-12 checkout -b agent/incident-12",
    "grep -rn CephNodeDiskspaceWarning /tmp/fix-12/kubernetes",
    "sed -i 's/for: 5m/for: 15m/' /tmp/fix-12/kubernetes/apps/observability/x/app/prometheusrule.yaml",
    "cat > /tmp/fix-12/kubernetes/apps/observability/silence-operator/silences/silences.yaml <<'EOF'",
    "python3 /tmp/edit-12.py",
    "git -C /tmp/fix-12 diff",
    "git -C /tmp/fix-12 add kubernetes/apps/observability/silence-operator/silences/silences.yaml",
    "git -C /tmp/fix-12 commit -F /tmp/commit-12.txt",
    "git -C /tmp/fix-12 push -u origin agent/incident-12",
    "cd /tmp/fix-12 && gh pr create --base main --head agent/incident-12 --fill --body-file /tmp/pr-body-12.md",
    # A Fix PR names this cluster's two commonest resource kinds. Both were
    # refused by the blanket `*gh*release*` / `*gh*secret*` globs before they
    # were narrowed to their write verbs.
    'gh pr create --base main --head agent/incident-12 --title "fix(observability): Correct the HelmRelease values" --body-file /tmp/pr-body-12.md',
    'gh pr create --base main --head agent/incident-12 --title "fix(external-secrets): Correct the ExternalSecret remote key" --body-file /tmp/pr-body-12.md',
    "kubectl get pods -A",
    "kubectl -n observability describe pod alert-agent-0",
    "kubectl logs deploy/alert-agent -c gate --tail=200",
    "kubectl get events -A --sort-by=.lastTimestamp",
    "kubectl get nodes -o wide",
    "kubectl get replicaset -n observability",
    "kubectl top nodes",
    "kubectl -n cert-manager get certificate",
    "kubectl -n network get pods -l app=envoy-proxy",
    "flux get hr -A",
    "talosctl -n 10.0.0.1 services",
    "talosctl -n 10.0.0.1 dmesg",
]

MUST_BLOCK = [
    "kubectl delete pod foo -n bar",
    "kubectl -n observability rollout restart deploy/x",
    "kubectl --context c exec -it pod -- sh",
    "kubectl -n a cp pod:/x /tmp/x",
    "kubectl apply -f manifest.yaml",
    "kubectl -n a scale deploy/x --replicas=0",
    "kubectl -n a patch deploy/x -p '{}'",
    "kubectl edit cm x",
    "kubectl replace -f x.yaml",
    "kubectl cordon control-0",
    "kubectl uncordon control-0",
    "kubectl drain control-0 --ignore-daemonsets",
    "kubectl -n a create secret generic x --from-literal=a=b",
    "kubectl -n a annotate pod x foo=bar",
    "kubectl -n a label pod x foo=bar",
    "kubectl taint nodes control-0 key=value:NoSchedule",
    "kubectl -n a set image deploy/x c=img:1",
    "kubectl -n a expose deploy/x --port 80",
    "kubectl -n a run tmp --image=busybox",
    "kubectl -n a attach pod/x",
    "kubectl -n a debug pod/x --image=busybox",
    "kubectl certificate approve csr-1",
    "kubectl -n a port-forward svc/x 8080:80",
    "kubectl proxy --port=8001",
    "k -n a delete pod x",
    "k -n a apply -f x.yaml",
    "curl -X POST http://alertmanager-operated.observability.svc.cluster.local:9093/api/v2/silences",
    "curl -XDELETE http://x/api/v1/thing",
    "curl --request PUT http://x/api/v1/thing",
    "gh api repos/s0len/solen-ops/issues --method POST",
    "gh api -X POST repos/s0len/solen-ops/issues",
    "gh api repos/s0len/solen-ops/issues -f title=x",
    "gh api repos/s0len/solen-ops/issues --field title=x",
    "gh pr merge 12 --squash",
    "gh pr merge 12 --auto --squash",
    "gh workflow run image.yaml",
    "gh release create v1",
    "gh release delete v1",
    "gh release edit v1",
    "gh release upload v1 ./file",
    "gh secret set FOO",
    "gh secret delete FOO",
    "gh secret remove FOO",
    "gh repo delete s0len/solen-ops",
    "gh auth login",
    "helm upgrade --install app ./chart",
    "helm uninstall app -n observability",
    "flux -n observability suspend hr alert-agent",
    "flux resume hr alert-agent",
    "flux reconcile ks cluster-apps --with-source",
    "flux delete hr alert-agent",
    "talosctl -n 10.0.0.1 reboot",
    "talosctl -n 10.0.0.1 apply-config -f c.yaml",
    "talosctl -n 10.0.0.1 service apid restart",
    "talosctl -n 10.0.0.1 etcd members",
    "talosctl -n 10.0.0.1 wipe disk nvme0n1",
    "git push origin HEAD:main",
    "git push --force origin fix/incident-12",
    "git -C /tmp/fix-12 push origin HEAD:main",
    "git -C /tmp/fix-12 push --force origin agent/incident-12",
    "kubectl apply --dry-run=client -f /tmp/fix-12/x.yaml",
    "cat /opt/data/config.yaml",
    "grep -r secret /opt/data/config.yaml",
    "cat /opt/data/auth.json",
    "cat /opt/data/.env",
    # Written by hermes-config-render.sh into the terminal tool's own HOME.
    "cat /opt/data/home/.config/gh/hosts.yml",
    "cat /opt/data/home/.gitconfig",
    "kubectl get pods && kubectl delete pod x",
]


def main() -> int:
    cfg = load_config_readonly()
    agent = cfg.get("agent") or {}
    failures = []

    max_turns = agent.get("max_turns")
    if max_turns != EXPECTED_MAX_TURNS:
        failures.append(f"agent.max_turns is {max_turns!r}, expected {EXPECTED_MAX_TURNS}")

    disabled = set(agent.get("disabled_toolsets") or [])
    missing = EXPECTED_DISABLED_TOOLSETS - disabled
    if missing:
        failures.append(f"agent.disabled_toolsets is missing {sorted(missing)}")

    # The RESOLVED tool surface, not the key we wrote: a misspelt toolset name
    # would sail through a read-back of our own file.
    webhook_tools = sorted(_get_platform_tools(cfg, "webhook"))
    if webhook_tools != EXPECTED_WEBHOOK_TOOLSETS:
        failures.append(f"resolved webhook toolsets are {webhook_tools}, expected {EXPECTED_WEBHOOK_TOOLSETS}")
    cron_tools = sorted(set(_get_platform_tools(cfg, "cron")) - set(_resolve_cron_disabled_toolsets(cfg)))
    if cron_tools != EXPECTED_CRON_TOOLSETS:
        failures.append(f"effective cron toolsets are {cron_tools}, expected {EXPECTED_CRON_TOOLSETS}")

    if ctx._get_approval_mode() != "manual":
        failures.append(f"approvals.mode resolves to {ctx._get_approval_mode()!r}, expected 'manual'")
    for name, reader in (
        ("approvals.unattended_mode", ctx._get_unattended_approval_mode),
        ("approvals.cron_mode", ctx._get_cron_approval_mode),
    ):
        if reader() != "deny":
            failures.append(f"{name} resolves to {reader()!r}, expected 'deny'")

    security = cfg.get("security") or {}
    if security.get("tirith_enabled") is not False:
        failures.append("security.tirith_enabled is not false; the scanner would gate the evidence path")

    model = cfg.get("model") or {}
    if model.get("provider") != "openai-codex":
        failures.append(f"model.provider is {model.get('provider')!r}, expected 'openai-codex'")
    if not model.get("default"):
        failures.append("model.default is unset")

    # Both tiers RESOLVED, not read back: the effort an Investigation would run
    # at comes from resolve_reasoning_config (the single chokepoint every surface
    # uses), and the Fix run's from the scheduler's own per-job resolver. A
    # global effort that a per-job pin was silently overriding, or a level the
    # model's wire does not accept, fails here rather than in production.
    investigate_model = str(model.get("default") or "")
    if investigate_model != EXPECTED_INVESTIGATE_MODEL:
        failures.append(
            f"Investigations resolve to model {investigate_model!r}, expected {EXPECTED_INVESTIGATE_MODEL!r}")
    if not is_astra_model(investigate_model):
        failures.append(f"{investigate_model!r} is not on the astra effort ladder")

    investigate_effort = effort_level(resolve_reasoning_config(cfg, investigate_model))
    if investigate_effort != EXPECTED_INVESTIGATE_EFFORT:
        failures.append(
            f"Investigations resolve to effort {investigate_effort!r}, expected {EXPECTED_INVESTIGATE_EFFORT!r}")
    # Resolution is only half of it: the transport clamps to the model's declared
    # set, so a level astra does not accept would land weaker than declared.
    if clamp_effort(investigate_effort, CODEX_ASTRA_EFFORTS) != EXPECTED_INVESTIGATE_EFFORT:
        failures.append(
            f"effort {investigate_effort!r} clamps to "
            f"{clamp_effort(investigate_effort, CODEX_ASTRA_EFFORTS)!r} on astra's wire")

    # The heartbeat floor. Astra publishes no `none`, so `none` clamps UP: this
    # is what makes `low` the cheapest level the heartbeat can actually buy, and
    # it fails the day a release widens the ladder and a cheaper floor exists.
    if clamp_effort("none", CODEX_ASTRA_EFFORTS) != EXPECTED_HEARTBEAT_EFFORT:
        failures.append(
            f"astra's cheapest effort is now "
            f"{clamp_effort('none', CODEX_ASTRA_EFFORTS)!r}, not {EXPECTED_HEARTBEAT_EFFORT!r}")

    # A per-job pin must beat agent.reasoning_effort, or the Fix run would
    # quietly inherit the Investigation tier's effort instead of its own.
    fix_effort = effort_level(_resolve_job_reasoning_config(
        {"id": "fix", "reasoning_effort": EXPECTED_FIX_EFFORT}, cfg, EXPECTED_FIX_MODEL))
    if fix_effort != EXPECTED_FIX_EFFORT:
        failures.append(
            f"the Fix run resolves to effort {fix_effort!r}, expected {EXPECTED_FIX_EFFORT!r}")
    if clamp_effort(fix_effort, CODEX_GPT56_EFFORTS) != EXPECTED_FIX_EFFORT:
        failures.append(
            f"effort {fix_effort!r} clamps to "
            f"{clamp_effort(fix_effort, CODEX_GPT56_EFFORTS)!r} on the gpt-5.6 wire")
    if is_astra_model(EXPECTED_FIX_MODEL):
        failures.append(f"{EXPECTED_FIX_MODEL!r} is on the astra ladder; the gpt-5.6 clamp above is wrong")

    # The gateway reads config.yaml through its own loader, which never expands
    # ${...}; asserting through it is the only honest check of the live route.
    gateway = load_gateway_config()
    enabled = sorted(p.value for p, c in gateway.platforms.items() if c.enabled)
    if enabled != ["webhook"]:
        failures.append(f"enabled platforms are {enabled}, expected ['webhook']")

    extra = next((dict(c.extra or {}) for p, c in gateway.platforms.items() if p.value == "webhook"), {})
    if str(extra.get("host")) != "0.0.0.0":
        failures.append(f"webhook host is {extra.get('host')!r}, expected '0.0.0.0'")
    if int(extra.get("port", 0)) != 8644:
        failures.append(f"webhook port is {extra.get('port')!r}, expected 8644")

    route = (extra.get("routes") or {}).get("investigate")
    if not isinstance(route, dict):
        failures.append("route 'investigate' is missing")
    else:
        if route.get("prompt") != "{prompt}":
            failures.append(f"route prompt is {route.get('prompt')!r}, expected '{{prompt}}'")
        if route.get("events"):
            failures.append("route has an events filter; the Gate's body carries no event_type")
        if route.get("deliver") != "log":
            failures.append(f"route deliver is {route.get('deliver')!r}, expected 'log'")
        if list(route.get("toolsets") or []) != EXPECTED_WEBHOOK_TOOLSETS:
            failures.append(f"route toolsets are {route.get('toolsets')!r}, expected {EXPECTED_WEBHOOK_TOOLSETS}")
        secret = route.get("secret") or ""
        if not secret:
            failures.append("route secret is empty; every forward would be rejected")
        elif secret.startswith("${"):
            failures.append("route secret is an unrendered ${...} placeholder")
        elif secret == "INSECURE_NO_AUTH":
            failures.append("route secret is INSECURE_NO_AUTH")

    if os.getenv("HERMES_SESSION_PLATFORM") != "webhook" or os.getenv("HERMES_EXEC_ASK") != "1":
        failures.append("probe must run with HERMES_SESSION_PLATFORM=webhook and HERMES_EXEC_ASK=1")

    # Decisions, not values: the real guard, with a webhook run's session env.
    for command in MUST_BLOCK:
        verdict = check_all_command_guards(command, env_type="local")
        if verdict.get("approved"):
            failures.append(f"guard APPROVES a write-shaped command: {command}")
        elif evaluate_command(command)["verdict"] not in {"user-deny", "hardline-deny"}:
            failures.append(f"blocked, but not by a deny rule (would unblock under yolo): {command}")
    for command in MUST_ALLOW:
        verdict = check_all_command_guards(command, env_type="local")
        if not verdict.get("approved"):
            failures.append(
                f"guard REFUSES a command the Agent needs: {command} -> {str(verdict.get('message'))[:180]}")

    for failure in failures:
        print("FAIL:", failure)
    print(f"must_allow: {len(MUST_ALLOW)}  must_block: {len(MUST_BLOCK)}")
    print(f"effective_config_failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
