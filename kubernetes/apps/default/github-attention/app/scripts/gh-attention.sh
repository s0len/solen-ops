#!/bin/sh
# Push a Pushover notification when something on GitHub needs attention.
#
# Design note: this reports things that *changed* inside LOOKBACK_HOURS, not
# everything currently open. A digest of "all open items" re-notifies about the
# same untouched PR every single day, which is exactly the notification fatigue
# this job exists to fix. Review requests are the one exception -- those are
# addressed to you personally and stay listed until they are cleared.
set -eu

ME="${GITHUB_USER:?}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-26}"   # > 24 so a late run never leaves a gap
SINCE="$(date -u -d "@$(( $(date -u +%s) - LOOKBACK_HOURS * 3600 ))" +%Y-%m-%dT%H:%M:%SZ)"

api() { curl -sfSL --retry 3 --retry-delay 5 -m 30 \
          -H "Authorization: Bearer ${GITHUB_TOKEN:?}" \
          -H "Accept: application/vnd.github+json" \
          -H "X-GitHub-Api-Version: 2022-11-28" "$@"; }

search() { api -G --data-urlencode "q=$1" --data-urlencode "per_page=50" \
             https://api.github.com/search/issues; }

# repo#123 Title — @author
fmt='.items[]
     | select(.user.login != $me and (.user.login | test("\\[bot\\]$") | not))
     | "  \(.repository_url | sub("^.*/repos/";""))#\(.number) \(.title[:70]) — @\(.user.login)"'

OUT=""; FIRST_URL=""
add() { [ -n "$2" ] && OUT="${OUT}$1
$2

" || true; }
first_url() { [ -z "$FIRST_URL" ] && FIRST_URL="$1" || true; }

# --- addressed to you personally, any repo -------------------------------
rr="$(search "is:open is:pr user-review-requested:$ME" | jq -r --arg me "$ME" "$fmt")"
add "REVIEW REQUESTED OF YOU" "$rr"
[ -n "$rr" ] && first_url "https://github.com/pulls/review-requested"

men="$(search "is:open mentions:$ME updated:>$SINCE" | jq -r --arg me "$ME" "$fmt")"
add "MENTIONED YOU (${LOOKBACK_HOURS}h)" "$men"
[ -n "$men" ] && first_url "https://github.com/notifications"

# --- your own repos: contributor activity is yours to triage -------------
prs="$(search "is:open is:pr user:$ME updated:>$SINCE" | jq -r --arg me "$ME" "$fmt")"
add "PRs ON YOUR REPOS (${LOOKBACK_HOURS}h)" "$prs"
[ -n "$prs" ] && first_url "https://github.com/pulls?q=is%3Aopen+is%3Apr+user%3A$ME"

iss="$(search "is:open is:issue user:$ME updated:>$SINCE" | jq -r --arg me "$ME" "$fmt")"
add "ISSUES ON YOUR REPOS (${LOOKBACK_HOURS}h)" "$iss"
[ -n "$iss" ] && first_url "https://github.com/issues?q=is%3Aopen+is%3Aissue+user%3A$ME"

# --- CI broken on a default branch you own -------------------------------
ci=""
repos="$(api "https://api.github.com/user/subscriptions?per_page=100" \
         | jq -r --arg me "$ME" '.[] | select(.owner.login==$me) | "\(.full_name) \(.default_branch)"')"
for line in $(echo "$repos" | tr ' ' ':'); do
  [ -z "$line" ] && continue
  repo="${line%%:*}"; branch="${line##*:}"
  run="$(api "https://api.github.com/repos/$repo/actions/runs?branch=$branch&status=failure&per_page=1" \
        | jq -r --arg since "$SINCE" '.workflow_runs[]? | select(.created_at > $since)
                 | "  \(.name) failed on \(.head_branch) — \(.html_url)"')" || continue
  [ -n "$run" ] && ci="${ci}${run}
"
done
add "CI FAILING (${LOOKBACK_HOURS}h)" "${ci%
}"

# --- deliver --------------------------------------------------------------
if [ -z "$(printf '%s' "$OUT" | tr -d ' \n')" ]; then
  echo "$(date -u +%FT%TZ) nothing needs attention"
  exit 0
fi

n="$(printf '%s' "$OUT" | grep -c '^  ' || true)"
printf '%s\n' "$OUT"

if [ -z "${PUSHOVER_USER_KEY:-}" ] || [ -z "${PUSHOVER_API_TOKEN:-}" ]; then
  echo "pushover: skipped (missing USER_KEY/API_TOKEN)"
  exit 0
fi

curl -sf -m 20 --output /dev/null \
  --form-string "token=$PUSHOVER_API_TOKEN" \
  --form-string "user=$PUSHOVER_USER_KEY" \
  --form-string "title=GitHub: $n item(s) need you" \
  --form-string "message=$(printf '%s' "$OUT" | head -c 1000)" \
  --form-string "url=${FIRST_URL:-https://github.com/notifications}" \
  --form-string "url_title=Open on GitHub" \
  https://api.pushover.net/1/messages.json \
  && echo "pushover: sent ($n items)" \
  || { echo "pushover: FAILED to send"; exit 1; }
