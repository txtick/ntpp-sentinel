#!/bin/sh
set -e

JOB="${1:-}"

# Cron often runs with a minimal environment.
# Pull needed vars from the container's PID 1 environment.
if [ -r /proc/1/environ ]; then
  export $(tr '\0' '\n' < /proc/1/environ \
    | grep -E '^(WEBHOOK_SECRET|WEB_BACKEND_SECRET|GHL_TOKEN|GHL_VERSION|MANAGER_CONTACT_IDS|GHL_LOCATION_ID)=' \
    | xargs)
fi

: "${WEBHOOK_SECRET:?WEBHOOK_SECRET is not set}"

HDR="X-NTPP-Secret: ${WEBHOOK_SECRET}"
WEB_BACKEND_HDR="X-NTPP-Secret: ${WEB_BACKEND_SECRET:-${WEBHOOK_SECRET}}"
CURL="/usr/bin/curl"
BASE="http://localhost:8000"
WEB_BACKEND_BASE="http://web-backend:8020"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

call_job() {
  URL="$1"
  echo "$(ts) cron: ${JOB} -> POST ${URL}"
  "$CURL" -sS -o /dev/null -w "$(ts) cron: ${JOB} <- http=%{http_code}\n" \
    -X POST "${URL}" -H "${HDR}"
}

call_backend_job() {
  URL="$1"
  echo "$(ts) cron: ${JOB} -> POST ${URL}"
  "$CURL" -sS -o /dev/null -w "$(ts) cron: ${JOB} <- http=%{http_code}\n" \
    -X POST "${URL}" -H "${WEB_BACKEND_HDR}"
}

case "$JOB" in
  morning)
    call_job "${BASE}/jobs/send_summary?slot=morning"
    ;;
  midday)
    call_job "${BASE}/jobs/send_summary?slot=midday"
    ;;
  afternoon)
    call_job "${BASE}/jobs/send_summary?slot=afternoon"
    ;;
  poll_resolver)
    call_job "${BASE}/jobs/poll_resolver"
    ;;
  verify_pending)
    call_job "${BASE}/jobs/verify_pending"
    ;;
  escalations)
    call_job "${BASE}/jobs/escalations"
    ;;
  skimmer_import)
    call_job "${BASE}/jobs/skimmer_import"
    ;;
  skimmer_drive_sync)
    call_job "${BASE}/jobs/skimmer_drive_sync?import_after=1"
    ;;
  weather_pollen)
    call_backend_job "${WEB_BACKEND_BASE}/jobs/weather/pollen_snapshot"
    ;;
  *)
    echo "$(ts) Unknown job: ${JOB}"
    exit 1
    ;;
esac
