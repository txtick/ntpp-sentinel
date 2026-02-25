#!/bin/sh
set -e

JOB="${1:-}"

# ---- Load env for cron (cron does NOT load your shell env) ----
# Try common paths (adjust if your repo lives elsewhere in-container)
ENV_FILE=""
for p in /opt/ntpp-sentinel/.env /app/.env /.env; do
  if [ -f "$p" ]; then ENV_FILE="$p"; break; fi
done

if [ -n "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

# Reuse the same secret as your webhooks/jobs auth
: "${WEBHOOK_SECRET:?WEBHOOK_SECRET is not set}"

HDR="X-NTPP-Secret: ${WEBHOOK_SECRET}"
CURL="/usr/bin/curl"
BASE="http://localhost:8000"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

call_job() {
  URL="$1"
  echo "$(ts) cron: ${JOB} -> POST ${URL}"
  # log status code but don't dump response body
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
  escalations)
    call_job "${BASE}/jobs/escalations"
    ;;
  *)
    echo "$(ts) Unknown job: ${JOB}"
    exit 1
    ;;
esac