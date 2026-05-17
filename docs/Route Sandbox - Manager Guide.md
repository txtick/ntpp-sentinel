# Route Sandbox — Manager Guide

The Route Sandbox lets you plan, test, and compare route changes before touching anything in Skimmer.
Changes you make in the sandbox stay in Sentinel until you're ready — nothing in Skimmer is affected until you manually apply the printed update packet.

---

## Getting There

Open Sentinel in your browser and click **Routes → Route Sandbox** in the left sidebar.

---

## The Layout

| Area | What it shows |
|---|---|
| **Toolbar** (top) | Current scenario name, scenario picker, action buttons |
| **Left panel** | Route groups — one card per tech/day combination |
| **Center area** | Map by default. Replaced by the comparison or update packet view when those are open. Click **← Back to Map** to return to the map. |
| **Right drawer** | Stop detail when you click a stop |

---

## Scenarios

A **scenario** is a working copy of your routes. You can have as many as you want. The live Skimmer data is always separate and untouched.

### Create a scenario
1. Click **New Scenario** in the toolbar.
2. Give it a name (e.g., "Summer Route Test") and optional notes.
3. Click **Create**. Sentinel copies the current Skimmer routes into the scenario.

### Switch between scenarios
Click the scenario name in the toolbar and select a different one from the list.

### Refresh from Skimmer
If Skimmer routes have changed since you created the scenario and you want to start over from current data, click **Snapshot Current** in the toolbar.

### Delete a scenario
Open the scenario, click the **⋯** menu in the toolbar, and select **Delete**.

---

## Viewing Routes

Each card in the left panel represents one tech's route for one day of the week.

- **Click a card header** to select that route — the driving path draws on the map (if an estimate has been run for it) and the card is highlighted with a blue left border.
- Switching the **Tech** or **Day** filter clears the active route from the map automatically.
- **Color coding:**
  - Viewing all techs → each tech gets a unique color.
  - Viewing one tech / all days → each day of the week gets a unique color.
- Use the **Tech** and **Day** filter dropdowns at the top of the left panel to narrow your view.
- Click any stop name to open its detail in the right drawer (address, pool info, frequency).

---

## Moving Stops

You can reassign any stop to a different position, a different day, or a different tech.

### Reorder within the same route
Drag a stop up or down within the same card. The list updates immediately and the stop is flagged as changed.

### Move to a different tech or day
Drag a stop and drop it onto a different card's header. The stop moves to the bottom of that route group. You can then drag it into the right position.

> Changes are saved automatically as you drag. The map updates to reflect the new arrangement. Nothing in Skimmer is changed.

---

## Drive Mileage and Time Estimates

Get a real Google Maps driving estimate for any route group.

1. Click the **Est.** button next to any route group header.
2. Wait a moment — Sentinel calls Google Maps and calculates the total drive distance and time for the stops in order.
3. A badge appears on the group header: `🗺 42.3 mi · 87 min drive`
4. The driving path draws on the map automatically and that route group becomes the active selection.

To switch which route is shown on the map, just click a different group's header. The previous route clears and the new one draws (if it has an estimate).

**Notes:**
- Results are cached. Running Est. again on the same route reuses cached route segments where possible.
- If you've moved or reordered stops after an estimate, the mileage badge is marked stale until you refresh the estimate.
- If a stop doesn't have GPS coordinates on file, Sentinel shows a warning and skips only the affected drive segment in the estimate.
- Est. requires the Google Maps server key to be configured in Sentinel. If you don't see the Est. button, contact your administrator.

---

## Route Optimization

Let Google suggest a more efficient stop order.

1. Expand a route group (must have at least 3 stops).
2. Click the **Opt.** button next to the group header.
3. A preview modal opens showing:
   - Current total distance and drive time
   - Optimized total distance and drive time
   - The proposed new stop order with before/after positions listed
4. **If you like it:** Click **Apply to Scenario**. The stop order updates in the sandbox.
5. **If you don't want it:** Click **Reject** or close the modal. Nothing changes.

> Optimization is always a preview. You control whether it's applied. Skimmer is never touched.

**Notes:**
- Optimization is off unless an administrator explicitly enables it with `GOOGLE_MAPS_ENABLE_OPTIMIZATION=true`.
- Optimization only reorders the selected tech/day route. It does not move stops to a different tech or day.
- Optimization is blocked when any stop in the selected route is missing GPS coordinates.
- Applying a preview updates only the sandbox scenario stop order.

---

## Comparing Scenarios to Current Routes

See what changed between your scenario and the live Skimmer routes.

1. Make sure a scenario is active in the toolbar.
2. Click **Compare Against Latest Skimmer Import** in the toolbar.
3. The map is replaced by a summary showing:
   - Stops added, removed, or reassigned between techs/days
   - Stops that changed position within a route (reordered)
   - A count of unchanged stops
4. Click **← Back to Map** to return to the map view.

Use this to review your changes before generating a packet.

> **Note:** The same pool appearing under two different techs (e.g. James does weekly maintenance and Jarrett handles repairs at the same location) is expected and will not show as a conflict.

---

## Generating a Manual Skimmer Update Packet

When you're happy with the scenario, generate a checklist for making the changes manually in Skimmer.

### Step 1 — Validate
Click **Validate** in the toolbar. A modal shows any errors or warnings in the scenario. Errors must be resolved before you can approve. Warnings (long commute, busy day) are informational only and won't block approval.

### Step 2 — Approve the scenario
Click **Approve Scenario**. Sentinel runs validation automatically — if anything is wrong it shows the error list instead of approving. If the scenario is clean, a confirmation dialog appears. Click OK to lock the scenario.

> Once approved, the scenario is read-only. You cannot move stops or re-run optimization.

### Step 3 — Generate the packet
Click **Generate Manual Update Packet**. The map is replaced by a full checklist of every change: stops that moved tech or day, stops that were added or removed, and stops whose position within a route changed.

At the top of the packet you have three options:
- **Approve Packet** — locks the packet and marks it ready to execute
- **Print Packet** — opens the print dialog so you can print or save as PDF
- **Export CSV** — downloads a spreadsheet version of the change list

### Step 4 — Work through the list in Skimmer
Open Skimmer in another tab and make each change one at a time, following the packet.

As you finish each item, return to Sentinel and mark it **Done**. If you decide to skip a change, mark it **Skipped**.

You don't have to complete the whole list in one sitting — your progress is saved and you can come back to the packet view at any time.

Click **← Back to Map** when you want to return to the route map view.

---

## Tips

- **You can have multiple scenarios at once.** Name them clearly — "Current", "Option A", "Summer Plan" etc.
- **Scenarios don't expire.** Come back to a scenario days later and your changes are still there.
- **Estimates persist.** Once you run Est. on a route group, the mileage badge and driving route stay saved. Click the group header any time to redraw it on the map.
- **Stale estimate badges should be refreshed** before using mileage/time for a final route comparison.
- **Nothing goes to Skimmer automatically.** The only way Skimmer is updated is when you manually make changes using the printed packet.
- **The sandbox is safe to experiment in.** Move stops around, optimize routes, compare options — then delete the scenario if you decide not to use it.

---

## Quick Reference

| Button | What it does |
|---|---|
| **New Scenario** | Create a fresh working copy of current routes |
| **Snapshot Current** | Refresh scenario from latest Skimmer data |
| **Compare** | Show diff between scenario and current routes |
| **Est.** | Get Google Maps mileage and drive time for one route |
| **Opt.** | Get a Google Maps optimized stop order (preview only) |
| **Validate** | Check scenario for errors before approving |
| **Approve Scenario** | Lock scenario and enable packet generation |
| **Generate Manual Update Packet** | Build the Skimmer change checklist (replaces map view) |
| **Approve Packet** | Lock packet for printing |
| **Print Packet** | Print or save the checklist as PDF |
| **Export CSV** | Download change list as a spreadsheet |
| **← Back to Map** | Close comparison or packet view and return to the map |
