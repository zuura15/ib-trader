import { useStore } from '../../data/store';

/**
 * Header-level "untie the knot" button.
 *
 * Click       — light resync: bumps ``resyncToken`` (every chart pane,
 *               positions panel, log stream tears down + re-establishes
 *               its WS subscription), force-reconnects ``wsManager``
 *               (watchlist + central state push), and hits the engine's
 *               ``/api/system/resync`` (``reload-watchlist`` +
 *               ``positions/refresh``). Safe to spam; no IB-side
 *               churn beyond the watchlist requalify.
 * Shift+click — deep resync: same as above PLUS the engine's
 *               prophylactic resub that cycles every ``reqMktData``,
 *               unsticking IB-side parked subscriptions. Heavier;
 *               briefly interrupts every quote stream. Use when the
 *               light resync didn't help.
 *
 * Status flips between idle / running / done / failed and shows a
 * one-line message inline. The store auto-clears back to idle after
 * a few seconds so the button doesn't keep shouting the last result.
 */
export function ResyncButton() {
  const status = useStore((s) => s.resyncStatus);
  const message = useStore((s) => s.resyncMessage);
  const triggerResync = useStore((s) => s.triggerResync);
  const triggerDeepResync = useStore((s) => s.triggerDeepResync);

  const isRunning = status === 'running';
  const isDone = status === 'done';
  const isFailed = status === 'failed';

  const onClick = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (isRunning) return;
    if (e.shiftKey) {
      void triggerDeepResync();
    } else {
      void triggerResync();
    }
  };

  const icon = isRunning ? '⟳' : isDone ? '✓' : isFailed ? '!' : '⟳';
  const borderColor = isFailed
    ? 'var(--accent-red)'
    : isDone
      ? 'var(--accent-green)'
      : 'var(--border-default)';
  const textColor = isFailed
    ? 'var(--accent-red)'
    : isDone
      ? 'var(--accent-green)'
      : 'var(--text-secondary)';

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={isRunning}
      title={
        isRunning
          ? message || 'Resyncing…'
          : 'Click: resync subscriptions + watchlist + positions.\n'
            + 'Shift+click: deep resync (also cycles every IB '
            + 'market-data subscription — slower, ~30s).'
      }
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        background: 'var(--bg-primary)',
        color: textColor,
        border: `1px solid ${borderColor}`,
        borderRadius: 4,
        padding: '4px 10px',
        fontSize: 12,
        fontWeight: 500,
        cursor: isRunning ? 'progress' : 'pointer',
        minHeight: 28,
        whiteSpace: 'nowrap',
      }}
    >
      <span
        style={{
          fontSize: 14,
          display: 'inline-block',
          // Tiny CSS spin while running. The keyframes are inline
          // (no global stylesheet edit) — Chrome/Safari/Firefox all
          // honor inline @keyframes via a sibling <style>.
          animation: isRunning ? 'resync-spin 1s linear infinite' : 'none',
        }}
      >
        {icon}
      </span>
      <span>Resync</span>
      {message && (status === 'done' || status === 'failed') && (
        <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 4 }}>
          {message}
        </span>
      )}
      <style>{`@keyframes resync-spin { to { transform: rotate(360deg); } }`}</style>
    </button>
  );
}
