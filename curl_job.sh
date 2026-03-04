#!/usr/bin/env bash
set -euo pipefail

# Sentinel authenticated job caller.
# Usage:
#   ./curl_job.sh /jobs/verify_pending
#   ./curl_job.sh "/jobs/cleanup_raw_events?dry_run=1"
#   ./curl_job.sh https://sentinel.northtexaspoolpros.com/jobs/poll_resolver

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 <job-path-or-url>"
  echo "Example: $0 /jobs/verify_pending"
  exit 1
fi

BASE="${BASE:-https://sentinel.northtexaspoolpros.com}"
CONTAINER="${CONTAINER:-ntpp-sentinel}"

if [[ "$TARGET" =~ ^https?:// ]]; then
  URL="$TARGET"
else
  if [[ "$TARGET" != /* ]]; then
    TARGET="/$TARGET"
  fi
  URL="${BASE}${TARGET}"
fi

SECRET="${WEBHOOK_SECRET:-}"
if [[ -z "$SECRET" ]]; then
  if command -v docker >/dev/null 2>&1; then
    SECRET="$(docker exec "$CONTAINER" sh -lc 'printf %s "$WEBHOOK_SECRET"' 2>/dev/null || true)"
  fi
fi

if [[ -z "$SECRET" ]]; then
  echo "Error: WEBHOOK_SECRET not found in shell/.env or container env ($CONTAINER)."
  exit 1
fi

curl -sS -X POST "$URL" -H "X-NTPP-Secret: $SECRET"
echo
