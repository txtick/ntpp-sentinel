#!/bin/sh
set -e

JOB="${1:-}"

# Cron often runs with a minimal environment.
# Pull needed vars from the container's PID 1 environment.
if [ -r /proc/1/environ ]; then
  export $(tr '\0' '\n' < /proc/1/environ \
    | grep -E '^(WEBHOOK_SECRET|GHL_TOKEN|GHL_VERSION|MANAGER_CONTACT_IDS|GHL_LOCATION_ID)=' \
    | xargs)
fi

: "${WEBHOOK_SECRET:?WEBHOOK_SECRET is not set}"

HDR="X-NTPP-Secret: ${WEBHOOK_SECRET}"
CURL="/usr/bin/curl"
BASE="http://localhost:8000"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

call_job() {
  URL="$1"
  echo "$(ts) cron: ${JOB} -> POST ${URL}"
  "$CURL" -sS -o /dev/null -w "$(ts) cron: ${JOB} <- http=%{http_code}\n" \
    -X POST "${URL}" -H "${HDR}"
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
  *)
    echo "$(ts) Unknown job: ${JOB}"
    exit 1
    ;;
esac
