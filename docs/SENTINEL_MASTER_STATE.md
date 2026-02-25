# Sentinel Master State

**Project:** Sentinel (NTX Pool Pros)  
**Status:** Active development  
**Environment:** Production + Local Dev  
**Primary Repo:** https://github.com/txtick/ntpp-sentinel  

---

# 1) Purpose

Sentinel is an automation and orchestration service for North Texas Pool Pros.

It ingests events (primarily from GoHighLevel webhooks) and produces:

- Issue tracking (SMS + Calls)  
- SLA monitoring  
- Escalation detection  
- Manager summary notifications (scheduled)  
- Resolution detection via polling  

Sentinel acts as a lightweight operational layer on top of GoHighLevel.

---

# 2) High-Level Architecture

## Inbound Events
- `POST /webhook/ghl`
- `POST /webhook/ghl/unanswered_call`

## Internal Jobs
- `POST /jobs/poll_resolver`
- `POST /jobs/send_summary`

## Storage
- SQLite database
  - `issues`
  - `kv_store`
  - `raw_events`

## Outbound
- GHL API → `POST /conversations/messages`

---

# 3) Core Concepts

## Issues

An Issue is created when:
- An inbound customer SMS is received  
- An unanswered call webhook is received  

Each issue contains:
- `id`
- `type` (SMS / CALL)
- `contact_id`
- `conversation_id`
- `state` (OPEN / RESOLVED)
- `created_ts`
- `due_ts`
- `resolved_ts`

---

## SLA Logic

Each issue receives a `due_ts` when created.

If:

    now > due_ts AND state = OPEN

It becomes eligible for escalation and summary reporting.

---

## Resolution Logic

The `poll_resolver` job:

1. Queries GHL conversations  
2. Detects outbound replies from managers  
3. Marks issue as `RESOLVED`  
4. Sets `resolved_ts`  

---

# 4) Environment Variables

File location:

    /opt/ntpp-sentinel/.env

Required:

    GHL_TOKEN=your_api_token
    WEBHOOK_SECRET=your_internal_secret
    GHL_VERSION=2021-07-28
    MANAGER_CONTACT_IDS=contact1,contact2,contact3

Optional:

    GHL_LOCATION_ID=your_location_id

---

# 5) API Endpoints

## Inbound Webhook

    POST /webhook/ghl

Used for inbound SMS events.

---

## Unanswered Call Webhook

    POST /webhook/ghl/unanswered_call

Creates CALL-type issues.

---

## Poll Resolver

    POST /jobs/poll_resolver
    Header: X-NTPP-Secret: <WEBHOOK_SECRET>

Scans GHL conversations and resolves issues.

---

## Send Summary

    POST /jobs/send_summary?slot=morning|midday|afternoon&dry_run=1|0
    Header: X-NTPP-Secret: <WEBHOOK_SECRET>

Slots:
- morning
- midday
- afternoon

Behavior:

`dry_run=1`
- Generates summary
- Returns JSON
- Does NOT send SMS
- Does NOT advance watermark

Real run:
- Sends SMS to managers
- Updates watermark

---

# 6) Manager Summary Logic

Each summary contains:

- Calls (open + overdue)
- Texts (open + overdue)
- Escalated (>24 business hours if configured)
- Resolved since last summary

Watermarks stored in `kv_store`:

- `last_summary_ts_morning`
- `last_summary_ts_midday`
- `last_summary_ts_afternoon`

---

# 7) Local Development Workflow

Clone repo:

    git clone git@github.com:txtick/ntpp-sentinel.git
    cd ntpp-sentinel

Create branch:

    git checkout -b dev

Commit changes:

    git add -A
    git commit -m "Describe change"
    git push -u origin dev

Merge to main when ready:

    git checkout main
    git merge dev
    git push origin main

---

# 8) Production Deployment Workflow

SSH into droplet:

    ssh sentinel

Pull latest code:

    cd /opt/ntpp-sentinel
    git fetch --all --tags
    git checkout main
    git pull --ff-only

Restart service (Docker):

    docker compose down
    docker compose up -d --build

View logs:

    docker compose logs -f --tail=100

If using systemd:

    sudo systemctl restart ntpp-sentinel
    sudo systemctl status ntpp-sentinel --no-pager
    journalctl -u ntpp-sentinel -n 200 --no-pager

---

# 9) Cron Schedule (Recommended v1)

8:00 AM

    send_summary?slot=morning

11:00 AM

    send_summary?slot=midday

3:00 PM

    send_summary?slot=afternoon

Optional:
Have `send_summary` internally call `poll_resolver` to prevent double cron entries.

---

# 10) Smoke Test Checklist

Inbound SMS:
- Customer texts → Issue created

Resolver:
- Manager replies in GHL
- Run `poll_resolver`
- Issue becomes RESOLVED

Summary dry run:
- Before `due_ts` → not listed
- After `due_ts` → listed

Summary real:
- SMS delivered to managers

Resolved tracking:
- Run real summary once
- Resolve issue
- Run again
- Issue appears under "Resolved since last summary"

---

# 11) Force Issue to Overdue (Testing Only)

If `due_ts` is epoch:

    sqlite3 /opt/ntpp-sentinel/data/sentinel.db "UPDATE issues SET due_ts = strftime('%s','now') - 60 WHERE id = 1;"

If `due_ts` is datetime:

    sqlite3 /opt/ntpp-sentinel/data/sentinel.db "UPDATE issues SET due_ts = datetime('now','-2 minutes') WHERE id = 1;"

Inspect schema:

    sqlite3 /opt/ntpp-sentinel/data/sentinel.db "PRAGMA table_info(issues);"

---

# 12) Versioning

Tag release:

    git tag v0.1.0
    git push origin refs/tags/v0.1.0

Push all tags:

    git push origin --tags

---

# 13) Current Version Goals

## v0.1.x
- Stable issue creation
- Stable resolution polling
- Working manager summaries
- Cron automation

## v0.2.x (planned)
- Business-hour escalation logic
- Per-manager routing
- Call transcript analysis
- Structured logging
- Better summary formatting

---

# 14) Operational Philosophy

Sentinel must:

- Be deterministic  
- Be observable  
- Be testable via curl  
- Avoid hidden background behavior  
- Prefer idempotent jobs  
- Keep business logic explicit  