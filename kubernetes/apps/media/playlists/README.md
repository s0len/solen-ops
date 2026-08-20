# playlists — självbetjäning: ladda upp en spellista, få den privat i Navidrome

A small web app at `https://playlists.${SECRET_DOMAIN}` where a **non-technical
family member logs in with their own Navidrome credentials**, uploads an
iTunes/Apple Music playlist export, sees what matched, and gets a playlist in
Navidrome that is **theirs and private**.

It exists because Symfonium cannot do this. Its developer is explicit: for a managed
server like Navidrome "the playlist have to be on your server" — Symfonium imports
playlists *from* a server, never a file *to* one. And Navidrome's own `.m3u`
auto-import resolves entries by **file path**, not by artist/title, so an export from
someone else's Mac can never match. The matching step has to happen server-side, and
that is what this app is.

## Why the playlist ends up private and owned by them

This is structural, not a runtime check:

| Fact | Where |
| --- | --- |
| `createPlaylist` sets `OwnerID` to the **authenticated** user, unconditionally. There is no impersonation path. | `core/playlists/playlists.go` |
| A new playlist is built as `&model.Playlist{Name: name}`, so `Public` is the Go zero value — false. | same |
| The column agrees independently: `public bool default FALSE not null`. | `db/migrations/…_playlist_case_insensitive_name.sql` |
| `createPlaylist` has **no `public` parameter at all** — the handler reads only `songId`, `playlistId`, `name`. One sent there is silently ignored. | `server/subsonic/playlists.go` |
| `updatePlaylist` touches `Public` only when given a non-nil pointer, so omitting it never changes an existing choice. | `core/playlists/playlists.go` |

So "private" requires **no parameter**. `subsonic.py` contains no function that can
make a playlist public on the create path, and that absence is the guarantee — don't
add one. `repair_private()` is the only place `public` is ever sent, and only if a
read-back found a playlist public.

**Their login IS the authorization.** That is the whole design. No admin
impersonation, no stored passwords, and no service account: this app ships **zero
Kubernetes Secrets**.

Verified end-to-end against the live server before first deploy, with a throwaway
non-admin account: playlist created → `owner = that user`, `public = 0` in the DB, a
different non-admin could not see it in `getPlaylists`, and a direct `getPlaylist` by
id from that other account returned `error 70: playlist not found` — a denial that
does not even leak that the playlist exists.

**Caveat to state out loud to whoever uses it:** you, as Navidrome admin, still see
every playlist. Navidrome grants admins that. Other non-admin users (`susanna`,
`Silvercheek`, `audiomuse`) do not.

## How long the password exists

Roughly 10–20 ms, inside one function frame.

Subsonic token auth is `md5(password + salt)`, and that pair stays valid until the
password changes. So `POST /logga-in` derives `(salt, token)` once, burns the
password, and every later call in the flow authenticates with the derived pair. The
cleartext is held in a `Secret` object — bytearray-backed so `burn()` can actually
zero it, `__repr__`/`__str__` hard-wired to `***`, no getter — and burned in a
`finally`.

Honest limitations, stated rather than glossed:

- The derived `(salt, token)` is **password-equivalent authority**. It cannot be
  revoked short of a password change. It is in-process only, never a cookie, never
  serialised, never logged, dropped when the session ends (20 min idle / 60 min hard).
- Navidrome 0.63.2 has **no app-specific passwords, no API keys and no access
  tokens** — the OpenSubsonic `apiKeyAuthentication` extension is not implemented.
  So the app necessarily handles the real password once per login. Navidrome also
  stores passwords reversibly by design, because Subsonic token auth needs the
  cleartext server-side. The honest framing: **this app is as trusted as Navidrome
  itself.**
- CPython does not zero freed memory, so `Secret.burn()` bounds reachability rather
  than scrubbing every copy the interpreter may have made.

## Architecture

| Piece | What |
| --- | --- |
| Deployment `playlists` | `python:3.12-slim`, **stdlib only** — no pip, no venv, no image build, no PVC. `command: python3 -u /src/server.py`. `replicas: 1` is load-bearing. |
| ConfigMap `playlists-src` → `/src` | `server.py`, `subsonic.py`, `library.py`, `jobs.py`, `ui.py` |
| ConfigMap `playlists-web` → `/web` | `style.css`, `app.js` |
| ConfigMap `tunesynctool-scripts` → `/shared` | **Another app's ConfigMap.** `matcher.py` and `filesync.py` are imported from here via `PYTHONPATH=/shared`. |
| `emptyDir` → `/tmp` | Uploads are spooled here for the parsers and unlinked immediately. 64Mi cap. |
| HTTPRoute `playlists` | Hand-written (not the chart's `route:` block) so it can strip client-IP headers, and so the no-auth SecurityPolicy can target it by name. |
| CiliumNetworkPolicy | Egress restricted to Navidrome + DNS. Nothing else. |

`replicas: 1` matters: sessions, jobs, the login throttle and the library index are
all in-process. A second replica would not fail loudly — it would log people out at
random and build the index twice.

### The `/shared` mount is a real cross-app dependency

`matcher.py` is the single source of truth for matching across three consumers
(`plexsync.py`, `filesync.py`, this app). Rather than vendoring a copy that would
drift, this pod mounts **tunesynctool's** ConfigMap read-only.

Consequences to know:

- `ks.yaml` has `dependsOn: tunesynctool`.
- Editing `matcher.py` or `filesync.py` rolls **both** pods and must be checked
  against both.
- Deleting the tunesynctool app breaks this one loudly — `ImportError` at startup,
  CrashLoopBackOff.
- `filesync.py`'s `parse_itunes_txt` / `parse_itunes_xml` / `parse_m3u` are now an
  **API for another app**. Don't refactor their signatures without checking
  `app/src/jobs.py`.

## The flow

1. `GET /` — login. Same username and password as in Symfonium.
2. `POST /logga-in` — validates with Subsonic `ping` (not `/auth/login`, which is
   IP-throttled 5-per-20s and would let one person lock out the household from
   behind the shared gateway IP). Derives `(salt, token)`, burns the password, and
   **kicks the library crawl immediately** — he is still hunting for the file, so he
   waits for nothing later.
3. `GET /ny` — pick a file, or paste `Artist - Låt` lines.
4. `POST /ladda-upp` — size-capped read, content sniff, parse, then a 303. No HTTP
   request is ever held open for the real work, which is what makes Cloudflare's
   ~100 s origin timeout and Envoy's 60 s idle timeout structurally unreachable.
5. `GET /forhandsgranska` — self-advancing progress page, then the preview:
   `Hittade 13 av 26 låtar`, what each track matched to, misses behind a closed
   `<details>`.
6. `POST /skapa` — one write, as him.
7. `GET /klart`.

### Additive, never a mirror

An upload of a playlist he already has **adds** the missing tracks and removes
nothing. Deliberately `updatePlaylist` + `songIdToAdd`, not
`createPlaylist` + `playlistId` — the latter *replaces* the whole track list, which
would silently discard tracks he added himself in Symfonium.

Idempotency keys on `(owner, exact name)`. **Limitation:** renaming the playlist in
Symfonium orphans it, and the next upload of the same file creates a new one.

### Writes are never retried

Navidrome has **no unique constraint on `(name, owner_id)`** — only a plain index on
`name`. A retried `createPlaylist` is therefore a duplicate playlist, not an
idempotent no-op. So every write goes out with `retries=0`, and a write whose answer
was lost raises `NavidromeAmbiguousWrite`, which is resolved by **reading** —
`reconcile()` — never by trying again. A double-click is stopped earlier: the commit
flag is set under a lock before any network call, and an HMAC commit token binds the
write to the exact preview he was shown.

## Hardening

The route is exempt from the gateway's OIDC policy — it has to be, he has no account
there — so the in-app controls are **not optional**.

- **CSRF**: a multipart POST is a CORS-simple request, so `Origin` must equal
  `TRUSTED_ORIGIN` (falling back to `Referer`), plus a per-session token compared
  with `hmac.compare_digest`. Verified: no Origin → 403, wrong Origin → 403, bad
  token → 403.
- **Login throttle**: per-username sliding window (5 per 15 min) reported as a
  concrete wait, a global brake (20/hour → 429), and a flat 400 ms delay on *every*
  attempt so timing reveals nothing. Identical message for a wrong password and a
  nonexistent user, so the form cannot enumerate accounts.
- **Uploads**: `Content-Length` required, numeric and ≤ 8 MiB, then exactly that many
  bytes read — never an unbounded `read()`, and a missing/chunked length is refused
  rather than streamed. Verified: a 9.96 MB file gets a Swedish 413.
- **Multipart** via `email.parser`, max 4 parts. Not `cgi` (removed in 3.13), not a
  hand-rolled boundary splitter, and not Werkzeug/Starlette/python-multipart — so
  none of their multipart-DoS advisories apply here.
- **Content sniff** before parsing: decode the first 4 KiB as
  `utf-8-sig`/`utf-16`/`utf-16-le`/`utf-16-be` — **`latin-1` is deliberately absent
  because it never raises and would make the check always pass** — reject NULs in the
  *decoded* text (never the raw bytes: an iTunes `.txt` is UTF-16 and legitimately
  full of NULs), then require a tab in the first line, or an XML/plist prefix, or
  `#EXTM3U`/`#EXTINF`.
- **The client's filename is a suggestion, never a path.** It is NFC-normalised,
  stripped of control characters, allowlisted and capped at 100 chars before it is
  shown, stored or sent anywhere.
- **No proxy-header trust.** `X-Forwarded-Proto` is forwarded verbatim by
  envoy-external and `X-Real-IP` is rewritten by the gateway's own patch policy, so
  the scheme is hard-coded, every `Location` is relative, and the HTTPRoute strips
  those headers anyway.
- **Nothing leaks the password.** No traceback ever reaches a response; the catch-all
  logs only the exception class name and a request id. Request logging uses an
  allowlisted field set that structurally cannot contain a password or the `t`/`s`
  pair. Subsonic credentials always travel in the POST body, never a query string.
- Security headers on every response, including a CSP with no `unsafe-inline` (hence
  no inline `<style>`/`<script>` anywhere).

**Deliberately not built:** a gateway-level request-body cap (an
`EnvoyExtensionPolicy` Lua filter). The in-app 8 MiB cap is the control that always
applies — it is also the only one that applies to someone hitting the gateway IP
directly on the LAN — and adding hand-written Lua to a production route that cannot
be tested before it is pushed was the riskiest part of this change for the least
benefit. Cloudflare's 100 MB is the only other cap in the path. Add it later if a cap
above 8 MiB ever proves necessary.

## Match rate is about the library, not the app

A miss means the track is not owned. Silvercheek's first playlist matched 13 of 26,
and the 13 misses were verified genuine gaps — mostly **covers** where only the
original is on the shelf. The preview says this in plain Swedish
(`13 låtar finns inte i musiksamlingen, så de kan inte läggas till`) precisely so
nobody concludes the tool is broken.

## Local development

Stdlib-only, so it runs on a laptop — which is how the whole flow was tested before
the first deploy:

```bash
kubectl port-forward -n media svc/navidrome 14533:4533 &

cd kubernetes/apps/media/playlists/app
PYTHONPATH=../../tunesynctool/app/scripts \
NAVIDROME_URL=http://127.0.0.1 NAVIDROME_PORT=14533 \
TRUSTED_ORIGIN=http://127.0.0.1:8099 PORT=8099 \
WEB_DIR=./web COOKIE_SECURE=0 TMPDIR=/tmp \
  python3 -u src/server.py
```

`COOKIE_SECURE=0` exists **only** for this: a `Secure` cookie is never sent back over
`http://localhost`. Never set it in the Deployment.

## Failures that look like something else

| Symptom | Actual cause |
| --- | --- |
| He lands on an Authelia 2FA screen | The HTTPRoute is missing from `kubernetes/apps/media/no-auth-securitypolicy.yaml`. Flux is green, DNS and TLS work, and there is no recoverable path for him. |
| Every login fails with a connection error while Navidrome is healthy | navidrome's pod labels changed, so the CiliumNetworkPolicy no longer matches. |
| CrashLoopBackOff with a `SyntaxError` right after an edit | The `kustomize.toolkit.fluxcd.io/substitute: disabled` annotation was dropped from `app/kustomization.yaml`. (The source currently contains no `$` at all, so this is latent rather than active — but it will bite the first time someone adds an f-string with a `$`.) |
| CrashLoopBackOff with `ImportError: matcher` | The tunesynctool app or its ConfigMap is gone. |
| Playlist appears but he cannot see it in Symfonium | Pull down to refresh. Symfonium caches the playlist list. |
