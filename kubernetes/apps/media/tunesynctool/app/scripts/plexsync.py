#!/usr/bin/env python3
"""plexsync.py — sync a Spotify playlist into a Plex playlist, matching SMARTER
than tunesynctool by normalizing remaster / edit / feat suffixes before matching.

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
import re
import sys
import unicodedata

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from plexapi.server import PlexServer

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

# Trailing "- <noise>" clauses (optionally wrapping a year): "- Remastered 2011",
# "- 2004 Remaster", "- Radio Edit", "- Single Version", "- Live", "- Mono", ...
_DASH_SUFFIX = re.compile(
    r"\s*-\s*(?:\d{4}\s+)?"
    r"(?:remaster(?:ed)?(?:\s+version)?|mono(?:\s+version)?|stereo(?:\s+version)?|"
    r"radio\s+edit|single\s+version|album\s+version|re-?recorded|"
    r"acoustic(?:\s+version)?|live(?:\b.*)?|demo|edit|version|"
    r"deluxe(?:\b.*)?|bonus\s+track|explicit|clean)"
    r"(?:\s+\d{4})?\s*$",
    re.IGNORECASE,
)
# Parenthetical / bracketed "(feat. X)" / "(with X)".
_PAREN_FEAT = re.compile(
    r"\s*[\(\[]\s*(?:feat|ft|featuring|with)\.?\s+[^)\]]*[\)\]]",
    re.IGNORECASE,
)
# Parenthetical / bracketed remaster/version noise "(2011 Remaster)", "(Radio Edit)".
_PAREN_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*?"
    r"(?:remaster(?:ed)?|mono|stereo|radio\s+edit|single\s+version|album\s+version|"
    r"acoustic|live|demo|version|deluxe|bonus)"
    r"[^)\]]*[\)\]]",
    re.IGNORECASE,
)
# Leading "feat"/"with" split points inside an artist string.
_ARTIST_SPLIT = re.compile(r"\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|,|&|/", re.IGNORECASE)


def die(msg, code=1):
    print("ERROR: " + scrub(str(msg)), file=sys.stderr)
    sys.exit(code)


def scrub(msg):
    """Never leak the Plex token in echoed output / error strings."""
    if PLEX_TOKEN:
        return msg.replace(PLEX_TOKEN, "***")
    return msg


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def strip_suffixes(title):
    t = title or ""
    prev = None
    while prev != t:
        prev = t
        t = _PAREN_FEAT.sub("", t)
        t = _PAREN_NOISE.sub("", t)
        t = _DASH_SUFFIX.sub("", t)
    return t.strip()


def norm_title(s):
    s = fold(strip_suffixes(s)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_artist(s):
    s = fold(s or "").lower()
    s = _ARTIST_SPLIT.split(s)[0]
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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

def find_match(section, sp_track):
    """Return the best-matching Plex track object for a Spotify track, or None."""
    core = strip_suffixes(sp_track["name"]).strip()
    target_title = norm_title(sp_track["name"])
    target_artists = {norm_artist(a) for a in sp_track["artists"] if a}

    queries = []
    if core:
        queries.append(core)
    # Fallback: distinctive words (>=4 chars) so we still find it if the exact
    # core substring differs (punctuation etc.).
    for w in [w for w in re.split(r"\W+", fold(core)) if len(w) >= 4][:2]:
        queries.append(w)

    seen, candidates = set(), []
    for q in queries:
        try:
            hits = section.searchTracks(title__icontains=q, maxresults=80)
        except Exception:
            hits = []
        for h in hits:
            if h.ratingKey in seen:
                continue
            seen.add(h.ratingKey)
            candidates.append(h)

    best, best_score = None, None
    for c in candidates:
        c_title = norm_title(c.title)
        if not c_title or not target_title:
            continue
        title_ok = (c_title == target_title
                    or target_title in c_title
                    or c_title in target_title)
        if not title_ok:
            continue
        c_album_artist = norm_artist(getattr(c, "grandparentTitle", "") or "")
        c_track_artist = norm_artist(getattr(c, "originalTitle", "") or "")
        artist_ok = False
        for ta in target_artists:
            if not ta:
                continue
            for ca in (c_album_artist, c_track_artist):
                if ca and (ta == ca or ta in ca or ca in ta):
                    artist_ok = True
                    break
            if artist_ok:
                break
        try:
            ddur = abs((c.duration or 0) - sp_track["dur"])
        except Exception:
            ddur = 10 ** 9
        exact_title = 1 if c_title == target_title else 0
        # rank: artist match first, then exact-title, then closest duration
        score = (1 if artist_ok else 0, exact_title, -ddur)
        if best_score is None or score > best_score:
            best, best_score = c, score

    if best is None:
        return None
    artist_ok, exact_title, neg_ddur = best_score
    # Accept only a real match: artist confirmed, OR exact title within ~3s.
    if artist_ok:
        return best
    if exact_title and (-neg_ddur) <= 3000:
        return best
    return None


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
    print(f"Spotify playlist {sp_name!r}: {len(sp_tracks)} tracks")

    try:
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    except Exception as e:
        die(f"could not connect to Plex at {PLEX_URL}: {e}")
    try:
        section = plex.library.section(PLEX_LIBRARY)
    except Exception as e:
        die(f"no Plex library section named {PLEX_LIBRARY!r}: {e}")
    if section.type != "artist":
        die(f"section {PLEX_LIBRARY!r} is type {section.type!r}, expected 'artist' (music)")

    matched, matched_keys, missed = [], set(), []
    for t in sp_tracks:
        hit = find_match(section, t)
        if hit is not None:
            if hit.ratingKey not in matched_keys:
                matched_keys.add(hit.ratingKey)
                matched.append(hit)
        else:
            missed.append(t)

    print(f"\nMATCHED {len(matched)} / {len(sp_tracks)}   MISSED {len(missed)}")
    if missed:
        print("\nMissed (not found in Plex):")
        for t in missed:
            print(f"  - {label(t)}")

    if preview:
        print("\n--preview: no changes written.")
        return

    if not matched:
        print("\nNothing matched — not creating/updating the Plex playlist.")
        return

    existing = get_playlist(plex, plex_name)
    if existing is None:
        plex.createPlaylist(plex_name, items=matched)
        print(f"\nCREATED Plex playlist {plex_name!r} with {len(matched)} tracks.")
    else:
        present = {i.ratingKey for i in existing.items()}
        to_add = [t for t in matched if t.ratingKey not in present]
        if to_add:
            existing.addItems(to_add)
            print(f"\nUPDATED Plex playlist {plex_name!r}: added {len(to_add)} "
                  f"track(s) (was {len(present)}, now {len(present) + len(to_add)}).")
        else:
            print(f"\nUP TO DATE: Plex playlist {plex_name!r} already has all "
                  f"{len(matched)} matched tracks.")


if __name__ == "__main__":
    main()
