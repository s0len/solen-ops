#!/bin/sh
set -u

NODES=${ETCD_NODES:?}
TALOSCTL=${TALOSCTL:-/opt/talos/talosctl}
METRICS_PORT=${ETCD_METRICS_PORT:-2381}
RATIO_MAX=${DEFRAG_RATIO_MAX:-50}
MIN_DB_MB=${DEFRAG_MIN_DB_MB:-100}
SETTLE=${DEFRAG_SETTLE_SECONDS:-60}

log() { echo "$(date '+%F %T')  $*"; }
die() { log "ABORT: $*"; exit 1; }

# Gate on etcd's metrics, not on talosctl's table, which reshapes across releases.
node_stats() {
    _raw=$(wget -qO- --timeout=10 "http://$1:${METRICS_PORT}/metrics" 2>/dev/null) || return 1
    printf '%s\n' "$_raw" | awk '
        $1=="etcd_mvcc_db_total_size_in_bytes"        { total = $2 + 0 }
        $1=="etcd_mvcc_db_total_size_in_use_in_bytes" { inuse = $2 + 0 }
        $1=="etcd_server_has_leader"                  { has   = $2 + 0 }
        $1=="etcd_server_is_leader"                   { is    = $2 + 0 }
        END { if (total <= 0 || inuse <= 0) exit 1; printf "%d %d %d %d\n", total, inuse, has, is }'
}

mb() { echo $(( $1 / 1048576 )); }
pct() { echo $(( $2 * 100 / $1 )); }

# Re-derived on every call, so a member that degraded mid-run is caught.
survey() {
    SURVEY_LEADER=""
    SURVEY_ROWS=""
    _n=0
    for _node in $NODES; do
        _s=$(node_stats "$_node") || die "cannot read etcd metrics from $_node"
        set -- $_s
        [ "$#" -eq 4 ] || die "unexpected metric set from $_node"
        [ "$3" -eq 1 ] || die "$_node reports no etcd leader"
        _alarms=$("$TALOSCTL" -n "$_node" etcd alarm list) || die "cannot read etcd alarms from $_node"
        [ -z "$_alarms" ] || die "etcd alarm active on $_node: $_alarms"
        if [ "$4" -eq 1 ]; then
            [ -z "$SURVEY_LEADER" ] || die "more than one etcd leader reported"
            SURVEY_LEADER=$_node
        fi
        SURVEY_ROWS="${SURVEY_ROWS}${_node} $1 $2
"
        _n=$((_n + 1))
    done
    [ -n "$SURVEY_LEADER" ] || die "no etcd leader found"
    [ "$_n" -eq "$(echo "$NODES" | wc -w)" ] || die "surveyed $_n of $(echo "$NODES" | wc -w) members"
}

row_for() { printf '%s' "$SURVEY_ROWS" | awk -v n="$1" '$1==n {print $2, $3; found=1} END{exit !found}'; }

log "etcd defrag run starting (nodes: $NODES)"
survey
leader=$SURVEY_LEADER
log "leader is $leader"

candidates=""
for node in $NODES; do
    stats=$(row_for "$node") || die "lost stats for $node"
    set -- $stats
    total=$1; inuse=$2
    ratio=$(pct "$total" "$inuse")
    if [ "$(mb "$total")" -lt "$MIN_DB_MB" ]; then
        log "skip  $node  db $(mb "$total")MB under ${MIN_DB_MB}MB floor"
    elif [ "$ratio" -ge "$RATIO_MAX" ]; then
        log "skip  $node  db $(mb "$total")MB, in use $(mb "$inuse")MB (${ratio}%), not fragmented"
    else
        log "queue $node  db $(mb "$total")MB, in use $(mb "$inuse")MB (${ratio}%)"
        [ "$node" = "$leader" ] || candidates="$candidates $node"
    fi
done
# Leader last: it is the one member whose stall can also cost an election.
for node in $NODES; do
    [ "$node" = "$leader" ] || continue
    stats=$(row_for "$node") || die "lost stats for $node"
    set -- $stats
    [ "$(mb "$1")" -lt "$MIN_DB_MB" ] || [ "$(pct "$1" "$2")" -ge "$RATIO_MAX" ] || candidates="$candidates $node"
done

if [ -z "$candidates" ]; then
    log "nothing to defragment"
    exit 0
fi

failed=0
done_count=0
for node in $candidates; do
    if [ "$done_count" -gt 0 ]; then
        log "settling ${SETTLE}s"
        sleep "$SETTLE"
    fi
    survey
    [ "$SURVEY_LEADER" = "$leader" ] || die "leader moved to $SURVEY_LEADER mid-run, stopping"
    before=$(row_for "$node") || die "lost stats for $node"
    set -- $before
    log "defrag $node (db $(mb "$1")MB, in use $(mb "$2")MB)"
    if "$TALOSCTL" -n "$node" etcd defrag; then
        after=$(node_stats "$node") || die "cannot re-read metrics from $node after defrag"
        set -- $after
        log "done   $node  db now $(mb "$1")MB, in use $(mb "$2")MB ($(pct "$1" "$2")%)"
        done_count=$((done_count + 1))
    else
        log "FAILED $node  defrag returned non-zero"
        failed=$((failed + 1))
    fi
done

log "defragmented $done_count member(s), $failed failure(s)"
# Only a real defrag failure is a job failure; fragmentation left over is not.
[ "$failed" -eq 0 ] || exit 1
exit 0
