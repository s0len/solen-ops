#!/usr/bin/env python3
"""Push a Pushover notification when something on GitHub needs attention.

Reports what *changed* inside LOOKBACK_HOURS rather than everything currently
open. A digest of all open items re-notifies about the same untouched PR every
day, which is the notification fatigue this job exists to replace. Review
requests are the deliberate exception -- they are addressed to you personally,
so they stay listed until cleared.

Stdlib only: the image is plain python:alpine with no package install, so the
job cannot fail because a mirror was slow or apk needed root.
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

ME = os.environ["GITHUB_USER"]
TOKEN = os.environ["GITHUB_TOKEN"]
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))
PO_USER = os.environ.get("PUSHOVER_USER_KEY", "")
PO_TOKEN = os.environ.get("PUSHOVER_API_TOKEN", "")

# Pushover hard-caps the message at 1024 UTF-8 chars and HTML tags count
# toward it, so href attributes are expensive. Leave headroom for the
# "+N more" footer.
PUSHOVER_LIMIT = 1000
TITLE_CHARS = 52

SINCE = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)
BOT = re.compile(r"\[bot\]$")


class Item(NamedTuple):
    repo: str   # owner/name
    ref: str    # "#3523" or a workflow name
    title: str
    meta: str   # author login or branch
    url: str


def api(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{ME}-gh-attention",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def shorten(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.:;-") + "…"


def search(query):
    """Issues/PRs matching `query`, excluding your own and bot-authored items."""
    items = api(
        "https://api.github.com/search/issues", {"q": query, "per_page": 50}
    ).get("items", [])
    out = []
    for it in items:
        login = (it.get("user") or {}).get("login", "")
        if login == ME or BOT.search(login):
            continue
        out.append(
            Item(
                repo=it["repository_url"].split("/repos/", 1)[-1],
                ref=f"#{it['number']}",
                title=shorten(it["title"], TITLE_CHARS),
                meta=login,
                url=it["html_url"],
            )
        )
    return out


def failing_ci():
    """Runs that failed on a default branch you own, inside the window."""
    out = []
    for r in api("https://api.github.com/user/subscriptions", {"per_page": 100}):
        if r["owner"]["login"] != ME:
            continue
        runs = api(
            f"https://api.github.com/repos/{r['full_name']}/actions/runs",
            {"branch": r["default_branch"], "status": "failure", "per_page": 1},
        ).get("workflow_runs", [])
        for run in runs:
            if run["created_at"] > SINCE:
                out.append(
                    Item(
                        repo=r["full_name"],
                        ref=run["name"],
                        title=shorten(run.get("display_title") or "", TITLE_CHARS),
                        meta=run["head_branch"],
                        url=run["html_url"],
                    )
                )
    return out


def by_repo(items):
    grouped = OrderedDict()
    for it in items:
        grouped.setdefault(it.repo, []).append(it)
    return grouped


def render_html(sections, limit=PUSHOVER_LIMIT):
    """Build the notification body, dropping trailing items if it overflows.

    Grouping by repo is what makes this readable on a phone: without it every
    line restates "Kometa-Team/Kometa#", which wrapped to three lines per item
    and pushed the real content off the screen.
    """
    flat = [(head, it) for head, items in sections for it in items]

    def build(keep):
        parts, shown, last_repo, last_head = [], 0, None, None
        for head, it in flat[:keep]:
            if head != last_head:
                parts.append(f"<b>{html.escape(head)}</b>")
                last_head, last_repo = head, None
            if it.repo != last_repo:
                parts.append(f"<b>{html.escape(it.repo)}</b>")
                last_repo = it.repo
            meta = f" · {html.escape(it.meta)}" if it.meta else ""
            title = f" {html.escape(it.title)}" if it.title else ""
            parts.append(
                f'• <a href="{html.escape(it.url, quote=True)}">'
                f"{html.escape(it.ref)}</a>{title}{meta}"
            )
            shown += 1
        body = "\n".join(parts)
        if shown < len(flat):
            body += f"\n<i>+{len(flat) - shown} more</i>"
        return body

    keep = len(flat)
    body = build(keep)
    while keep > 1 and len(body) > limit:
        keep -= 1
        body = build(keep)
    if len(body) > limit:
        # A single item too long to fit. Cut on a line boundary rather than
        # letting Pushover truncate mid-tag and mangle the markup.
        body = body[:limit].rsplit("\n", 1)[0]
    return body


def render_text(sections):
    """Plain layout for `kubectl logs` -- no markup, full titles."""
    out = []
    for head, items in sections:
        if not items:
            continue
        out.append(head)
        for repo, group in by_repo(items).items():
            out.append(f"  {repo}")
            for it in group:
                meta = f" · {it.meta}" if it.meta else ""
                out.append(f"    {it.ref} {it.title}{meta}")
        out.append("")
    return "\n".join(out).rstrip()


def pushover(title, body, url):
    data = urllib.parse.urlencode(
        {
            "token": PO_TOKEN,
            "user": PO_USER,
            "title": title,
            "message": body,
            "html": 1,
            "url": url,
            "url_title": "Open on GitHub",
        }
    ).encode()
    with urllib.request.urlopen(
        "https://api.pushover.net/1/messages.json", data=data, timeout=20
    ) as resp:
        return resp.status == 200


def main():
    w = f"{LOOKBACK_HOURS}h"
    raw = [
        ("Review requested", "reviews",
         search(f"is:open is:pr user-review-requested:{ME}"),
         "https://github.com/pulls/review-requested"),
        (f"Mentioned you · {w}", "mentions",
         search(f"is:open mentions:{ME} updated:>{SINCE}"),
         "https://github.com/notifications"),
        (f"PRs on your repos · {w}", "PRs",
         search(f"is:open is:pr user:{ME} updated:>{SINCE}"),
         f"https://github.com/pulls?q=is%3Aopen+is%3Apr+user%3A{ME}"),
        (f"Issues on your repos · {w}", "issues",
         search(f"is:open is:issue user:{ME} updated:>{SINCE}"),
         f"https://github.com/issues?q=is%3Aopen+is%3Aissue+user%3A{ME}"),
        (f"CI failing · {w}", "CI", failing_ci(), f"https://github.com/{ME}"),
    ]

    sections = [(head, items) for head, _, items, _ in raw if items]
    if not sections:
        print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} nothing needs attention")
        return 0

    print(render_text(sections))

    if not PO_USER or not PO_TOKEN:
        print("pushover: skipped (missing USER_KEY/API_TOKEN)")
        return 0

    # Title carries the shape of the digest so the lock screen is useful
    # without opening it: "GitHub · 2 reviews · 5 PRs".
    counts = [f"{len(items)} {label}" for _, label, items, _ in raw if items]
    link = next(url for _, _, items, url in raw if items)
    body = render_html(sections)
    total = sum(len(items) for _, items in sections)

    pushover(f"GitHub · {' · '.join(counts)}", body, link)
    print(f"pushover: sent ({total} items, {len(body)} chars)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} from {exc.url}: {exc.read()[:300]!r}", file=sys.stderr)
        sys.exit(1)
