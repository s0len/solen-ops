# tunesynctool — persistent Spotify → Navidrome / Plex playlist sync

A small, always-on utility Deployment in the `media` namespace that syncs any
Spotify playlist into **Navidrome** (`sync.py`) or **Plex** (`plexsync.py`) with a
single `kubectl exec` — **no new pod, no re-install, no re-auth**.

The trick: the Python venv **and** the primed Spotify OAuth token both live on a
2Gi `ceph-block` PVC mounted at `/work`. You prime Spotify OAuth **once**; every
later sync reuses the cached token (spotipy refreshes it automatically).

## Architecture

| Piece | What |
| --- | --- |
| Deployment `tunesynctool` | `python:3.12-slim`, runs as uid/gid **568**, idles on `sleep infinity`. On first start it builds a venv on the PVC (`python -m venv /work/venv` + `pip install tunesynctool anyio plexapi spotipy`). On restart the venv already exists → skipped. |
| PVC `tunesynctool` (`/work`) | `ceph-block`, 2Gi, RWO. Holds `/work/venv` (deps) and `/work/.cache` (Spotify token). **No VolSync** — both are re-creatable. |
| ConfigMap `tunesynctool-scripts` (`/scripts`, read-only) | `prime.py`, `sync.py`, `plexsync.py`. Stable name (no hash) + `reloader.stakater.com/auto` → editing a script and reconciling rolls the pod; the venv/cache on the PVC survive the roll, so **no re-auth**. |
| Secret `tunesynctool-secret` (ExternalSecret) | `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` (1Password `spotify`), `PLEX_TOKEN` (`plex`), `ND_USER`/`ND_PASS` (`navidrome` → `SYNC_TO_USER`/`SYNC_TO_PASS`). Injected via `envFrom`. |

Non-secret config is baked into the Deployment env: `NAVIDROME_URL`
(`http://navidrome-app.media.svc.cluster.local:4533`), `PLEX_URL`
(`http://plex.media.svc.cluster.local:32400`), `PLEX_LIBRARY` (`Musik`), the OAuth
`SCOPES`/`REDIRECT`, and `TUNESYNC_WORKDIR=/work`.

> Everything is invoked as `/work/venv/bin/python /scripts/<script>.py`. The
> container's `PATH` also prepends `/work/venv/bin`, so `tunesynctool` (called by
> `sync.py`) resolves to the venv without a full path.

## First run (once, after Flux applies)

```bash
export KUBECONFIG=/Users/solen/GitHub/solen-ops/kubeconfig

# The pod bootstraps the venv on first start (~30–60s of pip). Watch it finish:
kubectl logs -n media deploy/tunesynctool -f            # look for "bootstrapping venv ..." then quiet
kubectl exec -n media deploy/tunesynctool -- test -x /work/venv/bin/tunesynctool && echo "venv ready"
```

### Prime the Spotify OAuth cache (once per Spotify identity)

`tunesynctool`/spotipy default to `open_browser=True`, which in a headless pod
silently binds `:8888` and prints **no URL**. `prime.py` runs the paste-back flow
(`open_browser=False`, `check_cache=False`) and writes the token to `/work/.cache`.

```bash
kubectl exec -it -n media deploy/tunesynctool -- /work/venv/bin/python /scripts/prime.py
```

It prints `Go to the following URL: https://accounts.spotify.com/authorize?...`.
Open that in your **laptop browser**, log in as the target Spotify user, click
**Agree**. The browser redirects to `http://127.0.0.1:8888/callback?code=...` which
**fails to load — that's expected** (nothing is listening). Copy the **full URL
from the address bar** and paste it at the pod's
`Enter the URL you were redirected to:` prompt. Success prints
`OK - token cached at /work/.cache`. Done — the token is now on the PVC.

> Spotify dev-app: the app is in **development mode**, so each imported user's
> Spotify email must be on the app's **User Management** allowlist (cap ~25). Not
> added ⇒ their playlists return 403.

## Find playlist IDs (reuses the cache — no re-auth)

```bash
kubectl exec -it -n media deploy/tunesynctool -- /work/venv/bin/python - <<'PY'
import os, spotipy
from spotipy.oauth2 import SpotifyOAuth
oa = SpotifyOAuth(client_id=os.environ["SPOTIFY_CLIENT_ID"],
                  client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
                  redirect_uri=os.environ["REDIRECT"], scope=os.environ["SCOPES"],
                  open_browser=False, cache_path="/work/.cache")
sp = spotipy.Spotify(auth_manager=oa); me = sp.me()["id"]
res = sp.current_user_playlists(limit=50); rows = []
while res:
    for p in res["items"]:
        own = "OWN " if p["owner"]["id"] == me else "sub "
        rows.append((p["id"], p["tracks"]["total"], own, p["name"]))
    res = sp.next(res) if res["next"] else None
for pid, n, own, name in rows:
    print(f"{pid}  {own} {n:>4}  {name}")
print(f"\n{len(rows)} playlists")
PY
```

## Sync to Navidrome — `sync.py`

Idempotent by **name**: resolves the Navidrome target playlist by name via the
Subsonic API, then `sync`-updates it in place if it exists, or `transfer`-creates
it the first time. `ND_USER`/`ND_PASS` come from the Secret — nothing to set.

```bash
# preview match rate, write nothing:
kubectl exec -it -n media deploy/tunesynctool -- \
  /work/venv/bin/python /scripts/sync.py <spotify_playlist_id> "Riktigt bra låtar" --preview

# real sync (create-or-update in place):
kubectl exec -it -n media deploy/tunesynctool -- \
  /work/venv/bin/python /scripts/sync.py <spotify_playlist_id> "Riktigt bra låtar"

# just resolve + print the Navidrome playlist ID, touch nothing:
kubectl exec -it -n media deploy/tunesynctool -- \
  /work/venv/bin/python /scripts/sync.py <spotify_playlist_id> "Riktigt bra låtar" --resolve-only
```

## Sync to Plex — `plexsync.py`

Matches **smarter than tunesynctool** by normalizing Spotify's remaster/edit/feat
noise before matching, so it catches owned-but-suffixed tracks
(`"… - Remastered 2011"`, `"… - 2004 Remaster"`, `"(feat. …)"`, `"- Radio Edit"`, …).
Idempotent by **name**: updates the named Plex playlist (adds only missing matched
tracks) if it exists, else creates it.

```bash
# preview matched/missed counts, write nothing:
kubectl exec -it -n media deploy/tunesynctool -- \
  /work/venv/bin/python /scripts/plexsync.py <spotify_playlist_id> "Riktigt bra låtar" --preview

# real sync (create-or-update the Plex playlist):
kubectl exec -it -n media deploy/tunesynctool -- \
  /work/venv/bin/python /scripts/plexsync.py <spotify_playlist_id> "Riktigt bra låtar"
```

Targets the `Musik` section (`PLEX_LIBRARY`, type `artist`) via `PLEX_TOKEN` +
`PLEX_URL` from env. The token is never printed (redacted in any echoed error).

### How `plexsync.py` matches

1. Strip trailing `- Remastered/Remaster/Radio Edit/Single Version/Mono/Acoustic/
   Live/… (year)` clauses and parenthetical `(feat …)`/`(with …)`/`(… Remaster)`.
2. Accent-fold + case-fold + strip punctuation on both title and artist.
3. Query Plex `searchTracks(title__icontains=<cleaned core>)` (plus a distinctive
   word as fallback), then keep candidates whose normalized title matches
   (equal or substring either way) **and** whose album-artist or track-artist
   matches the Spotify artist. Duration proximity (~3s) breaks ties and rescues
   an exact-title-no-artist case.

## Adding another Spotify user later

1. Add the new person's Spotify email to the dev-app **User Management** allowlist.
2. Re-prime as the new user — `prime.py` uses `check_cache=False`, so it overwrites
   `/work/.cache` even though a token already exists:
   ```bash
   kubectl exec -it -n media deploy/tunesynctool -- /work/venv/bin/python /scripts/prime.py
   ```
   Only **one** Spotify identity is active at a time (single shared cache). Sync
   that user's playlists, then re-prime again to switch back.
3. For a different Navidrome target user, update the `navidrome` 1Password item's
   `SYNC_TO_USER`/`SYNC_TO_PASS` (or generalize the ExternalSecret) and let
   External-Secrets resync.

## Maintenance

- **Rebuild the venv** (e.g. to pick up a newer `tunesynctool`):
  ```bash
  kubectl exec -n media deploy/tunesynctool -- rm -rf /work/venv
  kubectl rollout restart deploy/tunesynctool -n media   # re-bootstraps on next start
  ```
  The Spotify cache at `/work/.cache` is untouched, so **no re-auth**.
- **Editing a script** (`scripts/*.py`): commit + push; Flux updates the
  `tunesynctool-scripts` ConfigMap and reloader rolls the pod. Venv + cache persist.
- `transfer` (the old duplicate-on-every-run command) is **gone** — `sync.py` and
  `plexsync.py` are both idempotent-by-name wrappers.
