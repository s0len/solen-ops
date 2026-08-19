#!/usr/bin/env python3
"""plexsync.py — sync a Spotify playlist into a Plex playlist, matching SMARTER
than tunesynctool by normalizing remaster / edit / feat suffixes before matching.

The matching itself lives in matcher.py, shared with filesync.py.

This catches the tracks tunesynctool misses because Spotify tags them with noise
your local library does not carry, e.g.:
    "Bohemian Rhapsody - Remastered 2011"      -> "Bohemian Rhapsody"
    "The Chain - 2004 Remaster"                -> "The Chain"
    "Song (feat. Someone)"                     -> "Song"
    "Song - Radio Edit" / "- Single Version"   -> "Song"

Usage (inside the persistent tunesynctool pod, venv python):
    kubectl exec -it -n media deploy/tunesynctool -- \
        /work/venv/bin/python /scripts/plexsync.py <spotify_playlist_id> "<plex_playlist_name>" [--preview]

Idempotent by NAME:
    - a Plex playlist with that name exists -> update it (add only the missing matched tracks)
    - it does not exist                     -> create it from the matched tracks
    --preview matches only and writes NOTHING.

Env (injected by the Deployment):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET   # Spotify app creds (from Secret)
    PLEX_TOKEN                                 # Plex token (from Secret) — never printed
    PLEX_URL                                   # default http://plex.media.svc.cluster.local:32400
    PLEX_LIBRARY                               # music section title, default "Musik"
    SCOPES, REDIRECT                           # Spotify OAuth (cache reused, no re-auth)
    TUNESYNC_WORKDIR                           # default /work (holds .cache)
"""
import os
import sys
import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from plexapi.server import PlexServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import add_to_index, find_match  # noqa: E402

WORKDIR = os.environ.get("TUNESYNC_WORKDIR", "/work")
CACHE = os.path.join(WORKDIR, ".cache")
SCOPES = os.environ.get(
    "SCOPES",
    "user-library-read,playlist-read-private,playlist-read-collaborative,"
    "playlist-modify-public,playlist-modify-private",
)
REDIRECT = os.environ.get("REDIRECT", "http://127.0.0.1:8888/callback")
PLEX_URL = os.environ.get("PLEX_URL", "http://plex.media.svc.cluster.local:32400")
PLEX_LIBRARY = os.environ.get("PLEX_LIBRARY", "Musik")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

def die(msg, code=1):
    print("ERROR: " + scrub(str(msg)), file=sys.stderr)
    sys.exit(code)


def scrub(msg):
    """Never leak the Plex token in echoed output / error strings."""
    if PLEX_TOKEN:
        return msg.replace(PLEX_TOKEN, "***")
    return msg


# ---------- Spotify ----------

def spotify_client():
    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        if not os.environ.get(var):
            die(f"{var} is not set")
    if not os.path.exists(CACHE):
        die(f"no primed Spotify token at {CACHE} — run prime.py first")
    oa = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=REDIRECT,
        scope=SCOPES,
        open_browser=False,
        cache_path=CACHE,
    )
    return spotipy.Spotify(auth_manager=oa)


def spotify_playlist_name(sp, pid):
    try:
        return sp.playlist(pid, fields="name").get("name", pid)
    except Exception:
        return pid


def spotify_tracks(sp, pid):
    tracks = []
    res = sp.playlist_items(pid, additional_types=("track",), limit=100)
    while res:
        for it in res.get("items", []):
            tr = it.get("track")
            if not tr or tr.get("is_local") or tr.get("type") != "track":
                continue
            tracks.append({
                "name": tr.get("name", ""),
                "artists": [a["name"] for a in tr.get("artists", []) if a.get("name")],
                "album": (tr.get("album") or {}).get("name", ""),
                "dur": tr.get("duration_ms") or 0,
            })
        if res.get("next"):
            res = sp.next(res)
        else:
            break
    return tracks


# ---------- Plex matching ----------

def load_plex_index(plex, section):
    """Pull the whole music section ONCE into an in-memory index.

    Returns (by_title, title_keys, count):
      by_title   : {normalized_title -> [(ntitle, {norm_artists}, dur_ms, ratingKey)]}
      title_keys : list of the distinct normalized titles (the keys of by_title)
      count      : number of indexed tracks

    We read the raw /library/sections/<key>/all container in big pages instead of
    building heavy plexapi Track objects for all ~16k tracks — the listing already
    carries title / grandparentTitle (album artist) / originalTitle (track artist,
    only when it differs) / duration / ratingKey, which is everything matching needs.
    Matched ratingKeys are resolved to real Track objects later, on demand.
    """
    key = section.key
    by_title = {}
    page = 2000
    start = 0
    total = None
    while True:
        data = plex.query(
            f"/library/sections/{key}/all?type=10"
            f"&X-Plex-Container-Start={start}&X-Plex-Container-Size={page}"
        )
        if total is None:
            total = int(data.attrib.get("totalSize") or 0)
        batch = list(data)
        for el in batch:
            a = el.attrib
            try:
                dur = int(a.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            add_to_index(by_title, a.get("title"),
                         (a.get("grandparentTitle"), a.get("originalTitle")),
                         dur, a.get("ratingKey"))
        if not batch or start + page >= total:
            break
        start += page
    count = sum(len(v) for v in by_title.values())
    return by_title, list(by_title.keys()), count


def resolve_tracks(plex, keys):
    """Resolve matched ratingKeys to real plexapi Track objects in one batched call."""
    if not keys:
        return []
    objs = plex.fetchItems("/library/metadata/" + ",".join(keys))
    by_rk = {str(o.ratingKey): o for o in objs}
    return [by_rk[k] for k in keys if k in by_rk]


def get_playlist(plex, name):
    playlists = plex.playlists()
    for p in playlists:
        if p.title == name:
            return p
    for p in playlists:
        if p.title.lower() == name.lower():
            return p
    return None


def label(t):
    artist = ", ".join(t["artists"]) if t["artists"] else "?"
    return f"{artist} - {t['name']}"


def main():
    args = sys.argv[1:]
    preview = "--preview" in args
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) != 2:
        die(f"usage: {os.path.basename(sys.argv[0])} "
            "<spotify_playlist_id> \"<plex_playlist_name>\" [--preview]")
    spotify_id, plex_name = positional

    if not PLEX_TOKEN:
        die("PLEX_TOKEN is not set")

    sp = spotify_client()
    sp_name = spotify_playlist_name(sp, spotify_id)
    sp_tracks = spotify_tracks(sp, spotify_id)
    print(f"Spotify playlist {sp_name!r}: {len(sp_tracks)} tracks", flush=True)

    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=30)
    except Exception as e:
        die(f"could not connect to Plex at {PLEX_URL}: {e}")
    try:
        section = plex.library.section(PLEX_LIBRARY)
    except Exception as e:
        die(f"no Plex library section named {PLEX_LIBRARY!r}: {e}")
    if section.type != "artist":
        die(f"section {PLEX_LIBRARY!r} is type {section.type!r}, expected 'artist' (music)")

    t0 = time.monotonic()
    print(f"Indexing Plex library {PLEX_LIBRARY!r} ...", flush=True)
    by_title, title_keys, indexed = load_plex_index(plex, section)
    print(f"  indexed {indexed} tracks ({len(title_keys)} distinct titles) "
          f"in {time.monotonic() - t0:.1f}s", flush=True)

    total = len(sp_tracks)
    width = len(str(total))
    matched_keys, matched_set, missed = [], set(), []
    t1 = time.monotonic()
    for i, t in enumerate(sp_tracks, 1):
        rk = find_match(by_title, title_keys, t)
        if rk is not None:
            mark = "✓"  # check
            if rk not in matched_set:
                matched_set.add(rk)
                matched_keys.append(rk)
        else:
            mark = "·"  # middot
            missed.append(t)
        print(f"[{i:>{width}}/{total}] {mark} {label(t)}", flush=True)

    print(f"\nMATCHED {len(matched_keys)} / {total}   MISSED {len(missed)} "
          f"(matched in {time.monotonic() - t1:.1f}s)")
    if missed:
        print("\nMissed (not found in Plex):")
        for t in missed:
            print(f"  - {label(t)}")

    if preview:
        print("\n--preview: no changes written.")
        return

    if not matched_keys:
        print("\nNothing matched — not creating/updating the Plex playlist.")
        return

    matched = resolve_tracks(plex, matched_keys)
    existing = get_playlist(plex, plex_name)
    if existing is None:
        plex.createPlaylist(plex_name, items=matched)
        print(f"\nCREATED Plex playlist {plex_name!r} with {len(matched)} tracks.")
    else:
        present = {str(i.ratingKey) for i in existing.items()}
        to_add = [o for o in matched if str(o.ratingKey) not in present]
        if to_add:
            existing.addItems(to_add)
            print(f"\nUPDATED Plex playlist {plex_name!r}: added {len(to_add)} "
                  f"track(s) (was {len(present)}, now {len(present) + len(to_add)}).")
        else:
            print(f"\nUP TO DATE: Plex playlist {plex_name!r} already has all "
                  f"{len(matched)} matched tracks.")


if __name__ == "__main__":
    main()
