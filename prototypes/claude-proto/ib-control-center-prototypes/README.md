# IB Control Center — UI Prototypes

Frontend-only prototype exploring four workstation layout concepts for a professional trading control center.

**This does not connect to Interactive Brokers.** All data is mocked. No real trades are placed.

## Run

```bash
npm install
npm run dev
```

Open http://localhost:5173

## Switch Variants

Use the **A / B / C / D** buttons in the top-right of the global header bar.

| Variant | Name | Focus |
|---------|------|-------|
| A | Classic Workstation | Dense, all panes visible simultaneously |
| B | Control Center | Cleaner hierarchy, tabbed bottom drawer |
| C | Command Operator | Console-dominant, CLI-first workflow |
| D | Bot Supervisor | Large bot cards, automation-centric |

## Trigger Scenarios

Open the **Scenarios** panel (right border in A/C/D, bottom tab in B) and click any scenario button. Effects propagate across all panes in real time.

Available scenarios: Healthy, IB Disconnected, Reconnecting, Paper Mode, Live Account Warning, Command Running, Command Failure, Partial Fill, Order Rejection, Broker Message Burst, Stale Data, Bot Heartbeat Missing, Reconciliation Mismatch.

## Interact with Panes

All panels are **movable, dockable, resizable, and tabbable** via flexlayout-react. Drag tab headers to rearrange. Drag splitters to resize. Double-click a tab strip to maximize.

## Stack

- React + TypeScript
- Vite
- Tailwind CSS v4
- flexlayout-react (docking layout)
- Zustand (state management)

## Project Purpose

This is a UI/UX exploration prototype. It is not the real application. The goal is to evaluate layout concepts before building the actual client-server trading workstation.
