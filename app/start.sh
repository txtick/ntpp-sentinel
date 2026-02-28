#!/bin/sh
set -eu

/app/cron/render-crontab.sh
/etc/init.d/cron start

exec uvicorn main:app --host 0.0.0.0 --port 8000
