# Domain Rules

This document captures business behavior that should remain stable unless intentionally changed.

## Source Of Truth By Context

- Skimmer: service operations, route assignments, work orders, customer operational tags
- GHL: customer communication, conversations, inbound/outbound follow-up state
- Skimmer API: live route/cleaning counts and other live route context
- Nightly Skimmer DB download: broad/fuller Skimmer snapshot, but stale for same-day changes until the nightly refresh
- Postgres: Sentinel's normalized analytics and alerting layer built from the nightly Skimmer import

## Payroll / Labor

- Payroll week is `Sunday -> Saturday`.
- For this app, `pool`, `stop`, and Skimmer `cleaning` are treated interchangeably for technician payroll.
- Current-week cleaning counts must follow the live Skimmer route API so the Labor page matches Skimmer's Labor report.
- Filter-clean counts/pay come from completed Skimmer work orders imported into Sentinel, assigned to the completing tech.
  - Normal evidence is the completed, non-deleted `WorkOrder` row and its assigned `AccountId`.
  - When Skimmer omits the completed instance from the exported `WorkOrder` table, Sentinel falls back to the matching `CustomerActivityLog` `WorkOrderFinished` event and its `CreatedBy` tech.
  - The two sources are deduplicated by Skimmer work-order id; an activity event is used only when its id has no valid completed, non-deleted work order in the payroll range. Stale recurring/template rows with old service dates or placeholder completion times do not suppress the fallback.
- Pay rules:
  - `$16` per stop/pool
  - `$25` per filter clean
  - first `40` stops/pools = regular Gusto hours
  - over `40` stops/pools = commission
  - filter-clean pay is rolled into the final `Commission` amount for Gusto entry
- Salary techs:
  - Jarrett Mundy
  - Jim Mundy

## Dashboard Alerts

- Dashboard alerts are refreshed/tracked by `web-backend`.
- A customer should not generate dashboard alerts when:
  - they have a route assignment ending within the next `30` days
  - they carry the Skimmer tag `no-sentinel-alerts`
- Current meaningful customer tags in repo logic:
  - `freedom`
  - `no-sentinel-alerts`
  - `filter-cart`
  - `filter-de`
  - `filter-sand`
  - `service-only`
  - `inspection`

## Dashboard Reports

- Dashboard active-customer and active-pool totals mean weekly maintained pool service:
  - customer is operationally active
  - pool has a current non-deleted route assignment
  - route assignment frequency is weekly or blank/unknown
  - customer is not tagged `service-only`
  - customer is not tagged `inspection`
- The dashboard home `Active Pools` card counts distinct weekly service locations/stops, not physical pool/spa/water-feature records.
- The dashboard home `Customer Flow` widget uses weekly route assignment lifecycle dates, not Sentinel `customers.created_at` / `inactive_since` import timestamps:
  - new customers = first weekly route assignment start date
  - lost customers = most recent ended weekly route assignment when the customer no longer has a current weekly assignment
  - historical assignments remain part of the lifecycle after Skimmer marks the assignment deleted or the customer inactive; only the current-assignment check requires a non-deleted assignment
- `service-only` customers are one-off repair/cleaning customers and should not count as weekly pools.
- `inspection` customers are quote/inspection prospects for weekly service and should not count until they become active weekly route customers.
- The dashboard `Problem Pools` page compares each pool's last-30-days chemical cost against its monthly service rate.
- `chemical_percent = monthly_chemical_cost / monthly_service_rate`, displayed as a percentage with 1 decimal place.
- Problem Pools flag thresholds:
  - `Healthy` when chemical percent is under `20%`
  - `Watch` when chemical percent is `20%` up to but not including `25%`
  - `Problem` when chemical percent is `25%` up to but not including `35%`
  - `Critical` when chemical percent is `35%` or higher
- Problem Pools monthly leak uses a `20%` target:
  - `monthly_leak = max(0, monthly_chemical_cost - (monthly_service_rate * 20%))`
- Pools with missing or zero service rate stay visible on the report as `Missing Rate`, show `N/A` for percent, and are excluded from Watch/Problem/Critical totals.

## Sales Assist

- Sales Assist quote status comes from imported Skimmer quote tables: `sk_quote`, `sk_quote_location`, and `sk_quote_item`.
- Standard Skimmer ingest must refresh quote tables so approved/rejected quotes leave the open quote pipeline after the next Skimmer DB sync.
- The quote-only import job is a manual recovery path, not the normal freshness mechanism.
- When a quote customer has a mapped `ghl_contact_id`, Sales Assist should open that contact in GHL instead of defaulting sales calls through the device dialer.
- Quote expiration dates/status should not affect Sales Assist priority because expiration dates are not reliable in current Skimmer quoting habits.

## Route Sandbox

- Skimmer/current imported route data is the only production source of truth for routes.
- Route Sandbox scenarios and manual update packets are Sentinel-only planning artifacts.
- Scenario editing must only write to `route_scenarios`, `route_scenario_assignments`, `route_change_plans`, `route_change_plan_items`, and technician route profile settings.
- Sentinel must not write route assignment or stop changes back to Skimmer.
- Approved scenarios generate a Manual Skimmer Update Packet for humans to apply in Skimmer.
- Manual checklist completion tracks human progress only; it must not mutate Skimmer/current route tables.
- A scenario is invalid if the same service location/pool appears in more than one route group.
- Technician home/start/end route settings are Sentinel-only and must not be pushed to Skimmer unless a future confirmed Skimmer field and explicit requirement exist.
- Technician start/end mileage should report stop-to-stop miles separately from start-to-first-stop, last-stop-to-end, total with start/end, and total without start/end.
- Start/end drive should not influence weighted route mileage unless explicitly enabled for that technician.
- Google Maps route estimates and optimization must be explicit user actions, never automatic page-load or drag/drop side effects.
- Google Maps optimization must remain disabled unless `GOOGLE_MAPS_ENABLE_OPTIMIZATION=true`; applying an optimization preview only reorders `route_scenario_assignments`.
- Route estimates default to traffic-unaware planning unless `GOOGLE_MAPS_TRAFFIC_MODE` is intentionally set to a traffic-aware mode.

## Filter Clean Logic

- Filter-clean opportunities are PSI/work-order based, not just raw time-since-last-clean.
- Existing filter-clean opportunities should be suppressed when:
  - a matching filter clean is already scheduled/upcoming
- Customers tagged `filter-sand` are excluded from filter-clean alerts for now.
- `filter_clean_missing_psi` is also suppressed when a recent completed filter clean exists in the last `90` days.
- `filter_clean_trend` is not suppressed by a recent completed filter clean alone; if PSI is still repeatedly high after a clean, the alert should still appear.
- Freedom-package customers are expected to have filter cleans pre-scheduled.
- A separate alert exists for `freedom` customers when a filter clean should be scheduled but is missing.
- Filter-clean alerts support a dashboard `Notify Customer` action.
  - It sends the customer an SMS through GHL saying they are due for a filter clean and that a quote will follow.
  - It creates or updates a dashboard reminder so the quote does not get forgotten.
  - The reminder auto-completes when Sentinel later detects a matching filter-clean quote via the live Skimmer quotes API.
- Open filter-clean quote reminders are also rechecked automatically in the background during business hours.

## Communication / SLA

- Only real employee outbound activity should count for issue auto-resolution.
- Automated workflow SMS must not count as employee outbound.
- GHL is the source of truth for communication state.
- AI gate behavior should be debugged, not replaced with endless deterministic rules, unless a specific deterministic rule is truly required.

## Customer Lifecycle

- Some customers remain financially active for a short time after operational service ends because of advance billing.
- Near-term route end dates matter more than billing presence for dashboard alert eligibility.

## What To Do When Logic Changes

- Update this file.
- Update `docs/DECISIONS.md`.
- Update `docs/KNOWN QUIRKS.md` too if the change was driven by a surprising system behavior or data quirk.
- Update the relevant operator docs if a command/workflow changed.
