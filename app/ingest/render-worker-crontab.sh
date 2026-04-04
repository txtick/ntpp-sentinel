#!/bin/sh
set -eu

is_int() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

valid_hour() {
  is_int "$1" || return 1
  [ "$1" -ge 0 ] && [ "$1" -le 23 ]
}

valid_minute() {
  is_int "$1" || return 1
  [ "$1" -ge 0 ] && [ "$1" -le 59 ]
}

CRON_TZ="${CRON_TZ:-${TIMEZONE:-America/Chicago}}"
SYNC_MINUTE="${CRON_SKIMMER_SYNC_MINUTE:-15}"
SYNC_HOUR="${CRON_SKIMMER_SYNC_HOUR:-11}"
CRON_INGEST_WORKER_DOW="${CRON_INGEST_WORKER_DOW:-${CRON_SKIMMER_SYNC_DOW:-*}}"
INGEST_SOURCE_SYSTEM="${INGEST_SOURCE_SYSTEM:-skimmer}"
SKIMMER_DB_PATH="${SKIMMER_DB_PATH:-/data/skimmer/skimmer.db}"

valid_minute "$SYNC_MINUTE" || SYNC_MINUTE=15
valid_hour "$SYNC_HOUR" || SYNC_HOUR=11

if [ -z "${CRON_INGEST_WORKER_MINUTE:-}" ] || [ -z "${CRON_INGEST_WORKER_HOUR:-}" ]; then
  total_minutes=$((SYNC_HOUR * 60 + SYNC_MINUTE + 10))
  CRON_INGEST_WORKER_HOUR=$(((total_minutes / 60) % 24))
  CRON_INGEST_WORKER_MINUTE=$((total_minutes % 60))
else
  valid_minute "${CRON_INGEST_WORKER_MINUTE}" || CRON_INGEST_WORKER_MINUTE=25
  valid_hour "${CRON_INGEST_WORKER_HOUR}" || CRON_INGEST_WORKER_HOUR=11
fi

if ! printf '%s' "${CRON_INGEST_WORKER_DOW}" | grep -Eq '^[0-9,*/-]+$'; then
  CRON_INGEST_WORKER_DOW="*"
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
OUT="${SCRIPT_DIR}/worker-crontab.generated"
cat > "$OUT" <<EOF
TZ=${CRON_TZ}
${CRON_INGEST_WORKER_MINUTE} ${CRON_INGEST_WORKER_HOUR} * * ${CRON_INGEST_WORKER_DOW} /app/ingest/cron.sh >> /logs/ingest-worker.log 2>&1
EOF

if [ "${CRON_INSTALL:-1}" = "1" ]; then
  crontab "$OUT"
fi
echo "Generated worker cron schedule at $OUT"
