# ADR-009: Persistent REPL Session, Not One-Shot CLI

**Date:** 2026-03-08
**Status:** Accepted

## Decision

The CLI is a persistent interactive REPL that you start once and trade from throughout a session. It is not a one-shot script where each command is a separate process invocation.

## Reasoning

A one-shot CLI (`ib-trader buy MSFT 100 mid`) would require reconnecting to IB on every command, re-warming the contract cache, re-running health checks, and re-establishing the event subscription for fill notifications. This overhead (2-5 seconds per command) is unacceptable in a trading context. A persistent REPL connects once at startup, keeps the IB connection alive, and accepts commands at a prompt with sub-second response time after the first command. Fill events arrive via push callbacks registered at startup.

## Consequences

- `ib-trader` starts an interactive session, not a one-shot command.
- The REPL loop reads from stdin, parses commands, and dispatches to handlers.
- `exit` or Ctrl+C cleanly disconnects from IB and writes `REPL_EXIT_CLEAN` to SQLite.
- `click` is used for the entry point definition but the REPL loop is a custom `while True` loop — `argparse` is not used inside the REPL (it calls `sys.exit()` on errors).
- Commands are parsed with `shlex.split()` for shell-like quoting support.

## Future Considerations

A bot API (bots calling the engine as a Python module at high frequency) bypasses the REPL entirely and calls engine functions directly. The REPL is the human interface; the engine is the shared logic. This architecture is explicitly designed to support this future use case.
