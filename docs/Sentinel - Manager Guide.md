# Sentinel Manager Guide

## What Sentinel Is
Sentinel is a missed-call and customer-text tracker.

Its job is to make sure customer contacts get a real response in time and to alert managers when something is missed.

## What You Should Expect

### 1. Real-time breach alerts
If a call or text goes past the response time limit, Sentinel sends a manager alert.

- You get one breach alert per issue (no repeat spam for the same item).
- The same issue can still appear later in summary messages until it is resolved.

### 2. Scheduled summaries
You get summary texts on weekdays at:

- `8:00am`
- `11:00am`
- `3:00pm`

Each summary shows overdue calls/texts and what was resolved since the last summary.

## Response-time window (SLA)

- Business hours are `Monday-Friday, 8:00am-5:00pm` (America/Chicago).
- Response timers run in business hours.

## When Sentinel auto-resolves an issue
Sentinel can auto-resolve only when it sees a real employee response.

- Employee outbound message/call activity counts.
- Automated workflow messages do **not** count.

If no valid employee response is found in time, the issue stays open and appears in alerts/summaries.

## Commands You Can Text to Sentinel
Use these from an internal manager number/contact:

- `List` - show open issues
- `More` - show more open issues
- `Open 123` - show issue `123`
- `Resolve 123` - mark issue `123` resolved
- `Resolve 123 124` - resolve multiple issues
- `Spam 123` - mark issue as spam
- `Note 123 customer said they are good` - add a note

## Simple Manager Workflow
When you get an alert or summary:

1. Open the issue list (`List`).
2. Confirm someone has responded.
3. If handled, send `Resolve <id>`.
4. If junk, send `Spam <id>`.
5. If needed, add context with `Note <id> <text>`.

## If Something Looks Wrong

- If you responded but issue is still open, resolve it manually with `Resolve <id>`.
- If alerts are missing, notify admin to check manager IDs, cron, or integration status.
