# Sentinel Platform Architecture and Roadmap

## Purpose

This document captures the recommended architecture and rollout plan for evolving Sentinel from a GHL-focused workflow tool into an internal operational platform for North Texas Pool Pros.

The goal is to support:

* workflow automation
* customer and pool dashboards
* historical trend analysis
* internal AI-assisted business querying
* authenticated access for staff

---

## Strategic Direction

Sentinel should no longer be treated as a single-purpose missed-response tool.

It should evolve into:

**An internal operations platform that connects communication data, Skimmer operational data, dashboards, alerts, and AI-assisted querying.**

---

## Core System Responsibilities

### Skimmer Daily DB Export

Primary source of truth for:

* customers
* pools
* service locations
* service stops
* work orders
* routes and assignments
* invoices and payments
* quotes
* historical service data

### GoHighLevel (GHL)

Source of truth for:

* calls
* texts
* voicemails
* customer communication history
* contact records

### Sentinel

Responsible for:

* ingestion and orchestration
* correlation between GHL and Skimmer
* workflow automation
* reminders and summaries
* alert generation
* internal business logic

### Internal Web App

Responsible for:

* staff-facing dashboards
* customer and pool trend pages
* charts and alerts
* search and drill-down views
* AI chat entry point

### AI Query Layer

Responsible for:

* natural-language querying over operational data
* customer lookup assistance
* analytics questions
* maintenance and service-history questions

---

## Database Recommendation

### Recommendation

Move from SQLite to PostgreSQL before the internal dashboard and AI portal go live.

### Why

SQLite is fine for early single-node, low-concurrency workflow state, but the platform is growing into:

* multi-user access
* heavier reads
* time-series history
* dashboard queries
* AI-assisted querying
* business-critical reporting

### Preferred Option

**DigitalOcean Managed PostgreSQL**

Reasons:

* simpler operations than self-hosting
* backups and restore support
* better separation of app and database
* easier long-term scaling
* fits existing DigitalOcean footprint

### Temporary Alternative

Self-hosted PostgreSQL on the same server as a short-term bridge.

### Not Recommended

Continuing to scale SQLite as the primary platform database.

---

## Platform Architecture

### Recommended Service Split

#### 1. Sentinel App

* existing webhook handling
* issue tracking
* reminders and summaries
* rule evaluation
* orchestration jobs

#### 2. Ingest Worker

* downloads Skimmer DB export
* parses and normalizes data
* writes into PostgreSQL
* tracks imports and diffs

#### 3. Internal API

* exposes normalized business data
* supports dashboard and AI reads
* provides customer, pool, trend, and alert endpoints

#### 4. Internal Web App

* authenticated staff UI
* charts and customer detail pages
* AI chat interface

#### 5. AI Query Service

* natural-language interface
* uses tools/read-only query layer
* answers operational questions safely

### Deployment Recommendation

Use the current server for application containers for now, but keep the database as a managed external service.

Recommended layout:

* sentinel container
* ingest-worker container
* internal-api container
* internal-web container
* ai-query container
* managed PostgreSQL outside the droplet

---

## Data Model Direction

### Core Operational Entities

* customers
* service_locations
* pools
* service_stops
* work_orders
* quotes
* invoices
* payments
* route_assignments
* route_stops

### Historical Measurements / Facts

These enable trend analysis and charts.

Suggested fact tables:

* chemistry_readings
* psi_readings
* chemical_dose_events
* maintenance_events
* service_event_facts
* alert_facts

### Example Use Cases Enabled

* filter PSI trends over time
* chlorine usage by customer
* calcium rise monitoring
* pH stability trends
* filter-clean interval analysis
* repeated chemistry anomalies

---

## Alerting Model

Build an alert engine instead of only showing raw trends.

Example alert types:

* calcium above threshold
* PSI rising unusually fast
* filter likely needs cleaning
* unusual chlorine usage
* repeated low chlorine readings
* unstable pH across visits

Suggested alert storage:

* alert_type
* severity
* customer_id
* pool_id
* triggered_at
* resolved_at
* explanation_json

Sentinel can surface these through summaries, dashboards, and AI responses.

---

## AI Query Architecture

### Recommended Pattern

Do not begin with unrestricted model-generated SQL against production data.

Use:
**User -> Web App Chat UI -> AI Service -> Tool Layer / Read-Only Query Layer -> PostgreSQL**

### Tool Examples

* customer lookup
* maintenance history lookup
* trend summary lookup
* top-N operational metrics
* alert summary lookup

### Example Questions

* What 5 customers have the highest chlorine usage?
* When was the last time we cleaned Susie Smith’s filter?
* Which pools are trending high on calcium?
* Which customers have rising PSI and likely need a filter clean?

### Future State

Once the schema is stable, AI-generated read-only SQL may be allowed against a constrained read-only role or analytics replica.

---

## Authentication Recommendation

### Preferred Method

Authenticate internal users with Google Workspace using Google OpenID Connect.

### Recommended Controls

* Sign in with Google
* Validate ID token on backend
* Restrict access to `northtexaspoolpros.com`
* Store internal user record keyed to Google subject ID
* Use app-managed session cookies

### Benefits

* no separate password management
* easy internal-only access control
* aligns with company email identities

---

## How This Rolls Into Sentinel

Sentinel should remain the business workflow and orchestration engine.

It should not become a single giant monolith that directly handles every UI, chart, and AI concern.

Recommended role for Sentinel:

* ingestion scheduling
* rule execution
* reminder generation
* alert creation
* correlation of GHL and Skimmer
* operational summaries

Other services should consume the normalized data Sentinel helps produce.

---

## Recommended Rollout Plan

### Phase 1: Foundation

* stand up PostgreSQL
* build Skimmer import pipeline into PostgreSQL
* keep Sentinel operational
* begin moving normalized data model into PostgreSQL

### Phase 2: Internal Data Access

* add internal API
* expose customer, pool, stop, work-order, and alert endpoints
* build first customer detail views

### Phase 3: Dashboard Experience

* add internal web app
* implement Google Workspace auth
* build charts for chemistry, PSI, chlorine usage, and maintenance history

### Phase 4: AI-Assisted Querying

* add AI query service
* expose constrained read-only tools
* support operational natural-language questions

### Phase 5: Full Platform Integration

* blend Sentinel alerts, dashboard insights, and AI answers
* add more predictive or recommendation features
* expand operational decision support

---

## First Practical Deliverables

Recommended first deliverables after architecture approval:

1. PostgreSQL decision and setup
2. import pipeline design
3. normalized schema definition
4. Google auth design
5. first dashboard wireframe
6. first AI tool definitions

---

## Summary

The next evolution is not just “more Sentinel features.”

It is the creation of a small internal platform with:

* Postgres-backed operational data
* Sentinel orchestration
* dashboard visibility
* historical pool analytics
* AI-assisted internal search and questions

This creates a path from reactive follow-up automation to proactive business intelligence and operational assistance.
