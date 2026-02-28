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

valid_minute_step() {
  is_int "$1" || return 1
  [ "$1" -ge 1 ] && [ "$1" -le 59 ]
}

# Defaults preserve current behavior.
CRON_DOW="${CRON_DOW:-1-5}"
CRON_MORNING_HOUR="${CRON_MORNING_HOUR:-8}"
CRON_MIDDAY_HOUR="${CRON_MIDDAY_HOUR:-11}"
CRON_AFTERNOON_HOUR="${CRON_AFTERNOON_HOUR:-15}"
CRON_BUSINESS_HOURS="${CRON_BUSINESS_HOURS:-8-16}"
CRON_BUSINESS_END_HOUR="${CRON_BUSINESS_END_HOUR:-17}"
CRON_ESCALATIONS_EVERY_MINUTES="${CRON_ESCALATIONS_EVERY_MINUTES:-1}"
CRON_POLL_RESOLVER_EVERY_MINUTES="${CRON_POLL_RESOLVER_EVERY_MINUTES:-15}"
CRON_VERIFY_PENDING_EVERY_MINUTES="${CRON_VERIFY_PENDING_EVERY_MINUTES:-5}"

# Validation with safe fallback.
valid_hour "$CRON_MORNING_HOUR" || CRON_MORNING_HOUR=8
valid_hour "$CRON_MIDDAY_HOUR" || CRON_MIDDAY_HOUR=11
valid_hour "$CRON_AFTERNOON_HOUR" || CRON_AFTERNOON_HOUR=15
valid_hour "$CRON_BUSINESS_END_HOUR" || CRON_BUSINESS_END_HOUR=17
valid_minute_step "$CRON_ESCALATIONS_EVERY_MINUTES" || CRON_ESCALATIONS_EVERY_MINUTES=1
valid_minute_step "$CRON_POLL_RESOLVER_EVERY_MINUTES" || CRON_POLL_RESOLVER_EVERY_MINUTES=15
valid_minute_step "$CRON_VERIFY_PENDING_EVERY_MINUTES" || CRON_VERIFY_PENDING_EVERY_MINUTES=5

if ! printf '%s' "$CRON_BUSINESS_HOURS" | grep -Eq '^[0-9]{1,2}-[0-9]{1,2}$'; then
  CRON_BUSINESS_HOURS="8-16"
fi
if ! printf '%s' "$CRON_DOW" | grep -Eq '^[0-9,*/-]+$'; then
  CRON_DOW="1-5"
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
OUT="${SCRIPT_DIR}/crontab.generated"
cat > "$OUT" <<EOF
0 ${CRON_MORNING_HOUR} * * ${CRON_DOW} /app/cron/cron.sh morning >> /logs/cron.log 2>&1
0 ${CRON_MIDDAY_HOUR} * * ${CRON_DOW} /app/cron/cron.sh midday >> /logs/cron.log 2>&1
0 ${CRON_AFTERNOON_HOUR} * * ${CRON_DOW} /app/cron/cron.sh afternoon >> /logs/cron.log 2>&1

*/${CRON_ESCALATIONS_EVERY_MINUTES} ${CRON_BUSINESS_HOURS} * * ${CRON_DOW} /app/cron/cron.sh escalations >> /logs/cron.log 2>&1
0 ${CRON_BUSINESS_END_HOUR} * * ${CRON_DOW} /app/cron/cron.sh escalations >> /logs/cron.log 2>&1

*/${CRON_POLL_RESOLVER_EVERY_MINUTES} ${CRON_BUSINESS_HOURS} * * ${CRON_DOW} /app/cron/cron.sh poll_resolver >> /logs/cron.log 2>&1
0 ${CRON_BUSINESS_END_HOUR} * * ${CRON_DOW} /app/cron/cron.sh poll_resolver >> /logs/cron.log 2>&1

*/${CRON_VERIFY_PENDING_EVERY_MINUTES} ${CRON_BUSINESS_HOURS} * * ${CRON_DOW} /app/cron/cron.sh verify_pending >> /logs/cron.log 2>&1
0 ${CRON_BUSINESS_END_HOUR} * * ${CRON_DOW} /app/cron/cron.sh verify_pending >> /logs/cron.log 2>&1
EOF

if [ "${CRON_INSTALL:-1}" = "1" ]; then
  crontab "$OUT"
fi
echo "Generated cron schedule at $OUT"
