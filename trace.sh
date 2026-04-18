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
#   ./trace.sh +12146323629 --save
#   ./trace.sh +12146323629 --summary --save
#   ./trace.sh +12146323629 --output /tmp/jason-trace.txt

PHONE="${PHONE:-}"
SUMMARY_MODE=0
SAVE_MODE=0
OUTPUT_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --summary)
      SUMMARY_MODE=1
      shift
      ;;
    --save)
      SAVE_MODE=1
      shift
      ;;
    --output)
      OUTPUT_PATH="${2:-}"
      if [[ -z "$OUTPUT_PATH" ]]; then
        echo "Usage: $0 +1XXXXXXXXXX [--summary] [--save] [--output /path/to/report.txt]"
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 +1XXXXXXXXXX [--summary] [--save] [--output /path/to/report.txt]"
      exit 0
      ;;
    *)
      if [[ -z "$PHONE" ]]; then
        PHONE="$1"
        shift
      else
        echo "Unknown argument: $1"
        echo "Usage: $0 +1XXXXXXXXXX [--summary] [--save] [--output /path/to/report.txt]"
        exit 1
      fi
      ;;
  esac
done

if [[ -z "${PHONE}" ]]; then
  echo "Usage: $0 +1XXXXXXXXXX [--summary] [--save] [--output /path/to/report.txt]"
  exit 1
fi

BASE="${BASE:-https://sentinel.northtexaspoolpros.com}"
DB="${DB:-/opt/ntpp-sentinel/data/sentinel.db}"
LIMIT_EVENTS="${LIMIT_EVENTS:-50}"
LIMIT_ISSUES="${LIMIT_ISSUES:-30}"
LOG_TAIL="${LOG_TAIL:-1200}"

REPORT_TS="$(date +%Y%m%d-%H%M%S)"
SAFE_PHONE="$(printf '%s' "$PHONE" | tr -cd '0-9+')"
if [[ "$SAVE_MODE" == "1" && -z "$OUTPUT_PATH" ]]; then
  OUTPUT_PATH="/tmp/trace-${SAFE_PHONE}-${REPORT_TS}.txt"
fi

if [[ -n "$OUTPUT_PATH" ]]; then
  mkdir -p "$(dirname "$OUTPUT_PATH")"
  exec > >(tee "$OUTPUT_PATH") 2>&1
fi

if [[ -z "${WEBHOOK_SECRET:-}" ]]; then
  echo "Warning: WEBHOOK_SECRET is empty (set it in .env or export it)."
fi

echo "trace.sh mode: phone=${PHONE} summary=${SUMMARY_MODE} save=$([[ -n "$OUTPUT_PATH" ]] && echo 1 || echo 0)"
if [[ -n "$OUTPUT_PATH" ]]; then
  echo "trace.sh output: ${OUTPUT_PATH}"
fi
echo

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
if [[ "$SUMMARY_MODE" == "1" ]]; then
  if command -v rg >/dev/null 2>&1; then
    docker compose logs --tail="$LOG_TAIL" sentinel | rg "verify_pending|poll_resolver|escalations|Traceback|ERROR"
  else
    docker compose logs --tail="$LOG_TAIL" sentinel | grep -E "verify_pending|poll_resolver|escalations|Traceback|ERROR" || true
  fi
else
  if command -v rg >/dev/null 2>&1; then
    docker compose logs --tail="$LOG_TAIL" sentinel | rg "verify_pending|poll_resolver|escalations|Traceback|ERROR|FLOW"
  else
    docker compose logs --tail="$LOG_TAIL" sentinel | grep -E "verify_pending|poll_resolver|escalations|Traceback|ERROR|FLOW" || true
  fi
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

LATEST_AI_GATE_RAW=""

LATEST_UNANSWERED_RAW="$(sqlite3 -noheader -separator '|' "$DB" "
SELECT
  id,
  received_ts
FROM raw_events
WHERE source='unanswered_call'
  AND payload LIKE '%' || '$PHONE' || '%'
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

if [[ -n "${LATEST_CONVERSATION_ID}" ]]; then
  LATEST_AI_GATE_RAW="$(sqlite3 -noheader -separator '|' "$DB" "
SELECT
  conversation_id,
  last_msg_ts,
  needs_follow_up,
  confidence,
  COALESCE(evidence_json, ''),
  COALESCE(model, ''),
  created_ts
FROM conversation_ai_gate
WHERE conversation_id='${LATEST_CONVERSATION_ID}'
LIMIT 1;
")"
fi

LATEST_UNANSWERED_ID=""
LATEST_UNANSWERED_TS=""
if [[ -n "$LATEST_UNANSWERED_RAW" ]]; then
  IFS='|' read -r LATEST_UNANSWERED_ID LATEST_UNANSWERED_TS <<< "$LATEST_UNANSWERED_RAW"
fi

AI_GATE_CONVERSATION_ID=""
AI_GATE_LAST_MSG_TS=""
AI_GATE_NEEDS_FOLLOW_UP=""
AI_GATE_CONFIDENCE=""
AI_GATE_EVIDENCE_JSON=""
AI_GATE_MODEL=""
AI_GATE_CREATED_TS=""
if [[ -n "$LATEST_AI_GATE_RAW" ]]; then
  IFS='|' read -r AI_GATE_CONVERSATION_ID AI_GATE_LAST_MSG_TS AI_GATE_NEEDS_FOLLOW_UP AI_GATE_CONFIDENCE AI_GATE_EVIDENCE_JSON AI_GATE_MODEL AI_GATE_CREATED_TS <<< "$LATEST_AI_GATE_RAW"
fi

echo
echo "=== 5) Issues for ${PHONE} ==="
sqlite3 -header -column "$DB" "
SELECT id, issue_type, status, COALESCE(contact_name,'(no name)') AS name, phone,
  conversation_id, created_ts, due_ts, resolved_ts, breach_notified_ts,
  COALESCE(json_extract(meta,'$.resolved_by'),'') AS resolved_by,
  COALESCE(json_extract(meta,'$.resolution_signal'),'') AS resolution_signal
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
echo "=== 6b) Unanswered-call evidence for ${PHONE} ==="
sqlite3 -header -column "$DB" "
SELECT
  id,
  received_ts,
  source,
  COALESCE(
    json_extract(payload,'$.voicemail_route'),
    json_extract(payload,'$.data.voicemail_route'),
    json_extract(payload,'$.message.voicemail_route'),
    ''
  ) AS voicemail_route,
  COALESCE(
    json_extract(payload,'$.sentinel_missed_call'),
    json_extract(payload,'$.customData.sentinel_missed_call'),
    json_extract(payload,'$.data.sentinel_missed_call'),
    json_extract(payload,'$.message.sentinel_missed_call'),
    ''
  ) AS sentinel_missed_call,
  COALESCE(
    json_extract(payload,'$.missed_call'),
    json_extract(payload,'$.customData.missed_call'),
    json_extract(payload,'$.data.missed_call'),
    json_extract(payload,'$.message.missed_call'),
    ''
  ) AS missed_call,
  COALESCE(
    json_extract(payload,'$.is_missed_call'),
    json_extract(payload,'$.customData.is_missed_call'),
    json_extract(payload,'$.data.is_missed_call'),
    json_extract(payload,'$.message.is_missed_call'),
    ''
  ) AS is_missed_call,
  COALESCE(
    json_extract(payload,'$.conversationId'),
    json_extract(payload,'$.conversation_id'),
    json_extract(payload,'$.data.conversationId'),
    json_extract(payload,'$.data.conversation_id'),
    ''
  ) AS conversation_id
FROM raw_events
WHERE source='unanswered_call'
  AND payload LIKE '%' || '$PHONE' || '%'
ORDER BY id DESC
LIMIT ${LIMIT_EVENTS};
"

echo
echo "=== 6c) Latest unanswered_call payload (full) ==="
if [[ -n "${LATEST_UNANSWERED_ID}" ]]; then
  CALL_PAYLOAD="$(sqlite3 -noheader "$DB" "SELECT payload FROM raw_events WHERE id=${LATEST_UNANSWERED_ID} LIMIT 1;")"
  if [[ -n "$CALL_PAYLOAD" ]]; then
    if command -v jq >/dev/null 2>&1; then
      printf '%s\n' "$CALL_PAYLOAD" | jq .
    else
      printf '%s\n' "$CALL_PAYLOAD"
    fi
  else
    echo "No payload found for unanswered_call id ${LATEST_UNANSWERED_ID}"
  fi
else
  echo "No unanswered_call payload found for this phone."
fi

echo
echo "=== 7) Latest inbound + decision trace (most relevant) ==="
echo "Latest event id: ${EVENT_ID:-n/a}"
echo "Latest event ts: ${EVENT_TS:-n/a}"
echo "Latest event source: ${EVENT_SOURCE:-n/a}"
echo "Latest event body: ${EVENT_BODY:-n/a}"
echo "Latest contact_id: ${EVENT_CONTACT_ID:-n/a}"
echo "Latest issue id/status: ${LATEST_ISSUE_ID:-n/a} / ${LATEST_ISSUE_STATUS:-n/a}"
echo "Latest conversation_id: ${LATEST_CONVERSATION_ID:-n/a}"
echo "AI gate cache decision: ${AI_GATE_NEEDS_FOLLOW_UP:-n/a}"
echo "AI gate confidence: ${AI_GATE_CONFIDENCE:-n/a}"
echo "AI gate model: ${AI_GATE_MODEL:-n/a}"
echo "AI gate cached at: ${AI_GATE_CREATED_TS:-n/a}"
echo "AI gate cached last_msg_ts: ${AI_GATE_LAST_MSG_TS:-n/a}"
echo "AI gate evidence: ${AI_GATE_EVIDENCE_JSON:-n/a}"

echo
echo "=== 7c) Recheck hint ==="
if [[ -n "${LATEST_CONVERSATION_ID}" ]]; then
  echo "Force a fresh AI classification for this thread:"
  echo "./curl_job.sh \"/jobs/recheck_issue?conversation_id=${LATEST_CONVERSATION_ID}\""
  if [[ -n "${LATEST_ISSUE_ID}" ]]; then
    echo "Or by issue id:"
    echo "./curl_job.sh \"/jobs/recheck_issue?id=${LATEST_ISSUE_ID}\""
  fi
else
  echo "Skipped: no conversation_id found on latest issue for this phone."
fi

echo
echo "=== 7b) AI gate cache row ==="
if [[ -n "${LATEST_CONVERSATION_ID}" ]]; then
  sqlite3 -header -column "$DB" "
SELECT conversation_id, last_msg_ts, needs_follow_up, confidence, evidence_json, model, created_ts
FROM conversation_ai_gate
WHERE conversation_id='${LATEST_CONVERSATION_ID}';
"
else
  echo "Skipped: no conversation_id found on latest issue for this phone."
fi

FOCUSED_LOG_CMD=""
if [[ -n "${EVENT_TS}" ]]; then
  FOCUSED_LOG_CMD="docker compose logs --since=${EVENT_TS} --tail=400 sentinel"
fi

if command -v rg >/dev/null 2>&1; then
  LOG_CMD="docker compose logs --tail=${LOG_TAIL} sentinel"

  echo
  echo "--- decision events (FLOW + SMS/CALL decisions) ---"
  if [[ "$SUMMARY_MODE" == "1" ]]; then
    eval "$LOG_CMD" | rg "sms\\.ignored_ack_closeout|sms\\.issue_created|sms\\.issue_updated|sms\\.auto_resolved|call\\.issue_created|call\\.ignored|call\\.auto_resolved|ai_gate\\.inbound_call|ai_gate\\.inbound_sms" || true
  else
    eval "$LOG_CMD" | rg "FLOW|sms\\.ignored_ack_closeout|sms\\.issue_created|sms\\.issue_updated|sms\\.auto_resolved|call\\.issue_created|call\\.ignored|call\\.auto_resolved|ai_gate\\.inbound_call|ai_gate\\.inbound_sms" || true
  fi

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

  if [[ -n "${FOCUSED_LOG_CMD}" ]]; then
    echo
    echo "--- focused logs since latest event ts ${EVENT_TS} ---"
    eval "$FOCUSED_LOG_CMD" | rg "FLOW|ai_gate|ignored_|issue_created|issue_updated|auto_resolved|Traceback|ERROR" || true
  fi
else
  LOG_CMD="docker compose logs --tail=${LOG_TAIL} sentinel"

  echo
  echo "--- decision events (FLOW + SMS/CALL decisions) ---"
  if [[ "$SUMMARY_MODE" == "1" ]]; then
    eval "$LOG_CMD" | grep -E "sms\\.ignored_ack_closeout|sms\\.issue_created|sms\\.issue_updated|sms\\.auto_resolved|call\\.issue_created|call\\.ignored|call\\.auto_resolved|ai_gate\\.inbound_call|ai_gate\\.inbound_sms" || true
  else
    eval "$LOG_CMD" | grep -E "FLOW|sms\\.ignored_ack_closeout|sms\\.issue_created|sms\\.issue_updated|sms\\.auto_resolved|call\\.issue_created|call\\.ignored|call\\.auto_resolved|ai_gate\\.inbound_call|ai_gate\\.inbound_sms" || true
  fi

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

  if [[ -n "${FOCUSED_LOG_CMD}" ]]; then
    echo
    echo "--- focused logs since latest event ts ${EVENT_TS} ---"
    eval "$FOCUSED_LOG_CMD" | grep -E "FLOW|ai_gate|ignored_|issue_created|issue_updated|auto_resolved|Traceback|ERROR" || true
  fi
fi

echo
echo "=== 9) Share hint ==="
if [[ -n "$OUTPUT_PATH" ]]; then
  echo "Saved trace report to: ${OUTPUT_PATH}"
  echo "Smallest useful command to share it later:"
  echo "  sed -n '1,220p' ${OUTPUT_PATH}"
else
  echo "Tip: run with --save to write the report to /tmp instead of copy/pasting terminal output."
  echo "Tip: run with --summary --save for a much smaller report."
fi

echo
echo "=== 8) GHL conversation timeline (for resolver proof) ==="
if [[ -z "${LATEST_CONVERSATION_ID}" ]]; then
  echo "Skipped: no conversation_id found on latest issue for this phone."
elif [[ -z "${GHL_TOKEN:-}" ]]; then
  echo "Skipped: GHL_TOKEN not set in environment/.env."
else
  GHL_API="${GHL_BASE_URL:-https://services.leadconnectorhq.com}"
  GHL_VER="${GHL_VERSION:-2021-07-28}"
  GHL_LOC="${GHL_LOCATION_ID:-}"
  RESP="$(curl -sS --get "${GHL_API%/}/conversations/${LATEST_CONVERSATION_ID}/messages" \
    --data-urlencode "limit=50" \
    -H "Authorization: Bearer ${GHL_TOKEN}" \
    -H "Version: ${GHL_VER}" \
    -H "LocationId: ${GHL_LOC}")"
  if command -v jq >/dev/null 2>&1; then
    printf '%s\n' "$RESP" | jq -r '
      def msg_list:
        if (.messages | type) == "array" then .messages
        elif (.messages | type) == "object" and ((.messages.messages | type) == "array") then .messages.messages
        elif (.data | type) == "array" then .data
        elif (.data | type) == "object" and ((.data.messages | type) == "array") then .data.messages
        else []
        end;
      msg_list as $msgs
      | if (($msgs | length) > 0) then
          ($msgs[] | [
            (.dateAdded // ""),
            (.direction // ""),
            (.type // .messageType // ""),
            (.userId // ""),
            (.callStatus // .status // ""),
            ((.body // .message // .text // "") | tostring | gsub("[\\r\\n]+"; " ") | .[0:120])
          ] | @tsv)
        else
        (
          "No message list found in API response"
          + (if (type == "object") then " | keys=" + ((keys | join(",")) // "") else "" end)
          + (if (.statusCode != null) then " | statusCode=" + (.statusCode|tostring) else "" end)
          + (if (.message != null) then " | message=" + (.message|tostring) else "" end)
        )
        end'
  else
    printf '%s\n' "$RESP"
  fi
fi
