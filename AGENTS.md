# AGENTS.md

Read this file first at the start of a new chat.

After any compaction or in any fresh chat, reread this file and the linked core docs before making changes or proposing new logic.

This repository runs North Texas Pool Pros' internal Sentinel platform. This file is the fastest way to recover the business rules and operating assumptions that are easy to lose after compaction.

## What This Repo Owns

- `sentinel`: public API, GHL webhooks, SLA issue tracking, manager summaries, escalations, customer sync, Skimmer DB download trigger, Route Rollover, AI recheck jobs
- `ingest-worker`: Skimmer SQLite validation/import, normalized Postgres upserts, dashboard refresh trigger after successful ingest
- `web-backend`: dashboard APIs, alert tracking/refresh, reminders, labor/payroll view
- `web-frontend`: operator UI

## Product Rules That Matter

- Skimmer is the source of truth for most operational/service data.
- GHL is the source of truth for customer communication and conversation history.
- Skimmer API is the live source for route/cleaning counts, but it does not expose everything.
- Nightly Skimmer SQLite download is the full snapshot from Skimmer, but it can lag same-day changes until the nightly refresh.
- Postgres is Sentinel's normalized/internal analytics layer built from the nightly Skimmer DB import.

## Do Not Relearn These The Hard Way

- Current-week Labor cleanings must come from the live Skimmer route API, not the nightly SQLite snapshot and not normalized Postgres route-stop counts.
- Labor "pools" and Skimmer "cleanings/stops" are treated interchangeably for payroll in this app.
- Payroll week is `Sunday -> Saturday`.
- Gusto entry logic:
  - first `40` pools/stops = regular hours
  - pools/stops over `40` = commission
  - filter-clean pay is rolled into the final `Commission` dollar amount
  - Jarrett and Jim are salary techs; there are no other payroll exceptions today
- The nightly Skimmer DB download happens around `11:00pm`, so same-day service counts may differ from the DB-backed data until then.

## Alert Rules You Must Preserve

- Dashboard alerts are backend-owned and require `web-backend` refresh to clear/update.
- Customers with a route assignment `end_date` within the next `30` days should not generate dashboard alerts.
- Customers tagged `no-sentinel-alerts` in Skimmer should not generate dashboard alerts after import + refresh.
- `freedom` is a meaningful customer tag today.
- Filter-clean alerts must be suppressed when a matching filter clean is already scheduled/upcoming.
- Skimmer sometimes stores fake `complete_time` values like `2010-01-01`; treat those as not actually completed.
- Filter-clean alert follow-up currently uses `Notify Customer` + dashboard reminder + quote detection; do not assume Sentinel can create Skimmer quotes through the public API.

## Communication / SLA Rules

- Missed-call and missed-text issue tracking lives in Sentinel, not the dashboard.
- Only real employee outbound activity should auto-resolve SLA issues.
- Automated SMS/workflow messages must not count as employee outbound.
- AI gate decisions are conversation-scoped and rely on what message content Sentinel can extract from the conversation/transcript.
- `recheck_issue` exists to force a fresh AI decision for an already-open issue/conversation.

## Tags Currently Known To Matter

- `freedom`
- `no-sentinel-alerts`

If you discover more tag-driven behavior, document it in `docs/DOMAIN RULES.md` and `docs/DECISIONS.md`.

## Documentation Discipline

When you make a meaningful change, update the docs in the same pass.

Use this rule of thumb:

- Business rule changed:
  - update `docs/DOMAIN RULES.md`
- Intentional product/logic decision made:
  - update `docs/DECISIONS.md`
- Weird system behavior, placeholder values, container gotcha, query trap, or data-source mismatch discovered:
  - update `docs/KNOWN QUIRKS.md`
- Operator workflow, refresh command, recovery step, or troubleshooting command changed:
  - update `docs/START UP & RECOVERY.md`
  - and add/update the copy-paste version in `docs/OPERATOR CHEATSHEET.md`
- Manager-visible behavior changed:
  - update `docs/Sentinel - Manager Guide.md`
- A new tag starts affecting logic:
  - update this file
  - update `docs/DOMAIN RULES.md`
  - update `docs/DECISIONS.md`

Do not leave meaningful decisions only in chat history.
If a future chat would be likely to ask "why does it work this way?", that belongs in repo docs.

## Operator Commands Worth Remembering

See:

- [docs/START UP & RECOVERY.md](docs/START%20UP%20%26%20RECOVERY.md)
- [docs/OPERATOR CHEATSHEET.md](docs/OPERATOR%20CHEATSHEET.md)

## If You Are Unsure

- Prefer reading `docs/Current State - Source of Truth.md` first for architecture.
- Prefer reading `docs/DOMAIN RULES.md` for business behavior.
- Prefer reading `docs/KNOWN QUIRKS.md` before changing suppression, labor, or Skimmer-derived logic.
- Do not "simplify" behavior that was added to match Skimmer/GHL unless the docs are updated with a deliberate replacement decision.
