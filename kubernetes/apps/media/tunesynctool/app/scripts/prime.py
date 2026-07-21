#!/usr/bin/env python3
"""prime.py — one-time Spotify OAuth cache priming (headless paste-back).

Run ONCE inside the persistent tunesynctool pod. Writes the token to
<WORKDIR>/.cache on the PVC, so every later sync.py / plexsync.py run reuses it
with no browser and no re-auth (spotipy refreshes the token automatically).

    kubectl exec -it -n media deploy/tunesynctool -- /work/venv/bin/python /scripts/prime.py

It prints "Go to the following URL: https://accounts.spotify.com/authorize?...".
Open that in your laptop browser, log in as the target Spotify user, click Agree.
The browser redirects to http://127.0.0.1:8888/callback?code=... which fails to
load (nothing is listening — expected). Copy the FULL URL from the address bar and
paste it at the "Enter the URL you were redirected to:" prompt.

Env (all defaulted, normally injected by the Deployment):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET   # Spotify app creds (from Secret)
    SCOPES        # default: the full read/modify playlist scope set
    REDIRECT      # default: http://127.0.0.1:8888/callback
    TUNESYNC_WORKDIR   # default: /work  (cache = <workdir>/.cache)
"""
import os
import sys

from spotipy.oauth2 import SpotifyOAuth

WORKDIR = os.environ.get("TUNESYNC_WORKDIR", "/work")
CACHE = os.path.join(WORKDIR, ".cache")
SCOPES = os.environ.get(
    "SCOPES",
    "user-library-read,playlist-read-private,playlist-read-collaborative,"
    "playlist-modify-public,playlist-modify-private",
)
REDIRECT = os.environ.get("REDIRECT", "http://127.0.0.1:8888/callback")


def main():
    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        if not os.environ.get(var):
            print(f"ERROR: {var} is not set", file=sys.stderr)
            sys.exit(1)

    oa = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=REDIRECT,
        scope=SCOPES,
        open_browser=False,
        cache_path=CACHE,
    )
    # check_cache=False forces the interactive paste-back flow even if a (possibly
    # stale) cache exists — use this to re-prime as a different Spotify user.
    oa.get_access_token(check_cache=False)
    print(f"OK - token cached at {CACHE}")


if __name__ == "__main__":
    main()
