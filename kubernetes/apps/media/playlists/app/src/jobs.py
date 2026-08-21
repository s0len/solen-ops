#!/usr/bin/env python3
"""jobs.py — sessions, the job registry, the commit token, and file parsing.

Everything here is in-process and deliberately so: there is no database, no cache
server and no Kubernetes Secret anywhere in this app. The cost is that the
Deployment MUST stay at replicas: 1 — sessions, jobs, the login throttle and the
library index all live in this process, and a second replica would break all four
silently rather than loudly.

Parsing reuses filesync.py's three parsers verbatim from /shared, so the UTF-16
decode ladder, the Swedish iTunes column names and the plist Master/Distinguished
skip are not forked.
"""
import hmac
import logging
import os
import secrets
import tempfile
import threading
import time
from hashlib import sha256

import filesync  # from /shared via PYTHONPATH — parsers reused, not reimplemented

import library
import subsonic

log = logging.getLogger("playlists.jobs")

SESSION_TTL = int(os.environ.get("SESSION_TTL", "1200"))      # sliding idle timeout
SESSION_MAX = int(os.environ.get("SESSION_MAX", "3600"))      # hard cap
JOB_TTL = int(os.environ.get("JOB_TTL", "900"))
MAX_TRACKS = int(os.environ.get("MAX_TRACKS", "2000"))
MAX_PLAYLISTS = int(os.environ.get("MAX_PLAYLISTS", "20"))

# Dies with the process, which is exactly why this app needs no SECRET_KEY and no
# ExternalSecret. A pod roll invalidates every session and commit token — correct,
# because the sessions were in that process's memory anyway.
_PROCESS_KEY = secrets.token_bytes(32)

_LOCK = threading.Lock()
_SESSIONS = {}
_JOBS = {}


class ParseProblem(Exception):
    """A file we cannot use, with a message already written for him in Swedish."""


class Session:
    __slots__ = ("sid", "username", "creds", "fingerprint", "csrf",
                 "job_id", "created", "last_seen")

    def __init__(self, username, creds, fingerprint):
        self.sid = secrets.token_urlsafe(32)
        self.username = username
        self.creds = creds              # (username, salt, token) — password-equivalent
        self.fingerprint = fingerprint
        self.csrf = secrets.token_urlsafe(32)
        self.job_id = None
        self.created = time.time()
        self.last_seen = self.created

    def alive(self, now=None):
        now = now or time.time()
        return (now - self.last_seen) < SESSION_TTL and (now - self.created) < SESSION_MAX

    def burn(self):
        """Drop the derived credential.

        Honest limitation: the token is a str and CPython does not zero freed
        memory, so this removes the reference rather than scrubbing the bytes. It
        still bounds how long the value is reachable, which is what the TTL is for.
        """
        self.creds = None


class Job:
    __slots__ = ("job_id", "state", "step", "error", "target_name", "pairs",
                 "misses", "source_total", "existing", "commit_token",
                 "committed", "result", "created", "fingerprint", "filename")

    def __init__(self, target_name, filename):
        self.job_id = secrets.token_urlsafe(32)
        self.state = "running"
        self.step = "laser"            # laser -> matchar -> klar
        self.error = None
        self.target_name = target_name
        self.filename = filename
        self.pairs = []
        self.misses = []
        self.source_total = 0
        self.existing = None
        self.commit_token = None
        self.committed = False
        self.result = None
        self.created = time.time()
        self.fingerprint = None


# ---------- registry ----------

def new_session(username, creds, fingerprint):
    s = Session(username, creds, fingerprint)
    with _LOCK:
        _SESSIONS[s.sid] = s
    return s


def get_session(sid):
    if not sid:
        return None
    with _LOCK:
        s = _SESSIONS.get(sid)
        if not s:
            return None
        if not s.alive():
            s.burn()
            _SESSIONS.pop(sid, None)
            return None
        s.last_seen = time.time()
        return s


def drop_session(sid):
    with _LOCK:
        s = _SESSIONS.pop(sid, None)
    if s:
        s.burn()


def put_job(job):
    with _LOCK:
        _JOBS[job.job_id] = job


def get_job(job_id):
    if not job_id:
        return None
    with _LOCK:
        return _JOBS.get(job_id)


def claim_commit(job, token):
    """Verify the commit token and claim the write, atomically.

    The claim happens BEFORE any network call, so a double-click or a browser
    retrying the POST cannot produce a second playlist. Navidrome has no unique
    constraint on (name, owner_id), so that would be a real duplicate, not a no-op.
    """
    if not job.commit_token or not token:
        return False
    if not hmac.compare_digest(job.commit_token, token):
        return False
    with _LOCK:
        if job.committed:
            return False
        job.committed = True
    return True


def mint_commit_token(job, username):
    """Bind the write to the exact preview he was shown."""
    payload = "|".join([
        job.job_id, job.target_name, username,
        ",".join(ident for _, _, ident in job.pairs),
    ]).encode()
    return hmac.new(_PROCESS_KEY, payload, sha256).hexdigest()


def _janitor():
    while True:
        time.sleep(60)
        now = time.time()
        with _LOCK:
            dead_s = [k for k, v in _SESSIONS.items() if not v.alive(now)]
            for k in dead_s:
                _SESSIONS.pop(k).burn()
            dead_j = [k for k, v in _JOBS.items() if now - v.created > JOB_TTL]
            for k in dead_j:
                _JOBS.pop(k, None)


def start_janitor():
    threading.Thread(target=_janitor, name="janitor", daemon=True).start()


# ---------- parsing ----------

_PARSERS = {
    ".txt": "parse_itunes_txt",
    ".xml": "parse_itunes_xml",
    ".m3u": "parse_m3u",
    ".m3u8": "parse_m3u",
    ".csv": "parse_csv",      # exportify.net and other CSV exporters
}


def parse_upload(data, filename):
    """Parse validated bytes into [(playlist_name, tracks)].

    filesync's parsers take a PATH, so the already-size-capped and sniffed bytes are
    spooled to a random name in the /tmp emptyDir and removed in a finally. The
    client's filename is never used as a path — only its extension is read, and the
    name it suggests goes through the server's own validator into a visible field.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    fn_name = _PARSERS.get(ext)
    if not fn_name:
        raise ParseProblem(
            "Den filtypen känner jag inte igen. Jag kan ta emot .txt och .xml "
            "från Musik, .csv från exportify.net, och .m3u.")
    parser = getattr(filesync, fn_name)

    fd, path = tempfile.mkstemp(prefix="upload-", suffix=ext, dir=os.environ.get("TMPDIR", "/tmp"))
    temp_stem = os.path.splitext(os.path.basename(path))[0]
    client_stem = os.path.splitext(os.path.basename(filename or ""))[0]
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            parsed = parser(path)
        except SystemExit:
            # filesync.die() calls sys.exit(). In a worker thread that only ends the
            # thread, which would leave this job stuck on "running" forever. The
            # only path ever handed to die() is our own random /tmp name, so the
            # message it printed to stderr leaks nothing.
            raise ParseProblem(
                "Jag kunde inte läsa filen. Exportera spellistan igen och "
                "välj „Vanlig text”.") from None
        except ParseProblem:
            raise
        except Exception:                                   # noqa: BLE001
            raise ParseProblem(
                "Jag kunde inte läsa filen. Exportera spellistan igen och "
                "välj „Vanlig text”.") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # filesync's parsers derive a playlist name from the PATH they were given, and
    # the path we give them is a random temp file. Any name that came from there is
    # meaningless ("upload-bkss6hux"), so swap it for the name the client's file
    # actually had. A name from inside an iTunes .xml plist is real and kept as-is.
    playlists = [
        (client_stem if name == temp_stem else name, tracks)
        for name, tracks in parsed if tracks
    ]
    if not playlists:
        raise ParseProblem("Filen innehåller inga låtar.")
    if len(playlists) > MAX_PLAYLISTS:
        raise ParseProblem(
            f"Filen innehåller {len(playlists)} spellistor. Exportera en spellista "
            "i taget.")
    for _, tracks in playlists:
        if len(tracks) > MAX_TRACKS:
            raise ParseProblem(
                f"Spellistan har {len(tracks)} låtar, vilket är fler än jag kan ta "
                f"emot på en gång (max {MAX_TRACKS}).")
    return playlists


def parse_text(text):
    """The paste box: one 'Artist - Titel' per line.

    Which half is the artist is genuinely ambiguous in free text, so both readings
    are offered and library.match keeps whichever one hits.
    """
    tracks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (" - ", " – ", "\t", " — "):
            if sep in line:
                left, _, right = line.partition(sep)
                left, right = left.strip(), right.strip()
                if not left or not right:
                    continue
                tracks.append({
                    "name": right, "artists": [left], "album": "", "dur": 0,
                    "alt": {"name": left, "artists": [right], "album": "", "dur": 0},
                })
                break
        else:
            tracks.append({"name": line, "artists": [], "album": "", "dur": 0})
        if len(tracks) >= MAX_TRACKS:
            break
    if not tracks:
        raise ParseProblem("Jag hittade inga låtar i texten.")
    return [("Inklistrad lista", tracks)]


# ---------- the worker ----------

def run_job(session, job, tracks):
    """Index (blocking, with progress), match, and resolve an existing playlist.

    Nothing here writes to Navidrome. The only write happens later, in the commit
    step, after he has seen the preview and pressed the button.
    """
    try:
        job.fingerprint = session.fingerprint
        creds = session.creds
        if creds is None:
            job.state, job.error = "error", "Du blev utloggad. Logga in igen."
            return

        job.step = "laser"
        index = library.get_index(creds, session.fingerprint, blocking=True)
        if index is None:
            job.state, job.error = "error", (
                "Jag kunde inte läsa musiksamlingen just nu. Försök igen om en stund.")
            return

        job.step = "matchar"
        job.source_total = len(tracks)
        job.pairs, job.misses = library.match(index, tracks)

        if job.pairs:
            try:
                job.existing = subsonic.find_own_playlist(creds, session.username,
                                                          job.target_name)
            except (subsonic.NavidromeUnreachable, subsonic.NavidromeError):
                job.existing = None     # not fatal: worst case we create a new one
            job.commit_token = mint_commit_token(job, session.username)

        job.step = "klar"
        job.state = "done"
    except subsonic.NavidromeUnreachable:
        job.state, job.error = "error", (
            "Jag kunde inte nå musikservern. Försök igen om en stund.")
    except subsonic.NavidromeError:
        job.state, job.error = "error", (
            "Något gick inte att läsa från musikservern. Försök igen om en stund.")
    except Exception as e:                                  # noqa: BLE001
        log.error("job failed: %s", type(e).__name__)
        job.state, job.error = "error", (
            "Något oväntat hände. Försök igen om en stund.")


def start_job(session, job, tracks):
    threading.Thread(target=run_job, args=(session, job, tracks),
                     name="job", daemon=True).start()
