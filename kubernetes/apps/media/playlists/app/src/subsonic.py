#!/usr/bin/env python3
"""subsonic.py — the Subsonic client, and the write path this app does NOT share
with filesync.py.

filesync.py creates playlists PUBLIC by default and always follows up with an
explicit updatePlaylist to set the flag. That was a workaround for not having the
other person's password. This app has their password for ~15 ms, so it does not
need the workaround — and reusing that code would reproduce exactly the thing we
set out to remove. Hence a separate, deliberately smaller write path.

The privacy guarantee is structural, not a runtime check:

  * Navidrome's createPlaylist has NO `public` parameter at all (the handler reads
    only songId / playlistId / name). A `public=true` sent there is silently
    ignored.
  * A new playlist is built as `&model.Playlist{Name: name}`, so Public is the Go
    zero value — false — and the column is `public bool default FALSE not null`.
  * updatePlaylist mutates Public only when it is given a non-nil pointer, so
    omitting the parameter leaves an existing choice alone.

So "private" needs no parameter. There is no function in this module that can make
a playlist public on the create path. That absence IS the guarantee — do not add
one. `repair_private()` is the single place `public` is ever sent, and only to
force a playlist back to private if a read-back ever finds it public.
"""
import hashlib
import json
import logging
import os
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("playlists.subsonic")

BASE = (os.environ.get("NAVIDROME_URL", "http://navidrome.media.svc.cluster.local")
        + ":" + os.environ.get("NAVIDROME_PORT", "4533"))
CLIENT = os.environ.get("SUBSONIC_CLIENT", "playlist-import")
API_VERSION = "1.16.1"
TIMEOUT = 20

# Subsonic error codes we care about (http://www.subsonic.org/pages/api.jsp)
ERR_GENERIC = 0
ERR_MISSING_PARAM = 10
ERR_BAD_CREDENTIALS = 40
ERR_NOT_AUTHORIZED = 50
ERR_NOT_FOUND = 70


class NavidromeUnreachable(Exception):
    """Network-level failure on a READ. Safe to surface and safe to retry."""


class NavidromeAmbiguousWrite(Exception):
    """A WRITE whose outcome is unknown: the request left, no answer came back.

    Never retried — Navidrome has no unique constraint on (name, owner_id), so a
    retried createPlaylist silently produces a second playlist. The caller must
    reconcile by reading instead.
    """


class NavidromeError(Exception):
    """Navidrome answered with `status: failed`. Always terminal, never retried."""

    def __init__(self, code, message):
        super().__init__(f"subsonic error {code}: {message}")
        self.code = code
        self.message = message


def derive_creds(username, secret):
    """The ONLY function that touches the cleartext password.

    Returns (username, salt, token) — a Subsonic credential that stays valid until
    the password changes. The caller burns the Secret immediately afterwards, which
    is what collapses the password's lifetime to a few milliseconds inside one
    function frame.

    The pair is password-EQUIVALENT authority for the Subsonic API: it cannot be
    revoked short of a password change, so it is treated like the password itself —
    in-process only, never serialised, never logged, zeroed when the session ends.
    """
    salt = secrets.token_hex(8)          # 8 random bytes; Navidrome's own /auth/login salt is 3
    token = hashlib.md5((secret.reveal() + salt).encode()).hexdigest()
    return (username, salt, token)


def _auth_fields(creds):
    username, salt, token = creds
    return [("u", username), ("t", token), ("s", salt),
            ("v", API_VERSION), ("c", CLIENT), ("f", "json")]


def call(creds, endpoint, params=None, multi=None, retries=3):
    """POST a Subsonic call and return the parsed subsonic-response.

    Credentials always travel in the request BODY, never a query string — Envoy
    access-logs the path with its query. (Navidrome internally rewrites the form
    into its own RawQuery, but it logs only the path, so the secret stays out of
    both log streams.)

    The HTTP status is never branched on: bad credentials come back as HTTP 200
    with `error.code 40` in the body. Only the JSON body decides.

    retries applies to READS only. Every write passes retries=0.
    """
    fields = _auth_fields(creds) + list((params or {}).items())
    for key, values in (multi or {}).items():
        fields.extend((key, v) for v in values)
    body = urllib.parse.urlencode(fields).encode()
    url = f"{BASE}/rest/{endpoint}"

    delays = (0.5, 2, 6)
    attempt = 0
    while True:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload = json.loads(r.read().decode())["subsonic-response"]
            break
        except urllib.error.HTTPError as e:
            # 5xx is worth a retry on a read; 4xx never is.
            transient = e.code >= 500
            if transient and attempt < retries:
                time.sleep(delays[min(attempt, len(delays) - 1)])
                attempt += 1
                continue
            if retries == 0:
                raise NavidromeAmbiguousWrite(f"HTTP {e.code} on {endpoint}") from None
            raise NavidromeUnreachable(f"HTTP {e.code} on {endpoint}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError, KeyError):
            if attempt < retries:
                time.sleep(delays[min(attempt, len(delays) - 1)])
                attempt += 1
                continue
            # A write that got no parseable answer may or may not have landed.
            if retries == 0:
                raise NavidromeAmbiguousWrite(f"no answer from {endpoint}") from None
            raise NavidromeUnreachable(f"could not reach Navidrome at {BASE}") from None

    if payload.get("status") != "ok":
        err = payload.get("error") or {}
        raise NavidromeError(err.get("code", ERR_GENERIC), err.get("message", "okänt fel"))
    return payload


# ---------- reads ----------

def ping(creds):
    """Cheapest credential check: no repository access, short-circuits on auth."""
    call(creds, "ping")
    return True


def get_music_folders(creds):
    """The caller's visible libraries, as a stable fingerprint.

    search3 is library-scoped per user in Navidrome, and a track from a library the
    viewer cannot see is stored in a playlist but silently filtered out on read. So
    an index is only reusable between two identities that see the same libraries.
    """
    res = call(creds, "getMusicFolders")
    folders = (res.get("musicFolders") or {}).get("musicFolder") or []
    return tuple(sorted(int(f["id"]) for f in folders if f.get("id") is not None))


def get_playlists(creds):
    res = call(creds, "getPlaylists")
    return (res.get("playlists") or {}).get("playlist") or []


def get_playlist(creds, pid):
    res = call(creds, "getPlaylist", {"id": pid})
    return res.get("playlist") or {}


def search3_page(creds, offset, count):
    res = call(creds, "search3", {
        "query": "", "songCount": count, "songOffset": offset,
        "artistCount": 0, "albumCount": 0,
    })
    return (res.get("searchResult3") or {}).get("song") or []


def find_own_playlist(creds, username, name):
    """An existing playlist of THIS user's with this name, or None.

    Navidrome has no unique constraint on (name, owner_id), so without this an
    upload of the same file twice would create a second playlist.
    """
    me = username.casefold()
    mine = [p for p in get_playlists(creds)
            if (p.get("owner") or "").casefold() == me]
    for candidate in mine:
        if candidate.get("name") == name:
            return candidate
    for candidate in mine:
        if (candidate.get("name") or "").casefold() == name.casefold():
            return candidate
    return None


# ---------- writes (retries=0, always) ----------

def create_playlist(creds, name, song_ids):
    """Create a NEW playlist owned by the authenticating user.

    Sends only `name` + repeated `songId`. No `public` parameter (createPlaylist
    has none) and no follow-up updatePlaylist, so the playlist is private by the
    struct zero value and the column default.
    """
    if not song_ids:
        raise ValueError("refusing to create an empty playlist")
    res = call(creds, "createPlaylist", {"name": name},
               {"songId": list(song_ids)}, retries=0)
    return (res.get("playlist") or {}).get("id")


def extend_playlist(creds, pid, song_ids_to_add):
    """ADD tracks to an existing playlist. Additive, never a mirror.

    Deliberately updatePlaylist+songIdToAdd rather than createPlaylist+playlistId:
    the latter REPLACES the whole track list, which would silently discard tracks
    he had added himself in Symfonium.

    No `public` parameter — Navidrome only touches Public when given a non-nil
    pointer, so whatever sharing choice he made himself survives.
    """
    if not song_ids_to_add:
        raise ValueError("refusing to send an empty update")
    call(creds, "updatePlaylist", {"playlistId": pid},
         {"songIdToAdd": list(song_ids_to_add)}, retries=0)
    return pid


# ---------- verification ----------

def verify(creds, username, pid, expected_ids):
    """Read the playlist back and prove it is his, private, and complete.

    Success is never reported to him on the strength of a write returning 200.

    NOTE the public test: Subsonic renders `public` with omitempty, so a private
    playlist has NO public key at all. `p["public"] == False` would raise KeyError
    and `p.get("public") == False` would be False for a private playlist. The only
    correct test is `is not True`.
    """
    pl = get_playlist(creds, pid)
    owner_ok = (pl.get("owner") or "").casefold() == username.casefold()
    private_ok = pl.get("public") is not True
    present = {e.get("id") for e in (pl.get("entry") or [])}
    missing = [i for i in expected_ids if i not in present]
    return {
        "owner_ok": owner_ok,
        "private_ok": private_ok,
        "missing": missing,
        "owner": pl.get("owner"),
        "count": len(present),
        "name": pl.get("name"),
    }


def repair_private(creds, pid):
    """The ONLY place `public` is ever sent. Fires only if verify() found it public.

    Should be dead code. If it ever runs, something in Navidrome's defaults changed
    and the ERROR log is the signal to re-read this module's assumptions.
    """
    log.error("playlist %s came back public — forcing it private", pid)
    call(creds, "updatePlaylist", {"playlistId": pid, "public": "false"}, retries=0)


def reconcile(creds, username, name):
    """After an ambiguous write: what actually exists now?

    Cheaper and safer than guessing, and the only correct response to a write whose
    answer was lost — retrying would duplicate.
    """
    try:
        return find_own_playlist(creds, username, name)
    except (NavidromeUnreachable, NavidromeError):
        return None
