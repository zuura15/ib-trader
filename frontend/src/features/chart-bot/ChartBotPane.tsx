import { useEffect, useMemo, useState } from 'react';
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

function loadQty(slot: number): number {
  try {
    const raw = localStorage.getItem(QTY_STORAGE_KEY(slot));
    if (!raw) return 1;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 1;
  } catch { return 1; }
}

// Micro futures roots. The operator trades micros in larger clip
// sizes (10s) than full-size contracts (1s), so the qty spinner
// steps by 10 for these and by 1 for everything else.
const MICRO_ROOTS = new Set([
  'MES', 'MNQ', 'MGC', 'MCL', 'M2K', 'MYM',
  'MBT', 'M6E', 'M6A', 'M6B', 'MET', 'MNG', 'MHG',
]);
// Futures localSymbol = root + month-letter (F G H J K M N Q U V X Z)
// + 1-2 digit year, e.g. ``MNQM6`` → root ``MNQ``.
const FUT_LOCAL_SYM_RE = /^([A-Z][A-Z0-9]{0,4}?)[FGHJKMNQUVXZ]\d{1,2}$/;

/** Step size for the qty spinner: 10 for micro futures, 1 otherwise. */
function qtyStepFor(symbol: string | null): number {
  if (!symbol) return 1;
  const m = symbol.toUpperCase().match(FUT_LOCAL_SYM_RE);
  const root = m ? m[1] : symbol.toUpperCase();
  return MICRO_ROOTS.has(root) ? 10 : 1;
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
  // Per-contract qty+P&L published by PositionsPanel (the single
  // calculation — see publishChartPnl there). Empty when the panel
  // isn't mounted or no positions are held; chart treats "no entry"
  // as flat (no P&L shown, Close disabled). Updates at the server's
  // positions-push cadence (~2 Hz max) — re-renders here are cheap
  // and the chart's own subscriptions key on primitive strings, so
  // they never re-fire from a parent re-render.
  const chartPnl = useStore((s) => s.chartPnl);
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
  // Qty spinner step: micros click up/down by 10, full-size by 1.
  // ``min`` is set to the same value so the spinner lands on clean
  // multiples (10/20/30 for micros) instead of 1/11/21.
  const qtyStep = qtyStepFor(symbol);

  // Held position for THIS exact contract (map is keyed by localSymbol
  // on the publisher side, so a plain lookup is the whole match —
  // NQM6 holdings show on the NQM6 pane only, never on NQU6).
  // Signed qty: positive = LONG, negative = SHORT, absent = flat.
  const pnlEntry = symbol ? chartPnl[symbol] : undefined;
  const positionQty = pnlEntry?.qty ?? 0;
  const livePnl = pnlEntry?.pnl ?? null;

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
    if (!symbol || positionQty === 0) return;
    // Net-flat at IB market price: opposite side for the held qty.
    // Uses explicit ``market`` strategy to skip the smart_market
    // walker — Close should be immediate, not patient.
    const absQty = Math.abs(positionQty);
    const side = positionQty > 0 ? 'sell' : 'buy';
    void fireCommand(`${side} ${symbol} ${absQty} market`, 'close');
  };

  const canClose = positionQty !== 0;
  const positionLabel = positionQty === 0 ? null
    : positionQty > 0 ? `LONG ${positionQty}`
    : `SHORT ${Math.abs(positionQty)}`;

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
      <div className="flex-1" style={{ minHeight: 0, position: 'relative' }}>
        <BotChart
          botId={botId}
          botRef={bot?.ref_id}
          symbol={symbol}
          secType={secType}
          // FSM header explicitly suppressed — chart pane is always live
          // for manual scanning. The bot lifecycle still cycles in the
          // background (manual_entry_only=true drops any auto-entries)
          // but we don't surface it on the chart.
          renderHeader={() => null}
        />
        {/* Live P&L for the open position on this exact contract.
            Free-floating colored number, bottom-right of the plot
            area — inset from the right so it clears the price axis,
            and from the bottom so it clears the time axis.
            ``pointer-events: none`` so chart pan/zoom/crosshair pass
            straight through. Hidden when flat or when the publisher
            (PositionsPanel) isn't mounted. */}
        {livePnl != null && positionQty !== 0 && (
          <span
            style={{
              position: 'absolute',
              bottom: 28,
              right: 72,
              zIndex: 15,
              pointerEvents: 'none',
              fontFamily: 'ui-monospace, monospace',
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: '0.05em',
              color: livePnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
            }}
            data-testid={`chart-live-pnl-${slot}`}
          >
            {livePnl >= 0 ? '+$' : '-$'}{Math.abs(livePnl).toFixed(2)}
          </span>
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
            min={qtyStep}
            step={qtyStep}
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
