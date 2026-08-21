#!/usr/bin/env python3
"""server.py — the HTTP layer: routing, hardening, multipart, throttling, logging.

Stdlib only. No Flask, no uvicorn, no pip, no venv, no image build — which also
means this file runs unchanged on a laptop against a port-forwarded Navidrome, and
that none of the multipart-parser CVEs in Werkzeug / Starlette / python-multipart
apply here.

Two hardening notes that are easy to get wrong and load-bearing here:

  * This route is exempt from the gateway's OIDC policy (it has to be — he has no
    account there), so the in-app login throttle is NOT optional.
  * X-Forwarded-Proto is forwarded verbatim by envoy-external and X-Real-IP is
    actively rewritten by the gateway's own patch policy, so nothing in this file
    trusts either. The scheme is hard-coded and every Location header is relative.
"""
import email.parser
import email.policy
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import filesync

import jobs
import library
import subsonic
import ui

PORT = int(os.environ.get("PORT", "8080"))
TRUSTED_ORIGIN = os.environ.get("TRUSTED_ORIGIN", "").rstrip("/")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_BYTES", "8388608"))
MAX_FORM = 8192
LOGIN_FAIL_LIMIT = int(os.environ.get("LOGIN_FAIL_LIMIT", "5"))
LOGIN_FAIL_WINDOW = int(os.environ.get("LOGIN_FAIL_WINDOW", "900"))
LOGIN_GLOBAL_FAIL_LIMIT = int(os.environ.get("LOGIN_GLOBAL_FAIL_LIMIT", "20"))
LOGIN_DELAY_MS = int(os.environ.get("LOGIN_DELAY_MS", "400"))
WEB_DIR = os.environ.get("WEB_DIR", "/web")
# Always on in the cluster. The only reason this is a knob is so the app can be
# smoke-tested over http://localhost on a laptop, where a Secure cookie is
# never sent back. Never set it to 0 in the Deployment.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"

def _named_cookie(name, value, max_age):
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


def _cookie(value, max_age):
    return _named_cookie("sid", value, max_age)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("playlists")

_NAME_OK = re.compile(r"[^\w \-_&',.()!?]", re.UNICODE)
_WS = re.compile(r"\s+")


class Secret:
    """Holds the cleartext password and nothing else can read it.

    bytearray-backed specifically so burn() can overwrite the bytes — a str cannot
    be mutated. There is no __getattr__ escape and repr/str are '***', so no
    f-string, no logging(exc_info=True) and no repr() of a locals dict can leak it.
    """
    __slots__ = ("_buf",)

    def __init__(self, text):
        self._buf = bytearray(text.encode())

    def reveal(self):
        return self._buf.decode()

    def burn(self):
        for i in range(len(self._buf)):
            self._buf[i] = 0
        self._buf = bytearray()

    def __repr__(self):
        return "***"

    __str__ = __repr__


# ---------- login throttle ----------

_TLOCK = threading.Lock()
_FAILS = {}          # username(casefold) -> [timestamps]
_GLOBAL = []         # timestamps of all failures


def _throttle_state(username):
    now = time.time()
    key = username.casefold()
    with _TLOCK:
        fails = [t for t in _FAILS.get(key, []) if now - t < LOGIN_FAIL_WINDOW]
        _FAILS[key] = fails
        globals_ = [t for t in _GLOBAL if now - t < 3600]
        _GLOBAL[:] = globals_
    if len(fails) >= LOGIN_FAIL_LIMIT:
        return "locked", int(LOGIN_FAIL_WINDOW - (now - fails[0]))
    if len(globals_) >= LOGIN_GLOBAL_FAIL_LIMIT:
        return "global", 0
    return "ok", 0


def _record_failure(username):
    now = time.time()
    with _TLOCK:
        _FAILS.setdefault(username.casefold(), []).append(now)
        _GLOBAL.append(now)


def _clear_failures(username):
    with _TLOCK:
        _FAILS.pop(username.casefold(), None)


# ---------- input validation ----------

def clean_playlist_name(raw, fallback="Importerad spellista"):
    """The client's filename is a SUGGESTION, never a path and never trusted.

    filesync.py derives a playlist name from a basename, which is fine for a file an
    operator placed on a PVC and not fine for one an internet client uploaded. So the
    name is normalised, stripped of control characters, allowlisted and capped here
    before it is ever shown, stored or sent to Navidrome.
    """
    text = unicodedata.normalize("NFC", (raw or "").strip())
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = _WS.sub(" ", text).strip()
    text = _NAME_OK.sub("", text).strip()
    text = text[:100].strip()
    return text or fallback


_SNIFF_ENCODINGS = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be")


def sniff_playlist(data):
    """Confirm the upload looks like a playlist export before parsing it.

    latin-1 is deliberately absent from the ladder: it never raises, so including it
    would make this check always pass. NULs are rejected in the DECODED text and
    never in the raw bytes — an iTunes .txt export is UTF-16 and legitimately full of
    NUL bytes.
    """
    head = data[:4096]
    for enc in _SNIFF_ENCODINGS:
        try:
            text = head.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if "\x00" in text:
            continue
        stripped = text.lstrip("\ufeff \t\r\n")
        first = stripped.split("\n", 1)[0]
        low = stripped[:200].lower()
        if "\t" in first:
            return True
        if low.startswith("<?xml") or low.startswith("<plist") or "<plist" in low:
            return True
        if stripped.startswith("#EXTM3U") or "#EXTINF" in stripped[:400]:
            return True
        if _looks_like_csv(first):
            return True
    return False


def _looks_like_csv(header_line):
    """A delimiter alone is not enough — require a column we can actually use.

    Otherwise any prose containing a comma would pass the sniff and then fail
    deeper in, with a worse message. The candidate names come from filesync so the
    sniff and the parser can never disagree about what is supported.
    """
    if not any(d in header_line for d in (",", ";", "\t")):
        return False
    cells = [c.strip().strip('"').casefold()
             for c in re.split(r"[,;\t]", header_line)]
    known = set(filesync._CSV_TITLE) | set(filesync._CSV_ARTIST)
    return any(c == k or (c and k in c) for c in cells for k in known)


# ---------- handler ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "playlists"
    sys_version = ""

    # --- plumbing ---

    def log_message(self, fmt, *args):
        pass    # replaced by the single structured line emitted in _send()

    def _emit(self, status, extra=None):
        rec = {"event": "req", "req_id": self.req_id, "method": self.command,
               "path": self.path.split("?", 1)[0], "status": status,
               "ms": int((time.monotonic() - self.t0) * 1000)}
        if extra:
            rec.update(extra)          # allowlisted keys only, see callers
        log.info(json.dumps(rec, ensure_ascii=False))

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8",
              headers=None, extra_log=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._emit(status, extra_log)

    def _redirect(self, location, cookie=None):
        # Relative Location only: the scheme is not inferable from request headers
        # here and must never be guessed.
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(303, b"", headers=headers)

    # --- request helpers ---

    def _cookies(self):
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                out[k.strip()] = v.strip()
        return out

    def _pre_csrf(self):
        """Double-submit token for the one POST that has no session yet.

        The value is in a cookie AND in a hidden field; a cross-site attacker can
        forge the field but cannot read the cookie, so the two only agree on a form
        this app actually served. Returns (value, set_cookie_or_None).
        """
        existing = self._cookies().get("csrf0")
        if existing and len(existing) >= 20:
            return existing, None
        fresh = secrets.token_urlsafe(32)
        return fresh, _named_cookie("csrf0", fresh, 3600)

    def _pre_csrf_ok(self, supplied):
        cookie = self._cookies().get("csrf0") or ""
        return bool(cookie) and bool(supplied) and hmac.compare_digest(cookie, supplied)

    def _session(self):
        return jobs.get_session(self._cookies().get("sid"))

    def _read_body(self, cap):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return None                # chunked or absent: refused, not streamed
        size = int(length)
        if size > cap:
            return b""                 # signals "too big" to the caller
        return self.rfile.read(size)

    def _origin_ok(self):
        """Reject a MISMATCHED Origin/Referer, but never require one to be present.

        Origin and Referer are both optional on a same-origin form POST — browsers
        differ, and this app sets Referrer-Policy itself — so treating their absence
        as an attack just locks real people out. It did exactly that: the login form
        carried no token of its own, so this was the only gate and every real browser
        POST got a 403.

        The actual CSRF control is a token the attacker cannot read: the session
        token on authenticated POSTs, and a double-submit cookie on the login form.
        This stays as defence in depth for the case where a header IS sent.
        """
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and origin.lower() != "null":
            return bool(TRUSTED_ORIGIN) and origin == TRUSTED_ORIGIN
        referer = self.headers.get("Referer") or ""
        if referer:
            return bool(TRUSTED_ORIGIN) and referer.startswith(TRUSTED_ORIGIN + "/")
        return True

    def _csrf_ok(self, session, supplied):
        return bool(session) and bool(supplied) and hmac.compare_digest(
            session.csrf, supplied)

    # --- routing ---

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        # Without this BaseHTTPRequestHandler answers 501, which makes any uptime
        # check or tool that probes with HEAD look like an outage. _send() already
        # suppresses the body for HEAD.
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        self.req_id = secrets.token_hex(4)
        self.t0 = time.monotonic()
        path = urlparse(self.path).path
        try:
            if method == "GET":
                self._route_get(path)
            else:
                if not self._origin_ok():
                    self._send(403, ui.problem(
                        "Sidan kunde inte skicka formuläret. Öppna sidan på nytt.",
                        back="/", title="Börja om"))
                    return
                self._route_post(path)
        except BrokenPipeError:
            pass
        except Exception as e:                              # noqa: BLE001
            # Only the class name — a traceback could carry form values.
            log.error(json.dumps({"event": "unhandled", "req_id": self.req_id,
                                  "err": type(e).__name__}))
            try:
                self._send(500, ui.problem(
                    "Något oväntat hände. Försök igen om en stund.", back="/"))
            except Exception:                               # noqa: BLE001
                pass

    def _route_get(self, path):
        if path == "/healthz":
            # Deliberately does NOT touch Navidrome: a liveness probe coupled to a
            # dependency turns a Navidrome restart into a killed pod here.
            self._send(200, b"ok", ctype="text/plain; charset=utf-8")
            return
        if path.startswith("/web/"):
            self._static(path)
            return

        session = self._session()
        if path == "/":
            if session:
                self._redirect("/ny")
            else:
                token, set_cookie = self._pre_csrf()
                self._send(200, ui.login(csrf=token),
                           headers=({"Set-Cookie": set_cookie} if set_cookie else None))
            return
        if path == "/favicon.ico":
            self._send(404, b"", ctype="text/plain; charset=utf-8")
            return
        if not session:
            self._redirect("/")
            return

        if path == "/ny":
            self._send(200, ui.choose(session, session.csrf))
            return
        if path == "/forhandsgranska":
            job = jobs.get_job(session.job_id)
            if not job:
                self._send(410, ui.expired())
                return
            if job.state == "running":
                self._send(200, ui.working(job, library.progress_for(session.fingerprint)))
            elif job.state == "error":
                self._send(200, ui.problem(job.error or "Det gick inte."))
            elif job.result is not None:
                self._send(200, ui.done(job))
            else:
                self._send(200, ui.preview(job, session.csrf, job.filename or ""),
                           extra_log={"tracks": job.source_total,
                                      "matched": len(job.pairs)})
            return
        if path == "/status":
            job = jobs.get_job(session.job_id)
            payload = {"state": job.state if job else "gone",
                       "step": job.step if job else None,
                       "read": library.progress_for(session.fingerprint)}
            self._send(200, json.dumps(payload), ctype="application/json")
            return
        if path == "/klart":
            job = jobs.get_job(session.job_id)
            self._send(200, ui.done(job) if job and job.result else ui.expired())
            return
        self._send(404, ui.problem("Sidan finns inte.", back="/"))

    def _route_post(self, path):
        if path == "/logga-in":
            self._login()
            return
        session = self._session()
        if not session:
            # Bouncing straight to the login page is what a dead session used to
            # do, and it looked exactly like the app swallowing the upload: he
            # picked a file, app.js submitted it, and he landed on a login screen
            # with no explanation and his file gone. Say what happened instead.
            token, set_cookie = self._pre_csrf()
            self._send(200, ui.session_gone(csrf=token),
                       headers=({"Set-Cookie": set_cookie} if set_cookie else None),
                       extra_log={"action": "session-expired"})
            return
        if path == "/logga-ut":
            body = parse_qs((self._read_body(MAX_FORM) or b"").decode("utf-8", "replace"))
            if self._csrf_ok(session, (body.get("csrf") or [""])[0]):
                jobs.drop_session(session.sid)
            self._redirect("/", cookie=_cookie("", 0))
            return
        if path == "/ladda-upp":
            self._upload(session)
            return
        if path == "/skapa":
            self._create(session)
            return
        self._send(404, ui.problem("Sidan finns inte.", back="/"))

    # --- static ---

    def _static(self, path):
        name = os.path.basename(path)
        if name not in ("style.css", "app.js"):
            self._send(404, b"", ctype="text/plain")
            return
        full = os.path.join(WEB_DIR, name)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, b"", ctype="text/plain")
            return
        ctype = "text/css; charset=utf-8" if name.endswith(".css") \
            else "application/javascript; charset=utf-8"
        self._send(200, data, ctype=ctype)

    # --- login ---

    def _login(self):
        raw = self._read_body(MAX_FORM)
        if raw is None or raw == b"":
            self._send(400, ui.login(csrf=self._cookies().get("csrf0", ""),
                                 error="Något blev fel med formuläret. Försök igen."))
            return
        form = parse_qs(raw.decode("utf-8", "replace"))
        if not self._pre_csrf_ok((form.get("csrf") or [""])[0]):
            # A stale tab or a cookie-less client, not necessarily an attack.
            token, set_cookie = self._pre_csrf()
            self._send(200, ui.login(
                csrf=token,
                error="Sidan hade blivit gammal. Fyll i uppgifterna igen."),
                headers=({"Set-Cookie": set_cookie} if set_cookie else None))
            return
        username = (form.get("anvandarnamn") or [""])[0].strip()
        secret = Secret((form.get("losenord") or [""])[0])
        # Overwrite the parsed structures that still hold the cleartext.
        form.clear()
        del raw

        try:
            if not username or not secret.reveal():
                self._send(200, ui.login(csrf=self._cookies().get("csrf0", ""), error="Fyll i både användarnamn och lösenord."))
                return

            state, wait = _throttle_state(username)
            if state == "locked":
                self._send(429, ui.login(csrf=self._cookies().get("csrf0", ""), locked_seconds=wait))
                return
            if state == "global":
                self._send(429, ui.login(
                    error="För många inloggningsförsök just nu. Vänta en stund."))
                return

            # Flat delay on every attempt, success or not, so timing says nothing.
            time.sleep(LOGIN_DELAY_MS / 1000.0)

            creds = subsonic.derive_creds(username, secret)
        finally:
            secret.burn()               # the cleartext's life ends here

        try:
            subsonic.ping(creds)
        except subsonic.NavidromeError as e:
            if e.code == subsonic.ERR_BAD_CREDENTIALS:
                _record_failure(username)
                # Identical text for a wrong password and a nonexistent user, so the
                # form cannot be used to discover who has an account.
                self._send(200, ui.login(
                    error="Fel användarnamn eller lösenord. Det är samma som i Navidrome."),
                    extra_log={"user": username, "action": "login-denied"})
                return
            self._send(200, ui.login(
                error="Musikservern svarade inte som väntat. Försök igen om en stund."))
            return
        except subsonic.NavidromeUnreachable:
            self._send(200, ui.login(
                error="Jag kunde inte nå musikservern. Försök igen om en stund."))
            return

        _clear_failures(username)
        try:
            fingerprint = subsonic.get_music_folders(creds)
        except (subsonic.NavidromeError, subsonic.NavidromeUnreachable):
            fingerprint = ()
        session = jobs.new_session(username, creds, fingerprint)
        # He still has to find the file, so start the crawl now and he waits for
        # nothing later.
        library.kick_refresh(creds, fingerprint)
        self._redirect("/ny", cookie=_cookie(session.sid, jobs.SESSION_MAX))
        self._emit(303, {"user": username, "action": "login-ok"})

    # --- upload ---

    def _upload(self, session):
        raw = self._read_body(MAX_UPLOAD)
        if raw == b"":
            self._send(413, ui.too_big(MAX_UPLOAD // (1024 * 1024)))
            return
        if raw is None:
            self._send(400, ui.problem("Filen kunde inte tas emot. Försök igen."))
            return

        ctype = self.headers.get("Content-Type") or ""
        fields, filename, filedata = {}, "", None
        if ctype.startswith("multipart/form-data"):
            fields, filename, filedata = self._parse_multipart(ctype, raw)
        else:
            fields = {k: v[0] for k, v in
                      parse_qs(raw.decode("utf-8", "replace")).items()}

        if not self._csrf_ok(session, fields.get("csrf", "")):
            self._send(403, ui.problem(
                "Sidan hade blivit gammal. Öppna den på nytt.", back="/ny"))
            return

        pasted = (fields.get("text") or "").strip()
        try:
            if filedata:
                if not sniff_playlist(filedata):
                    self._send(200, ui.problem(
                        "Det ser inte ut som en spellista. Exportera från Musik och "
                        "välj „Vanlig text”.", back="/ny"))
                    return
                parsed = jobs.parse_upload(filedata, filename)
            elif pasted:
                parsed = jobs.parse_text(pasted)
                filename = ""
            else:
                self._send(200, ui.problem(
                    "Ingen fil valdes. Tryck på knappen och välj din spellista.",
                    back="/ny"))
                return
        except jobs.ParseProblem as e:
            self._send(200, ui.problem(str(e), back="/ny"))
            return

        # One playlist per upload keeps the whole flow to a single decision.
        name_hint = fields.get("namn") or parsed[0][0] or filename
        target = clean_playlist_name(name_hint)
        tracks = parsed[0][1]

        job = jobs.Job(target, filename)
        jobs.put_job(job)
        session.job_id = job.job_id
        jobs.start_job(session, job, tracks)
        self._redirect("/forhandsgranska")

    def _parse_multipart(self, ctype, raw):
        """email.parser over the already-bounded bytes.

        Not `cgi` (removed in 3.13), not a hand-rolled boundary splitter, and not
        Werkzeug/Starlette/python-multipart — so none of their multipart-DoS
        advisories are in play.
        """
        parser = email.parser.BytesFeedParser(policy=email.policy.default)
        parser.feed(b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n")
        parser.feed(raw)
        msg = parser.close()
        fields, filename, filedata = {}, "", None
        for i, part in enumerate(msg.iter_parts()):
            if i >= 4:
                break               # a legitimate submit has csrf, fil, namn, text
            name = part.get_param("name", header="content-disposition")
            fname = part.get_param("filename", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if fname:
                filename = os.path.basename(str(fname))
                filedata = payload or None
            elif name:
                fields[str(name)] = payload.decode("utf-8", "replace")
        return fields, filename, filedata

    # --- commit ---

    def _create(self, session):
        raw = self._read_body(MAX_FORM)
        form = parse_qs((raw or b"").decode("utf-8", "replace"))
        if not self._csrf_ok(session, (form.get("csrf") or [""])[0]):
            self._send(403, ui.problem("Sidan hade blivit gammal.", back="/ny"))
            return
        job = jobs.get_job((form.get("jobb") or [""])[0])
        if not job or job.job_id != session.job_id:
            self._send(410, ui.expired())
            return
        if job.result is not None:
            self._redirect("/klart")            # already done; never write twice
            return
        if not jobs.claim_commit(job, (form.get("bekrafta") or [""])[0]):
            self._send(409, ui.problem(
                "Spellistan är redan på väg in. Vänta en stund och titta i Symfonium.",
                back="/ny"))
            return

        creds = session.creds
        ids = [ident for _, _, ident in job.pairs]
        if not ids or creds is None:
            self._send(200, ui.problem("Det fanns inga låtar att lägga in.", back="/ny"))
            return

        try:
            if job.existing:
                have = {e.get("id") for e in
                        (subsonic.get_playlist(creds, job.existing["id"]).get("entry") or [])}
                to_add = [i for i in ids if i not in have]
                if not to_add:
                    job.result = {"added": 0, "nothing_new": True,
                                  "playlist_id": job.existing["id"]}
                    self._redirect("/klart")
                    self._emit(303, {"user": session.username, "action": "nothing-new"})
                    return
                pid = subsonic.extend_playlist(creds, job.existing["id"], to_add)
                added = len(to_add)
            else:
                pid = subsonic.create_playlist(creds, job.target_name, ids)
                added = len(ids)
        except subsonic.NavidromeAmbiguousWrite:
            # Never retry a write: there is no unique constraint on (name, owner),
            # so a retry means a duplicate playlist. Read instead.
            found = subsonic.reconcile(creds, session.username, job.target_name)
            if found:
                job.result = {"added": len(ids), "playlist_id": found["id"]}
                self._redirect("/klart")
                return
            self._send(200, ui.problem(
                "Jag vet inte om spellistan hann sparas. Titta i Symfonium innan du "
                "försöker igen, så den inte blir dubbel.", back="/ny"))
            return
        except (subsonic.NavidromeError, subsonic.NavidromeUnreachable, ValueError):
            self._send(200, ui.problem(
                "Spellistan kunde inte sparas just nu. Försök igen om en stund.",
                back="/ny"))
            return

        # Success is never claimed on the strength of a 200 from the write.
        try:
            check = subsonic.verify(creds, session.username, pid, ids)
            if not check["private_ok"]:
                subsonic.repair_private(creds, pid)
            if not check["owner_ok"]:
                log.error(json.dumps({"event": "owner-mismatch", "req_id": self.req_id,
                                      "owner": check.get("owner")}))
        except (subsonic.NavidromeError, subsonic.NavidromeUnreachable):
            pass                                 # the write landed; the read can wait

        job.result = {"added": added, "playlist_id": pid}
        self._redirect("/klart")
        self._emit(303, {"user": session.username, "action": "created",
                         "playlist_id": pid, "matched": added})


def main():
    if not TRUSTED_ORIGIN:
        log.error(json.dumps({"event": "config", "err": "TRUSTED_ORIGIN is not set"}))
        raise SystemExit(2)
    try:
        import pyexpat
        if tuple(int(x) for x in pyexpat.EXPAT_VERSION.split("_")[-1].split(".")) < (2, 7, 2):
            log.warning(json.dumps({"event": "config",
                                    "warn": f"old expat {pyexpat.EXPAT_VERSION}"}))
    except Exception:                                       # noqa: BLE001
        pass
    jobs.start_janitor()
    log.info(json.dumps({"event": "start", "port": PORT, "origin": TRUSTED_ORIGIN}))
    srv = ThreadingHTTPServer(("", PORT), Handler)
    srv.daemon_threads = True
    srv.timeout = 30

    # Without this, SIGTERM kills serve_forever() mid-call and the container exits
    # non-zero, so every rollout leaves a pod behind in Failed/Terminated. shutdown()
    # has to be called from another thread — it blocks until serve_forever returns.
    def _stop(_signum, _frame):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        log.info(json.dumps({"event": "stop"}))


if __name__ == "__main__":
    main()
