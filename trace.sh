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
LOG_TAIL="${LOG_TAIL:-500}"
if [[ -z "${WEBHOOK_SECRET:-}" ]]; then
  echo "Warning: WEBHOOK_SECRET is empty (set it in .env or export it)."
fi

echo "=== 1) Raw webhook events for ${PHONE} ==="
sqlite3 -header -column "$DB" "
SELECT id, received_ts, source
FROM raw_events
WHERE payload LIKE '%' || '$PHONE' || '%'
ORDER BY id DESC
LIMIT ${LIMIT_EVENTS};
"

echo
echo "=== 2) Issues for ${PHONE} ==="
sqlite3 -header -column "$DB" "
SELECT id, issue_type, status, COALESCE(contact_name,'(no name)') AS name, phone,
       conversation_id, created_ts, due_ts, resolved_ts, breach_notified_ts
FROM issues
WHERE phone='$PHONE'
ORDER BY id DESC
LIMIT ${LIMIT_ISSUES};
"

echo
echo "=== 3) Recent cron activity ==="
tail -n 60 /opt/ntpp-sentinel/logs/cron.log

echo
echo "=== 4) Active runtime crontab ==="
docker exec -i ntpp-sentinel sh -lc 'crontab -l'

echo
echo "=== 5) Manual job checks ==="
curl -i -s -X POST "$BASE/jobs/verify_pending" -H "X-NTPP-Secret: ${WEBHOOK_SECRET:-}"
echo
curl -i -s -X POST "$BASE/jobs/escalations" -H "X-NTPP-Secret: ${WEBHOOK_SECRET:-}"
echo
curl -i -s -X POST "$BASE/jobs/poll_resolver" -H "X-NTPP-Secret: ${WEBHOOK_SECRET:-}"
echo

echo
echo "=== 6) App logs (decision path/errors) ==="
if command -v rg >/dev/null 2>&1; then
  docker compose logs --tail="$LOG_TAIL" sentinel | rg -F "$PHONE"
else
  docker compose logs --tail="$LOG_TAIL" sentinel | grep -F "$PHONE" || true
fi

echo
echo "=== 7) App logs (system-level verify/escalation errors) ==="
if command -v rg >/dev/null 2>&1; then
  docker compose logs --tail="$LOG_TAIL" sentinel | rg "verify_pending|poll_resolver|escalations|Traceback|ERROR|FLOW"
else
  docker compose logs --tail="$LOG_TAIL" sentinel | grep -E "verify_pending|poll_resolver|escalations|Traceback|ERROR|FLOW" || true
fi
