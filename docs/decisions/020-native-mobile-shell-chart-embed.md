# ADR-020: Native mobile shell in Burrow, chart as an embedded page

**Status:** Accepted (2026-11-11) · Issue #105 · builds on ADR-019

## Context

On the phone, IB Trader ran as the full web app inside Burrow's WebView.
The mobile layout worked, but its collapsible chart panels felt clunky, and
the whole app (store, several sockets, every panel) loaded just to watch
a few charts and press BUY/SELL. We wanted an IBKR-Mobile-style native
UI without rewriting the chart, which has years of tuned behaviour
(lightweight-charts, RSI pane and divergences, S/R, order lines with
drag-to-amend, fills).

## Decision

1. **Burrow draws Portfolio, Charts, Watchlist and More natively (Compose)**
   from ib-api's existing REST + `/ws` protocol. No new backend endpoints:
   - positions: `subscribe_positions`;
   - quotes: `subscribe_quote`;
   - status: the `status` channel;
   - orders, trades, realized P&L, chart-bots: polled REST;
   - BUY/SELL/CLOSE: the same console commands the web chart sends, via
     `POST /api/commands`, with the outcome read from `command_output`.
   All traffic goes through Burrow's tsnet loopback proxy, which injects
   the ADR-019 bearer key.
2. **The chart stays web.** `/?embed=chart&symbol=…&secType=…&slot=…&tf=…&theme=…`
   renders a single `BotChart` full-bleed with no app shell. Burrow keeps
   one WebView on it and swaps symbol, timeframe or theme in place through
   `window.burrowChart.set({...})`. Two embed-only themes,
   `burrow-dark` / `burrow-light`, match the native palette; they are
   never persisted to the desktop theme setting.
3. **Timeframes (1m, 3m, 1h, 4h, 1D) are IB bar sizes.** The chart takes a
   `timeframe` prop and asks `/api/history` for that `bar_size` with a
   matching window. IB aggregates; we never resample or store bars.
   S/R, auto-signals and the recommendation gauge stay 1m-only, because
   their look-backs are tuned for 1-minute bars. From 1h up, live ticks
   update the newest bar, since IB aligns those bars to the session.

## Consequences

- The web mobile layout stays as the fallback ("Full web app" in More).
- Account net liquidation isn't shown: ib-api has no account-summary data.
  Adding it means a new `ib/base.py` call and its own change.
- Changing the embed's URL contract means updating Burrow's `ChartHost`.
