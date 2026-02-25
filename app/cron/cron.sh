#!/bin/sh
set -e

JOB="$1"

# Reuse the same secret as your webhooks/jobs auth
# (must exist in .env so the container gets it)
if [ -z "${WEBHOOK_SECRET:-}" ]; then
  echo "WEBHOOK_SECRET is not set"
  exit 1
fi

HDR="X-NTPP-Secret: ${WEBHOOK_SECRET}"
CURL="/usr/bin/curl"

if [ "$JOB" = "morning" ]; then
  curl -fsS -X POST "http://localhost:8000/jobs/send_summary?slot=morning" -H "$HDR" >/dev/null
elif [ "$JOB" = "midday" ]; then
  curl -fsS -X POST "http://localhost:8000/jobs/send_summary?slot=midday" -H "$HDR" >/dev/null
elif [ "$JOB" = "afternoon" ]; then
  curl -fsS -X POST "http://localhost:8000/jobs/send_summary?slot=afternoon" -H "$HDR" >/dev/null
elif [ "$JOB" = "poll_resolver" ]; then
  curl -fsS -X POST "http://localhost:8000/jobs/poll_resolver" -H "$HDR" >/dev/null
elif [ "$JOB" = "escalations" ]; then
  curl -fsS -X POST "http://localhost:8000/jobs/escalations" -H "$HDR" >/dev/null
else
  echo "Unknown job: $JOB"
  exit 1
fi
