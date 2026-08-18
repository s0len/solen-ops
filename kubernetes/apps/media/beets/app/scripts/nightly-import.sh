#!/usr/bin/env bash
set -uo pipefail

SRC=${BEETS_IMPORT_SRC:-/data/torrents/music}
OVERLAY=${BEETS_IMPORT_OVERLAY:-/config/import-overlay.yaml}
BEET=/lsiopy/bin/beet
VERBOSE_LOG=/config/nightly-import.log

# A torrent arrives in /data/torrents/music by being moved out of
# /data/torrents/temp. The move is atomic per file, not per directory, so give
# anything recently touched time to settle rather than importing half an album.
QUIET_MINUTES=30

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }
tracks() { "$BEET" stats 2>/dev/null | awk '/^Tracks:/{print $2}'; }
skips() { grep -c '^skip ' /config/import.log 2>/dev/null || printf '0'; }

log "nightly import startad"

if [[ ! -d $SRC ]]; then
    log "AVBRYTER: $SRC finns inte"
    exit 1
fi

: >"$VERBOSE_LOG"

before_tracks=$(tracks)
before_skips=$(skips)
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

    if "$BEET" -c "$OVERLAY" import -q "$dir" >>"$VERBOSE_LOG" 2>&1; then
        processed=$((processed + 1))
    else
        rc=$?
        log "FEL      $name (beet avslutade med $rc)"
        failed=$((failed + 1))
    fi
done

after_tracks=$(tracks)
after_skips=$(skips)

log "klart: $total mappar, $processed behandlade, $settling väntar, $failed fel"
log "nya spår i biblioteket: $(( ${after_tracks:-0} - ${before_tracks:-0} ))"
log "album utan MusicBrainz-match, ej importerade: $(( after_skips - before_skips )) (totalt $after_skips i /config/import.log)"
log "detaljerad utdata: $VERBOSE_LOG"

[[ $failed -gt 0 ]] && exit 1
exit 0
