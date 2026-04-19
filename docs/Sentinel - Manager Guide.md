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
You get scheduled summary texts (timing set by admin), typically:

- `8:00am`
- `11:00am`
- `3:00pm`

Each summary shows overdue calls/texts and what was resolved since the last summary.

The default repo schedule runs on Monday-Saturday, but exact send times and active days come from admin configuration.

## Response-time window (SLA)

- Business hours are configured by admin (for example `8:00am-5:00pm`).
- Response timers run only during configured business hours.

## Route Rollover
Sentinel also supports Route Rollover messaging.

This is separate from missed-call and missed-text monitoring.

Its job is to help when a technician route changes and a customer needs a rollover or apology message sent into the correct conversation.

What managers should know:

- It is a live operational workflow inside Sentinel.
- It uses Skimmer assignment data to identify the technician context.
- It sends only when Sentinel finds a single confident customer conversation match.
- If the match is unclear, Sentinel is designed to avoid sending rather than risk messaging the wrong customer.

In practice, this means Route Rollover is meant to reduce manual follow-up during technician handoffs without creating extra customer confusion.

## Labor Page
The dashboard also includes a `Labor` tab for weekly payroll prep.

What it shows:

- weekly pool counts by technician
- weekly filter clean counts by technician
- first `40` pools as regular Gusto hours
- pools over `40` as commission dollars
- separate filter-clean pay

Default pay logic:

- `$16` per pool
- `$25` per filter clean
- salary technicians can be hidden from the list

This page is meant to give you the numbers you need for Gusto without manually counting route work and filter-clean work orders.

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
