# NTPP Platform Build Spec  
## Sentinel + Data Integration Pipeline + Dashboard

## 1. Purpose

This document defines the technical build approach for evolving Sentinel into a broader internal operations platform for North Texas Pool Pros.

The immediate goal is to deliver the dashboard quickly, but to do so on top of a stable ingestion and normalization layer so the dashboard does not define the backend by accident.

The platform must support:

- workflow automation  
- customer and pool dashboards  
- chemical trend analysis  
- revenue opportunity detection  
- internal AI-assisted querying  
- authenticated internal access  

---

## 2. Strategic Direction

Sentinel should not remain a single-purpose missed-response system. It should become the orchestration and rules engine in a broader internal platform.

That platform consists of:

- Skimmer as source of truth for pool operations and service data  
- GHL as source of truth for communication data  
- Sentinel as orchestration, alerting, and business-rule engine  
- PostgreSQL as the normalized operational and analytics store  
- Dashboard as the staff-facing read layer  
- AI Query Layer as the internal natural-language access layer  

---

## 3. Build Order

### Phase 1 — Data Integration Pipeline  
### Phase 2 — Derived Business Logic  
### Phase 3 — Dashboard  
### Phase 4 — AI Query Layer  

---

## 4. Core Architecture

### 4.1 Source Systems

#### Skimmer
Primary source of truth for:
- customers  
- pools  
- service stops  
- chemical readings  
- dosage events  

#### GoHighLevel
Primary source of truth for:
- calls  
- texts  
- voicemails  

#### Sentinel
Responsible for:
- orchestration  
- alert generation  
- opportunity generation  

---

## 5. Key Design Rule: Pipeline First

The integration pipeline is the contract.

The dashboard must not query whatever happens to be in Postgres.

---

## 6. Postgres Data Layering Model

### 6.1 Raw Import Layer
- raw_sk_customer  
- raw_sk_pool  
- raw_sk_service_stop  

### 6.2 Normalized Operational Layer
- customers  
- pools  
- chemistry_readings  
- chemical_dose_events  

### 6.3 Derived Analytics Layer
- current_chemistry_alerts_v  
- chemistry_trend_alerts_v  
- revenue_opportunities_v  

---

## 7. Scope Control

Only include operationally relevant data:
- active customers  
- active pools  
- recent activity  

Exclude:
- long inactive customers  
- stale pools  

---

## 8. Pipeline Stages

1. File discovery  
2. Raw load  
3. Validation  
4. Normalization  
5. Operational filtering  
6. Derived refresh  
7. Audit  

---

## 9. Derived Models

### Alerts
- threshold-based  
- trend-based  

### Revenue Opportunities
- drain & refill  
- filter clean  
- phosphate treatment  

---

## 10. Dashboard Model

Dashboard reads ONLY:
- normalized tables  
- derived views  

No business logic in UI.

---

## 11. Summary

Pipeline defines the system.  
Dashboard consumes the system.  

Build the pipeline first to avoid drift.
