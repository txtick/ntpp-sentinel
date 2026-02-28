#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./push.sh "my commit message"
#   ./push.sh "my commit message" main

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 \"commit message\" [branch]"
  exit 1
fi

MSG="$1"
BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD)}"

if [[ -z "$(git status --porcelain)" ]]; then
  echo "[push] no changes to commit"
  exit 0
fi

echo "[push] branch: ${BRANCH}"
git add -A

if git diff --cached --quiet; then
  echo "[push] nothing staged after git add -A"
  exit 0
fi

git commit -m "$MSG"
git push origin "$BRANCH"
echo "[push] done"
