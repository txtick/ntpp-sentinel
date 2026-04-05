# NTPP Platform Build Spec — V2.1 (Stabilized Implementation Version)
## Sentinel + Data Integration Pipeline + Dashboard

---

# 1. Purpose

This is the stabilized implementation version of the NTPP platform spec.

This version:
- fixes validation issues (lead vs pool logic)
- prevents pipeline drift
- aligns system behavior with real business workflow
- is safe for Codex / Claude / future iterations

---

# 2. Core Principle (DO NOT VIOLATE)

Pipeline defines reality.  
Dashboard consumes reality.  

---

# 3. Data Integration Pipeline (Finalized)

## 3.1 Input

- Source: Skimmer SQLite export
- Frequency: daily
- Trigger: cron or file detection

---

## 3.2 Pipeline Stages

### Stage 1 — Import Run

Insert into import_runs:
- started_at
- filename
- status = running

---

### Stage 2 — Raw Load

Load ALL data into raw tables:

- raw_sk_customer
- raw_sk_pool
- raw_sk_service_stop
- raw_sk_service_stop_entry
- raw_sk_entry_description

Rules:
- no filtering
- preserve source IDs
- attach import_run_id

---

### Stage 3 — Validation (FINALIZED RULES)

## Fatal Errors (FAIL PIPELINE)

- missing required tables
- missing required columns
- row count drop >30%
- service_stop_entry missing required relationships
- chemistry or service records missing pool_id (ONLY where required)
- joins that break operational integrity

### Stage 3 — Validation (FINALIZED RULES)

## Fatal Errors (FAIL PIPELINE)

- missing required tables
- missing required columns
- row count drop >30%
- service_stop_entry missing required relationships
- chemistry or service records missing pool_id (ONLY where required)
- joins that break operational integrity

---

## Non-Fatal Warnings (LOG ONLY)

Log warnings, but allow pipeline to continue.

### Valid No-Pool States (Expected Behavior)

- customer in `lead`, `prospect`, or `quoted` lifecycle stage with no pool
- quote-only customer without pool data
- customer early in onboarding (inspection not complete)

---

### Operational Data Gaps (Actionable Warnings)

- operationally active customer with no pool record
- customer with work orders, invoices, or service activity but no associated pool
- customer remains without pool data for extended period  
  (e.g. >30 days after first service activity)

---

### Recommended Handling

- log as `DATA_GAP_WARNING`
- DO NOT fail pipeline
- DO NOT block normalization
- may be surfaced in future dashboard reporting

---

## Pool Dependency Rules

### REQUIRE pool_id

- chemistry_readings
- chemical_dose_events
- filter pressure readings
- completed service stops tied to pool service

---

### DO NOT REQUIRE pool_id

- customers (lead/prospect/quoted)
- quotes
- invoices
- ghl contacts
- pre-service interactions
---

### Stage 4 — Normalization

Transform into operational tables:

#### Customers
- build full_name
- map lifecycle stage
- assign operational flag

#### Pools
- map to customer
- assign active/inactive

#### Chemistry
- filter entry_type = Reading
- map reading_type → parameter_name

#### Dosage
- map chemical usage
- calculate cost

---

### Stage 5 — Operational Filtering

Include ONLY if:

- active customer
OR
- unpaid invoice
OR
- service within last 90 days

Exclude:

- inactive >60 days with no activity or balance

---

### Stage 6 — Derived Views

Refresh:

- current_chemistry_alerts_v
- chemistry_trend_alerts_v
- revenue_opportunities_v
- dashboard_summary_v

---

### Stage 7 — Audit

Update import_runs:
- success
- counts
- duration

---

# 4. Lifecycle Model (FINALIZED)

## Source of Truth

Customer operational status is determined directly from Skimmer status fields.

---

## Operational Flag

`is_operationally_active` is derived as follows:

- TRUE if Skimmer marks the customer as `active`
- TRUE if Skimmer marks the customer as `lead` and the customer is **not** marked `inactive`
- FALSE if Skimmer marks the customer as `inactive`
- FALSE if Skimmer marks the customer as both `lead` and `inactive`

No additional lifecycle inference is performed.

---

## Notes

- A customer marked `lead` is still operationally active unless they are also marked `inactive`
- Customers may be operationally active without pool data
- Pool presence is NOT required for operational status

---

## System Behavior

### Alerts Engine

- ONLY evaluates pools with valid `pool_id`
- Customers without pools do NOT generate chemistry alerts

---

### Dashboard

- Includes all operationally active customers
- Excludes customers without pools from chemistry views
- May surface data gap warnings in future enhancements

---

## Summary

This approach ensures:

- alignment with Skimmer as the system of record
- support for Skimmer's real-world lead/active/inactive behavior
- simpler, more reliable pipeline behavior
- support for service-only customers and incomplete pool records

---

# 5. Schema (Stable Targets)

## customers

- id
- source_customer_id
- full_name
- address
- city
- phone
- ghl_contact_id
- lifecycle_stage
- is_operationally_active

---

## pools

- id
- source_pool_id
- customer_id
- is_operationally_active
- last_service_date

---

## chemistry_readings

- id
- pool_id
- service_date
- parameter_name
- value

---

## chemical_dose_events

- id
- pool_id
- service_date
- chemical_name
- amount
- unit_cost
- extended_cost

---

# 6. Alert Engine (Backend Only)

## Threshold Alerts

- FC > 10 → critical
- CYA > 80 → warning
- CYA > 100 → critical
- Phosphates > 500 → warning
- Phosphates > 1000 → critical
- pH > 7.8 → warning
- pH > 8.2 → critical

---

# 7. Trend Engine

- 3 of last 5 out of range → trend alert
- CYA +15 in 60 days → trend alert
- PSI rising  and > 18 → filter alert

---

# 8. Revenue Engine

## Drain & Refill
- CYA > 100 twice

## Filter Clean
- no PSI OR rising PSI

## Phosphate Treatment
- phosphates > 500

## High Chemical Cost
- > $25/month

---

# 9. Dashboard Contract (STRICT)

Dashboard reads ONLY:

- current_chemistry_alerts_v
- chemistry_trend_alerts_v
- revenue_opportunities_v
- dashboard_summary_v

NO business logic in UI.

---

# 10. Implementation Order (LOCKED)

1. import_runs
2. raw tables
3. normalization
4. filtering
5. derived views
6. pipeline test
7. dashboard

---

# 11. Final Summary

This system is:

- pipeline-driven
- lifecycle-aware
- revenue-focused
- operationally scoped

If something feels off in the dashboard:
→ the pipeline is wrong, not the UI

## Existing Production Data Compatibility

The existing `sk_*` PostgreSQL tables are already in production use for source ingestion and customer synchronization.

They must be treated as the current production source-ingest layer, not as disposable temporary staging.

### Rules

- Preserve backward compatibility with existing `sk_*` tables during pipeline expansion
- Do not break current customer sync logic that depends on `sk_customer`, `customer_status`, or `customer_identity_map`
- Build normalized operational tables alongside the existing `sk_*` tables
- Build dashboard and analytics features against normalized tables and derived views
- Migrate customer sync to normalized tables only as a separate, explicit step

### Status Handling

`customer_status` remains a CRM/GHL-facing compatibility field and may continue to use values such as:

- `active`
- `past`
- `lead`

Operational logic for the new platform must use a separate derived field:

- `is_operationally_active`

Derive `is_operationally_active` as follows:

- TRUE if Skimmer marks the customer as `active`
- TRUE if Skimmer marks the customer as `lead` and the customer is not `inactive`
- FALSE if Skimmer marks the customer as `inactive`
- FALSE if the customer is both `lead` and `inactive`

This prevents breaking the current sync model while allowing the dashboard and analytics pipeline to use correct operational logic.