#!/usr/bin/env bash
set -uo pipefail

SRC=${BEETS_IMPORT_SRC:-/data/torrents/music}
OVERLAY=${BEETS_IMPORT_OVERLAY:-/config/import-overlay.yaml}
BEET=/lsiopy/bin/beet
PYTHON=/lsiopy/bin/python3
CHARTPACK=${BEETS_CHARTPACK:-/scripts/chartpack-import.py}
CHARTPACK_STATE=${BEETS_CHARTPACK_STATE:-/config/chartpack-state.json}
CHARTPACK_LOG=/config/chartpack-latest.log
LOCK=/config/nightly-import.lock
STAGING=${BEETS_CHARTPACK_STAGING:-/data/staging/chartpack-import}
VERBOSE_LOG=/config/nightly-import.log
IMPORT_LOG=/config/nightly-verbs.log
UNMATCHED=/config/unmatched-latest.txt

# A torrent arrives in /data/torrents/music by being moved out of
# /data/torrents/temp. The move is atomic per file, not per directory, so give
# anything recently touched time to settle rather than importing half an album.
QUIET_MINUTES=30

# How long a directory stays in the retry set. -R keeps a skipped album out of
# beets' incremental state so it is retried on later runs -- which is what lets a
# release MusicBrainz has not indexed yet import itself once it appears. The cost
# is that the album is re-searched every single night forever: measured 46s per
# night with -R versus 0.28s once recorded. Torrent directories are immutable
# once seeded, so a directory this old can never yield new content and there is
# nothing left to wait for.
RETRY_DAYS=14

SUMMARY=""

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }
say() {
    log "$*"
    SUMMARY="${SUMMARY}$*"$'\n'
}
tracks() { "$BEET" stats 2>/dev/null | awk '/^Tracks:/{print $2}'; }

notify() {
    local title=$1 priority=$2 body=$3
    if [[ -z ${PUSHOVER_USER_KEY:-} || -z ${PUSHOVER_API_TOKEN:-} ]]; then
        log "pushover: hoppar över (saknar USER_KEY/API_TOKEN)"
        return 0
    fi
    # Pushover truncates past 1024 characters, so send the head of the summary.
    curl -sf -m 20 --output /dev/null \
        --form-string "token=$PUSHOVER_API_TOKEN" \
        --form-string "user=$PUSHOVER_USER_KEY" \
        --form-string "title=$title" \
        --form-string "priority=$priority" \
        --form-string "message=${body:0:1000}" \
        https://api.pushover.net/1/messages.json &&
        log "pushover: notis skickad" ||
        log "pushover: kunde inte skicka notis"
}

log "nightly import startad"

exec 9>"$LOCK" || true
if ! flock -n 9; then
    log "en annan körning håller $LOCK - avslutar utan att göra något"
    exit 0
fi

on_term() {
    log "avbruten av SIGTERM efter $((SECONDS / 60)) min"
    notify "beets: importen avbröts" 1 \
        "${SUMMARY}Avbruten efter $((SECONDS / 60)) min, troligen jobbets activeDeadlineSeconds."
    exit 143
}
trap on_term TERM

if [[ ! -d $SRC ]]; then
    log "AVBRYTER: $SRC finns inte"
    notify "beets: import misslyckades" 1 "Källkatalogen $SRC finns inte."
    exit 1
fi

TIMINGS=$(mktemp)
trap 'rm -f "$TIMINGS"' EXIT

: >"$VERBOSE_LOG"

before_tracks=$(tracks)
# Create it without truncating: beets appends its verbs here, and the report
# reads back only the lines added after this point. bash applies the input
# redirection before stderr is silenced, so a missing file would print an error
# no matter how the redirections are ordered.
: >>"$IMPORT_LOG"
before_lines=$(wc -l <"$IMPORT_LOG")
log "bibliotek före: ${before_tracks:-?} spår"

# Chart packs first, because this step is what refreshes the classification the
# album loop below reads. A pack is not an album: 50-90 tracks from as many
# releases, so album matching cannot succeed and used to fail expensively -- one
# Apple Music pack was 36 of the 109 minutes of the 2026-08-20 run.
chart_new=0
chart_upgraded=0
chart_packs=0
chart_failed=""
if [[ -x $CHARTPACK ]] || [[ -f $CHARTPACK ]]; then
    log "chartpaket: letar och stagear"
    # Claim the log BEFORE running, and abandon the step if that fails. Output goes
    # straight to a file, unbuffered, because the first sweep of this tree takes the
    # best part of an hour and holding it in a variable means a run killed by the job
    # deadline leaves nothing behind to explain how far it got.
    #
    # The counters are parsed only when the run actually succeeded. On 2026-08-23 a
    # root-owned log left behind by a manual run made the redirect fail, the step
    # never ran -- and the counters were then read out of the PREVIOUS night's log, so
    # the report was about to claim 40 packs and 11566 songs this run never touched.
    if ! : >"$CHARTPACK_LOG" 2>/dev/null; then
        chart_failed="kan inte skriva $CHARTPACK_LOG (ägarskap?)"
        log "FEL chartpaket: $chart_failed"
    elif CHARTPACK_DATE=$(date '+%F') "$PYTHON" -u "$CHARTPACK" \
            --src "$SRC" --staging "$STAGING" --state "$CHARTPACK_STATE" --apply \
            >>"$CHARTPACK_LOG" 2>&1; then
        chart_new=$(awk -F: '/^att importera:/{gsub(/ /,"",$2); print $2}' "$CHARTPACK_LOG")
        chart_upgraded=$(awk -F: '/^uppgraderade:/{split($2,a," "); print a[1]}' "$CHARTPACK_LOG")
        chart_packs=$(awk '/^paket: /{print $2}' "$CHARTPACK_LOG")
    else
        chart_failed="skriptet avslutade med $?"
        log "FEL chartpaket: $chart_failed"
    fi
    [[ -s $CHARTPACK_LOG ]] && cat "$CHARTPACK_LOG" >>"$VERBOSE_LOG"

    # The staged files are copies with corrected tags, so this import moves them
    # rather than copying again -- staging drains itself and nothing is left to
    # clean up. -A because the tags are already right and MusicBrainz cannot match
    # loose chart tracks anyway: a 16-track folder cost 397s with autotagging on
    # and produced one wrong-but-confident match, versus 0.7s with it off.
    if [[ -d $STAGING ]] && find "$STAGING" -type f -print -quit 2>/dev/null | grep -q .; then
        log "chartpaket: importerar staging"
        "$BEET" -c "$OVERLAY" import -q -A -m "$STAGING" >>"$VERBOSE_LOG" 2>&1 ||
            log "FEL chartpaket-import avslutade med $?"
        find "$STAGING" -type d -empty -delete 2>/dev/null || true
    fi
fi

# Directories the classifier called chart packs must not also go through the
# album loop: it cannot match them, and it would put every one of them into the
# unmatched queue every single night.
declare -A IS_CHART=()
declare -A IS_LOOSE=()
if [[ -f $CHARTPACK_STATE ]]; then
    while IFS=$'\t' read -r kind name; do
        [[ -z $name ]] && continue
        case $kind in
            chartpack) IS_CHART["$name"]=1 ;;
            loose) IS_LOOSE["$name"]=1 ;;
        esac
    done < <("$PYTHON" -c '
import json, sys
try:
    state = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for name, rec in state.items():
    kind = rec.get("kind")
    if kind in ("chartpack", "loose"):
        print(f"{kind}\t{name}")
' "$CHARTPACK_STATE" 2>/dev/null)
fi

total=0
processed=0
settling=0
failed=0
charts=0
loose=0

# One beet process per top-level directory. Importing the whole tree in a single
# process has OOM-killed this pod twice: beets keeps every album's MusicBrainz
# candidates alive for the life of the process, so memory grows without bound.
for dir in "$SRC"/*/; do
    [[ -d $dir ]] || continue
    total=$((total + 1))
    name=${dir%/}
    name=${name##*/}

    if [[ -n ${IS_CHART[$name]:-} ]]; then
        charts=$((charts + 1))
        continue
    fi

    if [[ -n ${IS_LOOSE[$name]:-} ]]; then
        loose=$((loose + 1))
        continue
    fi

    if [[ -n $(find "$dir" -maxdepth 0 -mmin "-$QUIET_MINUTES" 2>/dev/null) ]]; then
        log "väntar   $name (ändrad senaste $QUIET_MINUTES min)"
        settling=$((settling + 1))
        continue
    fi

    if [[ -n $(find "$dir" -maxdepth 0 -mtime "-$RETRY_DAYS" 2>/dev/null) ]]; then
        retry_flag=(-R)   # recent arrival: keep it in the retry set
    else
        retry_flag=()     # settled: record the outcome and stop re-searching it
    fi

    started=$SECONDS
    if "$BEET" -c "$OVERLAY" import -q "${retry_flag[@]}" "$dir" >>"$VERBOSE_LOG" 2>&1; then
        processed=$((processed + 1))
    else
        rc=$?
        log "FEL      $name (beet avslutade med $rc)"
        failed=$((failed + 1))
    fi
    printf '%s\t%s\n' "$((SECONDS - started))" "$name" >>"$TIMINGS"
done

after_tracks=$(tracks)
added=$(( ${after_tracks:-0} - ${before_tracks:-0} ))

# This run's skips are the whole outstanding queue, not just its tail: -R keeps
# skipped albums out of the incremental state, so every one of them is retried
# on every run. An album that finally matches simply stops appearing here.
tail -n "+$((before_lines + 1))" "$IMPORT_LOG" 2>/dev/null |
    grep "^skip $SRC" | cut -d' ' -f2- | tr ';' '\n' | sed 's/^ //' | sort -u >"$UNMATCHED" || : >"$UNMATCHED"
unmatched=$(wc -l <"$UNMATCHED")

say "$added nya spår, biblioteket har nu ${after_tracks:-?}"
say "$total mappar: $processed behandlade, $charts chartpaket, $loose lösa spår, $settling väntar, $failed fel"
if [[ -n $chart_failed ]]; then
    # Say it in the notification, not only in the log. A silent chart-pack failure
    # looks exactly like a quiet night, and packs then pile up unnoticed.
    say "CHARTPAKET HOPPADES ÖVER: $chart_failed"
elif [[ ${chart_packs:-0} -gt 0 ]]; then
    say "chartpaket: ${chart_packs:-0} behandlade, ${chart_new:-0} låtar, ${chart_upgraded:-0} uppgraderade"
fi

# The slowest directories are the only line a human can act on: one of them in
# the overlay's ignore list, or handed to chartpack-import.py, is the whole fix.
if [[ -s $TIMINGS ]]; then
    say "långsammaste:"
    sort -rn "$TIMINGS" | head -3 | while IFS=$'\t' read -r secs name; do
        say "  $((secs / 60))m$((secs % 60))s  $name"
    done
fi

if [[ $unmatched -gt 0 ]]; then
    # Deliberately not "waiting for MusicBrainz": a triage of one night's 95
    # entries found 5 genuinely missing from MB, 8 already in the library under
    # other tags, and 21 that MB does hold but cannot match as-shaped.
    say "$unmatched utan säker MusicBrainz-match (kan redan finnas i biblioteket):"
    head -8 "$UNMATCHED" | while IFS= read -r p; do say "  ${p#"$SRC"/}"; done
    [[ $unmatched -gt 8 ]] && say "  ... och $((unmatched - 8)) till, hela listan i $UNMATCHED"
fi

log "detaljerad utdata: $VERBOSE_LOG, omatchade i $UNMATCHED"

if [[ $failed -gt 0 ]]; then
    notify "beets: $failed mappar failade" 1 "$SUMMARY"
    exit 1
fi

if [[ -n $chart_failed ]]; then
    notify "beets: chartpaketsteget failade" 1 "$SUMMARY"
    exit 1
fi

notify "beets: $added nya spår" 0 "$SUMMARY"
exit 0
