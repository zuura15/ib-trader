import { useEffect, useState } from 'react';
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
  botId: string,
  path: 'force-quit' | 'rearm' | 'start' | 'stop' | 'reset'
      | 'force-buy' | 'force-short',
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
  // Track in-flight POSTs per action so the button can disable itself.
  // Without this, an operator clicking Force-quit twice in 50ms fired
  // two HTTP requests — the second often hit a 409 from the runner's
  // FSM gate and surfaced as "Quit failed", even when the first one
  // had already succeeded.
  const [pendingAction, setPendingAction] = useState<
    'force-quit' | 'rearm' | 'start' | 'stop' | 'reset'
    | 'force-buy' | 'force-short' | null
  >(null);

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

  const runAction = async (
    action: 'force-quit' | 'rearm' | 'start' | 'stop' | 'reset'
          | 'force-buy' | 'force-short',
    pendingMsg: string, okMsg: string, failPrefix: string,
  ) => {
    if (pendingAction) return;
    setPendingAction(action);
    setActionMsg(pendingMsg);
    try {
      const res = await postBotAction(botId, action);
      setActionMsg(res.ok ? okMsg : `${failPrefix}: ${res.detail}`);
    } finally {
      setPendingAction(null);
      setTimeout(() => setActionMsg(null), 4000);
    }
  };
  const onForceQuit = () =>
    runAction('force-quit', 'Closing…', 'Quit sent.', 'Quit failed');
  const onRearm = () =>
    runAction('rearm', 'Re-arming…', 'Armed.', 'Re-arm failed');
  const onStart = () =>
    runAction('start', 'Starting…', 'Started.', 'Start failed');
  const onStop = () =>
    runAction('stop', 'Stopping…', 'Stopped.', 'Stop failed');
  const onForceLong = () =>
    runAction('force-buy', 'Force LONG…', 'LONG fired.', 'Force LONG failed');
  const onForceShort = () =>
    runAction('force-short', 'Force SHORT…', 'SHORT fired.', 'Force SHORT failed');
  // Reset is the only path out of ERRORED — the runner's ``/start``
  // gate (``is_clean_for_start``) rejects anything but ``state=OFF``,
  // so a Start click on an ERRORED bot would silently 409. Reset
  // itself refuses while the bot task is still registered (the
  // runner returns 409 "Bot is running — call /stop first"), so we
  // chain stop → reset. Both are idempotent; stop on an already-off
  // task is a no-op.
  const onReset = async () => {
    if (pendingAction) return;
    setPendingAction('reset');
    setActionMsg('Resetting…');
    try {
      // Stop swallows its result intentionally — a 409 here just
      // means the task wasn't running and reset will succeed anyway.
      await postBotAction(botId, 'stop');
      const res = await postBotAction(botId, 'reset');
      setActionMsg(res.ok ? 'Reset to OFF.' : `Reset failed: ${res.detail}`);
    } finally {
      setPendingAction(null);
      setTimeout(() => setActionMsg(null), 4000);
    }
  };

  // (was a PanelShell title — now redundant with the flexlayout tab label)

  const renderRight = (state: BotPositionState) => {
    const fsmState = (state.state as string | undefined) ?? 'UNKNOWN';
    const armed = state.armed ?? false;
    const hasPosition = POSITION_STATES.has(fsmState);
    // ERRORED is sticky and requires an explicit Reset before Start
    // is allowed (lifecycle.is_clean_for_start gates on state==OFF).
    // Treat the two "not-running" buckets separately so the toolbar
    // surfaces the right next-step button.
    const isErrored = fsmState === 'ERRORED';
    const isOff = fsmState === 'OFF' || fsmState === 'STOPPED'
      || fsmState === 'UNKNOWN';
    const isStopped = isOff || isErrored;
    const errMsg = typeof state.error_message === 'string'
      ? state.error_message : null;
    const errReason = typeof state.error_reason === 'string'
      ? state.error_reason : null;
    const statusTone =
      fsmState === 'AWAITING_EXIT_TRIGGER' ? 'var(--accent-green)' :
      fsmState === 'ENTRY_ORDER_PLACED' || fsmState === 'EXIT_ORDER_PLACED' ? 'var(--accent-yellow)' :
      fsmState === 'AWAITING_ENTRY_TRIGGER' && armed ? 'var(--accent-blue)' :
      isErrored ? 'var(--accent-red)' :
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
          title={isErrored && (errMsg || errReason)
            ? `ERRORED — ${errMsg || errReason}`
            : `fsm=${fsmState} armed=${armed}`}
        >
          {fsmState
            .replace(/_/g, ' ')
            // Drop the redundant "TRIGGER" suffix on the two watching
            // states so the banner stays narrow enough to keep the
            // toolbar buttons un-squished on small panes.
            .replace(/\s+TRIGGER$/, '')}{
            !isStopped && !armed ? ' · DISARMED' : ''
          }
        </span>
        {isOff && (
          <button
            onClick={onStart}
            disabled={pendingAction !== null}
            title="Start the bot (it boots armed and watches for 3-touch signals)"
            style={{
              background: 'var(--accent-green)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600,
              cursor: pendingAction ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'start' ? 0.5 : 1,
            }}
          >
            Start
          </button>
        )}
        {isErrored && (
          <button
            onClick={onReset}
            disabled={pendingAction !== null}
            title={errMsg || errReason
              ? `Reset to OFF (${errMsg || errReason})`
              : 'Reset bot state to OFF (required after ERRORED)'}
            style={{
              background: 'var(--accent-blue)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600,
              cursor: pendingAction ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'reset' ? 0.5 : 1,
            }}
            data-testid={`bot-reset-${botId}`}
          >
            Reset
          </button>
        )}
        {hasPosition && (
          <button
            onClick={onForceQuit}
            disabled={pendingAction !== null}
            title="Close at mid (one click, no confirm)"
            style={{
              background: 'var(--accent-red)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600,
              cursor: pendingAction ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'force-quit' ? 0.5 : 1,
            }}
          >
            Force quit
          </button>
        )}
        {!isStopped && !hasPosition && !armed && (
          <button
            onClick={onRearm}
            disabled={pendingAction !== null}
            title="Allow the next 3-touch signal to fire"
            style={{
              background: 'var(--accent-blue)', color: '#fff',
              border: 'none', borderRadius: 3, padding: '2px 8px',
              fontSize: 11, fontWeight: 600,
              cursor: pendingAction ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'rearm' ? 0.5 : 1,
            }}
          >
            Re-arm
          </button>
        )}
        {fsmState === 'AWAITING_ENTRY_TRIGGER' && (
          <>
            {/* No ``title=`` on the force buttons — the native browser
                tooltip hovers above the button and obscures the price
                lines underneath at the right moment the operator is
                about to click. Button labels are self-explanatory;
                the help text lives in ADR 019 if needed. */}
            <button
              onClick={onForceLong}
              disabled={pendingAction !== null}
              style={{
                background: 'var(--accent-green)', color: '#fff',
                border: 'none', borderRadius: 3, padding: '2px 8px',
                fontSize: 11, fontWeight: 600,
                cursor: pendingAction ? 'not-allowed' : 'pointer',
                opacity: pendingAction && pendingAction !== 'force-buy' ? 0.5 : 1,
              }}
              data-testid={`bot-force-long-${botId}`}
            >
              Force LONG
            </button>
            <button
              onClick={onForceShort}
              disabled={pendingAction !== null}
              style={{
                background: 'var(--accent-red)', color: '#fff',
                border: 'none', borderRadius: 3, padding: '2px 8px',
                fontSize: 11, fontWeight: 600,
                cursor: pendingAction ? 'not-allowed' : 'pointer',
                opacity: pendingAction && pendingAction !== 'force-short' ? 0.5 : 1,
              }}
              data-testid={`bot-force-short-${botId}`}
            >
              Force SHORT
            </button>
          </>
        )}
        {!isStopped && !hasPosition && (
          <button
            onClick={onStop}
            disabled={pendingAction !== null}
            title="Stop the bot (it stops watching for signals)"
            style={{
              background: 'transparent', color: 'var(--text-muted)',
              border: '1px solid var(--border-default)',
              borderRadius: 3, padding: '2px 6px',
              fontSize: 11,
              cursor: pendingAction ? 'not-allowed' : 'pointer',
              opacity: pendingAction && pendingAction !== 'stop' ? 0.5 : 1,
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
    // PanelShell stripped: the chart-bot tab label already shows the
    // slot/symbol via flexlayout's tab handle, and BotChart's own
    // toolbar carries the state banner + action buttons. The extra
    // header row was double-billing and stealing chart real estate.
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
  );
}
