"""Import chart/playlist packs (Apple Music, Spotify, ...) into the beets library.

A chart pack is a torrent of per-country playlist folders holding loose hit songs.
The nightly album-oriented import cannot do anything with it: each folder holds
50-90 tracks from 50-90 different releases, so there is no album to match. The
tags, however, are complete and trustworthy (artist/title/album on 100% of files
in both packs surveyed), and MusicBrainz mostly cannot match the material anyway
-- a 16-track folder cost 397s with autotagging and produced one wrong-but-
confident match, versus 0.7s with autotagging off.

So: stage the wanted files as copies with corrected tags, then let beets move
them in with autotagging off. Staging as copies rather than hardlinks is what
makes it safe to rewrite tags -- the torrent payload is never touched.

The caller intends to DELETE the pack from /data/torrents afterwards, so a track
wrongly judged a duplicate is music lost for good. Dedup is therefore biased
towards importing: it skips only on a primary-artist + title match whose duration
also agrees. A redundant copy in the library is recoverable; a deletion is not.
"""

import argparse
import collections
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata

import mutagen
import mutagen.easyid3

AUDIO = (".mp3", ".flac", ".m4a", ".ogg", ".opus")
LIBRARY = "/config/library.db"
MUSIC_ROOT = "/data/media/musik"

# Bump whenever classify() changes its mind about anything. Cached verdicts
# are keyed on the directory's mtime, and a torrent directory never changes once
# it seeds, so without this a corrected classifier would never revisit a verdict
# it got wrong. Version 1 read the first files in os.walk order and called The
# Beatles' discography a chart pack. Version 2 had no notion of loose tracks and
# called DJ pool packs albums. Version 3 had no notion of collections and would
# have handed a 2,084-album jazz box to beets in four processes.
CLASSIFIER_VERSION = 4

# A version suffix survives in the strict key, so "Style" and "Style (Taylor's
# Version)" stay distinct. The loose key drops it to catch credit shuffles
# ("Money Trees" vs "Money Trees (feat. Jay Rock)"), which are the same recording.
#
# The same master encoded to mp3 and to flac differs by well under a second;
# anything beyond a couple of seconds is a different master or edit. Acoustic
# fingerprinting of the review's disputed pairs put every wrong skip between 2.5
# and 5 seconds, so the window stops short of that -- at the price of copying a
# few genuine duplicates, which costs disk rather than music.
STRICT_TOLERANCE = 2.0
LOOSE_TOLERANCE = 1.0

# A song the library already holds is not automatically unwanted: a 128 kbps rip
# is better than silence, but it should give way once a better one turns up. So a
# match is not the end of the decision, only the start of a comparison.
#
# A lossless file is never replaced. Two lossless copies of one album differing in
# resolution turned out to be different transfers, not a better and a worse copy,
# so "higher numbers" does not mean "better master" and swapping one for the other
# would be a coin flip dressed up as an upgrade.
#
# Between lossy files the margin exists because VBR headers report a few kbps
# either way for the same encode; without it the same song would churn nightly.
LOSSLESS = {"FLAC", "ALAC", "APE", "WAVPACK", "AIFF", "WAV"}
BITRATE_MARGIN = 32000

# Tighter than the window used to decide "we already have this". Skipping the wrong
# song costs nothing permanent; replacing the wrong file destroys the only copy.
UPGRADE_TOLERANCE = 1.0

EXT_FORMAT = {
    ".mp3": "MP3", ".flac": "FLAC", ".m4a": "AAC", ".ogg": "OGG", ".opus": "Opus",
}


def lossless(fmt):
    return (fmt or "").upper() in LOSSLESS


def better(new_fmt, new_bitrate, old_fmt, old_bitrate):
    """Is the incoming file an audible improvement on what the library holds?"""
    if lossless(old_fmt):
        return False
    if lossless(new_fmt):
        return True
    return (new_bitrate or 0) >= (old_bitrate or 0) + BITRATE_MARGIN


def describe(fmt, bitrate):
    return fmt if lossless(fmt) else f"{fmt} {round((bitrate or 0) / 1000)}k"


def load_state(path):
    """Read the cached verdicts, starting over if the file cannot be trusted.

    Say so out loud when that happens. Rebuilding costs the better part of an hour
    of NFS reads, so a cache that quietly reset itself every night would look like
    nothing worse than a slow job.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        print(f"  VARNING {path} gick inte att läsa ({e}); klassificerar om allt")
        return {}


def save_state(path, state):
    """Write via a sibling so an interrupted run cannot leave a truncated cache.

    The sibling carries the pid. A fixed name meant two runs wrote the same
    temporary file and whichever renamed first left the other renaming something
    that no longer existed -- which is exactly how this failed on 2026-08-22.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        print(f"  VARNING kunde inte spara {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def take_lock(path):
    """Refuse to run while another copy is working on the same library.

    Two concurrent runs corrupt each other: they share the state cache, and they
    would each decide to replace the same library file. The CronJob's
    concurrencyPolicy only guards the CronJob against itself, so a run started by
    hand alongside it needs this. The lock releases itself when the process dies,
    however it dies.
    """
    import fcntl

    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def apply_upgrade(up, lib):
    """Replace one library file with a better encode of the same recording.

    Never writes through the file already in place. The rename is atomic, so a
    crash mid-copy cannot leave a truncated file where music used to be, and if a
    library file ever shares its inode with a seeding torrent again -- imports
    before 2026-08-19 used hardlinks, though a sweep on 2026-08-22 found none left
    -- writing a sibling and renaming over the name drops only the library's link
    rather than corrupting what peers are downloading.

    The library's own tagging is stamped back onto the new file rather than keeping
    the pack's: the pack calls the album whatever the playlist was called, and
    letting that through would tear a track out of the album it belongs to.

    Returns (result, destination) where result is "ok", "borta", or a message.
    """
    old_path = up["old_path"]
    if not os.path.isabs(old_path):
        old_path = os.path.join(MUSIC_ROOT, old_path)
    if not os.path.exists(old_path):
        return "borta", ""

    ext = os.path.splitext(up["src"])[1].lower()
    dest = os.path.splitext(old_path)[0] + ext
    tmp = f"{dest}.upgrade-tmp"
    try:
        shutil.copy2(up["src"], tmp)
        os.replace(tmp, dest)
    except Exception as e:
        for leftover in (tmp,):
            try:
                os.remove(leftover)
            except OSError:
                pass
        return f"kopiering misslyckades: {e}", ""

    # Only once the replacement is safely in place does the old name go, and only
    # when the extension changed so it is a different name.
    if dest != old_path:
        try:
            os.remove(old_path)
        except OSError as e:
            return f"kunde inte ta bort {old_path}: {e}", dest

    try:
        from beets.util import bytestring_path

        it = lib.get_item(up["id"])
        if it is None:
            return "beets-posten finns inte", dest
        it.path = bytestring_path(dest)
        try:
            it.write()
        except Exception as e:
            print(f"  VARNING kunde inte skriva taggar till {dest}: {e}")
        mf = mutagen.File(dest)
        info = getattr(mf, "info", None) if mf is not None else None
        if info is not None:
            it.length = float(getattr(info, "length", it.length) or it.length)
            it.bitrate = int(getattr(info, "bitrate", 0) or 0)
            it.samplerate = int(getattr(info, "sample_rate", 0) or 0)
            it.bitdepth = int(getattr(info, "bits_per_sample", 0) or 0)
        it.format = up["fmt"]
        it.store()
    except Exception as e:
        return f"kunde inte uppdatera beets: {e}", dest
    return "ok", dest

# Chart tags credit every guest in the artist field ("Bad Bunny, Chencho
# Corleone"), which would scatter one album across several artist folders. Split
# on the comma and on explicit featuring markers only: "&", "/" and "x" belong
# inside band names far too often ("ARTIK & ASTI", "Miksu / Macloud", "AC/DC").
SPLIT = re.compile(
    r"\s*(?:,|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b|\bvs\.?\b)\s+",
    re.IGNORECASE,
)
PARENS = re.compile(r"\([^)]*\)|\[[^]]*\]")


def _fold(s):
    """Casefold and drop punctuation while keeping non-Latin scripts intact.

    A [^a-z0-9] filter empties Cyrillic, Khmer, Japanese and Hebrew titles, which
    made every such track collide under one key and silently dropped 74 of them.
    str.isalnum() is Unicode-aware, so the script survives.
    """
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(ch for ch in s if ch.isalnum())


def norm(s):
    """Strict key: version suffixes are significant."""
    return _fold(s)


def norm_loose(s):
    """Loose key: parenthesised credits and remaster notes removed."""
    return _fold(PARENS.sub("", s or ""))


def primary(artist, known=None):
    """The lead act. An artist string the library already knows is left whole."""
    artist = (artist or "").strip()
    if known and norm(artist) in known:
        return artist
    parts = SPLIT.split(artist, maxsplit=1)
    return (parts[0] or artist).strip()


def safe(name):
    """A single path component beets would also accept."""
    name = re.sub(r"[/\x00]", "_", name or "")
    return name.strip(" .") or "Unknown"


def read_tags(path):
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return None
    if f is None:
        return None
    get = lambda k: (f.get(k) or [""])[0]
    info = getattr(f, "info", None)
    length = getattr(info, "length", 0.0) or 0.0
    return {
        "artist": get("artist"),
        "albumartist": get("albumartist"),
        "album": get("album"),
        "title": get("title"),
        "track": get("tracknumber"),
        "date": get("date"),
        "genre": get("genre"),
        "length": float(length),
        "bitrate": int(getattr(info, "bitrate", 0) or 0),
        "format": EXT_FORMAT.get(os.path.splitext(path)[1].lower(), "?"),
    }


def load_library():
    """Two title indexes keyed on the lead artist, plus canonical spellings."""
    db = sqlite3.connect(f"file:{LIBRARY}?mode=ro", uri=True)
    rows = list(
        db.execute(
            "SELECT artist, albumartist, title, length, id, format, bitrate, path FROM items"
        )
    )

    # Full artist strings first, so primary() can recognise a band whose name
    # contains a comma or a featuring word and leave it intact.
    known = {norm(who) for row in rows for who in (row[0], row[1]) if who}
    known.discard("")

    strict = collections.defaultdict(list)
    loose = collections.defaultdict(list)
    spellings = collections.defaultdict(collections.Counter)
    for artist, albumartist, title, length, iid, fmt, bitrate, path in rows:
        for who in (artist, albumartist):
            if not who:
                continue
            lead = primary(who, known)
            key_artist = norm(lead)
            if not key_artist:
                continue
            spellings[key_artist][lead] += 1
            if not title:
                continue
            rec = {
                "len": float(length or 0),
                "id": iid,
                "fmt": fmt or "",
                "bitrate": bitrate or 0,
                "path": os.fsdecode(path) if path else "",
            }
            if norm(title):
                strict[(key_artist, norm(title))].append(rec)
            if norm_loose(title):
                loose[(key_artist, norm_loose(title))].append(rec)
    canonical = {k: c.most_common(1)[0][0] for k, c in spellings.items() if k}

    # Album folders that already exist. A chart track from an album we own must
    # not be dropped into that album's directory: beets' %aunique{} does not
    # disambiguate here (its first key is albumtype, "" on an as-is import versus
    # "album" on the MB-tagged one, so the loop breaks without adding a suffix)
    # and 47 mp3s would have landed among an existing album's flacs.
    albums = {
        (norm(aa), norm(alb))
        for aa, alb in db.execute("SELECT albumartist, album FROM albums")
        if aa and alb
    }
    return strict, loose, canonical, known, albums


def find_existing(strict, loose, key_artist, title, length):
    """The library rows that plainly hold this same recording, best copy first.

    Biased towards returning nothing: a missed duplicate costs disk, while a false
    positive either skips music the library lacks or, worse, overwrites a good file
    with an unrelated one. Returns rows rather than a yes/no so the caller can ask
    the second question -- is what we already have worse than this?
    """
    if not key_artist:
        return []
    hits, seen = [], set()
    t, tl = norm(title), norm_loose(title)
    for key, index, tol, exact in (
        ((key_artist, t), strict, STRICT_TOLERANCE, True),
        ((key_artist, tl), loose, LOOSE_TOLERANCE, False),
    ):
        if not key[1]:
            continue
        for rec in index.get(key, ()):
            delta = abs(rec["len"] - length)
            if delta <= tol and rec["id"] not in seen:
                seen.add(rec["id"])
                # Copied: one row sits in the index under several artist keys.
                hits.append(dict(rec, strict=exact, delta=delta))
    hits.sort(key=lambda r: (lossless(r["fmt"]), r["bitrate"]), reverse=True)
    return hits


COLLECTION_MIN = 4


def distinct_albums(path, per_dir=2, max_dirs=40):
    """How many different albums live under here, at any depth.

    This is what separates a collection from a multi-disc album, and it has to be
    depth-independent: counting subdirectories one level down called The Beatles'
    discography a plain album, because its records sit two levels in behind an
    "Original Masters" folder. Album NAMES do not care how the tree is shaped --
    CD1 and CD2 of one record share one, and a collection has as many as it has
    records.

    Sampling is capped because this walks over NFS and the answer only needs to
    clear a threshold of four, not be exact.
    """
    names = set()
    seen_dirs = 0
    for root, _, files in os.walk(path):
        audio = [f for f in sorted(files) if f.lower().endswith(AUDIO)]
        if not audio:
            continue
        seen_dirs += 1
        if seen_dirs > max_dirs:
            break
        for f in audio[:per_dir]:
            tags = read_tags(os.path.join(root, f))
            if tags and tags["album"]:
                names.add(norm(tags["album"]))
        if len(names) >= COLLECTION_MIN * 3:
            break
    return names


def classify(path, sample=12):
    """Decide whether a torrent directory is an album, a chart pack, or loose tracks.

    A chart pack keeps many releases in ONE directory; a discography keeps one
    release per directory.

    Counting distinct album tags across the whole tree cannot tell those apart --
    a thirteen-album discography has thirteen album tags too. What separates them
    is where the tags sit, so this asks the question one directory at a time: an
    album folder answers with a single album tag, a chart folder with one per
    track.

    Sampling in os.walk order instead classified "The Beatles - Discography
    (1963-2013) [FLAC]" as a chart pack on 2026-08-22, because the walk crossed
    album folders inside the first handful of files and saw eight album names.

    One such directory is not enough to convict the whole torrent, though: a
    discography usually ships a "Singles" folder, and that folder genuinely does
    hold one release per file. So the directories vote by weight of audio files,
    and a torrent is a chart pack only when most of its music sits in many-release
    directories. BTS's discography has fifteen singles against two hundred album
    tracks; the packs have nothing but chart folders.

    A directory whose children are themselves many DIFFERENT albums is a fourth
    thing: a collection. One beets process per top-level directory was the fix for
    an OOM, but a 2,084-album jazz box in four directories turns that fix back into
    the bug -- so a collection is imported one child at a time instead.

    A directory whose files carry no album tag at ALL is a third thing: loose
    tracks. Counting distinct album tags cannot see it, because zero tags yields
    zero distinct tags and reads exactly like a single-album folder. DJ pool packs
    are shaped this way -- artist and title on every file, album on none -- and
    calling them albums fed 9,684 files through the album importer one folder at a
    time.

    Reads stop as soon as the remaining files cannot reach the threshold, which
    keeps the cost near seven tag reads for an ordinary album folder rather than
    the full sample; this walks the whole tree over NFS. The early exit needs at
    least one album tag first, or an untagged folder would bail before proving it.
    """
    chart_files = album_files = loose_files = 0
    for root, _, names in os.walk(path):
        audio = [n for n in sorted(names) if n.lower().endswith(AUDIO)]
        if not audio:
            continue
        if len(audio) < sample:
            # Too few to judge, and a chart folder is never this small.
            album_files += len(audio)
            continue
        albums, tagged, checked = set(), 0, 0
        for n in audio[:sample]:
            tags = read_tags(os.path.join(root, n))
            checked += 1
            if tags and tags["album"]:
                albums.add(norm(tags["album"]))
                tagged += 1
            if tagged and len(albums) + (sample - checked) <= sample // 2:
                break
        if not tagged:
            loose_files += len(audio)
        elif len(albums) > sample // 2:
            chart_files += len(audio)
        else:
            album_files += len(audio)
    if loose_files > chart_files and loose_files > album_files:
        return "loose"
    if chart_files > album_files:
        return "chartpack"
    if len(distinct_albums(path)) >= COLLECTION_MIN:
        return "collection"
    return "album"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("packs", nargs="*", help="pack directories (default: autodetect)")
    ap.add_argument("--src", default="/data/torrents/music")
    ap.add_argument("--staging", default="/data/staging/chartpack-import")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--manifest",
        default="/config/chartpack-manifest.tsv",
        help="record of source -> staged, so the deletion of the packs can be audited",
    )
    ap.add_argument(
        "--upgrade-log",
        default="/config/chartpack-upgrades.tsv",
        help="record of which library files were replaced and by what",
    )
    ap.add_argument(
        "--no-upgrade",
        action="store_true",
        help="only import songs the library lacks; leave existing files alone",
    )
    ap.add_argument(
        "--state",
        default="/config/chartpack-state.json",
        help="cached album/chartpack verdict per directory, keyed on its mtime",
    )
    ap.add_argument(
        "--reimport",
        action="store_true",
        help="process packs already recorded as imported",
    )
    ap.add_argument(
        "--quiet-minutes",
        type=int,
        default=30,
        help="leave a directory alone until it has been unchanged this long",
    )
    ap.add_argument(
        "--lock",
        default="/config/chartpack.lock",
        help="refuse to run while another copy holds this",
    )
    args = ap.parse_args()

    lock = take_lock(args.lock)
    if lock is None:
        print(f"en annan körning håller {args.lock} - avbryter")
        return 0

    packs = args.packs
    state = {}
    if not packs:
        # Classifying by tags means reading files over NFS, and a full sweep of
        # this tree took over twenty minutes of blocked I/O for 666 directories.
        # A torrent directory is immutable once seeded, so its verdict can never
        # change: cache it against the directory's mtime and the nightly run only
        # pays for whatever actually arrived that day.
        state = load_state(args.state)
        fresh = stale = 0
        settling = []
        packs = []
        for d in sorted(os.listdir(args.src)):
            full = os.path.join(args.src, d)
            if not os.path.isdir(full):
                continue
            try:
                mtime = os.stat(full).st_mtime
            except OSError:
                continue
            # A torrent lands here by being moved out of /data/torrents/temp, and
            # that move is atomic per file rather than per directory. Classifying
            # or staging a directory still being filled would read half a pack --
            # the album loop already waits this out, and with autobrr feeding packs
            # in unattended there is nothing to notice it if this one does not.
            if time.time() - mtime < args.quiet_minutes * 60:
                settling.append(d)
                continue
            rec = state.get(d)
            if not rec or rec.get("mtime") != mtime or rec.get("v") != CLASSIFIER_VERSION:
                entry = {"mtime": mtime, "v": CLASSIFIER_VERSION, "kind": classify(full)}
                # Carry the import mark across a classifier bump. It records what was
                # already staged, which has nothing to do with how the directory is
                # classified -- dropping it on the v3-to-v4 bump sent all 98 chart
                # packs back through staging for nothing.
                if rec and rec.get("mtime") == mtime and rec.get("imported"):
                    entry["imported"] = rec["imported"]
                rec = entry
                state[d] = rec
                stale += 1
                # Checkpoint as we go. The first sweep of this tree took over
                # twenty minutes of blocked NFS reads, and saving only at the end
                # means a pod eviction or a deadline throws all of it away and
                # tomorrow starts from nothing.
                if stale % 25 == 0:
                    save_state(args.state, state)
            else:
                fresh += 1
            if rec["kind"] != "chartpack":
                continue
            if rec.get("imported") and not args.reimport:
                continue
            packs.append(full)
        save_state(args.state, state)
        print(f"klassificerade: {stale} nya/ändrade, {fresh} ur cache ({args.state})")
        if settling:
            print(f"väntar (ändrade senaste {args.quiet_minutes} min): {len(settling)}")
            for d in settling[:5]:
                print(f"  {d}")

    if not packs:
        print("inga chartpaket att behandla")
        return 0
    print(f"paket: {len(packs)}")
    for p in packs:
        print(f"  {p}")

    strict, loose, canonical, known, lib_albums = load_library()
    print(f"biblioteksindex: {len(strict)} strikta, {len(loose)} lösa (artist, titel)-par")
    print(f"befintliga album: {len(lib_albums)}")

    # Same shape as the library index so an intra-pack collapse also has to agree
    # on duration. Without that, four different recordings of one title became
    # whichever os.walk happened to reach first -- in testing, a remix.
    manifest = []
    seen = collections.defaultdict(list)
    upgrades = {}
    staged = in_library = in_pack = unreadable = renamed = isolated = 0
    per_pack = collections.Counter()

    for i, pack in enumerate(packs, 1):
        # Say where we are before the slow part, not after. Scanning forty packs
        # is tens of thousands of tag reads over NFS with nothing to show for it,
        # and a run killed by the job deadline needs to leave behind how far it
        # got -- otherwise the log ends mid-silence and says nothing.
        print(f"[{i}/{len(packs)}] {os.path.basename(pack)}")
        for root, _, names in os.walk(pack):
            for n in sorted(names):
                if not n.lower().endswith(AUDIO):
                    continue
                path = os.path.join(root, n)
                tags = read_tags(path)
                if not tags or not tags["title"] or not tags["artist"]:
                    unreadable += 1
                    continue

                prim = primary(tags["artist"], known)
                key_artist = norm(prim)

                have = find_existing(
                    strict, loose, key_artist, tags["title"], tags["length"]
                )
                if have:
                    old = have[0]
                    if args.no_upgrade or not better(
                        tags["format"], tags["bitrate"], old["fmt"], old["bitrate"]
                    ):
                        in_library += 1
                        continue
                    # Replacing demands a stricter match than skipping does, because
                    # the two mistakes do not cost the same: a wrong skip leaves the
                    # song in the pack to import another day, while a wrong replace
                    # destroys a library file and only the log remembers it. So an
                    # upgrade needs the exact title key and a duration that agrees
                    # closely -- a loose credit-shuffle match is not enough.
                    if not old["strict"] or old["delta"] > UPGRADE_TOLERANCE:
                        in_library += 1
                        continue
                    # The same song reaches us from a dozen country playlists, so
                    # several files can beat one library row. Keep the best of them
                    # and apply once: replacing the same path twice would leave the
                    # library pointing at a file the second pass already renamed.
                    prev = upgrades.get(old["id"])
                    if prev is None or better(
                        tags["format"], tags["bitrate"], prev["fmt"], prev["bitrate"]
                    ):
                        upgrades[old["id"]] = {
                            "id": old["id"],
                            "old_path": old["path"],
                            "old_desc": describe(old["fmt"], old["bitrate"]),
                            "src": path,
                            "fmt": tags["format"],
                            "bitrate": tags["bitrate"],
                            "new_desc": describe(tags["format"], tags["bitrate"]),
                            "who": prim,
                            "title": tags["title"],
                        }
                    continue

                seen_key = (key_artist, norm(tags["title"]))
                if key_artist and norm(tags["title"]) and any(
                    abs(d - tags["length"]) <= STRICT_TOLERANCE
                    for d in seen[seen_key]
                ):
                    in_pack += 1
                    continue
                seen[seen_key].append(tags["length"])

                # canonical[""] once resolved to an unrelated artist and would have
                # filed a Russian track under Ofra Haza; an empty key never maps.
                canon = canonical.get(key_artist, prim) if key_artist else prim
                if canon != prim:
                    renamed += 1
                elif key_artist:
                    # An artist new to the library still has to be spelled one way:
                    # the packs themselves carry both "SQUASH" and "Squash", and
                    # without this the first spelling does not bind the rest.
                    canonical[key_artist] = canon

                album = tags["album"] or tags["title"]
                if (norm(canon), norm(album)) in lib_albums:
                    album = f"{album} [chart]"
                    isolated += 1

                dest_dir = os.path.join(args.staging, safe(canon), safe(album))
                dest = os.path.join(dest_dir, n)

                if not args.apply:
                    staged += 1
                    per_pack[os.path.basename(pack)] += 1
                    manifest.append(
                        (path, dest, canon, album, tags["title"], f"{tags['length']:.1f}")
                    )
                    continue

                os.makedirs(dest_dir, exist_ok=True)
                if os.path.exists(dest):
                    # Same song, same filename, from another country folder, with
                    # durations too far apart for the intra-pack window to catch.
                    # copy2 would overwrite it silently.
                    in_pack += 1
                    continue
                shutil.copy2(path, dest)
                staged += 1
                per_pack[os.path.basename(pack)] += 1
                manifest.append(
                    (path, dest, canon, album, tags["title"], f"{tags['length']:.1f}")
                )
                # Write the corrected album artist onto the COPY only.
                try:
                    ez = mutagen.File(dest, easy=True)
                    if ez is not None:
                        ez["albumartist"] = canon
                        ez["album"] = album
                        full = tags["artist"]
                        nf = norm(full)
                        ez["artist"] = canonical.get(nf, full) if nf else full
                        ez.save()
                except Exception as e:
                    print(f"  VARNING kunde inte tagga {dest}: {e}")

        # Mark each pack the moment it is done rather than all of them at the end.
        # The first sweep has forty packs and some hold two thousand files, so a
        # run that hits the job's deadline part way through would otherwise redo
        # every finished pack the following night.
        if args.apply and state:
            rec = state.get(os.path.basename(pack))
            if rec is not None:
                rec["imported"] = os.environ.get("CHARTPACK_DATE") or "imported"
                save_state(args.state, state)

    if upgrades:
        print()
        print(f"=== UPPGRADERINGAR: {len(upgrades)} ===")
        for up in sorted(upgrades.values(), key=lambda u: (u["who"], u["title"]))[:40]:
            print(f"  {up['old_desc']:>10s} -> {up['new_desc']:<10s} {up['who']} — {up['title']}")
        if len(upgrades) > 40:
            print(f"  ... och {len(upgrades) - 40} till")

    if args.apply and upgrades:
        from beets import library as beetslib

        lib = beetslib.Library(LIBRARY, directory=MUSIC_ROOT)
        done = failed = vanished = 0
        # Appended, never rewritten: replacing a file is not reversible, so this
        # log is the only record of what the library used to hold. A nightly run
        # opening it with "w" would erase every previous night's evidence.
        stamp = os.environ.get("CHARTPACK_DATE") or ""
        header = not os.path.exists(args.upgrade_log) or os.path.getsize(args.upgrade_log) == 0
        with open(args.upgrade_log, "a", encoding="utf-8") as fh:
            if header:
                fh.write("date\told_path\tnew_path\tfrom\tto\tartist\ttitle\n")
            for up in upgrades.values():
                result, dest = apply_upgrade(up, lib)
                if result == "ok":
                    done += 1
                    fh.write("\t".join([
                        stamp, up["old_path"], dest, up["old_desc"], up["new_desc"],
                        up["who"], up["title"],
                    ]) + "\n")
                    fh.flush()
                elif result == "borta":
                    vanished += 1
                else:
                    failed += 1
                    print(f"  VARNING uppgradering misslyckades: {up['old_path']}: {result}")
        print(f"uppgraderade:           {done} ({failed} fel, {vanished} redan borta)")
        print(f"uppgraderingslogg:      {args.upgrade_log}")

    print()
    print(f"att importera:          {staged}")
    print(f"finns i biblioteket:    {in_library}")
    print(f"uppgraderingar:         {len(upgrades)}")
    print(f"upprepning inom paket:  {in_pack}")
    print(f"olästa:                 {unreadable}")
    print(f"artistnamn rättade:     {renamed}")
    print(f"album isolerade [chart]:{isolated}")
    for p, n in per_pack.most_common():
        print(f"  {n:5d}  {p}")
    if args.apply and manifest:
        # Appended for the same reason as the upgrade log: this is what says which
        # pack a library file came from, and it is what makes deleting a pack from
        # /data/torrents an audited decision rather than a hopeful one.
        header = not os.path.exists(args.manifest) or os.path.getsize(args.manifest) == 0
        with open(args.manifest, "a", encoding="utf-8") as fh:
            if header:
                fh.write("date\tsource\tstaged\talbumartist\talbum\ttitle\tlength\n")
            date = os.environ.get("CHARTPACK_DATE") or ""
            for row in manifest:
                fh.write("\t".join((date,) + tuple(row)) + "\n")
        print(f"manifest:               {args.manifest} ({len(manifest)} rader)")

    if not args.apply:
        print("\nTORRKÖRNING - inget kopierat. Kör med --apply.")
    else:
        print(f"\nstaging klar: {args.staging}")
        print("nästa steg: importera staging till biblioteket, verifiera mot")
        print("manifestet, och radera paketen FÖRST när varje rad är bekräftad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
