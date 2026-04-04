# NTPP Ingest Pipeline

## Run Flow

1. `sentinel` downloads the latest Skimmer SQLite export to `SKIMMER_DB_PATH`.
2. After a successful download, `sentinel` calls `POST /jobs/run` on the internal `ingest-worker`.
3. `ingest-worker` validates the SQLite snapshot, imports the production-compatible `sk_*` source-ingest tables, then upserts the normalized operational tables and refreshes derived views.
4. A fallback cron still exists inside `ingest-worker`, but it is secondary to the download-triggered flow.

## Layering

- Source-ingest compatibility: `sk_*` tables remain the customer-sync source of truth.
- Normalized operational layer: `customers`, `pools`, `chemistry_readings`, `chemical_dose_events`.
- Derived analytics layer: `current_chemistry_alerts_v`, `chemistry_trend_alerts_v`, `revenue_opportunities_v`, `dashboard_summary_v`.

## Pruning

- Normalized customers stay in scope while operationally active.
- Inactive customers stay in scope for `INACTIVE_PRUNE_DAYS`.
- Once a customer ages past that window, the normalized customer row is pruned and dependent pool data cascades away.
- Source-ingest `sk_*` tables are not pruned by this process.

## Worker CLI

```bash
python -m ingest.run --sqlite /data/skimmer/skimmer.db --source-system skimmer
```

Validation only:

```bash
python -m ingest.run --sqlite /data/skimmer/skimmer.db --validate-only
```
