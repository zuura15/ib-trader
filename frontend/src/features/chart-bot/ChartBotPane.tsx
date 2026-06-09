import { useCallback, useEffect, useMemo, useState } from 'react';
import { BotChart } from './BotChart';
import type { ChartTarget } from '../../data/store';
import { useStore } from '../../data/store';

interface Props {
  /** Trader-layout slot 1..4. Bound to bot id ``chart-bot-<slot>``. */
  slot: number;
}

interface BotApiShape {
  id: string;
  name?: string;
  ref_id?: string;
  symbols_json?: string;
  sec_type?: string;
}

function botIdForSlot(slot: number): string {
  return `chart-bot-${slot}`;
}

const QTY_STORAGE_KEY = (slot: number) => `ib-chart-qty-${slot}`;

// Futures localSymbol regex (mirror of ``commission_estimates.py``
// ``_LOCAL_SYM_RE``): root + month-letter + 1-or-2-digit year. Year
// limited to two digits so we don't false-positive an unrelated
// long-alpha-numeric ticker as a futures symbol.
const FUT_LOCAL_SYM_RE = /^([A-Z][A-Z0-9]{0,4}?)([FGHJKMNQUVXZ])(\d{1,2})$/;

/** Strip the month+year suffix from a futures localSymbol. Returns
 *  the input upper-cased unchanged for STK / non-futures shapes. */
function chartSymbolRoot(symbol: string): string {
  const u = symbol.toUpperCase().trim();
  const m = u.match(FUT_LOCAL_SYM_RE);
  return m ? m[1] : u;
}

/** Map a CME futures month integer (1-12) to its month-code letter.
 *  Returns null when the month is invalid. */
const MONTH_CODES = [
  null, 'F', 'G', 'H', 'J', 'K', 'M',
  'N', 'Q', 'U', 'V', 'X', 'Z',
] as const;
function monthLetterFromExpiry(expiry: string | null | undefined): string | null {
  if (!expiry || expiry.length < 6) return null;
  const monthNum = parseInt(expiry.slice(4, 6), 10);
  if (!Number.isInteger(monthNum) || monthNum < 1 || monthNum > 12) return null;
  return MONTH_CODES[monthNum] ?? null;
}

/** Reconstruct a futures localSymbol (``NQM6``) from a position row's
 *  IB root + expiry. Returns null when fields are missing or malformed
 *  so the caller can fall back to the chart symbol. */
function positionLocalSymbol(p: { symbol?: string | null; expiry?: string | null }): string | null {
  const root = (p.symbol ?? '').toString().toUpperCase();
  if (!root) return null;
  const expiry = (p.expiry ?? '').toString();
  const monthLetter = monthLetterFromExpiry(expiry);
  if (!monthLetter) return null;
  // Use last-digit-of-year to match the IB-paste localSymbol shape
  // operators type (``NQM6``, not ``NQM26``). The qualifier accepts
  // both; we pick the short form to keep the console line readable.
  const yearLast = expiry.slice(3, 4);
  return `${root}${monthLetter}${yearLast}`;
}

function parseFinite(v: unknown): number | null {
  if (v == null) return null;
  const n = typeof v === 'number' ? v : Number(String(v));
  return Number.isFinite(n) ? n : null;
}

function loadQty(slot: number): number {
  try {
    const raw = localStorage.getItem(QTY_STORAGE_KEY(slot));
    if (!raw) return 1;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 1;
  } catch { return 1; }
}

/**
 * Desktop Trader-layout chart pane. The bot's FSM lifecycle is
 * intentionally hidden — the chart is always live for manual scanning
 * (chart-bot-N.yaml is configured with ``manual_entry_only: true`` and
 * ``auto_start: false``, so the bot never auto-fires entries or exits).
 *
 * Operator-facing controls are a single ``[qty] [BUY] [SELL] [CLOSE]``
 * strip below the chart. Every click routes through the console
 * (``addCommand`` → /api/commands → engine) so each manual order is
 * recorded identically to a typed command — same audit trail, same
 * fill handlers, same 24h P&L attribution. Auto-fire on click: the
 * click IS the operator's confirmation, no Enter required.
 *
 * Close = net-flat market: reads the current position for this chart's
 * symbol from the store, fires ``buy N <sym> market`` or
 * ``sell N <sym> market`` for the inverse side at the held qty. Button
 * is disabled when no position is held.
 */
export function ChartBotPane({ slot }: Props) {
  const botId = botIdForSlot(slot);
  const [bot, setBot] = useState<BotApiShape | null>(null);
  const [botFetchError, setBotFetchError] = useState<string | null>(null);
  const [qty, setQty] = useState<number>(() => loadQty(slot));
  const [pendingAction, setPendingAction] = useState<
    'buy' | 'sell' | 'close' | null
  >(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  const addCommand = useStore((s) => s.addCommand);
  // ``livePositions`` is the broker-shape (snake_case strings) slice
  // populated by PositionsPanel's WS ``subscribe_positions`` stream.
  // The mock-friendly ``positions`` slice doesn't get live data in
  // live mode — see store.ts comments.
  const positions = useStore((s) => s.livePositions);
  const tradeGroups = useStore((s) => s.tradeGroups);

  // Resolve symbol + secType from the bot config (operator picks via the
  // YAML; live picker is the Variant H plan, not this round).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`/api/bots/${botId}`);
        if (!resp.ok) {
          if (cancelled) return;
          setBot(null);
          setBotFetchError(`bot ${botId} not configured (HTTP ${resp.status})`);
          return;
        }
        const body = await resp.json() as BotApiShape;
        if (cancelled) return;
        setBot(body);
        setBotFetchError(null);
      } catch (e) {
        if (cancelled) return;
        setBotFetchError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [botId]);

  // Persist qty per slot so each chart pane remembers its last-used
  // size across reloads. ``ib-chart-qty-1`` / ``-3`` / ``-4`` —
  // independent per slot.
  useEffect(() => {
    try { localStorage.setItem(QTY_STORAGE_KEY(slot), String(qty)); }
    catch { /* quota — ignore */ }
  }, [qty, slot]);

  const symbol = (() => {
    if (!bot) return null;
    try {
      const arr = bot.symbols_json ? JSON.parse(bot.symbols_json) : [];
      return Array.isArray(arr) && arr.length > 0 ? String(arr[0]) : null;
    } catch { return null; }
  })();
  const secType = (bot?.sec_type ?? 'STK').toUpperCase() as ChartTarget['secType'];

  // Held position for this chart's product, matched by ROOT (not by
  // localSymbol). ``/api/positions`` returns ``symbol="NQ"`` (IB root)
  // and ``display_symbol="NQ M26"`` for futures, but the chart's
  // ``symbol`` is the IB localSymbol form (``NQM6``, ``GCQ6``). A
  // strict equality check never matches. After today's M→U roll the
  // mismatch matters even more: the chart is on ``NQU6`` while the
  // operator still holds ``NQM6`` — Close must target the held
  // contract, not the chart's display contract.
  //
  // Strategy: extract the root from the chart symbol (``NQM6 → NQ``,
  // ``MGCQ6 → MGC``), find a FUT position with the same root, and
  // reconstruct that position's localSymbol from its expiry so the
  // close command goes to the correctly-qualified contract. Non-
  // futures fall through to the plain-symbol equality path.
  const heldPosition = useMemo(() => {
    if (!symbol) return null;
    const chartUpper = symbol.toUpperCase();
    const root = chartSymbolRoot(chartUpper);
    const isFutures = root !== chartUpper;
    for (const raw of positions) {
      // ``livePositions`` is typed as ``Array<Record<string, unknown>>``
      // because it carries the broker-shape (snake_case) rows the WS
      // delivers. Cast once per row to the known shape so the field
      // accesses below stay tidy.
      const p = raw as {
        symbol?: string | null;
        sec_type?: string | null;
        quantity?: string | number | null;
        avg_cost?: string | number | null;
        market_price?: string | number | null;
        multiplier?: string | number | null;
        expiry?: string | null;
        display_symbol?: string | null;
      };
      const pSym = (p.symbol ?? '').toString().toUpperCase();
      const pSecType = (p.sec_type ?? '').toString().toUpperCase();
      let local: string;
      if (isFutures) {
        if (pSecType !== 'FUT') continue;
        if (pSym !== root) continue;
        // Reconstruct the position's localSymbol from its expiry and
        // require an EXACT match against the chart's symbol. Pre-fix
        // we matched by root only — after the M6 → U6 roll the
        // operator's NQU6 chart was showing the held NQM6 position's
        // P&L, which is wrong (different contract, different ticker,
        // different P&L attribution). Strict contract match keeps
        // the overlay honest. Operator manages the old-contract
        // holding via the console (e.g. ``close <serial>``).
        const reconstructed = positionLocalSymbol(p);
        if (!reconstructed || reconstructed !== chartUpper) continue;
        local = reconstructed;
      } else {
        // STK / OPT path: direct symbol equality.
        if (pSym !== chartUpper) continue;
        local = chartUpper;
      }
      const q = parseFinite(p.quantity);
      if (q == null || q === 0) continue;
      // Pull the live mark + entry-avg + multiplier so the overlay
      // can compute unrealized P&L in dollars. Same formula as
      // PositionsPanel.computePnl: (mark - avg) × qty × multiplier.
      // Signed qty handles long/short — a SHORT (qty<0) profits when
      // mark drops below avg, formula produces a positive value.
      const avgCost = parseFinite(p.avg_cost);
      const markPrice = parseFinite(p.market_price);
      const mult = parseFinite(p.multiplier);
      const validMult = mult != null && mult > 0 ? mult : 1;
      const unrealizedPnl =
        avgCost != null && markPrice != null
          ? (markPrice - avgCost) * q * validMult
          : null;
      return {
        qty: q,
        localSymbol: local,
        avgCost,
        markPrice,
        unrealizedPnl,
      };
    }
    return null;
  }, [positions, symbol]);
  const positionQty = heldPosition?.qty ?? 0;

  const fireCommand = async (
    cmd: string, label: 'buy' | 'sell' | 'close',
  ) => {
    if (pendingAction || !symbol) return;
    setPendingAction(label);
    setStatusMsg(`${label}: ${cmd}`);
    try {
      // ``addCommand`` returns synchronously after the optimistic add;
      // the POST + WS subscription happen in the store. We don't await
      // the actual fill here — the operator's feedback for the order
      // status lives in the console pane via the normal channel.
      addCommand(cmd);
      setStatusMsg(`sent: ${cmd}`);
    } catch (e) {
      setStatusMsg(`${label} failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      // Small lockout so accidental double-clicks don't fire twice.
      setTimeout(() => {
        setPendingAction(null);
      }, 250);
      setTimeout(() => setStatusMsg(null), 4000);
    }
  };

  // Command grammar is ``<verb> SYMBOL QTY [STRATEGY] [PROFIT]`` —
  // SYMBOL goes BEFORE qty (see ``ib_trader/repl/commands.py``
  // positional[0]=symbol, positional[1]=qty). The 2026-06-09 build
  // shipped ``buy 1 GCQ6`` which the parser rejected because "1"
  // failed the futures-localSymbol detector. Fixed below.
  const onBuy = () => {
    if (!symbol || qty <= 0) return;
    void fireCommand(`buy ${symbol} ${qty}`, 'buy');
  };
  const onSell = () => {
    if (!symbol || qty <= 0) return;
    void fireCommand(`sell ${symbol} ${qty}`, 'sell');
  };
  const onClose = () => {
    if (!heldPosition) return;
    // Net-flat at IB market price: opposite side for the held qty
    // on the contract the operator actually holds (which may be on
    // a different expiry than the chart is currently showing — e.g.
    // chart on ``NQU6`` while the held position is ``NQM6`` from
    // before the roll).
    const absQty = Math.abs(heldPosition.qty);
    const side = heldPosition.qty > 0 ? 'sell' : 'buy';
    void fireCommand(
      `${side} ${heldPosition.localSymbol} ${absQty} market`, 'close',
    );
  };

  const canClose = positionQty !== 0;
  const positionLabel = positionQty === 0 ? null
    : positionQty > 0 ? `LONG ${positionQty}`
    : `SHORT ${Math.abs(positionQty)}`;

  // Stable callback for ``renderHeader`` so a new identity isn't
  // created on every render — protects ``BotChart``'s ``useEffect``
  // deps from re-firing whenever the parent re-renders (e.g. on
  // every ``livePositions`` WS push, which now fires constantly).
  const renderHeader = useCallback(() => null, []);

  // Memoize the chart subtree so it only re-renders when its own
  // inputs change. Pre-fix, ``ChartBotPane`` subscribed to
  // ``livePositions`` which pushes on every IB position event —
  // ~multi-Hz under active trading. Each parent re-render then
  // re-mounted/re-effected the chart's WS subscription (the
  // ``renderHeader`` literal had a new identity each time), causing
  // the chart's live-tick connection to wedge: subscribe → tear-
  // down before first tick arrived → repeat. Symptom: chart polyline
  // froze even though backend Redis streams were healthy.
  const chartElement = useMemo(() => (
    <BotChart
      botId={botId}
      botRef={bot?.ref_id}
      symbol={symbol}
      secType={secType}
      renderHeader={renderHeader}
    />
  ), [botId, bot?.ref_id, symbol, secType, renderHeader]);

  // 24h realized P&L for this chart's symbol. Pulled from the
  // ``tradeGroups`` store slice (populated from /api/trades, which
  // already prefers ``ib_realized_pnl`` over the engine-computed
  // ``realized_pnl`` per the serializer). Sums CLOSED trades on
  // this symbol whose ``closed_at`` is within the last 24 h.
  const pnl24h = useMemo(() => {
    if (!symbol) return { sum: 0, count: 0 };
    const cutoffMs = Date.now() - 24 * 60 * 60 * 1000;
    let sum = 0;
    let count = 0;
    for (const t of tradeGroups) {
      const tsym = (t.symbol ?? '').toString();
      const tdsym = (t.display_symbol ?? '').toString();
      if (tsym !== symbol && tdsym !== symbol) continue;
      if (t.status !== 'CLOSED') continue;
      if (!t.closedAt) continue;
      const closedMs = new Date(t.closedAt).getTime();
      if (!Number.isFinite(closedMs) || closedMs < cutoffMs) continue;
      const v = t.realizedPnl == null ? NaN : Number(t.realizedPnl);
      if (!Number.isFinite(v)) continue;
      sum += v;
      count += 1;
    }
    return { sum, count };
  }, [tradeGroups, symbol]);

  return (
    <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
      {botFetchError && (
        <div
          style={{
            padding: '6px 10px', fontSize: 11,
            color: 'var(--accent-red)',
            background: 'var(--bg-secondary)',
            borderBottom: '1px solid var(--border-default)',
          }}
        >
          {botFetchError} — add <code>config/bots/{botId}.yaml</code>{' '}
          with <code>strategy_name: chart_signal</code>.
        </div>
      )}
      <div
        className="flex-1"
        style={{ minHeight: 0, position: 'relative' }}
      >
        {chartElement}
        {/* Open-position P&L overlay. Sits in the top-right quadrant
            (top 25% from the top edge, right-justified ~12 px in from
            the right) so it floats above the price polyline without
            covering the most-recent candle. ``pointer-events: none``
            so chart pans / wheel zooms / crosshair clicks pass
            through. Only rendered when a position is actually held;
            otherwise the chart stays clean. */}
        {heldPosition && heldPosition.unrealizedPnl != null && (
          <div
            style={{
              position: 'absolute',
              // Tight to the chart's top edge. ``right`` is set to
              // clear lightweight-charts' price axis column (the
              // right-side labels) — ~62 px on a typical futures
              // price width — so the badge doesn't overlap any
              // tick label or grid notch.
              top: 4,
              right: 70,
              zIndex: 20,
              pointerEvents: 'none',
              padding: '1px 6px',
              borderRadius: 3,
              background: 'rgba(0,0,0,0.45)',
              fontFamily: 'ui-monospace, monospace',
              textAlign: 'right',
              lineHeight: 1.15,
            }}
            data-testid={`chart-pnl-overlay-${slot}`}
          >
            <span style={{
              fontSize: 11, fontWeight: 700,
              color: heldPosition.unrealizedPnl >= 0
                ? 'var(--accent-green)'
                : 'var(--accent-red)',
            }}>
              {heldPosition.unrealizedPnl >= 0 ? '+$' : '-$'}
              {Math.abs(heldPosition.unrealizedPnl).toFixed(2)}
            </span>
          </div>
        )}
      </div>
      {/* Trade strip. 24h P&L for this chart's symbol on the left,
          qty + BUY/SELL/CLOSE pushed to the right under the price
          action. Every click routes through ``addCommand`` ->
          /api/commands so manual chart-pane orders share the same
          audit / fill / P&L path as console-typed orders. */}
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '6px 10px',
          borderTop: '1px solid var(--border-default)',
          background: 'var(--header-bg)',
          flexShrink: 0,
        }}
        data-testid={`chart-trade-strip-${slot}`}
      >
        {/* Left: 24h realized P&L for this symbol. ``—`` when there's
            no closed trade in the last 24h so the slot doesn't look
            like a stale zero. */}
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          fontFamily: 'ui-monospace, monospace',
        }}>
          <span style={{
            fontSize: 10, letterSpacing: '0.15em',
            textTransform: 'uppercase', color: 'var(--text-muted)',
          }}>
            24h P&L
          </span>
          {pnl24h.count > 0 ? (
            <>
              <span style={{
                fontSize: 12, fontWeight: 700,
                color: pnl24h.sum >= 0
                  ? 'var(--accent-green)'
                  : 'var(--accent-red)',
              }}>
                {pnl24h.sum >= 0 ? '+$' : '-$'}
                {Math.abs(pnl24h.sum).toFixed(2)}
              </span>
              <span style={{
                fontSize: 10, color: 'var(--text-muted)',
              }}>
                / {pnl24h.count}
              </span>
            </>
          ) : (
            <span style={{
              fontSize: 12, color: 'var(--text-muted)',
            }}>—</span>
          )}
        </span>

        {/* Status message (transient — appears for 4s after a click). */}
        {statusMsg && (
          <span style={{
            fontSize: 10, color: 'var(--text-muted)',
            fontFamily: 'ui-monospace, monospace',
          }}>
            {statusMsg}
          </span>
        )}

        {/* Diagnostic: tells us at a glance what the chart pane sees.
            ``pos:N`` = livePositions count (publish path OK if >0).
            Then either ``no <root>`` (no root-match for this chart's
            symbol) OR a debug breadcrumb showing avg / mark / qty /
            unrealizedPnl so we can spot which value is null. Remove
            after the chart pane wiring is confirmed end-to-end. */}
        <span style={{
          fontSize: 9, color: 'var(--text-muted)', opacity: 0.6,
          fontFamily: 'ui-monospace, monospace',
        }}
          data-testid={`chart-livepos-debug-${slot}`}
        >
          [pos:{positions.length}
          {symbol && (
            <>
              {' '}
              {heldPosition
                ? `${chartSymbolRoot(symbol)}=${
                    heldPosition.qty > 0 ? '+' : ''}${heldPosition.qty} ` +
                  `avg=${heldPosition.avgCost ?? 'null'} ` +
                  `mark=${heldPosition.markPrice ?? 'null'} ` +
                  `pnl=${heldPosition.unrealizedPnl?.toFixed(2) ?? 'null'}`
                : `no ${chartSymbolRoot(symbol)} held`}
            </>
          )}
          ]
        </span>

        {/* Right: position badge + qty + BUY/SELL/CLOSE. The
            ``marginLeft: auto`` pushes the whole cluster against the
            right edge so it sits under the chart's price-axis column. */}
        <div style={{
          marginLeft: 'auto',
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          {positionLabel && (
            <span style={{
              fontSize: 10, color: 'var(--text-muted)',
              fontFamily: 'ui-monospace, monospace',
            }}>
              {positionLabel}
            </span>
          )}
          <span style={{
            fontSize: 10, letterSpacing: '0.15em',
            textTransform: 'uppercase', color: 'var(--text-muted)',
          }}>
            Qty
          </span>
          <input
            type="number"
            min={1}
            step={1}
            value={qty}
            onChange={(e) => {
              const n = Number(e.target.value);
              setQty(Number.isFinite(n) && n > 0 ? Math.floor(n) : 1);
            }}
            style={{
              width: 56,
              padding: '3px 6px',
              background: 'var(--bg-secondary)',
              border: '1px solid var(--border-default)',
              borderRadius: 3,
              color: 'var(--text-primary)',
              fontFamily: 'ui-monospace, monospace',
              fontSize: 12,
            }}
            data-testid={`chart-qty-${slot}`}
          />
          <button
            onClick={onBuy}
            disabled={pendingAction !== null || !symbol}
            title={symbol
              ? `buy ${symbol} ${qty} (smart_market) — auto-fires via console`
              : 'symbol not resolved yet'}
            style={{
              background: 'var(--accent-green)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '3px 14px',
              fontSize: 11, fontWeight: 700, letterSpacing: '0.05em',
              cursor: pendingAction || !symbol ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'buy' ? 0.5 : 1,
              fontFamily: 'ui-monospace, monospace',
            }}
            data-testid={`chart-buy-${slot}`}
          >
            BUY
          </button>
          <button
            onClick={onSell}
            disabled={pendingAction !== null || !symbol}
            title={symbol
              ? `sell ${symbol} ${qty} (smart_market) — auto-fires via console`
              : 'symbol not resolved yet'}
            style={{
              background: 'var(--accent-red)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '3px 14px',
              fontSize: 11, fontWeight: 700, letterSpacing: '0.05em',
              cursor: pendingAction || !symbol ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'sell' ? 0.5 : 1,
              fontFamily: 'ui-monospace, monospace',
            }}
            data-testid={`chart-sell-${slot}`}
          >
            SELL
          </button>
          <button
            onClick={onClose}
            disabled={pendingAction !== null || !canClose}
            title={canClose
              ? `Close ${positionLabel} ${symbol ?? ''} at market — auto-fires via console`
              : 'No open position on this symbol'}
            style={{
              background: 'var(--accent-blue)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '3px 14px',
              fontSize: 11, fontWeight: 700, letterSpacing: '0.05em',
              cursor: (pendingAction || !canClose) ? 'not-allowed' : 'pointer',
              opacity:
                (pendingAction && pendingAction !== 'close') || !canClose
                  ? 0.5 : 1,
              fontFamily: 'ui-monospace, monospace',
            }}
            data-testid={`chart-close-${slot}`}
          >
            CLOSE
          </button>
        </div>
      </div>
    </div>
  );
}
