# Sentinel Roadmap

---

# Current State (v0.1.x complete)

Delivered:
- SMS + CALL issue lifecycle with `PENDING/OPEN/RESOLVED/SPAM`
- Business-hour SLA timing
- Deterministic resolver jobs (`poll_resolver`, `verify_pending`)
- Real-time one-time SLA breach alerts
- Manager summaries with resolved watermark logic
- False-positive controls (internal-thread suppression + ack closeout)
- Optional AI follow-up gate with fail-open safeguards
- Env-driven cron scheduling

---

# v0.2.x – Operational Hardening

Goal:
Increase reliability and observability under real-world traffic.

Planned:
- Modularize `main.py` into focused modules
- Add tests for parser/ack/verify edge cases
- Add retry/error-classification policy for GHL/API calls
- Improve operational dashboards/metrics
- Add explicit runbook checks for AI gate behavior

---

# v0.3.x – Advanced Automation

Goal:
AI-assisted decision logic.

Planned:
- Call transcript parsing
- Better SMS intent classification
- Auto-categorization/routing hints
- Priority scoring
- Suggested reply generation

---

# v1.0 – Production-Grade Automation Layer

Goal:
Fully integrated operational control layer.

Includes:
- Observability dashboard and alerting
- Structured event pipeline
- Hardened retry framework
- Formal error classification
- Versioned configuration
- Multi-location support

---

# Long-Term Vision

Sentinel becomes:

- The operational brain for NTX Pool Pros
- Deterministic, auditable automation
- A lightweight internal control system
- Expandable to billing + route intelligence
