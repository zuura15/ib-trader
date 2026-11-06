import { useEffect, useState } from 'react';

/** Two-step cancel for one working order: ✕ arms it ("cancel?"), a
 *  second click within 4s sends. Used on the chart's order-line handles
 *  and in the header open-orders popover. ``onCancel`` resolves to null
 *  on success or an error message to show. */
export function CancelOrderButton({
  onCancel, disabledReason, size = 'sm', testId,
}: {
  onCancel: () => Promise<string | null>;
  /** When set the button renders disabled with this as its tooltip
   *  (e.g. a TWS-bound order this session can't cancel). */
  disabledReason?: string;
  size?: 'xs' | 'sm';
  testId?: string;
}) {
  const [stage, setStage] = useState<'idle' | 'armed' | 'busy' | 'sent' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);

  // Disarm / clear an error after a few seconds.
  useEffect(() => {
    if (stage !== 'armed' && stage !== 'error') return;
    const t = window.setTimeout(() => setStage('idle'), stage === 'armed' ? 4000 : 3000);
    return () => window.clearTimeout(t);
  }, [stage]);

  const click = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (disabledReason || stage === 'busy' || stage === 'sent') return;
    if (stage !== 'armed') { setStage('armed'); return; }
    setStage('busy');
    let err: string | null;
    try { err = await onCancel(); } catch (ex) { err = String(ex); }
    if (err) { setError(err); setStage('error'); } else { setStage('sent'); }
  };

  const h = size === 'xs' ? 16 : 18;
  const label = stage === 'armed' ? 'cancel?'
    : stage === 'busy' || stage === 'sent' ? '…'
    : stage === 'error' ? 'failed' : '✕';
  const title = disabledReason
    ?? (stage === 'armed' ? 'Click again to cancel this order at IB'
      : stage === 'error' ? `Cancel failed: ${error ?? ''}`
      : stage === 'sent' ? 'Cancel sent — waiting for IB'
      : 'Cancel order');
  const hot = stage === 'armed' || stage === 'error';
  return (
    <button
      type="button"
      data-testid={testId}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={click}
      title={title}
      disabled={!!disabledReason}
      style={{
        // Fixed width across states so arming doesn't shift the button
        // out from under the pointer before the confirming click.
        height: h, width: 52, padding: 0,
        border: 'none', borderRadius: 3,
        fontSize: 11, fontWeight: 700, lineHeight: 1,
        fontFamily: 'ui-monospace, monospace', whiteSpace: 'nowrap',
        cursor: disabledReason ? 'not-allowed' : 'pointer',
        opacity: disabledReason ? 0.4 : 1,
        background: hot ? 'var(--accent-red, #dc2626)' : 'var(--bg-secondary, #444)',
        color: hot ? '#fff' : 'var(--text-secondary, #ccc)',
      }}
    >
      {label}
    </button>
  );
}
