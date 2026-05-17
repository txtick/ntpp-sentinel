# Known Quirks

## Update Rule

Add an entry here whenever you discover a behavior that is easy to forget and likely to waste time later, especially if it caused a false assumption during debugging.

Examples:

- placeholder timestamps
- container mount differences
- API vs DB mismatches
- endpoint exposure surprises
- library/query gotchas
- stale cache/refresh behavior

## Skimmer

- Same-day Skimmer DB-backed analytics may lag until the nightly DB download, typically around `11:00pm`.
- Skimmer can store fake completion timestamps such as `2010-01-01`, which should be treated as placeholders rather than true completion.
- Skimmer API is live, but it does not contain every field/entity needed for all workflows.
- Public Skimmer quote access appears read-only for this project today, so Sentinel should not assume it can create quotes through the API.

## Dashboard Refresh

- `POST /jobs/dashboard/refresh` lives on `web-backend`, not on public Sentinel routes.
- Public `sentinel.northtexaspoolpros.com` does not expose every backend/internal job path.
- If an alert summary looks wrong after a code fix, remember that the dashboard may need both a backend refresh and a schema/view refresh path if the wrong value is coming from a SQL view rather than the frontend text formatter.

## Route Sandbox

- Route Sandbox must not expose a push-to-Skimmer workflow. Skimmer route changes are applied manually from the generated packet, then confirmed by a later Skimmer import/compare.
- Technician home/base addresses are Sentinel-only route-planning settings. Do not log them or include them in ordinary Route Sandbox map payloads unless the operator is editing technician route settings.

## Psycopg

- In psycopg SQL strings, `%` inside `LIKE` clauses must be escaped as `%%` because `%` is treated as a placeholder prefix.

## Labor

- Do not trust normalized stop counts alone to match Skimmer's Labor report for the current week.
- Current-week labor cleanings must be validated against the live Skimmer route API when something looks off.
- Labor filter-clean counts should use completed Skimmer work orders by work-order tech (`AccountId` / `sk_work_order.source_account_id`), not route-stop joins. Some completed filter-clean work orders do not line up with a normal same-day route stop, which can undercount techs on payroll.

## AI / Trace

- `poll_resolver` does not force a fresh AI classification; it only checks/resolves open issues using resolver logic.
- To force a new AI decision, use `recheck_issue`.
- `trace.sh` is the preferred operational artifact for debugging issue false positives.

## GHL API Token

- GHL appears to track API token "inactivity" per endpoint or integration type, not per token globally. The `sentinel` container calls GHL messaging/webhook endpoints all day, but `web-backend` calls `conversations/search` (used by notify-customer). If notify-customer has not been used in ~90 days, GHL may expire access to that endpoint even though the token is otherwise active.
- Symptom: notify-customer returns "GHL GET /conversations/search failed: 403" with a Cloudflare HTML error page.
- Fix: refresh the GHL token in `.env` on both containers, then `docker compose restart web-backend sentinel`.
- Use notify-customer periodically (or add a scheduled health check) to prevent the 90-day clock from resetting.

## Weather Widget APIs

- **Open-Meteo pollen (CAMS) does not cover North America.** Do not try to source Texas pollen history from Open-Meteo.
- **Open-Meteo dust works globally** — use it for Saharan dust events (dust μg/m³ > 75 = elevated, > 200 = Saharan event).
- **Production pollen fetches currently use Ambee latest-only data**, not a true historical API. The dashboard history fills from one stored row per day in `pollen_daily_log`.
- **Weather cache is in-process and resets on container restart.** First load after restart hits Open-Meteo weather, Open-Meteo AQ, and Ambee pollen. Cached for `WEATHER_CACHE_TTL` seconds (default 3600).
- **Pollen history was previously traffic-driven.** Before the dedicated `weather_pollen` cron job, blank days appeared whenever `/api/weather` was not loaded that day or the live pollen fetch failed.
- **Current pollen now falls back to today's stored snapshot if the live Ambee call fails.** This keeps the widget populated when the cron snapshot already saved a row earlier that day, but it cannot invent data for days where every snapshot attempt failed.
- **Pollen history must use the dashboard's local date, not Postgres `CURRENT_DATE`.** If the DB session is on UTC during the evening, a same-night pollen snapshot can land under tomorrow's date and leave the widget's `Today` row blank.
- **`WEATHER_LAT` / `WEATHER_LON`** env vars control coordinates (default 33.15, -96.82 = Frisco/McKinney area).
- `/api/weather` is on `web-backend` and requires dashboard auth. Weather data is fetched server-side and cached; the frontend does not call external APIs directly.

## Python urllib + Cloudflare + GHL

- Python's `urllib` sends `User-Agent: Python-urllib/3.x` by default. Cloudflare (which sits in front of GHL's API) blocks this UA and returns 403 with an HTML error page.
- `curl` works fine because its UA passes through. This creates a confusing split: manual curl tests succeed, but the app returns 403.
- Fix: always include `"User-Agent": "NTPP-Sentinel/1.0"` (or any non-Python UA) in `_ghl_headers()` and any other `urllib.request.Request` calls.

## GHL conversations/search — locationId must be a query param

- `conversations/search` requires `locationId` as a **query parameter**, not just as a header. Passing it only via the `LocationId` header causes a 400 or silent failure.
- Other endpoints (contacts, messages) accept `LocationId` as a header; this one does not.
- The startup health check and `_ghl_find_conversation_id` were both missing this param — fixed 2026-04-19.

## GHL / LeadConnector SMS Size Limit

- GHL can accept an outbound summary API request and still mark the actual SMS as `Failed` in the conversation if the message body is too long.
- The observed failure was `Error 30019` / `SMS Size Limit`.
- Even though GHL says it supports longer payloads, manager summary SMS needs to stay much shorter in practice to avoid carrier-side failures.

## Mounts / Containers

- `web-backend` does not mount `./data:/data`.
- `sentinel` and `ingest-worker` do mount `./data:/data`.
- Use the right container when querying the live Skimmer SQLite file.

## Dashboard Summary / Homepage

- `dashboard_summary_v` reads alert counts from `alert_instances`, not from the live analytics views (`current_chemistry_alerts_v`, `chemistry_trend_alerts_v`, `revenue_opportunities_v`).
- This means homepage stat cards reflect the last dashboard refresh run, not a real-time scan. This is intentional — re-running the full analytics on every page load caused 60+ second load times.
- If summary counts look stale, the fix is to trigger a dashboard refresh (`POST /jobs/dashboard/refresh`), not to query the views directly.
- If a customer's imported Skimmer tags change in a way that affects alert suppression, already-open tracked alerts will not disappear until the next dashboard refresh recalculates the backend-owned alert set.
- Customer-tag-driven alert suppression depends on the nightly Skimmer import preserving tags into `sk_customer.raw_json` and then `customers.raw_json`. In this dataset, customer tags may live in relational `CustomerTag`/`Tag` tables even when `Customer.Tags` is null, so the importer must merge both sources or dashboard suppression logic like `filter-sand` and `no-sentinel-alerts` will silently fail even when the tag exists in Skimmer.

## Problem Pools Report

- The `Problem Pools` page is not refresh-driven like tracked alerts; it reads directly from `problem_pools_v`, so page loads reflect the current imported Postgres data without a dashboard refresh step.
- Monthly service rate there is derived from imported `sk_service_location.rate` and `rate_type`.
- Known cadence conversions today:
  - weekly -> `rate * 52 / 12`
  - biweekly -> `rate * 26 / 12`
  - twice-monthly / semi-monthly -> `rate * 2`
  - quarterly -> `rate / 3`
- Any unmapped or blank `rate_type` falls back to the raw imported `rate`, because that is how current pricing workflows already treat Skimmer's service-location rate field.

## Quote Sync / Reminders

- `sync_filter_clean_quote_reminders()` runs after `Notify Customer`, as part of `POST /jobs/dashboard/refresh`, and on the dedicated `POST /jobs/filter-clean/quote-sync` background job. It no longer runs on every `GET /api/reminders` call.
- If a filter-clean quote reminder is not auto-completing, either trigger a dashboard refresh or run the dedicated quote-sync job directly.

## Dashboard Frontend Cache

- The dashboard SPA is static `index.html` + `app.js` + `styles.css` served by Caddy.
- If the browser keeps an older cached `app.js` after a deploy, a newly added nav item can appear in HTML while clicks silently fail because the old JS does not know that view exists yet.
- Frontend static assets now send no-cache headers to reduce HTML/JS version mismatches after deploys.

## Frontend Tooling / WSL

- Local frontend checks now live in repo-level `package.json` scripts (`lint`, `format:check`, `test`, `check`).
- In this WSL environment, `node`/`npm` may not be available in noninteractive shells until `nvm` is loaded manually with `source "$HOME/.nvm/nvm.sh"`.
- If `npm`-based checks fail with "command not found" in a fresh shell, load `nvm` first rather than assuming the repo is broken.
- The frontend smoke test is intentionally lightweight: it validates expected SPA files/selectors/hooks, but it does not replace a real browser check for responsive layout or chart rendering.

## Labor / Skimmer Route Cache

- Skimmer daily route API responses are cached in-process for 24 hours per calendar day.
- Past days are immutable so 24 hours is safe. Today's routes also cache for 24 hours since payroll is only reviewed on Monday/Tuesday.
- Cache is in-memory and resets on container restart. First load after a restart will hit the Skimmer API for each day in the selected range.
