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
import os
import re
import shutil
import sqlite3
import sys
import unicodedata

import mutagen
import mutagen.easyid3

AUDIO = (".mp3", ".flac", ".m4a", ".ogg", ".opus")
LIBRARY = "/config/library.db"

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
    length = getattr(getattr(f, "info", None), "length", 0.0) or 0.0
    return {
        "artist": get("artist"),
        "albumartist": get("albumartist"),
        "album": get("album"),
        "title": get("title"),
        "track": get("tracknumber"),
        "date": get("date"),
        "genre": get("genre"),
        "length": float(length),
    }


def load_library():
    """Two title indexes keyed on the lead artist, plus canonical spellings."""
    db = sqlite3.connect(f"file:{LIBRARY}?mode=ro", uri=True)
    rows = list(db.execute("SELECT artist, albumartist, title, length FROM items"))

    # Full artist strings first, so primary() can recognise a band whose name
    # contains a comma or a featuring word and leave it intact.
    known = {norm(who) for artist, albumartist, _, _ in rows for who in (artist, albumartist) if who}
    known.discard("")

    strict = collections.defaultdict(list)
    loose = collections.defaultdict(list)
    spellings = collections.defaultdict(collections.Counter)
    for artist, albumartist, title, length in rows:
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
            dur = float(length or 0)
            if norm(title):
                strict[(key_artist, norm(title))].append(dur)
            if norm_loose(title):
                loose[(key_artist, norm_loose(title))].append(dur)
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


def already_have(strict, loose, key_artist, title, length):
    """True only when the library plainly holds this same recording.

    Biased towards returning False: the caller deletes the source afterwards, so
    a missed duplicate costs disk while a false positive costs the music.
    """
    if not key_artist:
        return False
    t, tl = norm(title), norm_loose(title)
    if t and any(
        abs(d - length) <= STRICT_TOLERANCE for d in strict.get((key_artist, t), ())
    ):
        return True
    if tl and any(
        abs(d - length) <= LOOSE_TOLERANCE for d in loose.get((key_artist, tl), ())
    ):
        return True
    return False


def is_chart_pack(path, sample=8):
    """Many distinct album tags in one folder means it is not an album.

    A real album -- even a various-artists compilation -- carries one album tag.
    A chart folder carries one per track. Sampling a handful separates them
    cleanly, and the sample is kept small because this walks every candidate over
    NFS; pass the pack directories explicitly to skip detection entirely.
    """
    albums, files = set(), 0
    for root, _, names in os.walk(path):
        for n in sorted(names):
            if not n.lower().endswith(AUDIO):
                continue
            files += 1
            if files > sample:
                return len(albums) > sample * 0.5
            tags = read_tags(os.path.join(root, n))
            if tags and tags["album"]:
                albums.add(norm(tags["album"]))
    return files >= sample and len(albums) > files * 0.5


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
    args = ap.parse_args()

    packs = args.packs
    if not packs:
        print(f"letar chartpaket under {args.src} ...")
        packs = [
            os.path.join(args.src, d)
            for d in sorted(os.listdir(args.src))
            if os.path.isdir(os.path.join(args.src, d))
            and is_chart_pack(os.path.join(args.src, d))
        ]
    if not packs:
        print("inga chartpaket hittade")
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
    staged = in_library = in_pack = unreadable = renamed = isolated = 0
    per_pack = collections.Counter()

    for pack in packs:
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

                if already_have(strict, loose, key_artist, tags["title"], tags["length"]):
                    in_library += 1
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

    print()
    print(f"att importera:          {staged}")
    print(f"finns i biblioteket:    {in_library}")
    print(f"upprepning inom paket:  {in_pack}")
    print(f"olästa:                 {unreadable}")
    print(f"artistnamn rättade:     {renamed}")
    print(f"album isolerade [chart]:{isolated}")
    for p, n in per_pack.most_common():
        print(f"  {n:5d}  {p}")
    if args.apply and manifest:
        with open(args.manifest, "w", encoding="utf-8") as fh:
            fh.write("source\tstaged\talbumartist\talbum\ttitle\tlength\n")
            for row in manifest:
                fh.write("\t".join(row) + "\n")
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
