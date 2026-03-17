# IB Control Center Prototypes

This project is a frontend-only workstation prototype for exploring trading GUI directions. It does not connect to Interactive Brokers, does not place trades, and uses local mock data only.

## Stack

- React
- TypeScript
- Vite
- Tailwind CSS
- `flexlayout-react` for desktop docking, resizable panes, tabs, and drag/drop layouts

## Run Locally

```bash
npm install
npm run dev
```

Open the local Vite URL shown in the terminal.

## What It Does

- Simulates trading workstation activity with mock orders, positions, alerts, logs, bots, and command execution
- Supports four distinct workstation layout variants
- Includes draggable, dockable, tabbed, and resizable panes
- Includes scenario controls to force different operational states

## Switching Variants

Use the `Layout Variant` selector in the top header to switch between:

- Variant A: Classic Trading Workstation
- Variant B: Modern Control Center
- Variant C: Command-Centric Operator View
- Variant D: Bot / Automation Supervision

## Triggering Scenarios

Use the `Scenario Controls` pane to apply operating states such as:

- healthy
- IB disconnected
- reconnecting
- paper mode
- live account warning
- command running
- command failure
- partial fill
- order rejection
- broker message burst
- stale data
- bot heartbeat missing
- reconciliation mismatch

Scenario changes propagate across the header, alerts, logs, console, orders, bots, and status surfaces.

## Notes

- This is a UI/UX exploration project only.
- No backend services are included.
- No production architecture decisions are implied by this prototype.
