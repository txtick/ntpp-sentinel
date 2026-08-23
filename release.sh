#!/usr/bin/env bash
set -euo pipefail

# Phone/Codex-friendly production release helper.
# Usage:
#   ./release.sh "commit message"
#   ./release.sh "commit message" main
#
# Optional overrides:
#   REMOTE_HOST=kevin@sentinel.northtexaspoolpros.com
#   REMOTE_KEY=$HOME/.ssh/kevin-nttp-droplet

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 \"commit message\" [branch]"
  exit 1
fi

MSG="$1"
BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD)}"
REMOTE_HOST="${REMOTE_HOST:-kevin@sentinel.northtexaspoolpros.com}"
REMOTE_KEY="${REMOTE_KEY:-$HOME/.ssh/kevin-nttp-droplet}"

if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "[release] refusing unsafe branch name: ${BRANCH}" >&2
  exit 1
fi

if [[ ! -f "$REMOTE_KEY" ]]; then
  echo "[release] SSH key not found: ${REMOTE_KEY}" >&2
  exit 1
fi

echo "[release] branch: ${BRANCH}"
echo "[release] remote: ${REMOTE_HOST}"

./push.sh "$MSG" "$BRANCH"

echo "[release] deploying on production"
ssh -i "$REMOTE_KEY" "$REMOTE_HOST" "cd /opt/ntpp-sentinel && ./deploy.sh '$BRANCH'"

echo "[release] public health checks"
curl -fsS https://sentinel.northtexaspoolpros.com/health
echo
curl -fsS https://dashboard.northtexaspoolpros.com/health
echo

echo "[release] done"