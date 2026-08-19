#!/usr/bin/env bash
set -uo pipefail

SRC=${BEETS_IMPORT_SRC:-/data/torrents/music}
OVERLAY=${BEETS_IMPORT_OVERLAY:-/config/import-overlay.yaml}
BEET=/lsiopy/bin/beet
VERBOSE_LOG=/config/nightly-import.log
IMPORT_LOG=/config/import.log
UNMATCHED=/config/unmatched-latest.txt

# A torrent arrives in /data/torrents/music by being moved out of
# /data/torrents/temp. The move is atomic per file, not per directory, so give
# anything recently touched time to settle rather than importing half an album.
QUIET_MINUTES=30

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

if [[ ! -d $SRC ]]; then
    log "AVBRYTER: $SRC finns inte"
    notify "beets: import misslyckades" 1 "Källkatalogen $SRC finns inte."
    exit 1
fi

: >"$VERBOSE_LOG"

before_tracks=$(tracks)
# import.log is append-only and already holds thousands of entries from
# unrelated jobs, so anchor on its length to read back only this run.
before_lines=$(wc -l <"$IMPORT_LOG" 2>/dev/null || printf '0')
log "bibliotek före: ${before_tracks:-?} spår"

total=0
processed=0
settling=0
failed=0

# One beet process per top-level directory. Importing the whole tree in a single
# process has OOM-killed this pod twice: beets keeps every album's MusicBrainz
# candidates alive for the life of the process, so memory grows without bound.
for dir in "$SRC"/*/; do
    [[ -d $dir ]] || continue
    total=$((total + 1))
    name=${dir%/}
    name=${name##*/}

    if [[ -n $(find "$dir" -maxdepth 0 -mmin "-$QUIET_MINUTES" 2>/dev/null) ]]; then
        log "väntar   $name (ändrad senaste $QUIET_MINUTES min)"
        settling=$((settling + 1))
        continue
    fi

    # -R keeps skipped albums out of the incremental state, so anything
    # MusicBrainz cannot match yet is retried on later runs and imports itself
    # once the release shows up there.
    if "$BEET" -c "$OVERLAY" import -q -R "$dir" >>"$VERBOSE_LOG" 2>&1; then
        processed=$((processed + 1))
    else
        rc=$?
        log "FEL      $name (beet avslutade med $rc)"
        failed=$((failed + 1))
    fi
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
say "$total mappar: $processed behandlade, $settling väntar, $failed fel"

if [[ $unmatched -gt 0 ]]; then
    say "$unmatched album väntar på MusicBrainz-match (återförsöks varje natt):"
    head -12 "$UNMATCHED" | while IFS= read -r p; do say "  ${p#"$SRC"/}"; done
    [[ $unmatched -gt 12 ]] && say "  ... och $((unmatched - 12)) till, hela listan i $UNMATCHED"
fi

log "detaljerad utdata: $VERBOSE_LOG, omatchade i $UNMATCHED"

if [[ $failed -gt 0 ]]; then
    notify "beets: $failed mappar failade" 1 "$SUMMARY"
    exit 1
fi

notify "beets: $added nya spår" 0 "$SUMMARY"
exit 0
