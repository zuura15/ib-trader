# Design Notes

## Goals

Explore four fundamentally different workstation layouts for a professional IB trading control center. Each variant targets a different operator workflow and information priority.

## Design Philosophy

- **Dark theme only** — reduces eye strain during extended sessions; standard for trading software.
- **Monospace typography** — aligns numeric columns naturally; conveys precision and seriousness.
- **High density** — professional traders need maximum information per pixel; whitespace is wasted screen.
- **No chrome** — no gradients, shadows, rounded cards, or marketing aesthetics. Flat, functional, utilitarian.
- **Docking layout** — operators must customize their workspace. Fixed layouts frustrate power users.
- **Color is semantic** — green=positive/connected, red=negative/error, yellow=warning/partial, blue=accent/info. Never decorative.

## Operator Workflow Assumptions

### Variant A — Classic Workstation
The operator monitors everything simultaneously. They glance between panes constantly. The layout is dense and always-visible — nothing is hidden behind tabs or drawers. This suits operators who run a single large monitor and want all information at a glance.

### Variant B — Modern Control Center
The operator works in focused bursts. They primarily watch positions and orders, then pull up logs/console when needed via the bottom drawer. Alerts and bots have dedicated space but the layout is less overwhelming than Variant A. Suits operators who prefer visual hierarchy.

### Variant C — Command Operator
The operator thinks in commands. The console is their primary interface — they type to trade, check status, and control bots. Supporting panes (orders, positions, alerts) are visible but secondary. The log stream validates command execution. Suits keyboard-centric operators who come from TUI/terminal backgrounds.

### Variant D — Bot Supervisor
The operator manages automated strategies. Bot health and status are the primary concern. Individual bot cards show heartbeat, signals, actions, and errors at a glance. Orders and positions are secondary data that confirm bot activity. Suits operators running multiple automated strategies who need supervision, not direct trading.

## Component Separation

All feature components (Console, Logs, Orders, Positions, Alerts, Bots, Scenarios) are layout-agnostic. They accept optional props (compact, large) but have no knowledge of which variant they're in. This allows the docking system to place them anywhere.

Layout definitions live in `layout/variants.ts` as pure JSON models. Switching variants swaps the entire layout model — components are instantiated fresh in their new positions.

## Simulation Design

The simulation layer updates every 2 seconds:
- Position mark prices drift randomly (small deltas)
- Log entries appear periodically
- Bot heartbeats refresh
- Session uptime increments
- P&L totals recalculate from positions

Scenario triggers override specific state slices to demonstrate failure modes and edge cases. They reset to healthy state when "Healthy" is selected.
