# Design Notes

## Prototype Intent

The prototype explores workstation UX without committing to backend topology, transport, or Interactive Brokers integration details. The layouts emphasize operator workflow, situational awareness, and layout experimentation rather than implementation realism.

## Shared Design Philosophy

- Dark, dense, desktop-first interface
- Dockable desktop panes instead of static dashboard cards
- Mock operational realism through timestamps, broker-style messages, command state, and bot health
- Layout logic kept separate from pane content so the content can later be reused in other clients

## Variant A: Classic Trading Workstation

- Goal: maximize simultaneous visibility
- Assumption: operator monitors many streams in parallel and wants fewer hidden surfaces
- Strength: strongest at-a-glance awareness
- Weakness: highest cognitive load and visual density

## Variant B: Modern Control Center

- Goal: create stronger hierarchy with contextual detail
- Assumption: operator focuses on fewer surfaces at a time and drills into detail when needed
- Strength: cleaner reading flow and clearer focal areas
- Weakness: less simultaneous visibility than the dense workstation

## Variant C: Command-Centric Operator View

- Goal: center the command console as the primary operating surface
- Assumption: command entry and rapid feedback are the dominant workflow
- Strength: strongest support for keyboard-driven operations
- Weakness: less ideal for broad passive monitoring

## Variant D: Bot / Automation Supervision

- Goal: emphasize automation health, heartbeat quality, and intervention conditions
- Assumption: operator mainly supervises bots and exceptions rather than manually driving each action
- Strength: best for automation oversight
- Weakness: less efficient for detailed order management
