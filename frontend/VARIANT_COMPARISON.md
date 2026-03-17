# Variant Comparison

## Variant A — Classic Trading Workstation

- **Dominant pane:** None — all panes share equal visual weight
- **Layout philosophy:** Maximum simultaneous visibility; every pane always on screen
- **Target operator:** Active trader monitoring multiple data streams; glances constantly between panes
- **Navigation:** Direct — everything visible, no tabs to switch, no drawers to open
- **Information density:** Highest — 6 panes visible simultaneously plus border-docked scenarios
- **Strengths:**
  - Nothing hidden; full situational awareness at a glance
  - No clicks needed to see any data
  - Familiar to Bloomberg/TOS/TWS users
- **Weaknesses:**
  - Visually dense; can feel overwhelming
  - Each pane is smaller due to space sharing
  - Less clear visual hierarchy — harder to know where to look first

## Variant B — Modern Control Center

- **Dominant pane:** Positions/Orders occupy the main viewport
- **Layout philosophy:** Visual hierarchy with contextual detail; primary data prominent, secondary data in drawer
- **Target operator:** Focused trader who checks positions/orders primarily, pulls up logs/console on demand
- **Navigation:** Tab-based bottom drawer for secondary panes (Logs, Console, Scenarios)
- **Information density:** Medium — main area shows 3 panes, drawer adds 3 more on demand
- **Strengths:**
  - Cleaner, less overwhelming visual experience
  - Clear primary focus on positions and orders
  - Bottom drawer provides flexible secondary workspace
  - Bots have dedicated column with large cards
- **Weaknesses:**
  - Console and logs require clicking a tab to view
  - Less simultaneous information than Variant A
  - May require more clicks during active debugging

## Variant C — Command-Centric Operator View

- **Dominant pane:** Command Console — largest pane, left-center position
- **Layout philosophy:** Console is the primary interface; all other panes support and confirm command activity
- **Target operator:** Keyboard-centric operator from terminal/TUI background; thinks in commands
- **Navigation:** Type commands, scan results; supporting panes arranged as reference panels
- **Information density:** Medium-high — console + logs take 60% of screen, reference panels stacked right
- **Strengths:**
  - Fastest workflow for keyboard-centric operators
  - Console output is easy to read (large pane)
  - Log stream validates command execution in real time
  - Compact reference panels keep orders/positions/alerts visible
- **Weaknesses:**
  - Less useful for operators who prefer visual/mouse interaction
  - Right-side panels are compact — less detail visible per row
  - Requires knowing command syntax; no visual affordances for actions

## Variant D — Bot / Automation Supervision

- **Dominant pane:** Bots panel — large cards with full bot detail
- **Layout philosophy:** Automation health is the primary concern; trading data confirms bot activity
- **Target operator:** Bot supervisor managing multiple automated strategies; checks health, not individual trades
- **Navigation:** Scan bot cards for status, drill into alerts for issues, check logs for context
- **Information density:** Medium — bot cards are intentionally spacious for readability; trading data is compact
- **Strengths:**
  - Bot status immediately visible with full context per bot
  - Error states and heartbeat issues are prominent
  - Alerts panel is always visible for quick issue detection
  - Good for operators supervising many bots across symbols
- **Weaknesses:**
  - Less useful for manual trading workflows
  - Orders and positions share tabs — can't see both at once by default
  - Console is in the border drawer — less convenient for command-heavy work

## Summary Matrix

| Aspect | A | B | C | D |
|--------|---|---|---|---|
| Simultaneous panes | 6+ | 3+drawer | 5 | 4+drawer |
| Primary focus | Everything | Positions | Console | Bots |
| Operator style | Monitor-all | Focused | Keyboard | Supervisor |
| Visual density | Very high | Medium | Medium-high | Medium |
| Command access | Direct pane | Drawer tab | Dominant | Drawer tab |
| Bot visibility | Table row | Large cards | Drawer tab | Large cards |
