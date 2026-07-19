# tunesynctool — ad-hoc Spotify → Navidrome playlist import

Repeatable runbook for importing a Spotify user's playlists into Navidrome
(Subsonic API) using the [`tunesynctool`](https://github.com/WilliamNT/tunesynctool)
CLI, run as a **throwaway pod** in the `media` namespace.

This is **not** a service. Only the shared Spotify app credentials are managed by
Flux (the `ExternalSecret` in `app/`). Every import is ad-hoc: spin up the pod,
authorize, transfer, tear it down. Repeat later for a different Spotify user →
different Navidrome target user.

## What's Flux-managed vs manual

| File | Managed by | Purpose |
| --- | --- | --- |
| `ks.yaml`, `app/kustomization.yaml`, `app/externalsecret.yaml` | Flux | Syncs 1Password item `spotify` (`CLIENT_ID`/`CLIENT_SECRET`) into Secret `tunesynctool-secret`. |
| `pod.yaml` | **Manual only** | Throwaway pod. Lives outside `app/`, so Flux never applies it. |
| `sync.py` | **Manual only** | Wrapper for re-syncing a playlist in place (resolves the Navidrome target by name). Outside `app/`, so Flux ignores it. See [Re-syncing](#re-syncing-an-existing-playlist-update-in-place). |
| `README.md` | — | This runbook. |

## Prerequisites (per Spotify user)

- **Spotify Developer app** (the one behind the `spotify` 1Password item):
  - Redirect URI must include `http://127.0.0.1:8888/callback` (already set).
  - The app is in **development mode**, so each person whose playlists you import
    must be added to **User Management** (allowlist). Dev-mode apps are capped
    (~25 users) — keep the list pruned. Not added ⇒ their playlists return 403.
  - The account being imported should be able to authorize; **Premium recommended**.
- `tunesynctool-secret` exists in `media` (Flux-synced):
  `kubectl get secret tunesynctool-secret -n media`
- You know the **target Navidrome username + password** for this run.

## Runbook

Set kubeconfig first (host side):

```bash
export KUBECONFIG=/Users/solen/GitHub/solen-ops/kubeconfig
```

### 1. Launch the throwaway pod

```bash
kubectl apply -f kubernetes/apps/media/tunesynctool/pod.yaml
kubectl wait --for=condition=Ready pod/tunesync -n media --timeout=120s
```

### 2. Install the tool and open a shell

`anyio` is a missing transitive dep of `tunesynctool` and must be installed too,
or every invocation fails with `ModuleNotFoundError: No module named 'anyio'`.

```bash
kubectl exec -n media tunesync -- pip install --quiet tunesynctool anyio
kubectl exec -it -n media tunesync -- bash
```

Everything below runs **inside the pod**, from a fixed working dir so the OAuth
`.cache` file is reused across all commands:

```bash
mkdir -p /work && cd /work
export REDIRECT=http://127.0.0.1:8888/callback
export SCOPES='user-library-read,playlist-read-private,playlist-read-collaborative,playlist-modify-public,playlist-modify-private'
```

### 3. Prime the Spotify OAuth cache (paste-back — no port-forward)

`tunesynctool` uses spotipy with its default `open_browser=True`, which in a
headless pod silently binds a local server on `:8888` and prints **no URL** — useless
here. Instead we prime the token cache once with `open_browser=False` (clean
paste-back). The CLI then reuses `/work/.cache` and never prompts.

Write the primer to a **file** and run it — do **not** pipe it via
`python3 - <<'PY'`. The paste-back prompt reads the redirect URL from stdin, and a
heredoc leaves stdin at EOF, so `input()` fails with `EOFError`.

```bash
cat > /work/prime.py <<'PY'
import os
from spotipy.oauth2 import SpotifyOAuth
oa = SpotifyOAuth(
    client_id=os.environ["SPOTIFY_CLIENT_ID"],
    client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
    redirect_uri=os.environ["REDIRECT"],
    scope=os.environ["SCOPES"],
    open_browser=False, cache_path="/work/.cache",
)
oa.get_access_token(check_cache=False)
print("OK - token cached at /work/.cache")
PY
python3 /work/prime.py
```

It prints `Go to the following URL: https://accounts.spotify.com/authorize?...`.
Open that URL in **your laptop browser**, log in as the target Spotify user, click
**Agree**. The browser redirects to `http://127.0.0.1:8888/callback?code=...` which
**fails to load — that's expected** (nothing is listening). Copy the **full URL from
the address bar** and paste it at the pod's `Enter the URL you were redirected to:`
prompt. Success prints `OK - token cached at /work/.cache`.

### 4. List the user's playlist IDs (reuses the cache — no re-auth)

```bash
python3 - "$SCOPES" "$REDIRECT" <<'PY'
import sys, os, spotipy
from spotipy.oauth2 import SpotifyOAuth
scopes, redirect = sys.argv[1], sys.argv[2]
oa = SpotifyOAuth(
    client_id=os.environ["SPOTIFY_CLIENT_ID"],
    client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
    redirect_uri=redirect, scope=scopes,
    open_browser=False, cache_path="/work/.cache",
)
sp = spotipy.Spotify(auth_manager=oa)
me = sp.me()["id"]
res = sp.current_user_playlists(limit=50); offset = 0; rows = []
while res:
    for p in res["items"]:
        own = "OWN " if p["owner"]["id"] == me else "sub "
        rows.append((p["id"], p["tracks"]["total"], own, p["name"]))
    if res["next"]:
        offset += 50; res = sp.current_user_playlists(limit=50, offset=offset)
    else:
        break
for pid, n, own, name in rows:
    print(f"{pid}  {own} {n:>4}  {name}")
print(f"\n{len(rows)} playlists")
PY
```

Pick the IDs to import (usually the `OWN` ones).

### 5. Transfer each playlist into Navidrome

Set the per-run Navidrome target creds and loop over the chosen IDs. The Subsonic
target is reached in-cluster; `--subsonic-base-url` is scheme+host only, port is
separate. Navidrome uses modern token auth, so no `--subsonic-legacy-auth`.

```bash
export ND_USER='navidrome_username_for_this_run'
export ND_PASS='navidrome_password_for_this_run'

PLAYLISTS="37i9dQZF1DX... 3cEYpjA9oz... 1AbCdEf..."   # space-separated IDs from step 4

for PID in $PLAYLISTS; do
  echo "=== transferring $PID ==="
  tunesynctool \
    --spotify-client-id "$SPOTIFY_CLIENT_ID" \
    --spotify-client-secret "$SPOTIFY_CLIENT_SECRET" \
    --spotify-redirect-uri "$REDIRECT" \
    --subsonic-base-url http://navidrome-app.media.svc.cluster.local \
    --subsonic-port 4533 \
    --subsonic-username "$ND_USER" \
    --subsonic-password "$ND_PASS" \
    transfer "$PID" --from spotify --to subsonic
done
```

Tips:

- Add `--preview` to see match rates without writing anything to Navidrome.
  Matching quality depends on your FLAC library's tags — unmatched tracks print
  `Fail: No result for ...`.
- `--limit 0` (default) = all tracks.
- **`transfer` is not idempotent** — it always creates a *new* target playlist, so
  **re-running `transfer` duplicates the playlist in Navidrome.** To re-run against a
  playlist that already exists, use `sync.py` instead — see
  [Re-syncing an existing playlist](#re-syncing-an-existing-playlist-update-in-place)
  below. `sync.py` is the correct re-run path.

### 6. Cleanup

```bash
exit   # leave the pod shell
kubectl delete -f kubernetes/apps/media/tunesynctool/pod.yaml
```

## Re-syncing an existing playlist (update in place)

**Do not re-run `transfer` to refresh a playlist** — `transfer` always creates a
*new* Navidrome playlist, so a second run leaves you with two playlists of the same
name. To pull newly-matched tracks into the playlist you already created, update it
**in place** with `tunesynctool sync`, which needs the *Navidrome* playlist ID:

```bash
tunesynctool <global creds flags> \
  sync --from spotify --from-playlist <spotify_id> \
       --to subsonic --to-playlist <navidrome_playlist_id> --limit 0
```

Finding that Navidrome ID by hand is annoying, so use the committed **`sync.py`**
wrapper. Given a Spotify playlist ID and the *target playlist name*, it:

1. calls the Subsonic `getPlaylists` API and finds the Navidrome playlist whose name
   matches (exact, then case-insensitive; errors out if the name is ambiguous);
2. if found → runs `tunesynctool … sync … --to-playlist <id>` (update in place);
3. if not found → runs `tunesynctool … transfer <spotify_id>` (first-time create).

It reads the same env as the transfer runbook (`SPOTIFY_CLIENT_ID` /
`SPOTIFY_CLIENT_SECRET` from the Secret, plus `ND_USER` / `ND_PASS` you set per run)
and must run from the dir holding the primed `/work/.cache`.

```bash
# from the host: copy the wrapper into the running pod
kubectl cp kubernetes/apps/media/tunesynctool/sync.py media/tunesync:/work/sync.py

# inside the pod (steps 1–3 already done: pip install + primed /work/.cache):
cd /work
export ND_USER='navidrome_username_for_this_run'
export ND_PASS='navidrome_password_for_this_run'

# dry run — just resolve + print the Navidrome playlist ID, touch nothing:
python3 sync.py <spotify_playlist_id> "Riktigt bra låtar" --resolve-only

# real re-sync (update in place); add --preview to see matches without writing:
python3 sync.py <spotify_playlist_id> "Riktigt bra låtar"
```

`sync.py` uses Subsonic **token auth** (`t=md5(pass+salt)`, `s=salt`) for
`getPlaylists`, so the password is never placed in a URL. Navidrome accepts both
token and plaintext (`p=`) auth; token is preferred. `getPlaylists` always returns
HTTP 200 with a `{"subsonic-response":{…}}` envelope — check `status` (`ok` vs
`failed` with an `error.code`), never the HTTP status.

## Next run (different Spotify user → different Navidrome user)

1. Add the new person's Spotify email to the app's **User Management** allowlist.
2. New pod (steps 1–2). `tunesynctool-secret` (shared app creds) is already synced —
   nothing to change there.
3. Re-prime the cache (step 3) as the **new** Spotify user — each pod is fresh, so
   `/work/.cache` starts empty.
4. Steps 4–5 with the new `ND_USER` / `ND_PASS` for that person's Navidrome account.
5. Cleanup.
