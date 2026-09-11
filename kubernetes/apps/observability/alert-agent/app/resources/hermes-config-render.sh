#!/usr/bin/env bash
# Renders hermes-config.yaml into $HERMES_HOME/config.yaml with the route secret
# substituted. Runs before the gateway (init container).
#
# Hermes' gateway loader reads config.yaml with a bare yaml.safe_load and does NOT
# expand ${VAR} (gateway/config_loader.py::load_yaml_layer) — only the CLI loader
# does. So the placeholder must be substituted here, not by Hermes, or the route
# secret ends up being the literal string "${HERMES_WEBHOOK_SECRET}".
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
TEMPLATE="${TEMPLATE:-$(cd "$(dirname "$0")" && pwd)/hermes-config.yaml}"
TARGET="${TARGET:-${HERMES_HOME}/config.yaml}"

: "${HERMES_WEBHOOK_SECRET:?HERMES_WEBHOOK_SECRET must be set (Secret alert-agent-webhook-secret)}"

TEMPLATE="${TEMPLATE}" TARGET="${TARGET}" python3 - <<'PY'
import os
import pathlib
import tempfile

# The image's `hermes` user (Dockerfile: useradd -u 10000 -m -d /opt/data hermes).
HERMES_UID = int(os.environ.get("HERMES_UID", "10000"))
HERMES_GID = int(os.environ.get("HERMES_GID", HERMES_UID))

template = pathlib.Path(os.environ["TEMPLATE"]).read_text(encoding="utf-8")
placeholder = "${HERMES_WEBHOOK_SECRET}"
if placeholder not in template:
    raise SystemExit(f"{os.environ['TEMPLATE']}: no {placeholder} placeholder to render")
rendered = template.replace(placeholder, os.environ["HERMES_WEBHOOK_SECRET"])

target = pathlib.Path(os.environ["TARGET"])
target.parent.mkdir(parents=True, exist_ok=True)
handle, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".config.yaml.")
os.close(handle)
tmp = pathlib.Path(tmp)
tmp.write_text(rendered, encoding="utf-8")
tmp.chmod(0o640)
# 0640 root-owned is unreadable by the hermes runtime user; own that here rather
# than relying on the image's stage2 hook to chown it back.
try:
    os.chown(tmp, HERMES_UID, HERMES_GID)
except (PermissionError, OSError) as exc:
    print(f"[render] not chowning to {HERMES_UID}:{HERMES_GID} ({exc}); already unprivileged?")
tmp.replace(target)
print(f"[render] wrote {target}")
PY

# $HERMES_HOME/.env is loaded with override=True and the image's stage2 hook
# generates a strong API_SERVER_KEY into it on any boot where the variable is
# unset — which then enables the api_server listener no matter what the
# container environment says. Pin the sentinel on the volume so a PVC that
# already carries a generated key cannot resurrect that listener.
if [ -n "${API_SERVER_KEY:-}" ]; then
    HERMES_HOME="${HERMES_HOME}" API_SERVER_KEY="${API_SERVER_KEY}" python3 - <<'PY'
import os
import pathlib

env_path = pathlib.Path(os.environ["HERMES_HOME"]) / ".env"
line = f"API_SERVER_KEY={os.environ['API_SERVER_KEY']}"
try:
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
except OSError as exc:
    raise SystemExit(f"[render] cannot read {env_path}: {exc}")
kept = [entry for entry in existing if not entry.startswith("API_SERVER_KEY=")]
if kept != existing or line not in existing:
    env_path.write_text("\n".join(kept + [line]) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    try:
        os.chown(env_path, int(os.environ.get("HERMES_UID", "10000")),
                 int(os.environ.get("HERMES_GID", os.environ.get("HERMES_UID", "10000"))))
    except (PermissionError, OSError):
        pass
    print(f"[render] pinned API_SERVER_KEY in {env_path}")
PY
fi

# gh credentials as a file, not an environment variable. Hermes scrubs every
# tool subprocess's env of KEY/TOKEN/SECRET/AUTH names, and both GITHUB_TOKEN
# and GH_TOKEN sit on its provider blocklist, which operator config is refused
# permission to override (GHSA-rhgp-j443-p4rf). gh reads hosts.yml with no env
# at all, so the Agent can post its Diagnosis and, later, push a Fix branch.
if [ -n "${GITHUB_TOKEN:-}" ]; then
    HERMES_HOME="${HERMES_HOME}" GITHUB_TOKEN="${GITHUB_TOKEN}" \
    GITHUB_USER="${GITHUB_USER:-}" python3 - <<'PY'
import os
import pathlib

home = pathlib.Path(os.environ["HERMES_HOME"])
hosts = home / ".config" / "gh" / "hosts.yml"
hosts.parent.mkdir(parents=True, exist_ok=True)
user = os.environ.get("GITHUB_USER") or "x-access-token"
hosts.write_text(
    "github.com:\n"
    f"    oauth_token: {os.environ['GITHUB_TOKEN']}\n"
    f"    user: {user}\n"
    "    git_protocol: https\n",
    encoding="utf-8",
)
hosts.chmod(0o600)

gitconfig = home / ".gitconfig"
if not gitconfig.exists():
    gitconfig.write_text(
        "[credential \"https://github.com\"]\n"
        "\thelper = !gh auth git-credential\n"
        "[user]\n"
        f"\tname = {user}\n"
        f"\temail = {user}@users.noreply.github.com\n"
        "[safe]\n"
        "\tdirectory = *\n",
        encoding="utf-8",
    )

uid = int(os.environ.get("HERMES_UID", "10000"))
gid = int(os.environ.get("HERMES_GID", os.environ.get("HERMES_UID", "10000")))
for path in (hosts, hosts.parent, hosts.parent.parent, gitconfig):
    try:
        os.chown(path, uid, gid)
    except (PermissionError, OSError, FileNotFoundError):
        pass
print(f"[render] wrote {hosts} and a git credential helper")
PY
else
    echo "[render] GITHUB_TOKEN unset; gh will be unauthenticated" >&2
fi
