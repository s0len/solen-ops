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
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ME = os.environ["GITHUB_USER"]
TOKEN = os.environ["GITHUB_TOKEN"]
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))
PO_USER = os.environ.get("PUSHOVER_USER_KEY", "")
PO_TOKEN = os.environ.get("PUSHOVER_API_TOKEN", "")

SINCE = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)
BOT = re.compile(r"\[bot\]$")


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


def search(query):
    """Issues/PRs matching `query`, excluding your own and bot-authored items."""
    items = api(
        "https://api.github.com/search/issues",
        {"q": query, "per_page": 50},
    ).get("items", [])
    out = []
    for it in items:
        login = (it.get("user") or {}).get("login", "")
        if login == ME or BOT.search(login):
            continue
        repo = it["repository_url"].split("/repos/", 1)[-1]
        out.append(f"  {repo}#{it['number']} {it['title'][:70]} — @{login}")
    return out


def failing_ci():
    """Runs that failed on a default branch you own, inside the window."""
    out = []
    repos = api("https://api.github.com/user/subscriptions", {"per_page": 100})
    for r in repos:
        if r["owner"]["login"] != ME:
            continue
        runs = api(
            f"https://api.github.com/repos/{r['full_name']}/actions/runs",
            {"branch": r["default_branch"], "status": "failure", "per_page": 1},
        ).get("workflow_runs", [])
        for run in runs:
            if run["created_at"] > SINCE:
                out.append(
                    f"  {r['full_name']} [{run['head_branch']}] "
                    f"{run['name']} — {run['html_url']}"
                )
    return out


def pushover(title, message, url):
    data = urllib.parse.urlencode(
        {
            "token": PO_TOKEN,
            "user": PO_USER,
            "title": title,
            "message": message[:1000],  # Pushover truncates past 1024
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
    sections = [
        ("REVIEW REQUESTED OF YOU",
         search(f"is:open is:pr user-review-requested:{ME}"),
         "https://github.com/pulls/review-requested"),
        (f"MENTIONED YOU ({w})",
         search(f"is:open mentions:{ME} updated:>{SINCE}"),
         "https://github.com/notifications"),
        (f"PRs ON YOUR REPOS ({w})",
         search(f"is:open is:pr user:{ME} updated:>{SINCE}"),
         f"https://github.com/pulls?q=is%3Aopen+is%3Apr+user%3A{ME}"),
        (f"ISSUES ON YOUR REPOS ({w})",
         search(f"is:open is:issue user:{ME} updated:>{SINCE}"),
         f"https://github.com/issues?q=is%3Aopen+is%3Aissue+user%3A{ME}"),
        (f"CI FAILING ({w})", failing_ci(), "https://github.com/s0len"),
    ]

    blocks, count, link = [], 0, None
    for title, lines, url in sections:
        if not lines:
            continue
        blocks.append(title + "\n" + "\n".join(lines))
        count += len(lines)
        link = link or url

    if not blocks:
        print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} nothing needs attention")
        return 0

    body = "\n\n".join(blocks)
    print(body)

    if not PO_USER or not PO_TOKEN:
        print("pushover: skipped (missing USER_KEY/API_TOKEN)")
        return 0

    pushover(f"GitHub: {count} item(s) need you", body, link)
    print(f"pushover: sent ({count} items)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} from {exc.url}: {exc.read()[:300]!r}", file=sys.stderr)
        sys.exit(1)
