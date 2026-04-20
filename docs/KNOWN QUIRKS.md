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

## Psycopg

- In psycopg SQL strings, `%` inside `LIKE` clauses must be escaped as `%%` because `%` is treated as a placeholder prefix.

## Labor

- Do not trust normalized stop counts alone to match Skimmer's Labor report for the current week.
- Current-week labor cleanings must be validated against the live Skimmer route API when something looks off.

## AI / Trace

- `poll_resolver` does not force a fresh AI classification; it only checks/resolves open issues using resolver logic.
- To force a new AI decision, use `recheck_issue`.
- `trace.sh` is the preferred operational artifact for debugging issue false positives.

## Mounts / Containers

- `web-backend` does not mount `./data:/data`.
- `sentinel` and `ingest-worker` do mount `./data:/data`.
- Use the right container when querying the live Skimmer SQLite file.
