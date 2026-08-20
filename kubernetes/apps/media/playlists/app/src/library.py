#!/usr/bin/env python3
"""library.py — the Navidrome library index: crawl, cache, and the match loop.

The matching itself is NOT here. `matcher.py` is imported from /shared (the
tunesynctool ConfigMap, on PYTHONPATH) and used unchanged, so this app, plexsync.py
and filesync.py all match identically and cannot drift apart.

Built with the REQUESTING USER's credentials, never a service account. Not for
secrecy — the session holds a derived token, so a service account would not shorten
the password's life by a microsecond — but for correctness: search3 is
library-scoped per user in Navidrome, and a track from a library the viewer cannot
see is stored in a playlist yet silently filtered out on read. An index built by
another identity is therefore a silent-truncation bug waiting to happen. Building as
him makes it structurally impossible, and means this app holds no credential of its
own at all.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import matcher  # from /shared via PYTHONPATH — single source of truth for matching

import subsonic

log = logging.getLogger("playlists.library")

PAGE = int(os.environ.get("INDEX_PAGE", "5000"))
FRESH_TTL = int(os.environ.get("INDEX_FRESH_TTL", "900"))
STALE_TTL = int(os.environ.get("INDEX_STALE_TTL", "21600"))

# Two concurrent uploads must not pin every core. Matching is pure CPU.
_MATCH_SLOTS = threading.BoundedSemaphore(2)

_LOCK = threading.Lock()
_CACHE = {}            # fingerprint -> Index
_BUILDING = {}         # fingerprint -> threading.Event  (single-flight)
_PROGRESS = {}         # fingerprint -> int  (tracks read so far, for the UI)
_LAST_ERROR = {}       # fingerprint -> str


@dataclass(frozen=True)
class Index:
    """Immutable once published, so readers need no lock — publication is a single
    reference assignment into _CACHE."""
    by_title: dict
    title_keys: list
    meta: dict
    count: int
    fingerprint: tuple
    built_at: float
    generation: int = 0

    def age(self):
        return time.time() - self.built_at


def progress_for(fingerprint):
    with _LOCK:
        return _PROGRESS.get(fingerprint, 0)


def _build(creds, fingerprint):
    """Crawl the whole library into LOCAL structures, publishing only on success.

    A truncated index is far worse than a stale one: it turns tracks he owns into
    silent misses, and he has no way to tell the difference. So a failed crawl keeps
    whatever was already published.
    """
    by_title, meta, offset, total = {}, {}, 0, 0
    t0 = time.time()
    while True:
        songs = subsonic.search3_page(creds, offset, PAGE)
        if not songs:
            break
        for s in songs:
            sid = s.get("id")
            # search3 reports duration in SECONDS; matcher works in MILLISECONDS.
            matcher.add_to_index(
                by_title,
                s.get("title"),
                (s.get("artist"), s.get("displayAlbumArtist"), s.get("albumArtist")),
                int(s.get("duration") or 0) * 1000,
                sid,
            )
            # Kept so the preview can show WHAT each source track matched to —
            # a wrong match is worse than a miss, so it has to be inspectable.
            meta[sid] = {
                "artist": s.get("artist") or s.get("displayAlbumArtist") or "?",
                "title": s.get("title") or "?",
                "album": s.get("album") or "",
            }
        total += len(songs)
        offset += len(songs)
        with _LOCK:
            _PROGRESS[fingerprint] = total
        if len(songs) < PAGE:
            break

    with _LOCK:
        previous = _CACHE.get(fingerprint)
    # A double-digit shrink is a Navidrome scan problem, not reality. It is also
    # what an early empty page looks like — search3 terminating cleanly mid-crawl.
    if previous and previous.count > 1000 and total < previous.count * 0.9:
        raise RuntimeError(
            f"library appears to have shrunk {previous.count} -> {total}; keeping the old index")

    idx = Index(
        by_title=by_title,
        title_keys=list(by_title.keys()),
        meta=meta,
        count=total,
        fingerprint=fingerprint,
        built_at=time.time(),
        generation=(previous.generation + 1) if previous else 1,
    )
    log.info("indexed %d tracks under %d titles in %.1fs (generation %d)",
             total, len(idx.title_keys), time.time() - t0, idx.generation)
    return idx


def _build_and_publish(creds, fingerprint):
    try:
        idx = _build(creds, fingerprint)
        with _LOCK:
            _CACHE[fingerprint] = idx
            _LAST_ERROR.pop(fingerprint, None)
        return idx
    except Exception as e:                                  # noqa: BLE001
        with _LOCK:
            _LAST_ERROR[fingerprint] = type(e).__name__
        log.error("index build failed (%s): keeping previous index", type(e).__name__)
        raise
    finally:
        with _LOCK:
            _PROGRESS.pop(fingerprint, None)
            ev = _BUILDING.pop(fingerprint, None)
        if ev:
            ev.set()


def kick_refresh(creds, fingerprint):
    """Fire-and-forget rebuild. Used on login (he is still hunting for the file) and
    for stale-while-revalidate, so nobody ever waits on a refresh."""
    with _LOCK:
        if fingerprint in _BUILDING:
            return
        _BUILDING[fingerprint] = threading.Event()
    threading.Thread(
        target=lambda: _safe(_build_and_publish, creds, fingerprint),
        name="index-refresh", daemon=True,
    ).start()


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:                                       # noqa: BLE001
        pass    # already logged; a background refresh must never kill the process


def get_index(creds, fingerprint, blocking=True):
    """Serve the index for this fingerprint, honouring three freshness tiers.

    fresh  (< FRESH_TTL) -> serve, do nothing
    stale  (< STALE_TTL) -> serve NOW and rebuild behind the caller
    cold / absent / fingerprint mismatch -> build synchronously

    A fingerprint mismatch means the requester sees a different set of libraries
    than whoever built the cache, so the cache is refused rather than trusted. The
    day a second library appears this degrades to slower-but-correct instead of
    fast-and-wrong.
    """
    with _LOCK:
        idx = _CACHE.get(fingerprint)

    if idx and idx.age() < FRESH_TTL:
        return idx
    if idx and idx.age() < STALE_TTL:
        kick_refresh(creds, fingerprint)
        return idx

    # Cold, absent, or a fingerprint we have never crawled. Exactly one caller
    # builds; everyone else waits on that caller's event.
    with _LOCK:
        wait_for = _BUILDING.get(fingerprint)
        i_build = wait_for is None
        if i_build:
            _BUILDING[fingerprint] = threading.Event()

    if not i_build:
        if not blocking:
            return idx                 # may be None; the caller decides
        wait_for.wait(timeout=180)
        with _LOCK:
            return _CACHE.get(fingerprint)

    if not blocking:
        # We claimed the build slot but must not block the caller: hand it to a
        # thread. Returning the stale index (or None) is the caller's problem.
        threading.Thread(
            target=lambda: _safe(_build_and_publish, creds, fingerprint),
            name="index-build", daemon=True,
        ).start()
        return idx

    return _build_and_publish(creds, fingerprint)


def match(index, tracks):
    """Match parsed source tracks against the index.

    Returns (pairs, misses) where pairs is [(source_track, library_meta, song_id)]
    in the file's own order with duplicate ids collapsed.

    `matcher.find_match` is used exactly as-is. It scans every title key per source
    track, which reads like a quadratic trap but measures ~1.3 ms per track against
    a 45k index — 200 tracks in a quarter of a second. Not worth "optimising" into a
    behaviour change.
    """
    pairs, misses, seen = [], [], set()
    with _MATCH_SLOTS:
        for t in tracks:
            ident = matcher.find_match(index.by_title, index.title_keys, t)
            if not ident and t.get("alt"):
                # Pasted free text: "Artist - Titel" and "Titel - Artist" are
                # indistinguishable, so the parser offers both readings.
                ident = matcher.find_match(index.by_title, index.title_keys, t["alt"])
            if ident:
                if ident not in seen:
                    seen.add(ident)
                    pairs.append((t, index.meta.get(ident, {}), ident))
            else:
                misses.append(t)
    return pairs, misses
