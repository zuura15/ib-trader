import { useEffect, useState } from 'react';
import { BotChart } from './BotChart';
import type { ChartTarget } from '../../data/store';
import type { BotPositionState } from '../../data/useBotState';

interface BotApiShape {
  id: string;
  name?: string;
  ref_id?: string;
  symbols_json?: string;
  sec_type?: string;
  state?: string;
  config?: Record<string, unknown>;
}

const SLOTS = [1, 2, 3, 4] as const;
type Slot = (typeof SLOTS)[number];

const POSITION_STATES = new Set([
  'ENTRY_ORDER_PLACED',
  'AWAITING_EXIT_TRIGGER',
  'EXIT_ORDER_PLACED',
]);

async function postBotAction(
  botId: string, path: 'force-quit' | 'rearm' | 'start' | 'stop',
): Promise<{ ok: boolean; detail?: string }> {
  try {
    const resp = await fetch(`/api/bots/${botId}/${path}`, { method: 'POST' });
    if (resp.ok) return { ok: true };
    let detail = `${resp.status}`;
    try {
      const body = await resp.json();
      detail = body?.detail ?? detail;
    } catch { /* ignore */ }
    return { ok: false, detail };
  } catch (e) {
    return { ok: false, detail: e instanceof Error ? e.message : String(e) };
  }
}

/**
 * Mobile Charts tab — one BotChart at a time, with a 1/2/3/4 slot
 * picker across the top. Reuses the same ``BotChart`` as desktop so
 * bot overlays + position strip behave identically. Touch-friendly
 * Force-quit / Re-arm buttons live in the BotChart header.
 *
 * Slot selection is persisted to localStorage so a reload returns to
 * the last viewed chart.
 */
export function MobileChartBots() {
  const [activeSlot, setActiveSlotRaw] = useState<Slot>(() => {
    const saved = Number(localStorage.getItem('ib-mobile-chart-slot') ?? '1');
    return (SLOTS as readonly number[]).includes(saved) ? (saved as Slot) : 1;
  });
  const setActiveSlot = (s: Slot) => {
    localStorage.setItem('ib-mobile-chart-slot', String(s));
    // Clear the previous slot's bot details immediately so the chart
    // doesn't briefly render the wrong symbol while the new fetch is
    // in flight.
    setBot(null);
    setBotFetchError(null);
    setActiveSlotRaw(s);
  };

  const botId = `chart-bot-${activeSlot}`;
  const [bot, setBot] = useState<BotApiShape | null>(null);
  const [botFetchError, setBotFetchError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`/api/bots/${botId}`);
        if (!resp.ok) {
          if (cancelled) return;
          setBotFetchError(`bot ${botId} not configured (HTTP ${resp.status})`);
          return;
        }
        const body = await resp.json() as BotApiShape;
        if (cancelled) return;
        setBot(body);
      } catch (e) {
        if (cancelled) return;
        setBotFetchError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [botId]);

  const symbol = (() => {
    if (!bot) return null;
    try {
      const arr = bot.symbols_json ? JSON.parse(bot.symbols_json) : [];
      return Array.isArray(arr) && arr.length > 0 ? String(arr[0]) : null;
    } catch { return null; }
  })();
  // ``/api/bots/{id}`` exposes ``sec_type`` at top level.
  const secType = (bot?.sec_type ?? 'STK').toUpperCase() as ChartTarget['secType'];

  const onForceQuit = async () => {
    setActionMsg('Closing…');
    const res = await postBotAction(botId, 'force-quit');
    setActionMsg(res.ok ? 'Quit sent.' : `Quit failed: ${res.detail}`);
    setTimeout(() => setActionMsg(null), 4000);
  };
  const onRearm = async () => {
    setActionMsg('Re-arming…');
    const res = await postBotAction(botId, 'rearm');
    setActionMsg(res.ok ? 'Armed.' : `Re-arm failed: ${res.detail}`);
    setTimeout(() => setActionMsg(null), 4000);
  };
  const onStart = async () => {
    setActionMsg('Starting…');
    const res = await postBotAction(botId, 'start');
    setActionMsg(res.ok ? 'Started.' : `Start failed: ${res.detail}`);
    setTimeout(() => setActionMsg(null), 4000);
  };

  const renderHeader = (state: BotPositionState) => {
    const fsmState = (state.state as string | undefined) ?? 'UNKNOWN';
    const armed = state.armed ?? false;
    const hasPosition = POSITION_STATES.has(fsmState);
    const isStopped = fsmState === 'OFF' || fsmState === 'STOPPED'
      || fsmState === 'UNKNOWN' || fsmState === 'ERRORED';
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
        <span style={{ color: 'var(--text-muted)' }}>
          {fsmState.replace(/_/g, ' ')}{!isStopped && !armed ? ' · OFF' : ''}
        </span>
        {isStopped && (
          <button
            onClick={onStart}
            style={{
              background: 'var(--accent-green)', color: '#fff', border: 'none',
              borderRadius: 4, padding: '6px 10px',
              fontSize: 13, fontWeight: 700, minHeight: 32, cursor: 'pointer',
            }}
          >Start</button>
        )}
        {hasPosition && (
          <button
            onClick={onForceQuit}
            style={{
              background: 'var(--accent-red)', color: '#fff', border: 'none',
              borderRadius: 4, padding: '6px 10px',
              fontSize: 13, fontWeight: 700, minHeight: 32, cursor: 'pointer',
            }}
          >Quit</button>
        )}
        {!isStopped && !hasPosition && !armed && (
          <button
            onClick={onRearm}
            style={{
              background: 'var(--accent-blue)', color: '#fff', border: 'none',
              borderRadius: 4, padding: '6px 10px',
              fontSize: 13, fontWeight: 700, minHeight: 32, cursor: 'pointer',
            }}
          >Re-arm</button>
        )}
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
      {/* Slot picker — chunky touch buttons. */}
      <div
        style={{
          flexShrink: 0,
          display: 'grid',
          gridTemplateColumns: 'repeat(4, 1fr)',
          gap: 4,
          padding: 4,
          background: 'var(--bg-primary)',
          borderBottom: '1px solid var(--border-default)',
        }}
      >
        {SLOTS.map((s) => (
          <button
            key={s}
            onClick={() => setActiveSlot(s)}
            style={{
              padding: '8px 0',
              minHeight: 40,
              border: 'none',
              borderRadius: 4,
              fontSize: 14,
              fontWeight: activeSlot === s ? 700 : 500,
              background: activeSlot === s
                ? 'var(--accent-blue)' : 'var(--bg-secondary)',
              color: activeSlot === s ? '#fff' : 'var(--text-secondary)',
              cursor: 'pointer',
            }}
          >
            {s}
          </button>
        ))}
      </div>
      {botFetchError && (
        <div
          style={{
            padding: '6px 10px', fontSize: 11,
            color: 'var(--accent-red)',
            background: 'var(--bg-secondary)',
            flexShrink: 0,
          }}
        >
          {botFetchError}
        </div>
      )}
      {actionMsg && (
        <div
          style={{
            padding: '4px 10px', fontSize: 11,
            color: 'var(--text-muted)',
            background: 'var(--bg-primary)',
            flexShrink: 0,
          }}
        >
          {actionMsg}
        </div>
      )}
      <div className="flex-1" style={{ minHeight: 0 }}>
        <BotChart
          botId={botId}
          botRef={bot?.ref_id}
          symbol={symbol}
          secType={secType}
          renderHeader={renderHeader}
          renderTitle={() => (
            <span>{symbol ?? '—'} · {bot?.name ?? botId}</span>
          )}
        />
      </div>
    </div>
  );
}
