# Web Backend Architecture Plan

This document defines the target architecture and migration plan for the dashboard/web backend.

If this document conflicts with older archived planning docs, this document wins.

It is subordinate only to `docs/Current State - Source of Truth.md` for broad platform rules.

## Purpose

The next product phase is not more ingest cleverness.

The next phase is:

- keep ingest focused on durable facts
- move business interpretation into a dedicated web backend
- expose stable APIs for a separate web frontend
- support configurable tracking and alert lifecycle without re-ingesting history

## Target Runtime

Target containers:

- `sentinel`
- `ingest-worker`
- `web-backend`
- `web-frontend`
- `caddy`

### Service Boundaries

#### Sentinel

Sentinel remains the communications watchdog and workflow orchestrator.

Responsibilities:

- GHL webhooks
- SMS/CALL SLA issue tracking
- manager summaries and escalation alerts
- Route Rollover workflows
- Skimmer download orchestration
- triggering ingest after successful Skimmer download
- optionally triggering dashboard refresh jobs after successful ingest

Sentinel must not become the long-term home for dashboard/business interpretation logic.

#### Ingest Worker

The ingest worker remains the fact pipeline.

Responsibilities:

- validate Skimmer SQLite snapshots
- import production-compatible `sk_*` source-ingest tables
- upsert normalized operational tables
- preserve historical chemistry and dosage events
- maintain `inactive_since`
- prune normalized scope for long-inactive customers
- record ingest run history

The ingest worker must store facts, not business meaning.

The ingest worker must not be the long-term owner of:

- dashboard alert lifecycle
- reminder lifecycle
- business taxonomy
- frontend-facing dashboard APIs
- configurable business interpretation

#### Web Backend

The web backend becomes the application interpretation layer for dashboard use.

Responsibilities:

- alert taxonomy
- rule config management
- rule evaluation orchestration
- tracked alert state
- reminder tracking
- dashboard/home summary APIs
- customer detail APIs
- technician detail APIs
- alert feed APIs
- reminder APIs
- configuration APIs for the future frontend
- AI query data access layer later

The web backend may use SQL views, materialized views, or typed query functions, but it owns the meaning of surfaced items.

#### Web Frontend

The web frontend is a separate UI container.

Responsibilities:

- fetch backend APIs
- display tables/cards/charts
- search/filter/sort
- submit user actions

The frontend must not own business interpretation logic.

## Core Architecture Rule

Repeat this until it becomes project law:

- ingest owns durable normalized facts
- backend owns configurable interpretation and tracking
- frontend owns presentation

## Data Ownership

### Ingest-Owned Facts

Ingest remains the owner of these durable layers:

- `skimmer_import_runs`
- `sk_*` source-ingest tables
- `customers`
- `pools`
- `chemistry_readings`
- `chemical_dose_events`
- `ingest_pipeline_runs`

Ingest also owns:

- `is_operationally_active`
- `inactive_since`
- pruning of normalized/dashboard-facing scope for long-inactive customers

These are fact and scope-management concerns, not dashboard interpretation concerns.

### Backend-Owned Interpretation

The web backend should own:

- alert rule configuration
- reminder configuration
- alert evaluation refresh runs
- surfaced alert instances
- surfaced reminder instances
- alert state transitions
- assignee/ack/snooze/resolve actions
- frontend-oriented summary/read models

## Alert Taxonomy

Every surfaced item should classify into one of these buckets.

### 1. Pool Alerts

Meaning:
- likely water, chemistry, equipment, or service conditions needing attention

Examples:
- repeated low sanitizer
- repeated high CYA
- rising filter pressure
- recurring chemistry instability

### 2. Process Alerts

Meaning:
- technician, checklist, workflow, or data-capture failures

Examples:
- missing PSI
- missing required reading
- inconsistent service logging
- incomplete process capture

These are not the same as pool-condition alerts and should not be mixed together in UI or workflow.

### 3. Revenue Opportunities

Meaning:
- patterns that suggest a valuable sales, retention, or service recommendation

Examples:
- repeated high PSI suggesting filter clean
- repeated high CYA suggesting drain/refill
- chemistry instability suggesting additional service
- reactivation opportunities for inactive customers

### 4. Reminders

Meaning:
- scheduled or human workflow follow-up items

Examples:
- re-check after repeated issue
- follow up after a flagged service pattern
- manual follow-up tasks created from alert review

## Rule Model Guidance

Do not build a giant abstract rule engine yet.

Start with:

- explicit SQL/views/query functions
- config-backed thresholds
- typed categories
- typed severity
- typed status/action fields

Good enough beats over-generic.

The first 10 to 15 real rules should shape the model.

## Backend Data Model

The web backend should introduce tracked state on top of normalized facts.

Recommended first-pass tables:

- `dashboard_rule_sets`
- `dashboard_rule_overrides`
- `alert_refresh_runs`
- `alert_instances`
- `alert_instance_events`
- `reminder_instances`
- `reminder_events`

The exact naming can change, but the concepts should remain.

### Alert Instance Concept

An alert instance is a tracked surfaced item derived from facts plus rules.

It should be stable across refreshes until the underlying condition clears.

Recommended core fields:

- `id`
- `category`
- `rule_code`
- `entity_type`
- `entity_id`
- `customer_id`
- `pool_id`
- `technician_id` when relevant
- `status`
- `severity`
- `title`
- `summary`
- `first_detected_at`
- `last_detected_at`
- `last_evaluated_at`
- `cleared_at`
- `assigned_to`
- `acknowledged_at`
- `snoozed_until`
- `metadata_json`

Recommended uniqueness concept:

- stable natural key based on `category + rule_code + entity_type + entity_id`

That lets the backend refresh detection logic without creating a new row every run.

### Alert Event Concept

Track workflow transitions separately.

Examples:

- detected
- severity_changed
- acknowledged
- snoozed
- unsnoozed
- resolved
- reopened
- cleared_by_refresh

This keeps the surfaced system auditable.

## Detection Strategy

The backend should own interpretation, but it does not need to evaluate everything in Python.

Recommended approach:

- keep set-based detection in PostgreSQL where useful
- use backend-owned SQL views or materialized views for candidate detections
- use backend service code to upsert tracked alert/reminder instances

That means:

- detection may stay SQL-heavy
- ownership and lifecycle move to the backend

This is the preferred middle path because it avoids both extremes:

- not leaving business meaning trapped in ingest
- not rewriting relational analytics into premature Python rule-engine code

## First API Surface

The first backend slice should support the frontend without putting business logic in the browser.

Recommended initial endpoints:

- `GET /api/home/summary`
- `GET /api/alerts`
- `GET /api/alerts/{id}`
- `POST /api/alerts/{id}/ack`
- `POST /api/alerts/{id}/resolve`
- `POST /api/alerts/{id}/snooze`
- `GET /api/customers`
- `GET /api/customers/{id}`
- `GET /api/technicians`
- `GET /api/technicians/{id}`
- `GET /api/reminders`
- `GET /api/config/alerts`
- `PATCH /api/config/alerts/{rule_code}`

Operational endpoints:

- `POST /jobs/dashboard/refresh`
- `GET /health`
- `GET /health/postgres`

## Read Model Guidance

The frontend should consume backend read models, not raw database shape.

Examples:

- summary cards
- grouped alert counts by category/severity
- customer profile with current alert history
- technician profile with process-signal summary
- reminder queue

Avoid leaking raw relational complexity directly into the UI contract.

## Migration Plan

### Phase 0. Freeze Design

Before moving code:

- keep this document aligned with `Current State - Source of Truth`
- treat service boundaries as stable
- treat taxonomy as stable unless a real operational need changes it

### Phase 1. Create the Web Backend Service

Deliverables:

- add `web-backend` container to `docker-compose.yml`
- add separate FastAPI entrypoint for the web backend
- add shared code boundaries for Postgres access and common domain helpers
- keep the repo unified; do not split into a separate repo yet

Exit condition:

- `web-backend` boots independently and can reach Postgres

### Phase 2. Move Ownership of Dashboard Schema

Deliverables:

- move dashboard rule/config schema bootstrap out of ingest ownership
- create backend-owned schema/bootstrap for dashboard interpretation objects
- stop treating ingest as the owner of alert/revenue dashboard logic

Notes:

- it is acceptable to keep existing views temporarily while ownership moves
- low-risk migration is preferred over a perfect rewrite

Exit condition:

- backend can initialize and own its interpretation schema without ingest doing it

### Phase 3. Introduce Tracked Alert State

Deliverables:

- add `alert_refresh_runs`
- add `alert_instances`
- add `alert_instance_events`
- backfill tracked instances from current derived detections

Exit condition:

- alert lifecycle is durable across refreshes
- user actions can target stable alert IDs instead of ephemeral query rows

### Phase 4. Move Refresh/Interpretation Out of Ingest

Deliverables:

- ingest ends after normalized facts are updated
- `sentinel` or a completion hook triggers backend refresh
- backend evaluates detections and upserts tracked state

Exit condition:

- changing rule/config changes interpretation without re-ingesting history

### Phase 5. Expose First Read APIs

Deliverables:

- home summary API
- alert list/detail APIs
- customer list/detail APIs
- technician list/detail APIs
- reminder list APIs

Exit condition:

- frontend can be scaffolded against stable backend endpoints

### Phase 6. Add Config Mutation

Deliverables:

- config read/write endpoints
- typed validation for supported rule edits
- audit trail for config changes

Exit condition:

- operations can tune thresholds and statuses without code edits

### Phase 7. Build the Frontend on Top

Initial frontend pages:

- Home
- Customers
- Technicians
- Alerts
- Reminders
- AI Query

The first frontend pass should stay intentionally thin:

- fetch data
- render data
- filter/search/sort
- avoid heavy browser-side business logic

## Definition Of Done For This Phase

This platform phase is successful when:

- ingest can run repeatedly without changing business meaning
- historical data can be reinterpreted by backend rule/config changes alone
- surfaced items classify cleanly as pool, process, revenue, or reminder
- frontend pages render from backend APIs without DB-specific logic in the browser
- documentation does not require searching multiple conflicting planning docs

## Current Repo Implications

The current codebase already suggests the needed split:

- `sentinel` is a live operational app and should stay focused
- `ingest-worker` is already a separate container and should stay fact-focused
- current dashboard config and derived views living in ingest are transitional, not the target end state

The right next move is not to make ingest smarter.

The right next move is:

- create the dedicated backend service
- move interpretation ownership there
- give the frontend a stable API contract
