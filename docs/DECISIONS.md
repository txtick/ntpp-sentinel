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
- Labor filter-clean counts use the completed Skimmer work order's assigned tech (`AccountId` / `sk_work_order.source_account_id`), not a same-day route-stop match.
  - Reason: filter-clean work orders can exist without matching a normal route stop row, and route-stop joins undercount payroll work.
- Payroll week is `Sunday -> Saturday`.

## AI / SLA

- Added `recheck_issue` as a real recovery/debugging tool for false positives.
  - Reason: helps validate prompt changes and recover stale/bad AI decisions without inventing more deterministic rules.
- AI false positives should be debugged at the AI-gate/input/prompt layer first, not patched with endless deterministic suppressions.
- Manager summaries should include dashboard reminder pressure.
  - Reason: operational follow-up like quote work should ride the same manager reminder cadence so it does not get forgotten.
- Manager summaries currently omit dashboard reminder pressure and the `resolved since last summary` section.
  - Reason: GHL/LeadConnector was accepting the API request but failing delivery with SMS size-limit errors, so the summary had to be shortened first.
- Manager summary rows now use a shorter contact label and due-time-only formatting.
  - Reason: keeps the full issue list functional for field staff while shaving characters off every line to reduce SMS size-limit failures.

## Dashboard Alerts

- Customers with a route assignment ending within the next `30` days are suppressed from dashboard alerts.
  - Reason: they are operationally leaving and do not need new maintenance/revenue alerts.
- Customers tagged `no-sentinel-alerts` in Skimmer are suppressed from dashboard alerts.
  - Reason: gives operators a durable business-controlled opt-out.
- Customers tagged `filter-sand` are excluded from filter-clean alerts.
  - Reason: sand filters are not being tracked in the current filter-clean workflow, so PSI-driven filter-clean opportunities would be noisy or misleading for that equipment type.
- Filter-clean alerts suppress when the clean is already scheduled/upcoming.
  - Reason: do not alert for work already planned.
- Recent completed filter cleans (within 90 days, service_date in the past) suppress ALL `filter_clean` opportunity types including `filter_clean_trend`.
  - Reason: if a customer just had a filter clean, repeated high PSI readings are expected during the recovery period and are not actionable. Changed 2026-04-19 after Will Saba false-positive.
  - `recent_completed_filter_cleans` uses `service_date < CURRENT_DATE` instead of `complete_time IS NOT NULL`, because Skimmer stores fake `2010-01-01` completion timestamps that would otherwise exclude valid past filter cleans.
- `freedom` customers can generate a separate missing-scheduled-filter-clean alert.
  - Reason: those customers are expected to have cleans pre-scheduled.
- Filter-clean alerts use `Notify Customer` plus a dashboard reminder instead of trying to create Skimmer quotes directly.
  - Reason: the public Skimmer API appears read-only for quotes, so the reliable workflow is notify + reminder + quote detection.
- Filter-clean quote reminders auto-complete when a matching live Skimmer quote is detected.
  - Reason: once the quote exists, the human follow-up debt is gone and should drop out of reminder pressure automatically.
- Filter-clean quote reminder sync now has a dedicated background `web-backend` job in addition to the immediate post-notify check and dashboard refresh path.
  - Reason: quote cleanup should not depend on somebody loading the dashboard or manually triggering a full alert refresh.
- Sales Assist quote tables are included in the standard Skimmer ingest path.
  - Reason: Sales Assist reads `sk_quote` data from Postgres, so quote statuses such as approved/rejected must refresh with the normal Skimmer DB sync instead of relying on a separate manual quote import.
- Sales Assist opens mapped quote customers in GHL rather than trying to initiate calls directly from Sentinel.
  - Reason: GHL supports calling from its web/app dialer, while a normal `tel:` link may call from a salesperson's personal device number.
- Sales Assist priority ignores quote expiration dates/status.
  - Reason: technicians do not set quote expiration dates consistently enough for expiration to be a reliable follow-up signal.

## Dashboard Reports

- Dashboard active-customer and active-pool totals use weekly route-assigned pools, excluding `service-only` and `inspection` tags.
  - Reason: the home dashboard should match maintained weekly pool service, not every non-inactive Skimmer customer or one-off quote/repair record.
- Added a dedicated `Problem Pools` dashboard page backed by a backend-owned SQL view instead of frontend-only math.
  - Reason: chemical-cost review thresholds and leak dollars need one canonical calculation path that is easy to reuse for future notes/status/automation.
- `Problem Pools` uses `20%` as the healthy target chemical-cost ratio and reports leak dollars above that target.
  - Reason: managers want the page to surface both severity and estimated margin erosion, not just raw chemical spend.
- Pools with missing or zero service rate remain visible as `Missing Rate`, but they do not count toward Watch/Problem/Critical totals.
  - Reason: missing pricing data is operationally important, but it should not distort the flagged severity counts.

## Data Source Decisions

- GHL is authoritative for communication history.
- Skimmer is authoritative for service operations.
- Route Sandbox is planning-only: it reads imported Skimmer route data, writes only Sentinel scenario/manual packet tables, and never writes route changes back to Skimmer.
  - Reason: Skimmer route/stop assignment data is read-only for Sentinel today; operators must apply approved packets manually in Skimmer.
- Route Sandbox Google Maps optimization is off by default and must be enabled with `GOOGLE_MAPS_ENABLE_OPTIMIZATION=true`.
  - Reason: optimization can generate paid Google Maps calls and change sandbox stop order, so it should require an explicit operational choice even though it never writes to Skimmer.
- Route Sandbox Google Maps usage is capped inside Sentinel with daily request and route-matrix element limits.
  - Reason: Google Cloud quotas are not enough protection against accidental repeated estimate/optimization clicks in the dashboard.
- Route Sandbox Google Maps estimates default to traffic-unaware routing.
  - Reason: default planning should be stable and repeatable; traffic-aware estimates can be enabled deliberately with `GOOGLE_MAPS_TRAFFIC_MODE`.
- Skimmer API is the preferred live source when current-week route counts matter.
- Nightly Skimmer DB is preferred when the API does not include the needed data.
- Normalized Postgres is Sentinel's working analytics model, not the upstream source of truth.
- `Problem Pools` derives monthly service rate from imported `sk_service_location.rate` / `rate_type`, with explicit cadence conversion for a few known labels and a fallback to the raw rate.
  - Reason: the normalized dashboard layer already has reliable imported pricing there, and existing pricing workflows already treat Skimmer `ServiceLocation.Rate` as the operative service rate.
- The live Skimmer price-increase updater uses `sri_042026_price_export.csv` column `L` / `final_new_rate` as the source of truth for the new service rate per `service_location_id`.
  - Reason: that CSV is the manually approved pricing sheet, so the apply step should follow the finalized value directly instead of recalculating rates again.
- The Skimmer rate updater preserves the live `rateType` returned by the Skimmer API and changes only the numeric service rate.
  - Reason: the CSV decides the amount, but the billing cadence/type still belongs to the existing Skimmer service-location record.

## Performance Decisions

- `dashboard_summary_v` uses counts from `alert_instances` instead of scanning the live analytics views on every request.
  - Reason: the live views (`current_chemistry_alerts_v`, etc.) run full window-function scans over `chemistry_readings` on every query. With 27k+ readings, this caused 60+ second homepage load times. The dashboard is refresh-driven anyway, so `alert_instances` is the correct source.
- `sync_filter_clean_quote_reminders()` runs after the main alert refresh commits, not before it, and no longer runs on every `GET /api/reminders` call.
  - Reason: it makes blocking Skimmer API calls. Running it on read paths was causing all reminder and homepage requests to stall waiting on external HTTP.
- Skimmer daily route API results are cached in-process for 24 hours.
  - Reason: the labor page makes one HTTP call per calendar day in the selected range. For a payroll week that is 7 serial calls per page load. Cache is safe because past days are immutable and payroll is only reviewed Mon/Tue.

## Weather Widget

- Weather data is fetched server-side on `web-backend` at `/api/weather` and cached in-process for 1 hour.
  - Reason: avoids CORS issues, controls rate limiting, and prevents every page load hitting external APIs.
- Open-Meteo is used for weather and dust (free, no API key, global coverage).
  - Reason: best free weather source; dust model works globally including Saharan transport events over Texas.
- Google Pollen API or Ambee can be used for pollen via `POLLEN_PROVIDER`.
  - Reason: Ambee can reject active keys when the Pollen subscription is not active; Google Pollen gives us a supported Maps Platform replacement while keeping the same persisted daily snapshot model.
- Pollen history is collected by a dedicated authenticated `web-backend` cron job twice daily, with `/api/weather` also upserting on successful page loads.
  - Reason: relying on homepage traffic alone left blank days in the 7-day history whenever the widget was not loaded or the live fetch missed.
- Pool water temperature shown is the 7-day fleet-wide average of `temperature` readings from active pools in `chemistry_readings`.
  - Reason: real measured data is more accurate than estimating from air temperature. Falls back to 7-day air temp rolling mean if no readings exist.
- Algae risk thresholds: Low < 60°F, Low-Mod 60–65°F, Moderate 65–70°F, High 70–78°F, Peak > 78°F.
- Pollen season note is static by month based on North Texas seasonal patterns (cedar Jan–Feb, oak/elm Mar–Apr, grass May–Aug, ragweed Aug–Oct).
  - Reason: the live pollen source gives current conditions, while the seasonal note provides broader context for planning.
