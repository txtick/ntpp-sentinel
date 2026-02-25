Sentinel Master State

Project: Sentinel (North Texas Pool Pros)
Status: Active Development
Environment: Production (DigitalOcean Droplet)
Public Base URL: https://sentinel.northtexaspoolpros.com
Version: v0.1.x
Last Updated: 2026-02-25

⸻

1. Purpose

Sentinel is an automation and orchestration service that:
	•	Ingests events (primarily from GoHighLevel webhooks)
	•	Creates and tracks actionable “issues”
	•	Applies business rules and SLA logic
	•	Notifies managers via SMS summaries
	•	(Future) Orchestrates intelligent customer communication

Sentinel is the decision engine between:
	•	GoHighLevel (CRM & communications)
	•	Skimmer (operations & scheduling)
	•	Managers (internal SMS summaries)
	•	Customers (future intelligent notifications)

⸻

2. Current Production Capabilities (v0.1.x)

2.1 Inbound SMS → Issue Creation

Trigger:
	•	POST /webhook/ghl

Behavior:
	•	Customer inbound SMS creates an SMS_OPEN issue
	•	Issue includes:
	•	contact_id
	•	conversation_id
	•	opened_ts
	•	due_ts (based on SLA rules)
	•	status = OPEN

⸻

2.2 Poll Resolver

Endpoint:
	•	POST /jobs/poll_resolver

Behavior:
	•	Scans OPEN issues
	•	If outbound human message detected in conversation after opened_ts:
	•	status → RESOLVED
	•	resolved_ts recorded

Resolver does NOT currently distinguish:
	•	Human outbound
	•	Automated outbound

(Call logic refinement required — see Section 6)

⸻

2.3 Manager SMS Summaries

Endpoint:
	•	POST /jobs/send_summary?slot=morning|midday|afternoon&dry_run=1|0

Slots:
	•	Morning (8:00)
	•	Midday (11:00)
	•	Afternoon (15:00)

Behavior:
	•	Summarizes:
	•	Open SMS issues
	•	Escalated issues (past SLA)
	•	Resolved since last summary
	•	Sends SMS to all MANAGER_CONTACT_IDS
	•	Uses GHL endpoint:
	•	POST /conversations/messages
	•	type=“SMS”
	•	message=””
	•	contactId
	•	conversationId

Watermarking:
	•	last_summary_ts_{slot} stored in kv_store

Dry Run Mode:
	•	Generates output
	•	Does NOT send SMS
	•	Does NOT advance watermark

⸻

3. Issue Types (Current)
	•	SMS_OPEN
	•	CALL_MISSED (basic)
	•	(Future) JOB_SCHEDULED
	•	(Future) REPAIR_SCHEDULED

Core States:
	•	OPEN
	•	RESOLVED

Future States (Planned):
	•	AUTO_ACK
	•	CONTACTED
	•	ESCALATED
	•	CLOSED

⸻

4. SLA & Escalation Logic

Each issue has:
	•	opened_ts
	•	due_ts

If current_time > due_ts:
	•	Issue considered escalated
	•	Displayed in summary under Escalated section

Business-hour awareness may be added in future versions.

⸻

5. Missed Call Tracking (Current + Known Limitation)

Current Behavior
	•	Missed call webhook creates CALL_MISSED issue.
	•	Resolver treats any outbound message as resolution.

Problem:
	•	GHL IVR auto-sends missed-call SMS.
	•	This may falsely resolve CALL_MISSED issues.

⸻

6. Planned: Replace IVR Auto-SMS with Sentinel Decisioning

Objective

Disable GHL auto-missed-call SMS and move logic into Sentinel.

Rationale

Current auto-reply:
	•	Creates false resolution signals.
	•	Lacks context awareness.
	•	Causes occasional customer confusion.

⸻

Target Architecture (V1 – Rules-Based)

Flow:
	1.	Missed call received.
	2.	Sentinel creates CALL_MISSED issue.
	3.	Sentinel waits 3–5 minutes.
	4.	If no human follow-up:
	•	Send controlled missed-call SMS.
	5.	If human outbound occurs:
	•	Mark CONTACTED.

⸻

Decision Conditions (V1)

Do NOT auto-reply if:
	•	Repeat caller within 15 minutes
	•	Human response within 3–5 minutes
	•	Call returned

Auto-reply if:
	•	After hours
	•	No human follow-up within threshold
	•	New/unknown caller

⸻

Future V2 – AI-Assisted

If voicemail transcription available:

Sentinel will:
	•	Classify intent
	•	Select template
	•	Adjust tone
	•	Gate by confidence score

Low confidence:
	•	Include suggested reply in manager summary instead of auto-send.

⸻

Guardrail

If no human response within X minutes (e.g., 10):
	•	System must send fallback acknowledgment.

⸻

7. Future Feature: Customer SMS for Scheduled Jobs & Repairs

Current State
	•	Skimmer sends email only.
	•	Customers may miss email.
	•	No SMS confirmation currently sent.

⸻

Goal

When a job/repair is scheduled:
	•	Send SMS confirmation in addition to email.

⸻

Example SMS Templates

Repair Scheduled:

“Hi [First Name], your pool repair is scheduled for [Date]. If you have questions, reply here or call 833-689-7665. – North Texas Pool Pros”

Install Scheduled:

“Your pool service appointment is confirmed for [Date]. We’ll notify you if anything changes. Thank you!”

⸻

Future Enhancements
	•	24-hour reminder SMS
	•	Morning-of reminder
	•	Technician name personalization
	•	DND compliance checks
	•	Duplicate suppression if job edited

⸻

Required Fields (Future Issue Types)

JOB_SCHEDULED / REPAIR_SCHEDULED:
	•	contact_id
	•	scheduled_date
	•	notification_sent_ts
	•	reminder_sent_ts
	•	source (Skimmer / webhook / polling)

⸻

8. Deployment Model

Server Path:
/opt/ntpp-sentinel

Deployment Workflow:
	1.	Develop locally.
	2.	Push to GitHub.
	3.	SSH into droplet.
	4.	git pull.
	5.	Restart service (docker compose or systemd).

Environment Variables:
	•	GHL_TOKEN
	•	WEBHOOK_SECRET
	•	GHL_VERSION=2021-07-28
	•	MANAGER_CONTACT_IDS

⸻

9. Known Technical Debt
	•	Missed call auto-reply filtering not yet implemented.
	•	No differentiation between human vs automated outbound.
	•	No business-hours-aware SLA logic.
	•	No AI integration yet.
	•	No Skimmer integration yet.

⸻

10. Long-Term Vision

Sentinel evolves from:

Alert Tracker

To:

Operational Intelligence Layer

Capabilities roadmap:
	•	Context-aware auto replies
	•	Intelligent routing
	•	AI-generated draft responses
	•	Skimmer integration
	•	Customer notification orchestration
	•	Business-hours SLA engine
	•	Revenue-protection monitoring
	•	Technician accountability metrics

⸻

11. Strategic Direction

Sentinel will become the centralized logic layer that:
	•	Prevents dropped leads
	•	Prevents missed service issues
	•	Reduces manager cognitive load
	•	Improves customer communication consistency
	•	Enables AI-assisted operations

⸻

If you’d like next, I can:
	•	Generate a version-controlled CHANGELOG.md
	•	Create a DEPLOYMENT.md
	•	Or draft a simple ARCHITECTURE.md diagram spec for the repo
