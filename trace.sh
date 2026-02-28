#!/usr/bin/env bash
set -euo pipefail

# Load .env from repo root by default so WEBHOOK_SECRET and other vars
# are available without manual export.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

# Usage:
#   ./trace.sh +12146323629
#   PHONE=+12146323629 ./trace.sh

PHONE="${1:-${PHONE:-}}"
if [[ -z "${PHONE}" ]]; then
  echo "Usage: $0 +1XXXXXXXXXX"
  exit 1
fi

BASE="${BASE:-https://sentinel.northtexaspoolpros.com}"
DB="${DB:-/opt/ntpp-sentinel/data/sentinel.db}"
LIMIT_EVENTS="${LIMIT_EVENTS:-50}"
LIMIT_ISSUES="${LIMIT_ISSUES:-30}"
LOG_TAIL="${LOG_TAIL:-1200}"

if [[ -z "${WEBHOOK_SECRET:-}" ]]; then
  echo "Warning: WEBHOOK_SECRET is empty (set it in .env or export it)."
fi

echo "=== 1) Recent cron activity ==="
tail -n 120 /opt/ntpp-sentinel/logs/cron.log

echo
echo "=== 2) Active runtime crontab ==="
docker exec -i ntpp-sentinel sh -lc 'crontab -l'

echo
echo "=== 3) Manual job checks ==="
curl -i -s -X POST "$BASE/jobs/verify_pending" -H "X-NTPP-Secret: ${WEBHOOK_SECRET:-}"
echo
curl -i -s -X POST "$BASE/jobs/escalations" -H "X-NTPP-Secret: ${WEBHOOK_SECRET:-}"
echo
curl -i -s -X POST "$BASE/jobs/poll_resolver" -H "X-NTPP-Secret: ${WEBHOOK_SECRET:-}"
echo

echo
echo "=== 4) App logs (system-level verify/escalation errors) ==="
if command -v rg >/dev/null 2>&1; then
  docker compose logs --tail="$LOG_TAIL" sentinel | rg "verify_pending|poll_resolver|escalations|Traceback|ERROR|FLOW"
else
  docker compose logs --tail="$LOG_TAIL" sentinel | grep -E "verify_pending|poll_resolver|escalations|Traceback|ERROR|FLOW" || true
fi

LATEST_EVENT_RAW="$(sqlite3 -noheader -separator '|' "$DB" "
SELECT
  id,
  received_ts,
  source,
  COALESCE(json_extract(payload,'$.message.body'), json_extract(payload,'$.message'), '') AS msg_body,
  COALESCE(json_extract(payload,'$.contact_id'), json_extract(payload,'$.contactId'), '') AS contact_id
FROM raw_events
WHERE payload LIKE '%' || '$PHONE' || '%'
ORDER BY id DESC
LIMIT 1;
")"

LATEST_ISSUE_RAW="$(sqlite3 -noheader -separator '|' "$DB" "
SELECT
  id,
  status,
  COALESCE(conversation_id, '')
FROM issues
WHERE phone='$PHONE'
ORDER BY id DESC
LIMIT 1;
")"

EVENT_ID=""
EVENT_TS=""
EVENT_SOURCE=""
EVENT_BODY=""
EVENT_CONTACT_ID=""
if [[ -n "$LATEST_EVENT_RAW" ]]; then
  IFS='|' read -r EVENT_ID EVENT_TS EVENT_SOURCE EVENT_BODY EVENT_CONTACT_ID <<< "$LATEST_EVENT_RAW"
fi

LATEST_ISSUE_ID=""
LATEST_ISSUE_STATUS=""
LATEST_CONVERSATION_ID=""
if [[ -n "$LATEST_ISSUE_RAW" ]]; then
  IFS='|' read -r LATEST_ISSUE_ID LATEST_ISSUE_STATUS LATEST_CONVERSATION_ID <<< "$LATEST_ISSUE_RAW"
fi

echo
echo "=== 5) Issues for ${PHONE} ==="
sqlite3 -header -column "$DB" "
SELECT id, issue_type, status, COALESCE(contact_name,'(no name)') AS name, phone,
       conversation_id, created_ts, due_ts, resolved_ts, breach_notified_ts
FROM issues
WHERE phone='$PHONE'
ORDER BY id DESC
LIMIT ${LIMIT_ISSUES};
"

echo
echo "=== 6) Raw webhook events for ${PHONE} ==="
sqlite3 -header -column "$DB" "
SELECT id, received_ts, source
FROM raw_events
WHERE payload LIKE '%' || '$PHONE' || '%'
ORDER BY id DESC
LIMIT ${LIMIT_EVENTS};
"

echo
echo "=== 7) Latest inbound + decision trace (most relevant) ==="
echo "Latest event id: ${EVENT_ID:-n/a}"
echo "Latest event ts: ${EVENT_TS:-n/a}"
echo "Latest event source: ${EVENT_SOURCE:-n/a}"
echo "Latest event body: ${EVENT_BODY:-n/a}"
echo "Latest contact_id: ${EVENT_CONTACT_ID:-n/a}"
echo "Latest issue id/status: ${LATEST_ISSUE_ID:-n/a} / ${LATEST_ISSUE_STATUS:-n/a}"
echo "Latest conversation_id: ${LATEST_CONVERSATION_ID:-n/a}"

if command -v rg >/dev/null 2>&1; then
  LOG_CMD="docker compose logs --tail=${LOG_TAIL} sentinel"

  echo
  echo "--- decision events (FLOW + SMS decisions) ---"
  eval "$LOG_CMD" | rg "FLOW|sms\\.ignored_ack_closeout|sms\\.issue_created|sms\\.issue_updated|sms\\.promoted_open|sms\\.auto_resolved" || true

  if [[ -n "${EVENT_CONTACT_ID}" ]]; then
    echo
    echo "--- filtered by contact_id ${EVENT_CONTACT_ID} ---"
    eval "$LOG_CMD" | rg -F "${EVENT_CONTACT_ID}" || true
  fi

  if [[ -n "${LATEST_CONVERSATION_ID}" ]]; then
    echo
    echo "--- filtered by conversation_id ${LATEST_CONVERSATION_ID} ---"
    eval "$LOG_CMD" | rg -F "${LATEST_CONVERSATION_ID}" || true
  fi
else
  LOG_CMD="docker compose logs --tail=${LOG_TAIL} sentinel"

  echo
  echo "--- decision events (FLOW + SMS decisions) ---"
  eval "$LOG_CMD" | grep -E "FLOW|sms\\.ignored_ack_closeout|sms\\.issue_created|sms\\.issue_updated|sms\\.promoted_open|sms\\.auto_resolved" || true

  if [[ -n "${EVENT_CONTACT_ID}" ]]; then
    echo
    echo "--- filtered by contact_id ${EVENT_CONTACT_ID} ---"
    eval "$LOG_CMD" | grep -F "${EVENT_CONTACT_ID}" || true
  fi

  if [[ -n "${LATEST_CONVERSATION_ID}" ]]; then
    echo
    echo "--- filtered by conversation_id ${LATEST_CONVERSATION_ID} ---"
    eval "$LOG_CMD" | grep -F "${LATEST_CONVERSATION_ID}" || true
  fi
fi
