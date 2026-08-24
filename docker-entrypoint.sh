#!/bin/sh
set -eu

COUNTRY="${COUNTRY:-CH}"
DAYS="${DAYS:-7}"
OUTPUT_FILE="${OUTPUT_FILE:-/output/guide.xml}"
CACHE_FILE="${CACHE_FILE:-/data/program-details.sqlite3}"
CACHE_TTL_DAYS="${CACHE_TTL_DAYS:-30}"
UPDATE_INTERVAL_SECONDS="${UPDATE_INTERVAL_SECONDS:-43200}"
HTTP_PORT="${HTTP_PORT:-8080}"

generate_guide() {
    set -- \
        --country "$COUNTRY" \
        --days "$DAYS" \
        --output "$OUTPUT_FILE" \
        --cache "$CACHE_FILE" \
        --cache-ttl-days "$CACHE_TTL_DAYS"

    if [ -n "${CHANNEL_FILTER_FILE:-}" ]; then
        set -- "$@" --channel-filter "$CHANNEL_FILTER_FILE"
    fi

    if [ "${NO_DETAILS:-false}" = "true" ]; then
        set -- "$@" --no-details
    fi

    python /app/zattoo_epg.py "$@"
}

generate_guide

if [ "${RUN_ONCE:-false}" = "true" ]; then
    exit 0
fi

python -m http.server "$HTTP_PORT" --directory /output &
HTTP_PID=$!
trap 'kill "$HTTP_PID" 2>/dev/null || true' EXIT INT TERM

while sleep "$UPDATE_INTERVAL_SECONDS"; do
    generate_guide
done
