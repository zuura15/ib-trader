<!-- Spec frozen by the GitHub issue that links here via a commit-SHA
     permalink. Later edits to this file do not change what that issue
     references — the issue resolves the link against the original SHA. -->

# Chart pane — 4–6h line chart driven by Positions / Watchlist selection

## Why

The user already uses TradingView for eyeballing intraday trend on
symbols they care about. TradingView's feed goes stale after 5 PM PT
on most plans, while IB provides the full overnight tape. This pane
covers the overnight gap — a small, focused chart that shows "what
did the last few hours look like?" for whichever row the user clicked.

Out of scope: anything you'd use a real charting terminal for
(candles, volume overlays, indicators, drawing tools). If the user
wants that, they'll open TradingView. This is for "do I need to
trade?" eyeballing only.

## Scope — v1

- **Line chart**, close price only.
- **24 h of 1-min bars loaded** per selection (single IB call),
  with the **last 6 h visible by default**. Zoom / pan can walk
  back the other 18 h.
- Mouse-wheel zoom on the time axis, click-drag pan — native
  `lightweight-charts` behaviors, just enabled.
- Small "Reset" button that snaps the visible range back to the
  last 6 h.
- **Auto-refresh every 30 s** while the pane is visible.
  Refreshes must not steal the user's current zoom level — diff
  the last-loaded range before applying and restore it after.
- Placeholder message when nothing is selected.

### Security-type coverage

- **STK / ETF** — charted directly. Positions and Watchlist both
  support this.
- **FUT** — charted by `con_id` on the specific future contract.
  Positions rows already carry `con_id` + `sec_type="FUT"`.
- **OPT** — positions come through with `sec_type="OPT"`, but the
  chart plots **the underlying, not the option premium**. Premium
  over 24 h is dominated by theta / gamma and isn't useful for
  eyeballing. The `symbol` field on an IB Option contract is the
  underlying ticker; when `sec_type === "OPT"` the chart-target
  setter substitutes `{symbol, secType: "STK", conId: null}` so
  the chart auto-plots the underlying.
- **Watchlist** is equity-only by design.

Chart header labels which contract is being shown so the user
isn't misled: e.g. `"USO · STK"` when clicking an option row,
`"ES DEC5 · FUT"` when clicking a futures row.

## Libraries

- Frontend: add **`lightweight-charts`** (TradingView's open-source
  chart lib — small, performant, line + candle capable). No other
  charting dep in `frontend/package.json` today.

## Files to change

### Backend

- `ib_trader/ib/base.py` — add abstract
  `async def get_historical_bars(self, con_id, duration: str,
  bar_size: str, what_to_show: str = "TRADES", use_rth: bool = False)
  -> list[HistoryBar]`. Forces the call through the same rate-limit
  layer as other IB methods.
- `ib_trader/ib/insync_client.py` — implementation wraps
  `self._ib.reqHistoricalDataAsync(...)`. There's an existing
  private `_historical_midpoint` at lines 335–362 to crib from; lift
  the setup, drop the MIDPOINT hardcoding and use the passed
  arguments.
- `ib_trader/engine/internal_api.py` — new `GET /engine/history`
  endpoint accepting:
  - `?con_id=<int>` — preferred. Works for any security type
    without re-qualification.
  - `?symbol=<str>&sec_type=STK` — fallback for watchlist and
    manual selection where only the ticker is known. Calls
    `ctx.ib.qualify_contract(symbol, sec_type)` to resolve a
    `con_id`, then proceeds.
  Both paths end in
  `get_historical_bars(con_id, "24 H", "1 min")`.
  Tiny TTL dict cache keyed on `(con_id, hours, resolution)` for
  30 s to dedupe pane refreshes and respect IB's 2000 req / 10 min
  ceiling.
- `ib_trader/api/routes/history.py` — new public
  `GET /api/history?con_id=N` (or `?symbol=X&sec_type=STK`) that
  proxies to the engine. Mirrors the `routes/positions.py` proxy
  pattern.
- `ib_trader/api/serializers.py` — new `HistoryBarResponse` with
  fields `{ts, open, high, low, close, volume}`.
- `ib_trader/api/app.py` — `app.include_router(history.router)`.

### Frontend

- `frontend/package.json` — add `lightweight-charts`.
- `frontend/src/data/store.ts` — new Zustand slice:
  ```ts
  type ChartTarget = {
    symbol: string;       // display label + underlying for OPT
    secType: 'STK' | 'FUT' | 'OPT';
    conId: number | null; // preferred identifier; null for watchlist
  };
  selectedChartTarget: ChartTarget | null;
  setSelectedChartTarget(t: ChartTarget | null): void;
  ```
  The setter performs the **OPT → STK underlying substitution**: if
  `t.secType === 'OPT'`, it stores
  `{symbol: t.symbol, secType: 'STK', conId: null}` instead.
- `frontend/src/features/positions/PositionsPanel.tsx` — add
  `onClick` on each `<tr>` that calls
  `setSelectedChartTarget({symbol: row.symbol, secType: row.sec_type,
  conId: row.con_id})`. Highlight the active row via a background
  class tied to `selectedChartTarget?.conId === row.con_id`.
- `frontend/src/features/watchlist/WatchlistPanel.tsx` — same but
  simpler: `setSelectedChartTarget({symbol: row.symbol, secType:
  'STK', conId: null})`.
- `frontend/src/features/chart/ChartPane.tsx` — **new component.**
  - `useEffect` on `selectedChartTarget` → `GET /api/history` with
    either `?con_id=N` or `?symbol=X&sec_type=STK`.
  - `chart.addLineSeries().setData(...)` with the returned bars.
  - `chart.timeScale().setVisibleLogicalRange(...)` to initial-
    zoom to the last 6 h.
  - Small "Reset" button in a corner → re-applies the 6 h window.
  - `setInterval(30_000)` while mounted: refetch and `update()`
    the series. Before applying, capture the current visible
    range; reapply it after if the user had zoomed elsewhere so
    the refresh doesn't yank the viewport.
  - Header line above the chart: `"<symbol> · <secType>"`.
  - Renders a placeholder when `selectedChartTarget === null`.
  - Theme colors from the existing `var(--accent-*)` CSS tokens.
- `frontend/src/layout/ComponentFactory.tsx` — register
  `'chart'` → `<ChartPane />` in the switch.
- `frontend/src/layout/variants.ts` — add a `chart` tab slot near
  Positions. Include `MIGRATED_TABS` handling (same as Bot Trades
  got) so users with persisted layouts from before this change
  still see the pane.
- `frontend/src/api/client.ts` — `getHistory({conId?, symbol?,
  secType?, hours})` wrapper. Typed return: `HistoryBar[]`.

## Design decisions

- **On-demand contract qualification.** Any symbol clickable in
  the UI (including watchlist additions not in `symbols.yaml`)
  is chartable; `qualify_contract` result is already cached in
  SQLite per the existing flow.
- **`conId` is the primary identifier** passed to the history
  endpoint. It's unambiguous per IB contract and already carried
  on every Positions row. Symbol-only fallback exists for
  watchlist clicks where we haven't qualified yet.
- **Options chart the underlying, not the premium.** One-line
  substitution in the store setter. The chart header says "STK"
  for the clicked OPT row so the user isn't confused.
- **Close-price line, not candles.** One `addLineSeries` call;
  matches the "eyeball" intent; easy to upgrade later.
- **24 h preload, 6 h initial window.** Single IB call per
  selection; 1-min × 24 h = 1440 points; trivial for the chart
  lib to render.
- **Refetch every 30 s** rather than incrementally appending live
  5-second bars off Redis. Simpler first cut.
- **Shared Zustand `selectedChartTarget` state** — carries `conId`
  so futures chart correctly and the OPT→STK substitution has
  somewhere to land. Matches the existing cross-pane store
  pattern.
- **Engine-side TTL cache (30 s)** rather than client-side
  throttling — respects IB rate limits and keeps the chart cheap.

## Acceptance criteria

- [ ] Clicking a row in Positions or Watchlist highlights it and
      updates the chart.
- [ ] Chart shows a close-price line, last 6 h visible, 24 h of
      1-min bars available via zoom / pan.
- [ ] Mouse-wheel zoom on time axis; click-drag pan; "Reset"
      button restores the 6 h default.
- [ ] Auto-refresh every 30 s without stealing current zoom.
- [ ] STK positions chart directly.
- [ ] FUT positions chart by `con_id`; header says `"<symbol> · FUT"`.
- [ ] OPT positions chart the **underlying stock**; header says
      `"<symbol> · STK"`, not the option premium.
- [ ] Overnight / extended-hours data works (`useRTH=False`).
- [ ] Placeholder shown when nothing is selected.
- [ ] No behavior change to existing order/fill/ledger paths.

## Out of scope (v1)

- Candle / volume rendering.
- User-adjustable resolution buttons (1 m / 5 m / 15 m).
- Paging in older data on zoom-out past the 24 h preload.
- Live sub-minute updates from the 5-second Redis stream.
- Drawing tools, indicators, multi-symbol overlay.
- Persisting the selected symbol across reloads.

## Risk summary

- **Zero backend risk** on hot paths (orders, fills, ledger,
  commission hooks). New endpoints, new method, new router —
  orthogonal to the trading engine. The only IB API added is
  `reqHistoricalDataAsync`, which is read-only.
- **Two panels become clickable** (Positions, Watchlist). Rows
  previously did nothing on click; now they dispatch a symbol
  selection. No other UX flow is affected.
- **Layout migration** — the new `chart` tab in
  `layout/variants.ts` needs `MIGRATED_TABS` handling for users
  with persisted layouts, same pattern used for Bot Trades.
