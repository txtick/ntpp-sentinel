# Sentinel + Skimmer Integration Plan

## Overview

Sentinel is evolving from a GHL-focused issue tracker into an operational intelligence platform for NTPP. With access to the full Skimmer daily database export, Sentinel can now use real business data as its primary source of truth.

This document outlines the recommended architecture, schema, and phased implementation plan.

---

## Core System Roles

### Skimmer (Source of Truth)

* Customers
* Pools / Service Locations
* Service Stops & History
* Work Orders
* Routes & Assignments
* Invoices / Payments
* Quotes

### GoHighLevel (GHL)

* Calls, SMS, Voicemails
* Customer communication history
* Contact records

### Sentinel

* Correlation engine
* Workflow engine
* Reminder system
* Operational intelligence layer

---

## Key Architectural Decision

**The Skimmer daily DB export is the primary data backbone.**

* Use DB export for completeness and reliability
* Use API only for small real-time gaps (optional)
* Do NOT rely on API as primary source

---

## Data Architecture

### 1. Import Layer

Create a tracking system for each Skimmer import.

**Table: skimmer_import_runs**

* id
* imported_at
* source_filename
* db_path
* success
* error_message
* table_counts_json
* snapshot_date (if available)

Purpose:

* Detect failures
* Ensure freshness
* Enable auditing

---

### 2. Normalized Data Layer

Do NOT query the raw Skimmer DB directly for all logic.

Instead, ingest into Sentinel-controlled tables:

**Core Tables:**

* sk_customer
* sk_service_location
* sk_pool
* sk_work_order
* sk_quote
* sk_invoice
* sk_payment
* sk_service_stop

**Optional (later):**

* sk_route_assignment
* sk_route_stop
* sk_service_stop_entry

Each table should include:

* skimmer_id
* business fields
* CreatedAt
* UpdatedAt
* Deleted
* Version
* last_import_run_id

---

### 3. Identity Mapping Layer

**Critical component**

**Table: customer_identity_map**

* id
* skimmer_customer_id
* skimmer_service_location_id
* ghl_contact_id
* matched_by
* confidence
* is_manual_override
* notes
* created_at
* updated_at

**Matching Priority:**

1. Manual mapping
2. Phone
3. Email
4. Address
5. Name + address
6. Conflict queue

---

### 4. Decision Layer

This is where Sentinel becomes valuable.

* Issue tracking (existing)
* Customer classification
* Promise tracking
* Operational alerts
* Manager summaries

---

## Recommended Workflows

### 1. Customer Classification (High Priority)

Determine status:

* Active Customer (exists in Skimmer)
* Past Customer (historical only)
* Lead (GHL only)

This improves:

* Marketing
* Manager awareness
* Routing decisions

---

### 2. Promise Tracking

Detect commitments such as:

* “I’ll send a quote”
* “We’ll schedule you”
* “I’ll follow up”

Then verify:

* Quote created?
* Work order created?
* Service scheduled?

---

### 3. Enriched Alerts & Summaries

Enhance GHL alerts with Skimmer data:

Include:

* Customer status
* Service location
* Last service date
* Open work orders

---

### 4. Repair Workflow Monitoring

Detect:

* No quote after inquiry
* Quote without work order
* Work order without follow-up
* Completed work without customer notification

---

### 5. Billing / Collections (Later Phase)

Detect:

* Overdue invoices
* Missing payments
* At-risk customers

---

## Implementation Phases

### Phase 1: Data Plumbing

* Import tracking
* DB ingestion
* Core normalized tables
* Identity mapping

### Phase 2: Customer Awareness

* Active vs past vs lead
* Enrich GHL issues with Skimmer context

### Phase 3: Operational Workflows

* Promise tracking
* Repair workflows
* Service visibility

### Phase 4: Business Intelligence

* Billing alerts
* Route optimization insights
* Profitability analysis

---

## What NOT to Do

* Do not use Skimmer DB as runtime DB
* Do not ingest all 45 tables immediately
* Do not rely on AI before deterministic joins exist

---

## First Feature Recommendation

When a GHL event occurs, Sentinel should immediately know:

* Is this an active customer?
* What service location?
* Last service stop?
* Any open work orders?

This single feature unlocks most future value.

---

## Summary

Sentinel is no longer just a follow-up system.

It should become:

**An operational intelligence platform that connects communication (GHL) with execution (Skimmer).**

This is the foundation for automation, decision support, and eventually full business orchestration.
