#!/usr/bin/env python3
"""filesync.py — import playlist FILES into Navidrome, matched against your library.

For playlists that arrive as files rather than as a Spotify link — an iTunes/Music
export someone shares with you, an .m3u from another server, and so on. Uses the
same matcher as plexsync.py (matcher.py), so remaster/feat/edit noise and accents
do not cost you matches.

Supported inputs (pass files, directories, or both):
    *.txt         iTunes/Music "Export Playlist..." tab-separated export.
                  RICHEST format — has every track, including streaming-only ones.
    *.xml         iTunes/Music "Export Library/Playlist..." plist. Also complete;
                  may contain SEVERAL playlists, each imported under its own name.
    *.m3u/.m3u8   #EXTINF playlists. NOTE: an iTunes .m3u only lists tracks that
                  exist as local FILES, so streaming-only tracks are silently
                  missing from it. Prefer the .txt/.xml when you have one.
    *.csv         Spotify exports from exportify.net (and most other CSV
                  exporters). Columns are matched by NAME, not position, and the
                  delimiter is sniffed — exportify localises its headers, so a
                  Swedish export says "Låtens namn" where an English one says
                  "Track Name", and Excel in a Swedish locale writes ; not ,.

Given a DIRECTORY, the same playlist exported in several formats is imported ONCE:
the richest available format per basename wins (txt > csv > xml > m3u8 > m3u).

Usage (inside the persistent tunesynctool pod, venv python):
    kubectl exec -i -n media deploy/tunesynctool -- \
        /work/venv/bin/python /scripts/filesync.py /work/import [--preview]

    filesync.py <path> [<path>...]        # files and/or directories
      --preview          match only, write NOTHING (prints the miss list)
      --name "X"         override the playlist name (single-playlist input only)
      --public           make the playlist visible to all Navidrome users (default)
      --private          keep it to the syncing user only
      --mirror           make the playlist EXACTLY the file (removes extra tracks);
                         default is additive — only missing tracks are added
      --prefix "X "      prepend a string to every imported playlist name

Idempotent by NAME: an existing playlist owned by ND_USER with that name is updated
in place; otherwise it is created. Re-running is safe.

Env (injected by the Deployment):
    ND_USER, ND_PASS    Navidrome credentials (from 1Password `navidrome`)
    NAVIDROME_URL       default http://navidrome.media.svc.cluster.local
    NAVIDROME_PORT      default 4533
"""
import csv
import hashlib
import io
import json
import os
import plistlib
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matcher import add_to_index, find_match  # noqa: E402

NAV_URL = os.environ.get("NAVIDROME_URL", "http://navidrome.media.svc.cluster.local")
NAV_PORT = os.environ.get("NAVIDROME_PORT", "4533")
CLIENT = "tunesync-filesync-py"
API_VERSION = "1.16.1"
PAGE = 500

# Column headers in iTunes exports, English and Swedish.
COL_NAME = ("Name", "Namn")
COL_ARTIST = ("Artist",)
COL_ALBUM_ARTIST = ("Album Artist", "Albumartist", "Albumartist")
COL_ALBUM = ("Album",)
COL_TIME = ("Time", "Tid")

FORMAT_RANK = {".txt": 0, ".csv": 1, ".xml": 2, ".m3u8": 3, ".m3u": 4}


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def pick(row, names):
    for n in names:
        if n in row and (row[n] or "").strip():
            return row[n].strip()
    return ""


# ---------- Subsonic ----------

def _auth():
    for var in ("ND_USER", "ND_PASS"):
        if not os.environ.get(var):
            die(f"{var} is not set")
    user, password = os.environ["ND_USER"], os.environ["ND_PASS"]
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {"u": user, "t": token, "s": salt,
            "v": API_VERSION, "c": CLIENT, "f": "json"}


def call(endpoint, params=None, multi=None):
    """Subsonic call over POST (keeps long songId lists out of the URL)."""
    fields = list(_auth().items()) + list((params or {}).items())
    for key, values in (multi or {}).items():
        fields.extend((key, v) for v in values)
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{NAV_URL}:{NAV_PORT}/rest/{endpoint}", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.loads(r.read().decode())["subsonic-response"]
    except urllib.error.URLError as e:
        die(f"could not reach Navidrome at {NAV_URL}:{NAV_PORT}: {e}")
    except (ValueError, KeyError) as e:
        die(f"unexpected reply from Navidrome {endpoint}: {e}")
    if payload.get("status") != "ok":
        err = payload.get("error", {})
        die(f"Navidrome {endpoint} failed: {err.get('message', payload)}")
    return payload


def load_library_index():
    """Page the whole library once via search3 (empty query = match all).

    Returns (by_title, title_keys, count, meta) where meta maps a song id to the
    library's own artist/title/album, so --preview can show WHAT each source track
    matched to — a wrong match is worse than a miss, so it has to be inspectable.
    """
    by_title, offset, seen, meta = {}, 0, 0, {}
    while True:
        res = call("search3", {
            "query": "", "songCount": PAGE, "songOffset": offset,
            "artistCount": 0, "albumCount": 0,
        })
        songs = res.get("searchResult3", {}).get("song", [])
        if not songs:
            break
        for s in songs:
            seen += 1
            artists = [s.get("artist"), s.get("displayAlbumArtist"), s.get("albumArtist")]
            add_to_index(by_title, s.get("title"), artists,
                         int(s.get("duration") or 0) * 1000, s.get("id"))
            meta[s.get("id")] = {
                "artist": s.get("artist") or s.get("displayAlbumArtist") or "?",
                "title": s.get("title") or "?",
                "album": s.get("album") or "",
            }
        offset += len(songs)
        if len(songs) < PAGE:
            break
    return by_title, list(by_title.keys()), seen, meta


def find_playlist(name):
    """Existing playlist with this name owned by ND_USER, else None."""
    me = os.environ["ND_USER"]
    playlists = call("getPlaylists").get("playlists", {}).get("playlist", [])
    mine = [p for p in playlists if (p.get("owner") or me) == me]
    for pool in (mine, playlists):
        for p in pool:
            if p.get("name") == name:
                return p
        for p in pool:
            if (p.get("name") or "").lower() == name.lower():
                return p
    return None


# ---------- parsers ----------

def parse_itunes_txt(path):
    """iTunes 'Export Playlist...' — tab-separated, UTF-8 or UTF-16."""
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "latin-1"):
        try:
            candidate = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if "\t" in candidate.split("\n", 1)[0]:
            text = candidate
            break
    if text is None:
        die(f"{path}: not a tab-separated iTunes export")
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    tracks = []
    for row in rows:
        name = pick(row, COL_NAME)
        if not name:
            continue
        try:
            dur = int(float(pick(row, COL_TIME) or 0)) * 1000
        except ValueError:
            dur = 0
        artists = [a for a in (pick(row, COL_ARTIST), pick(row, COL_ALBUM_ARTIST)) if a]
        tracks.append({"name": name, "artists": artists,
                       "album": pick(row, COL_ALBUM), "dur": dur})
    return [(os.path.splitext(os.path.basename(path))[0], tracks)]


def parse_itunes_xml(path):
    """iTunes plist export. Returns every playlist it contains."""
    with open(path, "rb") as f:
        plist = plistlib.load(f)
    raw_tracks = plist.get("Tracks") or {}

    def to_track(entry):
        name = (entry.get("Name") or "").strip()
        if not name:
            return None
        artists = [a for a in (entry.get("Artist"), entry.get("Album Artist")) if a]
        return {"name": name, "artists": artists,
                "album": entry.get("Album") or "",
                "dur": int(entry.get("Total Time") or 0)}

    out = []
    for pl in plist.get("Playlists") or []:
        if pl.get("Master") or pl.get("Distinguished Kind"):
            continue
        items = pl.get("Playlist Items") or []
        tracks = []
        for it in items:
            entry = raw_tracks.get(str(it.get("Track ID")))
            track = to_track(entry) if entry else None
            if track:
                tracks.append(track)
        if tracks:
            out.append((pl.get("Name") or os.path.splitext(os.path.basename(path))[0], tracks))
    if not out:  # a library-only export with no playlist section
        tracks = [t for t in (to_track(e) for e in raw_tracks.values()) if t]
        if tracks:
            out.append((os.path.splitext(os.path.basename(path))[0], tracks))
    return out


def parse_m3u(path):
    """#EXTINF playlists. iTunes writes '#EXTINF:<secs>,<Title> - <Artist>'."""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        die(f"{path}: could not decode")
    tracks = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#EXTINF:"):
            continue
        meta = line[len("#EXTINF:"):]
        secs, _, label = meta.partition(",")
        try:
            dur = int(float(secs.strip())) * 1000
        except ValueError:
            dur = 0
        title, sep, artist = label.rpartition(" - ")
        if not sep:  # no separator: treat the whole label as the title
            title, artist = label, ""
        tracks.append({"name": title.strip(), "artists": [artist.strip()] if artist.strip() else [],
                       "album": "", "dur": dur})
    return [(os.path.splitext(os.path.basename(path))[0], tracks)]


# CSV column names, in the order they are tried. exportify.net localises its
# headers, so the Swedish and English variants both have to be recognised. Kept as
# lowercase for casefolded comparison.
_CSV_TITLE = ("track name", "låtens namn", "title", "titel", "name", "namn", "song", "låt")
_CSV_ARTIST = ("artist name(s)", "artistens namn", "artist name", "artist(s)",
               "artist", "artists", "artister", "artistnamn")
_CSV_ALBUM_ARTIST = ("album artist name(s)", "albumartistens namn", "album artist",
                     "albumartist")
_CSV_ALBUM = ("album name", "albumets namn", "album")
_CSV_DUR = ("track duration (ms)", "låtlängd (ms)", "duration (ms)", "duration_ms",
            "duration", "längd", "time", "tid")


def _sniff_delimiter(header_line):
    """Comma, semicolon or tab — whichever the header row actually uses.

    Excel in a Swedish locale writes ; and calls it CSV, so this cannot be assumed.
    """
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _resolve_column(headers, candidates, claimed):
    """Pick the column for one field: exact match first, then a prefix match.

    Two-pass and claim-aware on purpose. A Swedish exportify export has BOTH
    "Låtens namn" and "Artistens namn", so a naive substring match on "namn" would
    hand the artist column to the title field.
    """
    normed = {i: (h or "").strip().casefold() for i, h in enumerate(headers)}
    for cand in candidates:
        for i, h in normed.items():
            if i not in claimed and h == cand:
                claimed.add(i)
                return headers[i]
    for cand in candidates:
        for i, h in normed.items():
            if i not in claimed and h and (h.startswith(cand) or cand in h):
                claimed.add(i)
                return headers[i]
    return None


def parse_csv(path):
    """Spotify/exportify-style CSV. Columns matched by NAME, never by position."""
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            candidate = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        first = candidate.split("\n", 1)[0]
        if any(d in first for d in (",", ";", "\t")):
            text = candidate
            break
    if text is None:
        die(f"{path}: could not read as CSV")

    delimiter = _sniff_delimiter(text.split("\n", 1)[0])
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        die(f"{path}: empty CSV")
    headers = rows[0]

    claimed = set()
    col_title = _resolve_column(headers, _CSV_TITLE, claimed)
    col_artist = _resolve_column(headers, _CSV_ARTIST, claimed)
    col_album_artist = _resolve_column(headers, _CSV_ALBUM_ARTIST, claimed)
    col_album = _resolve_column(headers, _CSV_ALBUM, claimed)
    col_dur = _resolve_column(headers, _CSV_DUR, claimed)
    if not col_title:
        die(f"{path}: no recognisable track-title column in {headers[:8]}")

    idx = {h: i for i, h in enumerate(headers)}
    # "(ms)" in the header is exportify's own unit marker; iTunes-style columns are
    # seconds. Guessing from the magnitude would break on very long tracks.
    dur_in_ms = bool(col_dur) and "ms" in col_dur.strip().casefold()

    tracks = []
    for row in rows[1:]:
        if not row or len(row) <= idx[col_title]:
            continue

        def cell(col):
            if not col:
                return ""
            i = idx[col]
            return row[i].strip() if i < len(row) else ""

        name = cell(col_title)
        if not name:
            continue
        try:
            dur_raw = float(cell(col_dur) or 0)
        except ValueError:
            dur_raw = 0
        dur = int(dur_raw) if dur_in_ms else int(dur_raw) * 1000
        artists = [a for a in (cell(col_artist), cell(col_album_artist)) if a]
        tracks.append({"name": name, "artists": artists,
                       "album": cell(col_album), "dur": dur})
    if not tracks:
        die(f"{path}: found the columns but no rows with a track title")
    return [(os.path.splitext(os.path.basename(path))[0], tracks)]


PARSERS = {".txt": parse_itunes_txt, ".xml": parse_itunes_xml,
           ".m3u": parse_m3u, ".m3u8": parse_m3u,
           ".csv": parse_csv}


def collect_inputs(paths):
    """Expand files/dirs into a list of playlist files, richest format per basename."""
    found = []
    for p in paths:
        if os.path.isdir(p):
            for entry in sorted(os.listdir(p)):
                full = os.path.join(p, entry)
                if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in PARSERS:
                    found.append(full)
        elif os.path.isfile(p):
            if os.path.splitext(p)[1].lower() not in PARSERS:
                die(f"{p}: unsupported extension (want .txt/.xml/.m3u/.m3u8)")
            found.append(p)
        else:
            die(f"{p}: no such file or directory")
    if not found:
        die("no playlist files found in the given path(s)")

    best = {}
    for full in found:
        stem = os.path.splitext(os.path.basename(full))[0]
        ext = os.path.splitext(full)[1].lower()
        rank = FORMAT_RANK.get(ext, 99)
        if stem not in best or rank < best[stem][0]:
            best[stem] = (rank, full)
    chosen = sorted(v[1] for v in best.values())
    for full in found:
        if full not in chosen:
            print(f"  skip {os.path.basename(full)} "
                  f"(same playlist as {os.path.basename(best[os.path.splitext(os.path.basename(full))[0]][1])})")
    return chosen


# ---------- main ----------

def label(t):
    artist = ", ".join(t["artists"]) if t["artists"] else "?"
    return f"{artist} - {t['name']}"


def main():
    args = sys.argv[1:]
    preview = "--preview" in args
    mirror = "--mirror" in args
    public = "--private" not in args
    name_override = None
    prefix = ""
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--name":
            i += 1
            name_override = args[i] if i < len(args) else die("--name needs a value")
        elif a == "--prefix":
            i += 1
            prefix = args[i] if i < len(args) else die("--prefix needs a value")
        elif a in ("--preview", "--mirror", "--public", "--private"):
            pass
        elif a.startswith("--"):
            die(f"unknown flag {a}")
        else:
            positional.append(a)
        i += 1
    if not positional:
        die(f"usage: {os.path.basename(sys.argv[0])} <path> [<path>...] "
            "[--preview] [--name X] [--prefix X] [--private] [--mirror]")

    files = collect_inputs(positional)
    playlists = []
    for full in files:
        parser = PARSERS[os.path.splitext(full)[1].lower()]
        for pl_name, tracks in parser(full):
            if tracks:
                playlists.append((pl_name, tracks, full))
    if not playlists:
        die("parsed 0 playlists with tracks")
    if name_override and len(playlists) != 1:
        die(f"--name given but the input holds {len(playlists)} playlists")

    print(f"loading Navidrome library index from {NAV_URL}:{NAV_PORT} ...")
    by_title, title_keys, count, meta = load_library_index()
    print(f"indexed {count} tracks under {len(title_keys)} distinct titles\n")

    grand_matched = grand_total = 0
    for pl_name, tracks, src in playlists:
        target = prefix + (name_override or pl_name)
        print(f"=== {target}  ({len(tracks)} tracks from {os.path.basename(src)}) ===")
        matched_ids, misses, pairs = [], [], []
        for t in tracks:
            ident = find_match(by_title, title_keys, t)
            if ident:
                if ident not in matched_ids:
                    matched_ids.append(ident)
                pairs.append((t, ident))
            else:
                misses.append(t)
        grand_matched += len(matched_ids)
        grand_total += len(tracks)
        pct = (100.0 * len(matched_ids) / len(tracks)) if tracks else 0.0
        print(f"matched {len(matched_ids)}/{len(tracks)} ({pct:.0f}%)")
        if preview:
            for t, ident in pairs:
                m = meta.get(ident, {})
                album = f"  [{m.get('album')}]" if m.get("album") else ""
                print(f"  OK    {label(t)}\n          -> {m.get('artist')} - {m.get('title')}{album}")
        for t in misses:
            print(f"  MISS  {label(t)}")

        if preview:
            print("(--preview: nothing written)\n")
            continue
        if not matched_ids:
            print("nothing matched — not creating an empty playlist\n")
            continue

        existing = find_playlist(target)
        if existing:
            pid = existing["id"]
            have = [s["id"] for s in
                    call("getPlaylist", {"id": pid}).get("playlist", {}).get("entry", [])]
            params = {"playlistId": pid, "public": "true" if public else "false"}
            multi = {}
            if mirror:
                extra = [i for i, sid in enumerate(have) if sid not in matched_ids]
                multi["songIndexToRemove"] = [str(i) for i in sorted(extra, reverse=True)]
                multi["songIdToAdd"] = [s for s in matched_ids if s not in have]
                print(f"updating in place: +{len(multi['songIdToAdd'])} "
                      f"-{len(multi['songIndexToRemove'])} (mirror)")
            else:
                multi["songIdToAdd"] = [s for s in matched_ids if s not in have]
                print(f"updating in place: +{len(multi['songIdToAdd'])} new "
                      f"({len(have)} already there)")
            call("updatePlaylist", params, multi)
        else:
            res = call("createPlaylist", {"name": target}, {"songId": matched_ids})
            pid = (res.get("playlist") or {}).get("id")
            if pid:
                call("updatePlaylist", {"playlistId": pid,
                                        "public": "true" if public else "false"})
            print(f"created with {len(matched_ids)} tracks (id {pid})")
        print(f"{'public' if public else 'private'}\n")

    if len(playlists) > 1:
        pct = (100.0 * grand_matched / grand_total) if grand_total else 0.0
        print(f"TOTAL matched {grand_matched}/{grand_total} ({pct:.0f}%) "
              f"across {len(playlists)} playlists")


if __name__ == "__main__":
    main()
