#!/bin/sh
set -eu

exec uvicorn web_backend_main:app --host 0.0.0.0 --port 8020 --log-config /app/config/uvicorn_log_config.json
