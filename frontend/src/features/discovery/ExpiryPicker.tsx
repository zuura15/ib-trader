/**
 * Futures contract discovery UI (Epic 1 Phase 1).
 *
 * Workflow: user types a root (ES, MES, NQ...), component calls
 * GET /api/instruments/expiries, shows the upcoming expiries with
 * trading class + multiplier + tick size, and emits the selected
 * candidate back to the caller via onSelect.
 *
 * The mobile layout reuses this component as-is.
 */
import { useState } from 'react';
import { listFutureExpiries, type FutureExpiryCandidate } from '../../api/client';

interface Props {
  exchange?: string;
  onSelect: (candidate: FutureExpiryCandidate) => void;
  initialRoot?: string;
}

export function ExpiryPicker({ exchange = 'CME', onSelect, initialRoot = '' }: Props) {
  const [root, setRoot] = useState(initialRoot);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<FutureExpiryCandidate[]>([]);

  async function loadExpiries() {
    const trimmed = root.trim().toUpperCase();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    setCandidates([]);
    try {
      const list = await listFutureExpiries(trimmed, exchange);
      setCandidates(list);
      if (list.length === 0) {
        setError(`No upcoming expiries for ${trimmed} on ${exchange}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    loadExpiries();
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8 }}>
        <input
          type="text"
          value={root}
          onChange={(e) => setRoot(e.target.value)}
          placeholder="Root (ES, MES, NQ...)"
          style={{
            flex: 1,
            padding: '6px 10px',
            fontFamily: 'monospace',
            fontSize: 14,
            textTransform: 'uppercase',
          }}
          autoFocus
        />
        <button type="submit" disabled={loading || !root.trim()}>
          {loading ? '…' : 'Find'}
        </button>
      </form>

      {error && (
        <div style={{ color: 'var(--accent-red)', fontSize: 12 }}>
          {error}
        </div>
      )}

      {candidates.length > 0 && (
        <table style={{ width: '100%', fontFamily: 'monospace', fontSize: 13 }}>
          <thead>
            <tr style={{ color: 'var(--text-muted)', textAlign: 'left' }}>
              <th>Symbol</th>
              <th>Trading Class</th>
              <th style={{ textAlign: 'right' }}>Mult</th>
              <th style={{ textAlign: 'right' }}>Tick</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((c) => (
              <tr key={c.con_id}>
                <td>{c.display_symbol}</td>
                <td>{c.trading_class}</td>
                <td style={{ textAlign: 'right' }}>{c.multiplier}</td>
                <td style={{ textAlign: 'right' }}>{c.tick_size}</td>
                <td style={{ textAlign: 'right' }}>
                  <button type="button" onClick={() => onSelect(c)}>Select</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
