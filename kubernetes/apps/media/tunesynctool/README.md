# tunesynctool — persistent playlist sync (Spotify or playlist FILES → Navidrome / Plex)

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
| ConfigMap `tunesynctool-scripts` (`/scripts`, read-only) | `matcher.py`, `prime.py`, `sync.py`, `plexsync.py`, `filesync.py`. Stable name (no hash) + `reloader.stakater.com/auto` → editing a script and reconciling rolls the pod; the venv/cache on the PVC survive the roll, so **no re-auth**. |
| Secret `tunesynctool-secret` (ExternalSecret) | `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` (1Password `spotify`), `PLEX_TOKEN` (`plex`), `ND_USER`/`ND_PASS` (`navidrome` → `SYNC_TO_USER`/`SYNC_TO_PASS`). Injected via `envFrom`. |

Non-secret config is baked into the Deployment env: `NAVIDROME_URL`
(`http://navidrome.media.svc.cluster.local:4533`), `PLEX_URL`
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

## Import playlist FILES — `filesync.py` (no Spotify, no OAuth)

For playlists that arrive as **files** instead of a Spotify link — someone exports
their iTunes/Music playlist and shares it with you. Targets **Navidrome**, reuses the
same matcher as `plexsync.py` (both import `matcher.py`), and needs no Spotify
credentials at all.

```bash
POD=$(kubectl get pod -n media -l app.kubernetes.io/name=tunesynctool -o jsonpath='{.items[0].metadata.name}')

# 1. put the playlist file(s) on the PVC
kubectl exec -n media $POD -- mkdir -p /work/import
kubectl cp "./Karlskrona 2026.txt" "media/$POD:/work/import/Karlskrona 2026.txt"

# 2. preview: prints the match rate, WHAT each track matched to, and every miss
kubectl exec -n media $POD -- /work/venv/bin/python /scripts/filesync.py /work/import --preview

# 3. for real
kubectl exec -n media $POD -- /work/venv/bin/python /scripts/filesync.py /work/import
```

### Input formats — pick the richest one

| Format | Completeness |
| --- | --- |
| `.txt` | iTunes "Export Playlist…" tab-separated. **Best** — has every track, streaming-only included. UTF-8 and UTF-16 both handled. |
| `.xml` | iTunes plist. Also complete, and may hold **several** playlists — each is imported under its own name. |
| `.csv` | Spotify exports from **exportify.net**, and most other CSV exporters. Columns are matched by NAME (not position) and the delimiter is sniffed — exportify localises its headers (`Låtens namn` in Swedish vs `Track Name` in English) and Excel in a Swedish locale writes `;`. Duration is ms when the header says so, otherwise seconds. |
| `.m3u` / `.m3u8` | Only tracks that exist as local **files** on the sender's disk. An iTunes .m3u silently omits every Apple-Music-streamed track — Silvercheek's 26-track playlist came through as 4. Use only when there is no .txt/.xml. |

Point it at a **directory** and the same playlist exported in several formats is
imported **once** — richest format per basename wins (txt > csv > xml > m3u8 > m3u) — so
dropping in all four iTunes exports does not create four playlists.

### Flags

| Flag | Effect |
| --- | --- |
| `--preview` | Match only, write nothing. Prints `OK <source> -> <library track>` per hit so you can spot a wrong match, plus every `MISS`. |
| `--name "X"` | Override the playlist name (single-playlist input only; default is the filename stem, or the plist's own playlist name for `.xml`). |
| `--prefix "X "` | Prepend to every imported name, e.g. `--prefix "Silvercheek — "`. |
| `--private` | Keep the playlist to `ND_USER` only. **Default is public.** |
| `--mirror` | Make the playlist EXACTLY the file, removing tracks that are not in it. Default is additive: only missing tracks are added, so manual additions survive a re-run. |

Idempotent by **name**, like the other two wrappers: an existing playlist owned by
`ND_USER` is updated in place, otherwise it is created.

### Ownership — why filesync's imports are public, and when to use `playlists` instead

Subsonic's `createPlaylist` always creates the playlist owned by the **authenticated**
user (`ND_USER`, currently `solen`), and Navidrome has no admin-impersonation call. So
a playlist someone else sent you cannot be created *as* them without their password.
`filesync.py` therefore creates it as `ND_USER` and sets `public=true`, which makes it
visible and playable for every Navidrome user — including the person who sent it.
Pass `--private` if you do not want that.

**If the person can log in themselves, use the `playlists` app instead**
(`kubernetes/apps/media/playlists`, `https://playlists.${SECRET_DOMAIN}`). They upload
the file, authenticate with their own Navidrome credentials, and the playlist comes
out **owned by them and private** — because their login is the authorization, so no
workaround is needed. `filesync.py` remains the right tool for an operator-driven
import where the other person is not involved.

### Match rate is about your LIBRARY, not the tool

A miss almost always means you do not own the track. Before assuming the matcher is
at fault, search Navidrome for the title — the misses on Silvercheek's first playlist
were 15 genuine gaps (mostly **covers** where only the original is on the shelf:
Sator's `Ring Ring`, Clutch's `Fortunate Son`, Def Leppard's `Personal Jesus`, Jay
Smith's `Like a Prayer`, Dirty Honey's `Let's Go Crazy`) against exactly one real
matcher gap (`Pt. 2` vs `Part 2`, since fixed in `matcher.py`).

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
- **`matcher.py` is shared by `plexsync.py`, `filesync.py` AND the `playlists` app.**
  Change the normalization there and all three get it — that is the point, so they
  cannot drift. Verify a change by diffing `norm_title` over a corpus before/after;
  the regexes are order-sensitive and `strip_suffixes` loops until stable.
- **This app's ConfigMap is mounted by another app.** `kubernetes/apps/media/playlists`
  mounts `tunesynctool-scripts` read-only at `/shared` and imports `matcher` and
  `filesync` from it, so **editing a script here rolls that pod too** and must be
  verified against both. `filesync.py`'s `parse_itunes_txt` / `parse_itunes_xml` /
  `parse_m3u`, `pick()` and the `COL_*` tables are now an API — do not change their
  signatures without checking `playlists/app/src/jobs.py`. Deleting this app breaks
  that one loudly (ImportError at startup).
- `transfer` (the old duplicate-on-every-run command) is **gone** — `sync.py` and
  `plexsync.py` are both idempotent-by-name wrappers.
