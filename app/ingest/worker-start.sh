#!/bin/sh
set -eu

mkdir -p /logs /data
touch /logs/ingest-worker.log
/app/ingest/render-worker-crontab.sh

/etc/init.d/cron start

exec uvicorn ingest.worker_api:app --host 0.0.0.0 --port 8010
