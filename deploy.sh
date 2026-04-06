#!/usr/bin/env bash
set -euo pipefail

# Simple server deploy helper.
# Usage:
#   ./deploy.sh
#   ./deploy.sh main

BRANCH="${1:-main}"

echo "[deploy] repo: $(pwd)"
echo "[deploy] branch: ${BRANCH}"

git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"
docker compose up -d --build --remove-orphans

echo "[deploy] done"
docker compose ps

sleep 8

echo "ingest-worker logs"
docker compose logs --tail=50 ingest-worker

echo "sentinel logs"
docker compose logs --tail=50 sentinel