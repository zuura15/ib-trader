import { useEffect, useRef } from 'react';
import { useVisibilityWake } from '../hooks/useVisibilityWake';

// Shared shape of the Redis bot FSM doc as streamed over /ws.
// Additional fields (e.g. ``armed``, ``entry_line`` for chart_signal) flow
// through because the runtime pushes the raw doc; consumers can extend
// this interface or use ``[key: string]: unknown`` for ad-hoc reads.
export interface BotPositionState {
  position_state?: string;
  state?: string;
  entry_price?: string;
  trade_serial?: number;
  qty?: string;
  filled_qty?: string;
  last_price?: string;
  high_water_mark?: string;
  current_stop?: string;
  hard_stop?: string;
  trail_activation_price?: string;
  trail_width_pct?: string;
  trail_activated?: boolean;
  entry_time?: string;
  symbol?: string;
  // chart_signal additions
  armed?: boolean;
  entry_line?: {
    kind?: string;
    direction?: string;
    slope_per_bar?: number;
    intercept?: number;
    slope_per_sec?: number;
    anchor_time?: string;
    anchor_price?: number;
    anchor_b_idx?: number;
    /** Wallclock ISO of Q — the older construction pivot. Used by
     *  the chart to clip the entry-line render at the start of the
     *  bot's validation window so we don't backward-project across
     *  hours of unrelated price action. */
    from_time?: string;
    from_idx?: number;
    touches?: number;
  } | null;
  entry_bar_time?: string;
  unrealized_pnl?: string;
  /** Authoritative direction the runtime writes when ``on_entry_filled``
   *  records the entry. "LONG" for a BUY entry, "SHORT" for a SELL
   *  entry. Prefer this over ``entry_line.direction`` — the strategy
   *  may rewrite the line on later bars while the position is held. */
  position_direction?: 'LONG' | 'SHORT';
  /** Trailing-dip state surfaced by chart_signal (and set by the
   *  runtime's ``_apply_fill`` on entry). ``active_stop`` is the
   *  effective stop = max(line_value, trail_stop) for longs,
   *  min(...) for shorts. PositionStrip prefers this over the
   *  projected line value so the operator sees the actual exit
   *  trigger. ``exit_reason`` records which gate fired on close. */
  high_water_mark?: string;
  low_water_mark?: string;
  active_stop?: string;
  exit_reason?: 'line_breach' | 'trail_stop' | 'both' | null;
  // Catch-all for the rest of the Redis doc
  [key: string]: unknown;
}

export interface UseBotStateOptions {
  onBotState?: (s: BotPositionState) => void;
  onQuote?: (data: { bid?: string; ask?: string; last?: string }) => void;
  subscribeBot?: boolean;
  subscribeQuote?: boolean;
}

/**
 * Opens a /ws connection for one (symbol, bot_ref) pair, subscribes to
 * the requested streams, and reconnects with exponential backoff on
 * disconnect (cap 30s). Callbacks live on a ref so callers don't need
 * to memoise them.
 *
 * Extracted from ``BotsPanel`` so the new ``ChartBotPane`` can share the
 * same lifecycle. Both panes still use one socket each — multiplexing
 * is a future optimisation.
 */
export function useBotState(
  symbol: string,
  botRef: string | undefined,
  opts: UseBotStateOptions,
): void {
  const optsRef = useRef(opts);
  optsRef.current = opts;
  // Lifted reopen handle so the visibility-wake listener can force a
  // reconnect when the tab returns from background. The effect below
  // sets it on mount and clears it on cleanup.
  const wakeRef = useRef<() => void>(() => {});
  useVisibilityWake(() => wakeRef.current());

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${window.location.host}/ws`);
      ws.onopen = () => {
        attempt = 0;
        const sub = optsRef.current;
        if (sub.subscribeBot && botRef) {
          ws!.send(JSON.stringify({ type: 'subscribe_bot', bot_ref: botRef, symbol }));
        }
        if (sub.subscribeQuote) {
          ws!.send(JSON.stringify({ type: 'subscribe_quote', symbol }));
        }
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          const cb = optsRef.current;
          if (msg.type === 'bot_state' && msg.symbol === symbol && msg.strategy) {
            cb.onBotState?.(msg.strategy);
          } else if (msg.type === 'quote' && msg.symbol === symbol) {
            cb.onQuote?.(msg.data);
          }
        } catch {
          /* malformed frame — ignore */
        }
      };
      ws.onclose = () => {
        ws = null;
        if (cancelled) return;
        // Zero the consumer's view so the UI shows "—" until a fresh
        // snapshot lands on reconnect. Without this, the last-known
        // state lingered visibly during the outage and could mislead
        // the operator into trusting stale entry_line / qty / P&L.
        try {
          optsRef.current.onBotState?.({});
        } catch {
          /* consumer threw — ignore */
        }
        const delay = Math.min(30_000, 1000 * Math.pow(2, attempt));
        attempt++;
        timer = setTimeout(connect, delay);
      };
      ws.onerror = () => {
        ws?.close();
      };
    };

    connect();
    wakeRef.current = () => {
      if (cancelled) return;
      // Drop the throttled-tab socket and any pending backoff so the
      // user sees a fresh snapshot immediately on tab return.
      attempt = 0;
      if (timer) { clearTimeout(timer); timer = null; }
      if (ws) {
        ws.onclose = null;
        try { ws.close(); } catch { /* already closed */ }
        ws = null;
      }
      connect();
    };
    return () => {
      cancelled = true;
      wakeRef.current = () => {};
      if (timer) clearTimeout(timer);
      if (ws) ws.close();
    };
  }, [symbol, botRef]);
}
