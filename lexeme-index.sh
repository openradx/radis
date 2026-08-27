#!/bin/bash
# Toggle the PR #287 single-term FTS fast path (HYBRID_FTS_LEXEME_RANK_INDEX)
# on the running radis_staging stack: ./lexeme-index.sh on|off|status
#
# Flipping is a rolling restart of web + workers (~30-60 s; the script waits).
# It never touches the 28 GB rank table or its sync trigger, so flips are
# instant in both directions and need no resync. The flag lives only in the
# service specs (not .env), so a `stack-deploy` resets it to off — re-run
# this script afterwards if you want it back on.
set -euo pipefail

SERVICES="radis_staging_web radis_staging_default_worker radis_staging_llm_worker"
FLAG="HYBRID_FTS_LEXEME_RANK_INDEX"

replica_states() {
    for c in $(docker ps -q -f name=radis_staging_web); do
        if docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' \
            | grep -q "^${FLAG}=true$"; then echo on; else echo off; fi
    done
}

wait_until() {
    local want="$1"
    for _ in $(seq 1 60); do
        local states
        states=$(replica_states | sort -u)
        [ "$states" = "$want" ] && [ "$(docker ps -q -f name=radis_staging_web | wc -l)" -ge 3 ] && return 0
        sleep 5
    done
    echo "WARNING: replicas did not all reach '$want' within 5 min; check: $0 status" >&2
    return 1
}

case "${1:-}" in
  on)
    for s in $SERVICES; do
        docker service update --env-add "${FLAG}=true" --detach "$s" >/dev/null
    done
    echo "fast path -> ON, waiting for rolling restart..."
    wait_until on && echo "all web replicas ON (single-word searches now use the lexeme rank index)"
    ;;
  off)
    for s in $SERVICES; do
        docker service update --env-rm "$FLAG" --detach "$s" >/dev/null
    done
    echo "fast path -> OFF, waiting for rolling restart..."
    wait_until off && echo "all web replicas OFF (every search uses the ts_rank path)"
    ;;
  status)
    echo "web replicas: $(replica_states | sort | uniq -c | awk '{print $1"x "$2}' | paste -sd', ')"
    echo "last timing line:"
    docker service logs radis_staging_web --since 60m 2>&1 \
        | grep "hybrid fusion timings" | tail -1 || echo "  (none in the last hour)"
    ;;
  *)
    echo "usage: $0 on|off|status" >&2
    exit 2
    ;;
esac
