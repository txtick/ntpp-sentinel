# Skimmer Database Export

This file documents the discovered Skimmer SQLite export found in the repository as `3f19c6b0c1ef4a1d876f942348997106.db`.

## What it is

- A Skimmer/field-service system export, not the Sentinel runtime database.
- Contains customer, pool, work order, routing, invoice, quote, and service stop data.
- The export is a snapshot of operational service management records.

## Key observations

- Total tables: 45
- Only one empty table: `QuoteAttachment`
- Most tables follow the same metadata pattern:
  - `CreatedAt`, `UpdatedAt`, `Deleted`, `Version`
  - `id` as a text primary key
- Most entity tables are densely populated, which makes this a useful sample dataset.

## Populated tables and row counts

- `Customer`: 382
- `Pool`: 402
- `ServiceLocation`: 384
- `ServiceStop`: 3605
- `ServiceStopEntry`: 27006
- `WorkOrder`: 890
- `WorkOrderType`: 31
- `RouteStop`: 3630
- `RouteAssignment`: 1524
- `RouteMove`: 696
- `RouteSkip`: 336
- `Invoice`: 887
- `InvoiceItem`: 963
- `Quote`: 557
- `QuoteItem`: 1016
- `Payment`: 900
- `Product`: 81
- `ProductCategory`: 3
- `PartModel`: 3580
- `EntryValue`: 1664
- `EntryDescription`: 37
- `ShoppingListItem`: 51

## Important domain tables

- `Customer`: customer contact and billing information.
- `Pool`: pool-specific data associated with customers.
- `ServiceLocation`: service address records.
- `ServiceStop`: individual service stop records for routes.
- `ServiceStopEntry`: line items or actions performed at a stop.
- `WorkOrder`: work orders created for service jobs.
- `RouteStop`: routing stop records for daily schedule execution.
- `RouteAssignment`: crew/tech assignment records.
- `Invoice` / `InvoiceItem` / `Payment`: billing records and payments.
- `Quote` / `QuoteItem`: estimate and quote records.

## What this means for Sentinel

- This export is the type of data Sentinel is expected to ingest via the Skimmer import workflow.
- The new `/jobs/skimmer_link` endpoint is designed to receive a signed URL for a `.db.gz` export and immediately download it into `SKIMMER_DB_PATH`.
- Once downloaded and decompressed, this file can be used by custom ingestion logic or merged into Sentinel state.

## Notes

- The export includes both master data (`Customer`, `Pool`, `Product`, `PartModel`) and transactional data (`WorkOrder`, `Invoice`, `RouteStop`, `ServiceStopEntry`).
- There is no built-in Sentinel-specific schema in this export; it is entirely Skimmer/field-service data.
- If you want to explore the export further, use a SQLite tool or Python script to inspect individual tables and relationships.

## Next steps

- Use `SKIMMER_DB_PATH` in `.env` to designate where Sentinel should save the downloaded DB.
- Confirm that the export file structure matches your planned ingest logic before attempting a merge.
- Keep the export file under version control only for documentation or sample-data purposes, not as an operational runtime file.

## Next step

- Use `scripts/pg_bootstrap.sql` or `app/pg.py` to create the Postgres side tables.
- Run the first customer import with:

```bash
python scripts/import_skimmer_customers.py --sqlite /data/skimmer/skimmer.db
```

- After the first customer import, run the next tables with:

```bash
python scripts/import_skimmer_customers.py --sqlite /data/skimmer/skimmer.db --tables pools,locations
```

- Import service stop metadata and readings with:

```bash
python scripts/import_skimmer_customers.py --sqlite /data/skimmer/skimmer.db --tables entry_descriptions,service_stop_entries
```

- Or import everything with:

```bash
python scripts/import_skimmer_customers.py --sqlite /data/skimmer/skimmer.db --tables all
```

- To run this automatically after the nightly Skimmer download, add cron settings in `.env`:

```bash
SKIMMER_IMPORT_TABLES=all
CRON_SKIMMER_IMPORT_MINUTE=30
CRON_SKIMMER_IMPORT_HOUR=11
CRON_SKIMMER_IMPORT_DOW=1-5
```

This creates a weekday-only import job that runs after the nightly sync.

- The Postgres bootstrap now creates a `sk_pool_with_service_location` view for joined pool/location records and a `sk_service_stop_entry_with_location` view for service stop readings joined to the customer.
- Confirm `skimmer_import_runs`, `sk_customer`, `customer_identity_map`, `sk_pool`, `sk_service_location`, `sk_entry_description`, and `sk_service_stop_entry` are populated in Postgres.
