#!/usr/bin/env python3
"""Sync Hardcover lists to Audiobookshelf collections and Grimmory shelves.

Fetches book lists from Hardcover (GraphQL with a guest token minted by
rendering a page through FlareSolverr or the Jina reader proxy), matches
titles+authors against the libraries, and reconciles ABS collections and
Grimmory shelves to mirror the lists. Stdlib only.

Environment:
  ABS_URL, ABS_API_KEY            Audiobookshelf base URL + API key
  GRIMMORY_URL                    Grimmory base URL
  GRIMMORY_USERNAME/PASSWORD      Login credentials (preferred), or
  GRIMMORY_TOKEN                  a ready JWT
  FLARESOLVERR_URL                e.g. http://flaresolverr:8191 (optional;
                                  falls back to https://r.jina.ai)
  DRY_RUN                         "1" = report only, change nothing
"""
import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

LISTS = [
    ("adam", "npr-top-100-science-fiction-fantasy"),
    ("hardcover", "100-new-york-times-best-books-of-the-21st-century"),
    ("hardcover", "the-31-best-fantasy-books-everyone-should-read"),
    ("mikedeas", "200-ish-books-to-read-before-you-die"),
    ("hardcover", "winners-of-the-booker-prize"),
]

HC_API = "https://api.hardcover.app/v1/graphql"
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def http(method, url, headers=None, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hardcover-sync/1.0",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> {e.code}: {e.read()[:500]}")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------- hardcover
def jwt_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        return 0


def get_guest_token():
    """Mint a Hardcover guest JWT by rendering a list page through a proxy.

    Guest tokens live ~15 min; reject tokens about to expire (a cached page
    can hand back a stale one)."""
    fs = os.environ.get("FLARESOLVERR_URL")
    last_err = None
    for username, slug in LISTS:
        page = f"https://hardcover.app/@{username}/lists/{slug}"
        try:
            if fs:
                out = http("POST", f"{fs.rstrip('/')}/v1",
                           body={"cmd": "request.get", "url": page,
                                 "maxTimeout": 60000}, timeout=90)
                html = out.get("solution", {}).get("response", "")
            else:
                req = urllib.request.Request(
                    f"https://r.jina.ai/{page}",
                    headers={"X-Return-Format": "html",
                             "X-No-Cache": "true",
                             "User-Agent": "curl/8.7.1"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    html = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            continue
        for m in re.finditer(
                r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", html):
            if jwt_exp(m.group(0)) > time.time() + 300:
                return m.group(0)
        last_err = "only stale tokens in page"
    sys.exit(f"could not extract Hardcover guest token (last error: {last_err})")


def gql(token, query, variables):
    out = http("POST", HC_API,
               headers={"Authorization": f"Bearer {token}"},
               body={"query": query, "variables": variables})
    if out.get("errors"):
        sys.exit(f"hardcover graphql errors: {out['errors']}")
    return out["data"]


LIST_Q = """
query ($username: citext!, $slug: String!) {
  lists(where: {slug: {_eq: $slug}, user: {username: {_eq: $username}}}) {
    id name books_count
  }
}"""

BOOKS_Q = """
query ($listId: Int!, $offset: Int!) {
  list_books(where: {list_id: {_eq: $listId}},
             order_by: {position: asc}, limit: 100, offset: $offset) {
    book { title contributions { author { name } } }
  }
}"""


def fetch_hardcover_lists():
    token = get_guest_token()
    lists = []
    for username, slug in LISTS:
        rows = gql(token, LIST_Q, {"username": username, "slug": slug})["lists"]
        if not rows:
            sys.exit(f"hardcover list not found: @{username}/{slug}")
        lst = rows[0]
        books, offset = [], 0
        while True:
            page = gql(token, BOOKS_Q,
                       {"listId": lst["id"], "offset": offset})["list_books"]
            if not page:
                break
            for r in page:
                b = r["book"]
                books.append((b["title"],
                              [c["author"]["name"]
                               for c in (b.get("contributions") or [])
                               if c.get("author")]))
            offset += 100
        url = f"https://hardcover.app/@{username}/lists/{slug}"
        lists.append({"name": lst["name"], "url": url, "books": books})
        print(f"hardcover: {lst['name']}: {len(books)} books")
    return lists


# ---------------------------------------------------------------- matching
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(the|a|an) ", "", s)
    return re.sub(r" +", " ", s)


def title_keys(title):
    keys = {norm(title)}
    base = re.split(r"[:(]", title)[0]
    keys.add(norm(base))
    return {k for k in keys if k}


def last_names(authors):
    return {norm(a).split()[-1] for a in authors if norm(a)}


def build_index(items):
    """items: list of (id, title, authors). Returns key -> [(id, authors)]."""
    idx = {}
    for item_id, title, authors in items:
        for k in title_keys(title):
            idx.setdefault(k, []).append((item_id, authors))
    return idx


def match_list(books, idx):
    """Return set of matched item ids for (title, authors) pairs."""
    matched = set()
    for title, authors in books:
        want = last_names(authors)
        for k in title_keys(title):
            for item_id, item_authors in idx.get(k, []):
                have = last_names(item_authors)
                if not want or not have or want & have:
                    matched.add(item_id)
    return matched


# ---------------------------------------------------------------- abs
def abs_sync(hc_lists):
    base = os.environ["ABS_URL"].rstrip("/")
    hdrs = {"Authorization": f"Bearer {os.environ['ABS_API_KEY']}"}
    libraries = http("GET", f"{base}/api/libraries", hdrs)["libraries"]
    collections = http("GET", f"{base}/api/collections", hdrs)["collections"]

    for lib in libraries:
        if lib["mediaType"] != "book":
            continue
        data = http("GET", f"{base}/api/libraries/{lib['id']}/items?limit=0",
                    hdrs)
        items = []
        for it in data["results"]:
            md = it["media"]["metadata"]
            authors = [a.strip() for a in (md.get("authorName") or "").split(",")
                       if a.strip()]
            items.append((it["id"], md.get("title") or "", authors))
        idx = build_index(items)

        for lst in hc_lists:
            ids = sorted(match_list(lst["books"], idx))
            existing = next((c for c in collections
                             if c["libraryId"] == lib["id"]
                             and c["name"] == lst["name"]), None)
            tag = f"ABS [{lib['name']}] '{lst['name']}'"
            if not ids and not existing:
                print(f"{tag}: 0 matches, skipping")
                continue
            desc = (f"Auto-synced from {lst['url']} "
                    f"({len(lst['books'])} books on list, "
                    f"{len(ids)} in library)")
            if DRY_RUN:
                print(f"{tag}: {len(ids)}/{len(lst['books'])} matched "
                      f"({'update' if existing else 'create'})")
                continue
            if existing:
                cur = sorted(b["id"] for b in existing.get("books") or [])
                if cur == ids and existing.get("description") == desc:
                    print(f"{tag}: up to date ({len(ids)} books)")
                    continue
                http("PATCH", f"{base}/api/collections/{existing['id']}",
                     hdrs, {"books": ids, "description": desc})
                print(f"{tag}: updated, {len(ids)} books (was {len(cur)})")
            else:
                http("POST", f"{base}/api/collections", hdrs,
                     {"libraryId": lib["id"], "name": lst["name"],
                      "description": desc, "books": ids})
                print(f"{tag}: created with {len(ids)} books")


# ---------------------------------------------------------------- grimmory
def grimmory_token(base):
    tok = os.environ.get("GRIMMORY_TOKEN")
    if tok:
        return tok
    out = http("POST", f"{base}/api/v1/auth/login",
               body={"username": os.environ["GRIMMORY_USERNAME"],
                     "password": os.environ["GRIMMORY_PASSWORD"]})
    return out["accessToken"]


def grimmory_sync(hc_lists):
    base = os.environ["GRIMMORY_URL"].rstrip("/")
    hdrs = {"Authorization": f"Bearer {grimmory_token(base)}"}
    books = http("GET", f"{base}/api/v1/books", hdrs)
    shelves = http("GET", f"{base}/api/v1/shelves", hdrs)

    items = [(b["id"], b["metadata"].get("title") or "",
              b["metadata"].get("authors") or []) for b in books]
    idx = build_index(items)
    on_shelf = {}  # shelf id -> set of book ids
    for b in books:
        for s in b.get("shelves") or []:
            on_shelf.setdefault(s["id"], set()).add(b["id"])

    for lst in hc_lists:
        ids = match_list(lst["books"], idx)
        shelf = next((s for s in shelves if s["name"] == lst["name"]), None)
        tag = f"Grimmory '{lst['name']}'"
        if not ids and not shelf:
            print(f"{tag}: 0 matches, skipping")
            continue
        if DRY_RUN:
            print(f"{tag}: {len(ids)}/{len(lst['books'])} matched "
                  f"({'update' if shelf else 'create'})")
            continue
        if not shelf:
            shelf = http("POST", f"{base}/api/v1/shelves", hdrs,
                         {"name": lst["name"], "icon": "bookmark",
                          "iconType": "PRIME_NG", "publicShelf": False})
        current = on_shelf.get(shelf["id"], set())
        to_add = sorted(ids - current)
        to_remove = sorted(current - ids)
        if to_add:
            http("POST", f"{base}/api/v1/books/shelves", hdrs,
                 {"bookIds": to_add, "shelvesToAssign": [shelf["id"]],
                  "shelvesToUnassign": []})
        if to_remove:
            http("POST", f"{base}/api/v1/books/shelves", hdrs,
                 {"bookIds": to_remove, "shelvesToAssign": [],
                  "shelvesToUnassign": [shelf["id"]]})
        print(f"{tag}: {len(ids)}/{len(lst['books'])} matched "
              f"(+{len(to_add)} -{len(to_remove)})")


def main():
    hc_lists = fetch_hardcover_lists()
    if os.environ.get("ABS_URL"):
        abs_sync(hc_lists)
    if os.environ.get("GRIMMORY_URL"):
        grimmory_sync(hc_lists)


if __name__ == "__main__":
    main()
