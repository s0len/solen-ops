#!/usr/bin/env python3
"""matcher.py — shared, source-agnostic track matching for the tunesynctool scripts.

Extracted verbatim from plexsync.py so `plexsync.py` (Spotify -> Plex) and
`filesync.py` (playlist file -> Navidrome) match identically instead of drifting
apart. Nothing here talks to Spotify, Plex or Navidrome: callers build an index in
the documented shape and hand over plain dicts.

The point of the normalization is that exported playlists carry noise your local
library does not:
    "Bohemian Rhapsody - Remastered 2011"      -> "Bohemian Rhapsody"
    "The Chain - 2004 Remaster"                -> "The Chain"
    "Song (feat. Someone)"                     -> "Song"
    "White Wedding (Pt. 1 / Remastered 2002)"  -> "White Wedding (Pt. 1"  (paren noise)
"""
import re
import unicodedata

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
# Abbreviations that libraries and exports spell differently for the SAME track:
# "Another Brick In The Wall, Pt. 2" vs "Another Brick In The Wall (Part 2)".
# Only expanded when a number follows, so ordinary words are never touched.
_ABBREV = (
    (re.compile(r"\bpts?\s+(\d+)"), r"part \1"),
    (re.compile(r"\bvol\s+(\d+)"), r"volume \1"),
)

# A candidate that is an instrumental / karaoke / acapella cut when the source is
# not one is never what the person asked for, even if every other signal ties.
_UNWANTED_CUT = re.compile(r"\b(instrumental|karaoke|acapella|a cappella)\b", re.IGNORECASE)

# Leading "feat"/"with" split points inside an artist string.
_ARTIST_SPLIT = re.compile(r"\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|,|&|/", re.IGNORECASE)


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
    s = re.sub(r"\s+", " ", s).strip()
    for pattern, repl in _ABBREV:
        s = pattern.sub(repl, s)
    return s


def norm_artist(s):
    s = fold(s or "").lower()
    s = _ARTIST_SPLIT.split(s)[0]
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def add_to_index(by_title, title, artists, dur_ms, ident):
    """Insert one library track into an index built for find_match().

    by_title : {normalized_title -> [(ntitle, {norm_artists}, dur_ms, ident)]}
    artists  : iterable of raw artist strings (album artist, track artist, ...)
    dur_ms   : track length in MILLISECONDS (normalize before calling)
    ident    : whatever the caller needs back — Plex ratingKey, Navidrome song id, ...

    Returns True when the track was indexed, False when it had no usable title.
    """
    nt = norm_title(title or "")
    if not nt or ident is None:
        return False
    normed = set()
    for src in artists or ():
        if src:
            na = norm_artist(src)
            if na:
                normed.add(na)
    by_title.setdefault(nt, []).append((nt, normed, dur_ms or 0, ident))
    return True


def find_match(by_title, title_keys, track):
    """Return the best-matching library ident for a source track, or None.

    `track` is a dict: {"name": str, "artists": [str, ...], "dur": int_milliseconds}

    Acceptance semantics:
      - title filter: candidate title == target OR either is a substring of the other
      - artist filter: any source artist vs candidate album/track artist (substring ok)
      - rank: artist match first, then exact-title, then closest duration
      - accept: artist confirmed, OR exact title within ~3s
    """
    target_title = norm_title(track["name"])
    if not target_title:
        return None
    target_artists = {norm_artist(a) for a in track["artists"] if a}
    target_artists.discard("")
    sp_dur = track["dur"]
    tt = target_title

    source_is_cut = bool(_UNWANTED_CUT.search(track["name"] or ""))

    best, best_score, best_had_artists = None, None, False
    for k in title_keys:
        # title_ok: k == tt, or either is a substring of the other
        if tt not in k and k not in tt:
            continue
        exact_title = 1 if k == tt else 0
        for (_nt, artists, dur, ident) in by_title[k]:
            artist_ok = False
            for ta in target_artists:
                for ca in artists:
                    if ta == ca or ta in ca or ca in ta:
                        artist_ok = True
                        break
                if artist_ok:
                    break
            # An instrumental/karaoke cut loses to anything else, but still beats
            # nothing at all if it is the only thing in the library.
            wanted_cut = 0 if (not source_is_cut and _UNWANTED_CUT.search(k)) else 1
            ddur = abs(dur - sp_dur)
            score = (1 if artist_ok else 0, wanted_cut, exact_title, -ddur)
            if best_score is None or score > best_score:
                best, best_score = ident, score
                best_had_artists = bool(artists)

    if best is None:
        return None
    artist_ok, _wanted_cut, exact_title, neg_ddur = best_score
    if artist_ok:
        return best
    # The artist-less rescue exists for the case where one SIDE has no artist to
    # compare — a bare "Title" line in an m3u, or a library file with no artist tag.
    # It must not fire when both sides name an artist and they disagree: that is a
    # different recording by someone else, and a short generic title plus a
    # coincidental duration is all it takes. ("Arvingarna – I Do" matched
    # "Felix Jaehn – I Do", 183s vs 184s, and Arvingarna is not in the library.)
    if target_artists and best_had_artists:
        return None
    if exact_title and (-neg_ddur) <= 3000:
        return best
    return None
