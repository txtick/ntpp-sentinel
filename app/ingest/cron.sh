#!/bin/sh
set -eu

if [ -r /proc/1/environ ]; then
  export $(tr '\0' '\n' < /proc/1/environ \
    | grep -E '^(DATABASE_URL|SKIMMER_DB_PATH|INGEST_SOURCE_SYSTEM|INGEST_FATAL_DROP_RATIO|INGEST_PIPELINE_LOCK_KEY|INACTIVE_PRUNE_DAYS|MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD|TIMEZONE|TZ)=' \
    | xargs)
fi

: "${DATABASE_URL:?DATABASE_URL is not set}"
: "${SKIMMER_DB_PATH:?SKIMMER_DB_PATH is not set}"

python -m ingest.run --sqlite "${SKIMMER_DB_PATH}" --source-system "${INGEST_SOURCE_SYSTEM:-skimmer}"
