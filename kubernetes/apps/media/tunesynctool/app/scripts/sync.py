#!/usr/bin/env python3
"""
sync.py — re-sync a Spotify playlist into Navidrome *in place*, resolving the
Navidrome target playlist by NAME so you never hunt IDs and never get duplicates.

Why this exists:
  `tunesynctool transfer <id>` always CREATES a new Navidrome playlist, so
  re-running it duplicates the playlist. `tunesynctool sync` updates an existing
  target in place but needs the target playlist's Navidrome ID. This wrapper looks
  that ID up from the playlist NAME via the Subsonic getPlaylists API, then:
    - if the named playlist exists  -> runs `... sync ... --to-playlist <id>`  (update in place)
    - if it does not exist yet       -> runs `... transfer <spotify_id>`        (first-time create)

Run it INSIDE the persistent `tunesynctool` pod, from the working dir that holds the
primed OAuth cache (/work/.cache), so the Spotify token is reused without re-auth:

    kubectl exec -it -n media deploy/tunesynctool -- \
        /work/venv/bin/python /scripts/sync.py <spotify_playlist_id> "<navidrome_playlist_name>"

Usage:
    sync.py <spotify_playlist_id> "<navidrome_playlist_name>"
    sync.py <spotify_playlist_id> "<navidrome_playlist_name>" --resolve-only   # dry run: just print the resolved ID
    sync.py <spotify_playlist_id> "<navidrome_playlist_name>" --preview        # pass --preview to tunesynctool

Environment (injected by the Deployment from the tunesynctool-secret + values):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET   # from the Flux-synced tunesynctool-secret
    ND_USER, ND_PASS                           # target Navidrome username + password (from 1Password `navidrome`)
    REDIRECT                                   # optional; default http://127.0.0.1:8888/callback
    TUNESYNC_WORKDIR                           # optional; default /work (where .cache lives)
    NAVIDROME_URL                              # optional; default http://navidrome.media.svc.cluster.local
    NAVIDROME_PORT                             # optional; default 4533
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REDIRECT = os.environ.get("REDIRECT", "http://127.0.0.1:8888/callback")
WORKDIR = os.environ.get("TUNESYNC_WORKDIR", "/work")
NAV_URL = os.environ.get("NAVIDROME_URL", "http://navidrome.media.svc.cluster.local")
NAV_PORT = os.environ.get("NAVIDROME_PORT", "4533")
CLIENT = "tunesync-sync-py"
API_VERSION = "1.16.1"


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        die("missing required env var(s): " + ", ".join(missing))


def get_navidrome_playlists():
    """Return list of {id, name, songCount, ...} via Subsonic getPlaylists (token auth)."""
    user = os.environ["ND_USER"]
    password = os.environ["ND_PASS"]
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    params = {
        "u": user, "t": token, "s": salt,
        "v": API_VERSION, "c": CLIENT, "f": "json",
    }
    url = f"{NAV_URL}:{NAV_PORT}/rest/getPlaylists?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            body = r.read().decode()
    except urllib.error.URLError as e:
        die(f"could not reach Navidrome at {NAV_URL}:{NAV_PORT}: {e}")
    try:
        resp = json.loads(body)["subsonic-response"]
    except (ValueError, KeyError):
        die(f"unexpected response from getPlaylists: {body[:300]}")
    if resp.get("status") != "ok":
        err = resp.get("error", {})
        die(f"getPlaylists failed (code {err.get('code')}): {err.get('message')}")
    # When the user has zero playlists, the "playlist" key may be absent.
    return resp.get("playlists", {}).get("playlist", [])


def resolve_playlist_id(name):
    """Return (id, how) for the Navidrome playlist matching `name`, or (None, None)."""
    playlists = get_navidrome_playlists()
    exact = [p for p in playlists if p.get("name") == name]
    if len(exact) == 1:
        return exact[0]["id"], "exact"
    if len(exact) > 1:
        die(f"{len(exact)} playlists are named exactly {name!r} — ambiguous, "
            "resolve by ID manually:\n  " +
            "\n  ".join(f"{p['id']}  {p.get('songCount', '?')} tracks" for p in exact))
    ci = [p for p in playlists if p.get("name", "").lower() == name.lower()]
    if len(ci) == 1:
        return ci[0]["id"], "case-insensitive"
    if len(ci) > 1:
        die(f"{len(ci)} playlists case-insensitively match {name!r} — ambiguous, "
            "resolve by ID manually:\n  " +
            "\n  ".join(f"{p['id']}  {p.get('name')!r}" for p in ci))
    return None, None


def tunesynctool_global_flags():
    return [
        "tunesynctool",
        "--spotify-client-id", os.environ["SPOTIFY_CLIENT_ID"],
        "--spotify-client-secret", os.environ["SPOTIFY_CLIENT_SECRET"],
        "--spotify-redirect-uri", REDIRECT,
        "--subsonic-base-url", NAV_URL,
        "--subsonic-port", str(NAV_PORT),
        "--subsonic-username", os.environ["ND_USER"],
        "--subsonic-password", os.environ["ND_PASS"],
    ]


def run(cmd):
    print("+ " + " ".join(("<redacted>" if prev in (
        "--spotify-client-secret", "--subsonic-password", "--spotify-client-id") else a)
        for prev, a in zip([""] + cmd, cmd)))
    return subprocess.run(cmd, cwd=WORKDIR).returncode


def main():
    args = sys.argv[1:]
    resolve_only = "--resolve-only" in args
    preview = "--preview" in args
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) != 2:
        die(f"usage: {os.path.basename(sys.argv[0])} "
            "<spotify_playlist_id> \"<navidrome_playlist_name>\" [--resolve-only] [--preview]")
    spotify_id, nd_name = positional

    require_env("ND_USER", "ND_PASS")
    nd_id, how = resolve_playlist_id(nd_name)

    if nd_id:
        print(f"RESOLVED: Navidrome playlist {nd_name!r} -> id {nd_id} ({how} name match)")
    else:
        print(f"NOT FOUND: no Navidrome playlist named {nd_name!r}")

    if resolve_only:
        return

    require_env("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")
    if nd_id:
        print("PATH: sync (update existing playlist in place)")
        cmd = tunesynctool_global_flags() + [
            "sync",
            "--from", "spotify", "--from-playlist", spotify_id,
            "--to", "subsonic", "--to-playlist", nd_id,
            "--limit", "0",
        ]
    else:
        print("PATH: transfer (first-time create)")
        cmd = tunesynctool_global_flags() + [
            "transfer", spotify_id,
            "--from", "spotify", "--to", "subsonic",
            "--limit", "0",
        ]
    if preview:
        cmd.append("--preview")
    sys.exit(run(cmd))


if __name__ == "__main__":
    main()
