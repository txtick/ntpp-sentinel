# NTPP Platform Design (Current State)

This is the primary architecture and product-state document for the repository.

If a different doc disagrees with this one, this document wins.

## Active Docs

Top-level `docs/` is intentionally limited to:

- `Current State - Source of Truth.md`
- `START UP & RECOVERY.md`
- `Sentinel - Manager Guide.md`

Older planning, master-state, and architecture drafts are preserved under `docs/archive/`.

## What The Platform Is Today

The platform currently has two live backend services and one future-facing read layer plan.

### Sentinel

Sentinel is the live orchestration app.

Responsibilities:
- GHL webhooks
- SLA issue tracking for SMS and CALL follow-up
- manager summaries and escalation alerts
- customer sync between Skimmer and GHL
- Route Rollover technician messaging
- Skimmer DB download orchestration
- triggering the ingest worker after successful Skimmer download

### Ingest Worker

The ingest worker is the live normalization pipeline.

Responsibilities:
- validate Skimmer SQLite snapshots
- import the production-compatible `sk_*` source-ingest layer
- upsert normalized operational tables
- preserve historical chemistry and dosage events
- maintain `inactive_since`
- prune normalized/dashboard scope for customers inactive more than 60 days
- refresh derived operational analytics views

### Dashboard / App Layer

Not built yet.

The future dashboard must read:
- normalized tables
- derived views

It must not treat `sk_*` tables as the final application model.

## Runtime Architecture

### Public Traffic

- Caddy proxies public traffic to `sentinel:8000`
- `ingest-worker` stays internal on Docker networking

### Containers

- `sentinel`
- `ingest-worker`
- `caddy`

### Persistence

- SQLite runtime DB for Sentinel issue tracking
- PostgreSQL for Skimmer source-ingest, normalized operational data, and derived analytics
- shared `/data` mount for downloaded Skimmer DB snapshots
- shared `/logs` mount for runtime and cron logs

## Data Layers

### 1. Source-Ingest Layer

Current production-compatible tables:
- `skimmer_import_runs`
- `sk_customer`
- `customer_identity_map`
- `sk_pool`
- `sk_service_location`
- `sk_entry_description`
- `sk_service_stop_entry`

Rules:
- preserve compatibility
- do not rename or repurpose casually
- customer sync still depends on this layer

### 2. Normalized Operational Layer

Current normalized tables:
- `customers`
- `pools`
- `chemistry_readings`
- `chemical_dose_events`
- `ingest_pipeline_runs`

Rules:
- stable upserts, not delete/recreate
- stable normalized IDs across normal reruns
- chemistry and dosage history preserved by `source_entry_id`
- explicit pruning instead of full wipes

### 3. Config Layer

Current config tables:
- `alert_rule_config`
- `trend_rule_config`
- `revenue_rule_config`

Purpose:
- avoid scattering hard-coded thresholds
- allow future seasonal tuning and operational changes without rewriting view logic

### 4. Derived Analytics Layer

Current derived views:
- `current_chemistry_alerts_v`
- `chemistry_trend_alerts_v`
- `revenue_opportunities_v`
- `dashboard_summary_v`

## Current Skimmer Pipeline Flow

Primary path:

1. Sentinel downloads the latest Skimmer DB from the shared Google Drive folder.
2. Sentinel saves it to `SKIMMER_DB_PATH`.
3. Sentinel calls the internal ingest worker.
4. Worker accepts the job quickly and spawns the pipeline in the background.
5. Worker validates, imports `sk_*`, upserts normalized tables, and refreshes derived views.

Fallback path:

- ingest-worker also has a fallback cron schedule
- that cron is secondary, not the primary orchestration path

## Current Operational Rules

### Route Rollover

Route Rollover is live inside Sentinel.

Purpose:
- detect route rollover situations
- identify the assigned technician through Skimmer
- send the rollover/apology communication into the correct GHL conversation

Current behavior:
- controlled by `ROLLOVER_ENABLED`
- depends on Skimmer API access via `SKIMMER_API_BASE_URL` and `SKIMMER_API_KEY`
- uses `SKIMMER_TECH_ID_MAP` to map GHL internal contacts to Skimmer tech IDs
- uses guarded conversation matching so rollover messaging only sends on a single confident match

This is an operational workflow feature, separate from the dashboard ingest/normalization pipeline.

### Customer Status

`customer_status` remains a CRM/GHL compatibility field.

Expected values include:
- `active`
- `past`
- `lead`

### Operational Activity

The normalized layer derives `is_operationally_active`.

Current rule:
- inactive => not operationally active
- lead and not inactive => operationally active
- active/customer => operationally active

Practically this currently behaves like:
- `is_operationally_active = NOT is_inactive`

### inactive_since

Current rule:
- active / operationally active => `inactive_since = NULL`
- first observed inactive => set `inactive_since`
- still inactive on later runs => preserve existing `inactive_since`
- reactivated => reset to `NULL`
- inactive again later => start a new inactive window

### Pruning

Normalized/dashboard scope keeps:
- operationally active customers
- recently inactive customers within the last 60 days

Normalized/dashboard scope excludes:
- customers inactive for more than 60 days

This pruning applies to the normalized/dashboard-facing layer only.

The `sk_*` source-ingest layer remains the broader historical source of truth.

### No-Pool Customers

Valid and supported:
- active customers without pools
- leads without pools
- quote/work-order/onboarding states without pools

These customers stay valid in normalized `customers`.

They are excluded only from pool-specific chemistry and trend logic until valid pool linkage exists.

### Pool-Required Records

Pool linkage is required for:
- chemistry readings
- chemical dose events
- filter pressure logic
- pool-specific trends

Missing pool linkage for those records is a fatal validation problem.

## What Is Live Today

### Live / Production-Relevant

- Sentinel webhook + SLA workflow
- Skimmer Google Drive download
- source-ingest import into `sk_*`
- Skimmer → GHL customer sync
- ingest worker normalization pipeline
- config-driven alert/trend/revenue rule tables
- derived analytics views for dashboard use

### Not Built Yet

- dashboard backend API layer
- dashboard frontend
- alert config UI
- reminder workflows on top of normalized/dashboard data
- AI query layer over normalized data

## Design Principles

- preserve current production compatibility first
- make the normalized layer stable and durable
- keep ingestion separate from decision-making
- prefer explicit rules over cleverness
- keep dashboard reads off the `sk_*` tables
- preserve auditable run tracking

## Near-Term Next Steps

1. Validate the full two-container flow on the droplet end-to-end.
2. Add a small set of read-only validation endpoints or SQL checks for normalized/dashboard data.
3. Build dashboard backend read APIs on top of normalized tables and derived views.
4. Build the first dashboard UI pages.

## Out Of Scope For Now

- replacing the existing customer sync source layer
- removing the `sk_*` compatibility layer
- public-facing UI
- full AI analytics interface

Generated / updated for repo cleanup on 2026-04-04.
