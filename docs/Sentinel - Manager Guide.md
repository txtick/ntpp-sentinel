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

Current summary focus is overdue calls/texts plus escalated items.

To keep SMS delivery reliable, the manager summary currently does **not** include:

- the `resolved since last summary` section
- dashboard reminder pressure / overdue reminder detail

Current summary lines also prefer a shorter contact label:

- last name when a full contact name exists
- first/only name when there is no last name
- phone number fallback when no name is available

Issue rows show the due time only, not the original inbound timestamp.

The default repo schedule runs on Monday-Saturday, but exact send times and active days come from admin configuration.

## Response-time window (SLA)

- Business hours are configured by admin (for example `8:00am-5:00pm`).
- Response timers run only during configured business hours.

## Route Rollover
Sentinel supports a mobile-friendly Route Rollover page under `Routes -> Notify Route Change`.

This is separate from missed-call and missed-text monitoring.

Its job is to help when a technician route changes and a customer needs a rollover or apology message sent into the correct conversation.

What managers should know:

- Opening the dashboard while signed out shows a branded NTPP Sentinel login page instead of an empty dashboard frame. The dashboard appears only after Google sign-in succeeds.
- If a phone returns from Google without Sentinel's temporary login session, Sentinel safely retries once in that browser. If the phone still blocks the session, the login page explains how to try again instead of downloading an error file.
- If a saved dashboard session expires, Sentinel hides the dashboard and returns to the sign-in page when the app is reopened or the next protected action is attempted.
- Technicians sign in with their North Texas Pool Pros Google Workspace email.
- Active Skimmer `Tech` accounts land directly on Notify Route Change and do not see or have API access to the manager dashboard. Kevin, Jim, and Jarrett retain the full dashboard.
- Sentinel matches that email to the active Skimmer technician and shows only that technician's live route for today.
- Already-serviced stops are visible but cannot be selected.
- The technician selects the customers moving to tomorrow, enters the issue/reason, and either edits the standard message or asks AI to draft one.
- Every selected customer's personalized message is previewed before sending.
- A review checkbox and final confirmation are required before the GHL send starts.
- It sends only when Sentinel finds a single confident customer conversation match.
- If the match is unclear, Sentinel is designed to avoid sending rather than risk messaging the wrong customer.
- The result lists each customer as sent or failed. A double tap or browser retry will not duplicate the batch.

The old technician SMS command is disabled by default. It can be temporarily restored with `ROLLOVER_SMS_ENABLED=1`, but the webpage is the normal workflow.

## Labor Page
The dashboard also includes a `Labor` tab for weekly payroll prep.

What it shows:

- weekly pool counts by technician
- weekly filter clean counts by technician
- first `40` pools as regular Gusto hours
- pools over `40` as commission dollars
- one final commission dollar amount that already includes filter-clean pay

Default pay logic:

- `$16` per pool
- `$25` per filter clean
- salary technicians can be hidden from the list

Important details:

- The payroll week is `Sunday -> Saturday`.
- For `Cleanings` / pool stops, Sentinel follows the live Skimmer route API so the current week matches Skimmer's Labor report.
- The nightly Skimmer DB refresh happens around `11:00pm`, so the SQLite copy may be behind for same-day reporting.
- Filter cleans come from completed Skimmer work orders and are rolled into the final `Commission` dollar amount shown for Gusto entry. If Skimmer's export omits a completed work-order row, Sentinel verifies the matching `WorkOrderFinished` activity event and counts it once by work-order id.

Alert opt-out:

- Add the Skimmer tag `no-sentinel-alerts` to a customer to suppress dashboard alerts for that customer after the next Skimmer import and dashboard refresh.

This page is meant to give you the numbers you need for Gusto without manually counting route work and filter-clean work orders.

## Problem Pools Page

The dashboard also includes a `Problem Pools` page for chemical-cost pricing review.

The dashboard's active customer/pool totals are meant to represent weekly maintained pool service. One-off `service-only` customers and `inspection` prospects are excluded until they become active weekly route customers. The home dashboard's `Active Pools` number counts weekly service locations/stops, so a pool + spa at one stop does not double-count the home card.

What it shows:

- monthly chemical cost for each active pool based on the last `30` days
- monthly service rate pulled from imported Skimmer service-location pricing
- chemical cost as a percent of service rate
- a `monthly leak` amount above the healthy `20%` target
- a severity flag and suggested manager action

Flag meanings:

- `Watch` = `20%` to under `25%`
- `Problem` = `25%` to under `35%`
- `Critical` = `35%` or higher

Suggested actions:

- `Watch` -> monitor
- `Problem` -> review / recommend treatment
- `Critical` -> immediate review: treatment, price increase, or service adjustment

Important detail:

- pools with missing or zero service rate stay on the page as `Missing Rate`
- those rows show `N/A` for chemical percent
- they do not count toward the Watch / Problem / Critical totals

This page is meant to help managers quickly spot underpriced pools, unusually chemical-heavy pools, or accounts that need a service-plan conversation.

## Sales Assist

The dashboard includes a `Sales Assist` quote pipeline for following up on open Skimmer quotes.

What it shows:

- open quote status and amount from Skimmer quote data
- customer contact details
- quote line items and recent service context where available
- AI sales notes when generated

When the customer is mapped to a GHL contact, use `Open in GHL` to open the customer in HighLevel and place the call from GHL. The `Call from device` fallback uses the tablet or phone dialer and may not use the company GHL number.

## Dashboard Reminders

Dashboard alerts can create tracked reminders in the dashboard backend.

What managers should know:

- reminders are separate from missed-call and missed-text SLA issues
- reminders can be created from alerts when work still needs human follow-up

Filter-clean alert workflow:

- filter-clean alerts now support a `Notify Customer` button in the dashboard
- this sends an SMS telling the customer they are due for a filter clean and that a quote will follow
- it also creates or updates a dashboard reminder so someone still remembers to send the quote manually
- once the quote is detected in Skimmer, that reminder should auto-complete

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
