You are building a standalone mock UI prototype for a professional trading workstation.

This is a UI/UX exploration project only.

Do NOT build the real application yet.
Do NOT build backend services.
Do NOT integrate with Interactive Brokers.
Do NOT place real trades.

The goal is to generate a polished frontend-only prototype that explores several possible GUI designs before the real system is implemented.

---------------------------------------------------------------------

PROJECT CONTEXT

There is already a functioning trading engine behind this project that connects to Interactive Brokers and supports terminal/TUI-style workflows such as:

- command entry
- log/event output
- viewing orders
- viewing positions
- alerts and warnings
- monitoring background services
- operational health/status reporting

The long-term direction is to create a client-server architecture where the GUI becomes a remote client that communicates with the trading engine.

However, for this prototype phase:

- The backend architecture is intentionally undecided.
- The GUI must NOT lock the system into any specific backend design.
- The purpose is strictly to explore GUI/UX directions.

This prototype must simulate the behavior of such a system using local mock data only.

---------------------------------------------------------------------

PRIMARY GOAL

Build a frontend-only prototype that allows the user to explore FOUR different workstation layout concepts.

The prototype must:

- run locally
- use mocked data
- simulate live updates
- include movable and resizable panes
- include scenario controls to simulate operational states
- include a layout/variant switcher to compare designs

The prototype must feel like real professional software, not a toy dashboard.

---------------------------------------------------------------------

PROJECT STRUCTURE

Create the project in a fresh standalone folder.

Recommended folder name:

ib-control-center-prototypes

Example structure:

ib-control-center-prototypes/
  README.md
  DESIGN_NOTES.md
  VARIANT_COMPARISON.md
  package.json
  src/
    app/
    components/
    layout/
    features/
      header/
      console/
      logs/
      orders/
      positions/
      alerts/
      bots/
      scenarios/
    mock/
    data/
    types/
    utils/
    styles/

Keep the structure clean and modular.

---------------------------------------------------------------------

RECOMMENDED TECHNOLOGY STACK

Use a modern frontend stack suitable for a polished interactive prototype.

Preferred:

- React
- TypeScript
- Vite
- Tailwind CSS

Use component-based architecture.

Use local mock data.

No backend required.

---------------------------------------------------------------------

CRITICAL REQUIREMENT: USE A REAL DOCKING LAYOUT SYSTEM

The UI must use a proper docking/layout library that supports movable and resizable panels.

Do NOT simulate this with simple CSS grids or split panes.

Use a workstation-style layout library such as:

- flexlayout-react
- rc-dock
- golden-layout
- react-mosaic

Choose whichever library best fits the prototype.

The library must support:

- draggable panes
- dockable panes
- tabbed panes
- horizontal/vertical splitting
- resizable panels

The UI should resemble professional workstation software rather than a static webpage.

---------------------------------------------------------------------

MOVABLE AND RESIZABLE PANES

Major UI areas must exist as movable and resizable panes.

Examples:

- command console
- logs/events
- orders
- positions
- alerts
- bots
- detail panels

Requirements:

- panes must be draggable
- panes must be dockable
- panes must be resizable
- panes may appear as tabs
- layout should feel like desktop workstation software

---------------------------------------------------------------------

DESKTOP FIRST WITH FUTURE MOBILE SUPPORT

This prototype is desktop-first.

The main usage will be on a desktop workstation with large monitors.

However, a mobile UI will definitely be built later.

Therefore:

- do not assume the application will only run on desktop
- keep components modular and reusable
- separate layout logic from content components
- avoid deeply hardcoded screen assumptions

But:

- do NOT implement mobile UI
- do NOT compromise the desktop layout to support mobile
- do NOT implement responsive small-screen layouts

The docking system is intended only for the desktop experience.

---------------------------------------------------------------------

BUILD FOUR DISTINCT LAYOUT VARIANTS

The application must contain FOUR workstation layout variants.

Provide a visible layout switcher to change between them.

Each variant must be meaningfully different.

They must NOT be minor rearrangements of the same layout.

Variant A — Classic Trading Workstation

- dense layout
- many panes visible simultaneously
- minimal hidden panels
- optimized for simultaneous monitoring

Variant B — Modern Control Center

- cleaner structure
- more visual hierarchy
- expandable details
- fewer panes visible simultaneously

Variant C — Command-Centric Operator View

- command console is dominant
- typing commands is primary workflow
- other panes support command activity

Variant D — Bot / Automation Supervision

- bot widgets are prominent
- automation health emphasized
- alerts and system state highly visible

---------------------------------------------------------------------

MANDATORY VARIANT DIFFERENTIATION RULE

The 4 variants must be meaningfully different in structure, emphasis, and operator workflow.

They must NOT be simple rearrangements of the same base layout.

Each variant must differ in:

- primary focal pane
- pane grouping strategy
- navigation style
- information density
- operator workflow emphasis

Required layout fingerprints:

Variant A:
- at least 5 panes visible simultaneously

Variant B:
- more contextual panels/drawers

Variant C:
- command console must be dominant

Variant D:
- bot widgets must be large and prominent

At a glance, screenshots of each variant should be clearly distinguishable.

If they look too similar, redesign them.

---------------------------------------------------------------------

COMMON UI COMPONENTS

All variants must include the following functional areas.

GLOBAL STATUS HEADER

Show:

- IB connection status
- account mode (paper/live)
- service health
- stale data indicator
- warning state
- P&L snapshot
- backend/session connectivity indicator

COMMAND CONSOLE

Include:

- command prompt
- command history
- simulated execution
- states:
  queued
  running
  success
  failure

LOG / EVENT STREAM

Include:

- info messages
- warnings
- errors
- broker-style messages
- timestamps

ORDERS PANEL

Include:

- open orders table
- order source badges (system/bot/manual/external)
- status badges
- fill progress
- order age
- detail view

POSITIONS PANEL

Include:

- symbol
- quantity
- avg cost
- mark
- unrealized P&L
- realized P&L

ALERTS PANEL

Include:

- active alerts
- dismissed alerts
- expandable error cards

BOTS PANEL

Include:

- bot widgets
- heartbeat
- last signal
- last action
- error state

---------------------------------------------------------------------

SCENARIO SIMULATION PANEL

Add a visible scenario panel allowing simulation of operational states.

Scenarios must include:

- healthy state
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

These states must visibly affect multiple panes.

---------------------------------------------------------------------

SIMULATED LIVE BEHAVIOR

Use timers or state updates to simulate:

- log stream updates
- order updates
- bot heartbeat updates
- command progress
- alerts appearing/disappearing

---------------------------------------------------------------------

LOOK AND FEEL

The UI must feel like professional workstation software.

Requirements:

- dark theme
- high information density
- clear typography
- minimal wasted space
- no crypto dashboard styling
- no gimmicky gradients
- no marketing-style layout
- optimized for large monitors

---------------------------------------------------------------------

DOCUMENTATION REQUIREMENTS

README.md

Explain:

- this is a frontend-only prototype
- it does not connect to IB
- how to run the project
- how to switch variants
- how to trigger scenarios
- which docking library was used

DESIGN_NOTES.md

Explain:

- goals of each variant
- design philosophy
- operator workflow assumptions
- strengths and weaknesses

VARIANT_COMPARISON.md

For each variant describe:

- dominant pane
- layout philosophy
- target operator style
- strengths
- weaknesses
- how it differs from the others

---------------------------------------------------------------------

ACCEPTANCE CRITERIA

The prototype is complete only if:

1. It runs locally.
2. It contains four clearly distinct layouts.
3. Panels are movable and resizable.
4. Scenario simulation works.
5. Mock data appears realistic.
6. The UI convincingly simulates a professional trading workstation.

---------------------------------------------------------------------

FINAL INSTRUCTION

Prioritize:

- strong layout experimentation
- workstation-style interaction
- movable and dockable panes
- visual polish
- realistic simulation

Do NOT prioritize:

- backend implementation
- real broker logic
- production architecture.
