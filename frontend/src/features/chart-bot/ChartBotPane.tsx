import { useEffect, useRef, useState } from 'react';
import { BotChart } from './BotChart';
import type { ChartTarget } from '../../data/store';
import { useStore } from '../../data/store';

interface Props {
  /** Trader-layout slot 1..4. Bound to bot id ``chart-bot-<slot>``. */
  slot: number;
  /** Compact chrome for the mobile two-chart view: B/S/C button labels,
   *  larger touch targets, and no per-pane realized-P&L strip (that
   *  figure lives once in the mobile header). Defaults to the full
   *  desktop chrome. */
  compact?: boolean;
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

// Exchange tick sizes by futures root — drives the click-to-close
// strip's price rounding so a drafted limit is always on-tick (IB
// rejects off-tick limits). Extend as new contracts get chart slots;
// unknown roots fall back to 0.01, the safest common denominator.
const TICK_SIZE_BY_ROOT: Record<string, number> = {
  GC: 0.1, MGC: 0.1,
  ES: 0.25, MES: 0.25,
  NQ: 0.25, MNQ: 0.25,
  CL: 0.01, MCL: 0.01,
  SI: 0.005, RTY: 0.1, M2K: 0.1, YM: 1, MYM: 1,
};

/** Tick size for the click-to-close strip's price rounding. */
function tickSizeFor(symbol: string | null): number {
  if (!symbol) return 0.01;
  const m = symbol.toUpperCase().match(FUT_LOCAL_SYM_RE);
  const root = m ? m[1] : symbol.toUpperCase();
  return TICK_SIZE_BY_ROOT[root] ?? 0.01;
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
export function ChartBotPane({ slot, compact = false }: Props) {
  const botId = botIdForSlot(slot);
  const [bot, setBot] = useState<BotApiShape | null>(null);
  const [botFetchError, setBotFetchError] = useState<string | null>(null);
  const [qty, setQty] = useState<number>(() => loadQty(slot));
  const [pendingAction, setPendingAction] = useState<
    'buy' | 'sell' | 'close' | null
  >(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  // True while BotChart's fullscreen overlay is active. When true the
  // trade strip renders INSIDE that overlay (passed as fullscreenFooter)
  // rather than in normal flow, so it's not hidden behind the z-9999
  // cover — and never duplicated.
  const [isFullscreen, setIsFullscreen] = useState(false);

  const addCommand = useStore((s) => s.addCommand);
  const setConsoleDraft = useStore((s) => s.setConsoleDraft);
  // Command list — watched so an order button stays locked until the
  // order it fired reaches a terminal state (fill / reject), giving real
  // feedback and preventing the "no response → mash SELL 5× → 5 orders"
  // incident. Cleared by the effect below on terminal, or a safety timer.
  const commands = useStore((s) => s.commands);
  const pendingCmdIdRef = useRef<string | null>(null);
  const pendingTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Per-contract qty+P&L published by PositionsPanel (the single
  // calculation — see publishChartPnl there). Empty when the panel
  // isn't mounted or no positions are held; chart treats "no entry"
  // as flat (no P&L shown, Close disabled). Updates at the server's
  // positions-push cadence (~2 Hz max) — re-renders here are cheap
  // and the chart's own subscriptions key on primitive strings, so
  // they never re-fire from a parent re-render.
  const chartPnl = useStore((s) => s.chartPnl);

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
    // Hard-block while an order from this pane is still in flight. This
    // is the core guard: buttons stay disabled (see below) until the
    // order terminalizes, so a stuck/unresponsive engine can't be
    // spammed into placing duplicate orders.
    if (pendingAction || !symbol) return;
    setPendingAction(label);
    setStatusMsg(`${label}: working…`);
    try {
      // ``addCommand`` returns the command id synchronously (optimistic
      // add); the POST + WS status updates happen in the store. We track
      // that id and release the lock only when it reaches a terminal
      // state (the watch effect below) — not on a fixed timer.
      const id = addCommand(cmd);
      pendingCmdIdRef.current = id;
      // Safety net only: if no terminal status ever arrives (dropped
      // update / engine down), re-enable after a window LONGER than the
      // max order-wait so it never races a legitimately-walking order.
      clearTimeout(pendingTimerRef.current);
      pendingTimerRef.current = setTimeout(() => {
        if (pendingCmdIdRef.current !== id) return;
        pendingCmdIdRef.current = null;
        setPendingAction(null);
        setStatusMsg('no confirmation — check console before re-sending');
      }, 150_000);
    } catch (e) {
      pendingCmdIdRef.current = null;
      setPendingAction(null);
      setStatusMsg(`${label} failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Release the in-flight lock as soon as the order this pane fired
  // reaches a terminal state, and surface the outcome.
  useEffect(() => {
    const id = pendingCmdIdRef.current;
    if (!id) return;
    const c = commands.find((x) => x.id === id);
    if (!c || (c.status !== 'success' && c.status !== 'failure')) return;
    pendingCmdIdRef.current = null;
    clearTimeout(pendingTimerRef.current);
    setPendingAction(null);
    setStatusMsg(c.status === 'success' ? `done: ${c.command}` : `failed: ${c.command}`);
    setTimeout(() => setStatusMsg(null), 4000);
  }, [commands]);

  // Clear the safety timer if the pane unmounts mid-order.
  useEffect(() => () => clearTimeout(pendingTimerRef.current), []);

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
    // Net-flat with the session-aware smart_market algo (walks toward
    // the far side then crosses) — same execution the console default
    // uses, so chart closes match typed closes. Opposite side for the
    // held qty.
    const absQty = Math.abs(positionQty);
    const side = positionQty > 0 ? 'sell' : 'buy';
    void fireCommand(`${side} ${symbol} ${absQty} smart_market`, 'close');
  };

  // Click-to-close price strip (SymbolChart renders it hugging the
  // price axis). A click DRAFTS the full-position closing limit at the
  // picked price into the console input — never transmits. The console
  // is the single arm/fire point; the operator reviews and hits Enter.
  const pickTickSize = tickSizeFor(symbol);
  const onPricePick = (price: number) => {
    if (!symbol) return;
    if (positionQty === 0) {
      setStatusMsg('no position — nothing to close');
      setTimeout(() => setStatusMsg(null), 3000);
      return;
    }
    const decimals = (() => {
      const s = String(pickTickSize);
      const dot = s.indexOf('.');
      return dot < 0 ? 0 : s.length - dot - 1;
    })();
    const side = positionQty > 0 ? 'sell' : 'buy';
    const absQty = Math.abs(positionQty);
    const priceTxt = price.toFixed(decimals);
    setConsoleDraft(`${side} ${symbol} ${absQty} limit ${priceTxt}`);
    setStatusMsg(`drafted: ${side} ${absQty} @ ${priceTxt} — Enter in console to send`);
    setTimeout(() => setStatusMsg(null), 5000);
  };

  const canClose = positionQty !== 0;
  const positionLabel = positionQty === 0 ? null
    : positionQty > 0 ? `LONG ${positionQty}`
    : `SHORT ${Math.abs(positionQty)}`;

  // Realized-P&L rollup (24h + today/session), IB-authoritative and
  // account-wide — includes trades placed directly in TWS, not just
  // orders our system originated. Self-contained per-pane poll of
  // ``/api/chart/pnl-rollup`` (~30 s, abort-bounded): no shared store,
  // no WS, nothing the chart canvas depends on. The whole small map is
  // returned; we read our own symbol's entry. Replaces the old
  // tradeGroups-based 24h sum, which couldn't see manual TWS fills.
  const [pnlRollup, setPnlRollup] = useState<
    Record<string, { pnl_24h: number; pnl_session: number | null; sec_type: string }>
  >({});
  useEffect(() => {
    // Compact (mobile) panes show no per-pane P&L — the single 24h
    // figure lives in the header — so skip the poll entirely.
    if (compact) return;
    let cancelled = false;
    const poll = async () => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 4000);
      try {
        const r = await fetch('/api/chart/pnl-rollup', { signal: ctrl.signal });
        if (!r.ok) return;
        const data = await r.json();
        if (!cancelled && data && typeof data === 'object') setPnlRollup(data);
      } catch { /* transient — keep last good values */ }
      finally { clearTimeout(timer); }
    };
    poll();
    const id = window.setInterval(poll, 30_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [compact]);
  const rollupEntry = symbol ? pnlRollup[symbol] : undefined;
  const pnl24h = rollupEntry ? rollupEntry.pnl_24h : null;
  const pnlSession = rollupEntry ? rollupEntry.pnl_session : null;

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
          onFullscreenChange={setIsFullscreen}
          fullscreenFooter={renderTradeStrip()}
          onPricePick={onPricePick}
          pickTickSize={pickTickSize}
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
        {/* Transient action status (BUY/SELL/CLOSE click feedback),
            overlaid just above the live-P&L number — grey and
            ``pointer-events: none`` so it floats over the plot and never
            reflows the trade strip or resizes the pane. Right-anchored
            and clipped to the plot width so a long error can't run off
            the left edge; ellipsifies instead. */}
        {statusMsg && (
          <span
            style={{
              position: 'absolute',
              bottom: 46,
              right: 72,
              zIndex: 15,
              pointerEvents: 'none',
              display: 'inline-block',
              maxWidth: 'calc(100% - 84px)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              textAlign: 'right',
              fontFamily: 'ui-monospace, monospace',
              fontSize: 10,
              letterSpacing: '0.03em',
              color: 'var(--text-muted)',
            }}
            data-testid={`chart-status-${slot}`}
          >
            {statusMsg}
          </span>
        )}
      </div>
      {/* Trade strip — in fullscreen it renders inside BotChart's overlay
          (passed as fullscreenFooter, reachable over the z-9999 cover);
          here only when not fullscreen, so it's never hidden behind the
          overlay nor duplicated. */}
      {!isFullscreen && renderTradeStrip()}
    </div>
  );

  // Hoisted so it can be referenced above in both the fullscreenFooter
  // prop and the normal-flow render. 24h P&L on the left, qty +
  // BUY/SELL/CLOSE on the right. Every click routes through ``addCommand``
  // -> /api/commands so manual chart-pane orders share the same audit /
  // fill / P&L path as console-typed orders.
  function renderTradeStrip() {
    return (
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
        {/* Left: realized P&L — TODAY (since the futures session open,
            FUT only) and 24H. Both IB-authoritative and account-wide,
            so trades placed directly in TWS are included. ``—`` when no
            realized activity so the slot doesn't read as a stale zero.
            Hidden in compact (mobile) — the header carries one 24h. */}
        {!compact && (
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 10,
          fontFamily: 'ui-monospace, monospace',
        }}>
          {pnlSession != null && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span style={{
                fontSize: 10, letterSpacing: '0.15em',
                textTransform: 'uppercase', color: 'var(--text-muted)',
              }}>
                Today
              </span>
              <span style={{
                fontSize: 12, fontWeight: 700,
                color: pnlSession >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
              }}>
                {pnlSession >= 0 ? '+$' : '-$'}{Math.abs(pnlSession).toFixed(2)}
              </span>
            </span>
          )}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <span style={{
              fontSize: 10, letterSpacing: '0.15em',
              textTransform: 'uppercase', color: 'var(--text-muted)',
            }}>
              24h
            </span>
            {pnl24h != null ? (
              <span style={{
                fontSize: 12, fontWeight: 700,
                color: pnl24h >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
              }}>
                {pnl24h >= 0 ? '+$' : '-$'}{Math.abs(pnl24h).toFixed(2)}
              </span>
            ) : (
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>—</span>
            )}
          </span>
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
          {!compact && (
            <span style={{
              fontSize: 10, letterSpacing: '0.15em',
              textTransform: 'uppercase', color: 'var(--text-muted)',
            }}>
              Qty
            </span>
          )}
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
            style={{
              background: 'var(--accent-green)', color: '#fff',
              border: 'none', borderRadius: 3,
              padding: compact ? '9px 0' : '3px 14px',
              minWidth: compact ? 42 : undefined,
              fontSize: compact ? 15 : 11, fontWeight: 700, letterSpacing: '0.05em',
              cursor: pendingAction || !symbol ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'buy' ? 0.5 : 1,
              fontFamily: 'ui-monospace, monospace',
            }}
            data-testid={`chart-buy-${slot}`}
          >
            {compact ? 'B' : 'BUY'}
          </button>
          <button
            onClick={onSell}
            disabled={pendingAction !== null || !symbol}
            style={{
              background: 'var(--accent-red)', color: '#fff',
              border: 'none', borderRadius: 3,
              padding: compact ? '9px 0' : '3px 14px',
              minWidth: compact ? 42 : undefined,
              fontSize: compact ? 15 : 11, fontWeight: 700, letterSpacing: '0.05em',
              cursor: pendingAction || !symbol ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'sell' ? 0.5 : 1,
              fontFamily: 'ui-monospace, monospace',
            }}
            data-testid={`chart-sell-${slot}`}
          >
            {compact ? 'S' : 'SELL'}
          </button>
          <button
            onClick={onClose}
            disabled={pendingAction !== null || !canClose}
            style={{
              background: 'var(--accent-blue)', color: '#fff',
              border: 'none', borderRadius: 3,
              padding: compact ? '9px 0' : '3px 14px',
              minWidth: compact ? 42 : undefined,
              fontSize: compact ? 15 : 11, fontWeight: 700, letterSpacing: '0.05em',
              cursor: (pendingAction || !canClose) ? 'not-allowed' : 'pointer',
              opacity:
                (pendingAction && pendingAction !== 'close') || !canClose
                  ? 0.5 : 1,
              fontFamily: 'ui-monospace, monospace',
            }}
            data-testid={`chart-close-${slot}`}
          >
            {compact ? 'C' : 'CLOSE'}
          </button>
        </div>
      </div>
    );
  }
}
