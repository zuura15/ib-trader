import { useEffect, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { BotChart } from './BotChart';
import type { ChartTarget } from '../../data/store';
import type { BotPositionState } from '../../data/useBotState';

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
  state?: string;
  config?: Record<string, unknown>;
}

const POSITION_STATES = new Set([
  'ENTRY_ORDER_PLACED',
  'AWAITING_EXIT_TRIGGER',
  'EXIT_ORDER_PLACED',
]);

function botIdForSlot(slot: number): string {
  return `chart-bot-${slot}`;
}

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
 * Desktop Trader-layout pane. Wraps ``BotChart`` in a ``PanelShell``
 * with title/right chrome and surfaces Force-quit + Re-arm buttons.
 * The chart itself + bot subscription live in ``BotChart``.
 *
 * Symbol selection is config-driven (edit ``config/bots/chart-bot-N.yaml``).
 * A live symbol picker is a future iteration.
 */
export function ChartBotPane({ slot }: Props) {
  const botId = botIdForSlot(slot);
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

  const symbol = (() => {
    if (!bot) return null;
    try {
      const arr = bot.symbols_json ? JSON.parse(bot.symbols_json) : [];
      return Array.isArray(arr) && arr.length > 0 ? String(arr[0]) : null;
    } catch { return null; }
  })();
  // ``/api/bots/{id}`` exposes ``sec_type`` at top level — see
  // ``_serialize_bot_from_defn``. Default to STK only for legacy bot
  // shapes that pre-date the field.
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
  const onStop = async () => {
    setActionMsg('Stopping…');
    const res = await postBotAction(botId, 'stop');
    setActionMsg(res.ok ? 'Stopped.' : `Stop failed: ${res.detail}`);
    setTimeout(() => setActionMsg(null), 4000);
  };

  const header = `${symbol ?? '—'} · Slot ${slot}`;

  const renderRight = (state: BotPositionState) => {
    const fsmState = (state.state as string | undefined) ?? 'UNKNOWN';
    const armed = state.armed ?? false;
    const hasPosition = POSITION_STATES.has(fsmState);
    // ``isStopped`` covers fresh bots (state==OFF after first registry
    // load) and bots that were explicitly stopped. Re-arm requires a
    // running bot — the runner rejects with 409 "Bot is not running"
    // otherwise — so a Start button is shown instead when stopped.
    const isStopped = fsmState === 'OFF' || fsmState === 'STOPPED'
      || fsmState === 'UNKNOWN' || fsmState === 'ERRORED';
    const statusTone =
      fsmState === 'AWAITING_EXIT_TRIGGER' ? 'var(--accent-green)' :
      fsmState === 'ENTRY_ORDER_PLACED' || fsmState === 'EXIT_ORDER_PLACED' ? 'var(--accent-yellow)' :
      fsmState === 'AWAITING_ENTRY_TRIGGER' && armed ? 'var(--accent-blue)' :
      isStopped ? 'var(--text-muted)' :
      'var(--text-muted)';
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
        <span
          style={{
            padding: '1px 6px', borderRadius: 3,
            border: `1px solid ${statusTone}`, color: statusTone,
            fontWeight: 600, letterSpacing: '0.02em',
          }}
          title={`fsm=${fsmState} armed=${armed}`}
        >
          {fsmState.replace(/_/g, ' ')}{!isStopped && !armed ? ' · DISARMED' : ''}
        </span>
        {isStopped && (
          <button
            onClick={onStart}
            title="Start the bot (it boots armed and watches for 3-touch signals)"
            style={{
              background: 'var(--accent-green)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600, cursor: 'pointer',
            }}
          >
            Start
          </button>
        )}
        {hasPosition && (
          <button
            onClick={onForceQuit}
            title="Close at mid (one click, no confirm)"
            style={{
              background: 'var(--accent-red)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600, cursor: 'pointer',
            }}
          >
            Force quit
          </button>
        )}
        {!isStopped && !hasPosition && !armed && (
          <button
            onClick={onRearm}
            title="Allow the next 3-touch signal to fire"
            style={{
              background: 'var(--accent-blue)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600, cursor: 'pointer',
            }}
          >
            Re-arm
          </button>
        )}
        {!isStopped && !hasPosition && (
          <button
            onClick={onStop}
            title="Stop the bot (it stops watching for signals)"
            style={{
              background: 'transparent', color: 'var(--text-muted)',
              border: '1px solid var(--border-default)',
              borderRadius: 3, padding: '2px 6px',
              fontSize: 11, cursor: 'pointer',
            }}
          >
            Stop
          </button>
        )}
        {actionMsg && (
          <span style={{ color: 'var(--text-muted)' }}>{actionMsg}</span>
        )}
      </div>
    );
  };

  return (
    <PanelShell title={header}>
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
        <div className="flex-1" style={{ minHeight: 0 }}>
          <BotChart
            botId={botId}
            botRef={bot?.ref_id}
            symbol={symbol}
            secType={secType}
            renderHeader={renderRight}
          />
        </div>
      </div>
    </PanelShell>
  );
}
