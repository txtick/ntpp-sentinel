# Decisions Log

Short record of decisions that were made intentionally so they do not get re-litigated after compaction.

## Update Rule

Add an entry here whenever:

- a meaningful business rule changes
- a source-of-truth decision is made
- a suppression/eligibility rule changes
- a workflow is intentionally changed to match real operations
- a bug fix reflects a product decision rather than a one-off patch

Format:

- what changed
- why it was chosen

Keep entries short, but do not skip them.

## Labor / Payroll

- Current-week Labor cleanings use the live Skimmer route API, not the nightly SQLite DB and not normalized Postgres stop counts.
  - Reason: this is what matched Skimmer's Labor report exactly.
- Labor page treats stops/cleanings/pools interchangeably for payroll.
  - Reason: this matches how payroll is actually entered.
- Filter-clean pay is rolled into the final `Commission` amount shown for Gusto.
  - Reason: user wants one commission number to enter instead of separate overtime-like entries.
- Payroll week is `Sunday -> Saturday`.

## AI / SLA

- Added `recheck_issue` as a real recovery/debugging tool for false positives.
  - Reason: helps validate prompt changes and recover stale/bad AI decisions without inventing more deterministic rules.
- AI false positives should be debugged at the AI-gate/input/prompt layer first, not patched with endless deterministic suppressions.
- Manager summaries should include dashboard reminder pressure.
  - Reason: operational follow-up like quote work should ride the same manager reminder cadence so it does not get forgotten.

## Dashboard Alerts

- Customers with a route assignment ending within the next `30` days are suppressed from dashboard alerts.
  - Reason: they are operationally leaving and do not need new maintenance/revenue alerts.
- Customers tagged `no-sentinel-alerts` in Skimmer are suppressed from dashboard alerts.
  - Reason: gives operators a durable business-controlled opt-out.
- Filter-clean alerts suppress when the clean is already scheduled/upcoming.
  - Reason: do not alert for work already planned.
- `freedom` customers can generate a separate missing-scheduled-filter-clean alert.
  - Reason: those customers are expected to have cleans pre-scheduled.
- Filter-clean alerts use `Notify Customer` plus a dashboard reminder instead of trying to create Skimmer quotes directly.
  - Reason: the public Skimmer API appears read-only for quotes, so the reliable workflow is notify + reminder + quote detection.
- Filter-clean quote reminders auto-complete when a matching live Skimmer quote is detected.
  - Reason: once the quote exists, the human follow-up debt is gone and should drop out of reminder pressure automatically.

## Data Source Decisions

- GHL is authoritative for communication history.
- Skimmer is authoritative for service operations.
- Skimmer API is the preferred live source when current-week route counts matter.
- Nightly Skimmer DB is preferred when the API does not include the needed data.
- Normalized Postgres is Sentinel's working analytics model, not the upstream source of truth.
