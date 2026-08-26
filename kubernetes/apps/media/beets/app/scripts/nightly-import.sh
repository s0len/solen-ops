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
RUN_LOG=/config/nightly-run.log

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

# The album loop stops itself here instead of being SIGTERMed by the job's
# activeDeadlineSeconds: a killed run never reaches the report that says why.
WORK_BUDGET=${BEETS_WORK_BUDGET:-15300}
PER_DIR_MAX=${BEETS_PER_DIR_MAX:-900}
CHARTPACK_MAX=${BEETS_CHARTPACK_MAX:-1800}
CURSOR=/config/nightly-cursor.txt

SUMMARY=""

log() {
    local line
    line="$(date '+%F %T')  $*"
    printf '%s\n' "$line"
    printf '%s\n' "$line" >>"$RUN_LOG" 2>/dev/null || true
}
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
    [[ -n ${child_pid:-} ]] && kill "$child_pid" 2>/dev/null
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
    else
        CHARTPACK_DATE=$(date '+%F') timeout -k 60 "$CHARTPACK_MAX" "$PYTHON" -u "$CHARTPACK" \
            --src "$SRC" --staging "$STAGING" --state "$CHARTPACK_STATE" --apply \
            >>"$CHARTPACK_LOG" 2>&1 &
        child_pid=$!
        if wait "$child_pid"; then
            chart_new=$(awk -F: '/^att importera:/{gsub(/ /,"",$2); print $2}' "$CHARTPACK_LOG")
            chart_upgraded=$(awk -F: '/^uppgraderade:/{split($2,a," "); print a[1]}' "$CHARTPACK_LOG")
            chart_packs=$(awk '/^paket: /{print $2}' "$CHARTPACK_LOG")
        else
            chart_failed="skriptet avslutade med $?"
            log "FEL chartpaket: $chart_failed"
        fi
        child_pid=""
    fi
    [[ -s $CHARTPACK_LOG ]] && cat "$CHARTPACK_LOG" >>"$VERBOSE_LOG"

    # The staged files are copies with corrected tags, so this import moves them
    # rather than copying again -- staging drains itself and nothing is left to
    # clean up. -A because the tags are already right and MusicBrainz cannot match
    # loose chart tracks anyway: a 16-track folder cost 397s with autotagging on
    # and produced one wrong-but-confident match, versus 0.7s with it off.
    if [[ -d $STAGING ]] && find "$STAGING" -type f -print -quit 2>/dev/null | grep -q .; then
        log "chartpaket: importerar staging"
        "$BEET" -c "$OVERLAY" import -q -A -m "$STAGING" >>"$VERBOSE_LOG" 2>&1 &
        child_pid=$!
        wait "$child_pid" || log "FEL chartpaket-import avslutade med $?"
        child_pid=""
        find "$STAGING" -type d -empty -delete 2>/dev/null || true
    fi
fi

# Directories the classifier called chart packs must not also go through the
# album loop: it cannot match them, and it would put every one of them into the
# unmatched queue every single night.
declare -A IS_CHART=()
declare -A IS_LOOSE=()
declare -A IS_COLLECTION=()
if [[ -f $CHARTPACK_STATE ]]; then
    while IFS=$'\t' read -r kind name; do
        [[ -z $name ]] && continue
        case $kind in
            chartpack) IS_CHART["$name"]=1 ;;
            loose) IS_LOOSE["$name"]=1 ;;
            collection) IS_COLLECTION["$name"]=1 ;;
        esac
    done < <("$PYTHON" -c '
import json, sys
try:
    state = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for name, rec in state.items():
    kind = rec.get("kind")
    if kind in ("chartpack", "loose", "collection"):
        print(f"{kind}\t{name}")
' "$CHARTPACK_STATE" 2>/dev/null)
fi

total=0
processed=0
timedout=0
settling=0
failed=0
charts=0
loose=0
collections=0
LOOSE_NAMES=()

import_one() {
    local target=$1 label=$2 rc=0
    timeout -k 60 "$PER_DIR_MAX" "$BEET" -c "$OVERLAY" import -q "${retry_flag[@]}" "$target" \
        >>"$VERBOSE_LOG" 2>&1 &
    child_pid=$!
    wait "$child_pid" || rc=$?
    child_pid=""
    if (( rc == 0 )); then
        processed=$((processed + 1))
    elif (( rc == 124 )); then
        # Whatever finished is in the incremental state, so the next visit resumes.
        log "TIMEOUT  $label (över ${PER_DIR_MAX}s)"
        timedout=$((timedout + 1))
    else
        log "FEL      $label (beet avslutade med $rc)"
        failed=$((failed + 1))
    fi
}

# One beet process per top-level directory. Importing the whole tree in a single
# process has OOM-killed this pod twice: beets keeps every album's MusicBrainz
# candidates alive for the life of the process, so memory grows without bound.
DIRS=()
for dir in "$SRC"/*/; do
    [[ -d $dir ]] || continue
    DIRS+=("$dir")
done
ndirs=${#DIRS[@]}

# Resume where the last run ran out of budget, keyed on the name rather than an
# index so a torrent arriving or leaving overnight cannot shift the position.
# Without this the loop restarts at A every run and the tail is never reached.
start=0
if [[ -s $CURSOR ]]; then
    want=$(<"$CURSOR")
    for i in "${!DIRS[@]}"; do
        cand=${DIRS[i]%/}
        if [[ ! ${cand##*/} < $want ]]; then
            start=$i
            break
        fi
    done
fi

over_budget() { (( SECONDS >= WORK_BUDGET )); }
resume_at=""

for ((k = 0; k < ndirs; k++)); do
    dir=${DIRS[$(((start + k) % ndirs))]}
    name=${dir%/}
    name=${name##*/}

    if over_budget; then
        resume_at=$name
        break
    fi
    total=$((total + 1))

    if [[ -n ${IS_CHART[$name]:-} ]]; then
        charts=$((charts + 1))
        continue
    fi

    if [[ -n ${IS_LOOSE[$name]:-} ]]; then
        loose=$((loose + 1))
        LOOSE_NAMES+=("$name")
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
    if [[ -n ${IS_COLLECTION[$name]:-} ]]; then
        collections=$((collections + 1))
        for sub in "$dir"*/; do
            [[ -d $sub ]] || continue
            # Half a collection is safe to leave: what imported is in the
            # incremental state, so resuming here costs only the remainder.
            if over_budget; then
                resume_at=$name
                break
            fi
            subname=${sub%/}
            subname=${subname##*/}
            import_one "$sub" "$name/$subname"
        done
    else
        import_one "$dir" "$name"
    fi
    printf '%s\t%s\n' "$((SECONDS - started))" "$name" >>"$TIMINGS"
    [[ -n $resume_at ]] && break
done

remaining=0
if [[ -n $resume_at ]]; then
    remaining=$((ndirs - total))
    printf '%s\n' "$resume_at" >"$CURSOR"
else
    : >"$CURSOR"
fi

after_tracks=$(tracks)
added=$(( ${after_tracks:-0} - ${before_tracks:-0} ))

# This run's skips are the whole outstanding queue, not just its tail: -R keeps
# skipped albums out of the incremental state, so every one of them is retried
# on every run. An album that finally matches simply stops appearing here.
tail -n "+$((before_lines + 1))" "$IMPORT_LOG" 2>/dev/null |
    grep "^skip $SRC" | cut -d' ' -f2- | tr ';' '\n' | sed 's/^ //' | sort -u >"$UNMATCHED" || : >"$UNMATCHED"
unmatched=$(wc -l <"$UNMATCHED")

say "$added nya spår, biblioteket har nu ${after_tracks:-?}"
say "$total mappar: $processed importanrop, $collections samlingar, $charts chartpaket, $loose lösa spår, $settling väntar, $timedout avbrutna, $failed fel"
if (( remaining > 0 )); then
    say "budgeten tog slut efter $((SECONDS / 60)) min: $remaining mappar kvar, nästa körning börjar vid $resume_at"
fi
if (( timedout > 0 )); then
    say "$timedout mappar nådde ${PER_DIR_MAX}s och avbröts, sök TIMEOUT i $RUN_LOG"
fi
# Naming them matters: a directory of loose tracks can be a DJ tool pack nobody
# wants or a real artist's loose singles, and the classifier only sees structure.
# Skipping is the safe default; staying silent about it is not.
if [[ $loose -gt 0 ]]; then
    say "$loose mappar med lösa spår hoppades över:"
    for n in "${LOOSE_NAMES[@]:0:5}"; do say "  $n"; done
    [[ $loose -gt 5 ]] && say "  ... och $((loose - 5)) till"
fi

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
    partial=""
    (( remaining > 0 )) && partial=" av de $total genomsökta"
    say "$unmatched utan säker MusicBrainz-match$partial (kan redan finnas i biblioteket):"
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
