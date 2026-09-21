import { useState } from 'react';
import { PanelShell } from '../../components/PanelShell';

/**
 * Direction Lab (#100, iteration 2) — isolated playground for the
 * directional callout. Nothing here runs automatically: the button
 * POSTs /api/direction/compute, which reads the engine's live sample
 * buffer, classifies slope/curvature locally, and fires one round per
 * configured LLM provider (Grok / Jev). Results stack newest-first
 * with raw replies, latency, and the exact prompt sent — the whole
 * point is iterating on the logic without touching the chart panes.
 */

type ProviderResult = {
  status: string; dir?: string; raw?: string; ms?: number;
  model?: string; error?: string;
};
type Run = {
  at: string; symbol: string;
  local: { status: string; dir: string; slope_ticks_per_min: number;
           accel: string; samples: number };
  grok: ProviderResult; jev: ProviderResult;
  prompt: string | null; closes: number; samples: number;
};

const glyph = (d?: string) =>
  d === 'UP' ? '▲ UP' : d === 'DOWN' ? '▼ DOWN' : '— FLAT';
const dirColor = (d?: string) =>
  d === 'UP' ? 'var(--accent-green)'
  : d === 'DOWN' ? 'var(--accent-red)' : 'var(--text-secondary)';

function ProviderRow({ tag, p }: { tag: string; p: ProviderResult }) {
  return (
    <div style={{
      display: 'flex', gap: 8, alignItems: 'baseline',
      fontFamily: 'ui-monospace, monospace', fontSize: 12,
    }}>
      <span style={{
        width: 44, color: 'var(--text-muted)', fontSize: 10,
        letterSpacing: '0.1em',
      }}>{tag}</span>
      {p.status === 'ok' || p.status === 'unparsed' ? (
        <>
          <span style={{ fontWeight: 700, color: dirColor(p.dir) }}>
            {glyph(p.dir)}
          </span>
          {p.raw != null && (
            <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
              raw “{p.raw}”
            </span>
          )}
          {p.ms != null && (
            <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
              {p.ms}ms
            </span>
          )}
          {p.model && (
            <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
              {p.model}
            </span>
          )}
        </>
      ) : (
        <span style={{
          fontSize: 11,
          color: p.status === 'err' ? 'var(--accent-red)' : 'var(--text-muted)',
        }}>
          {p.status}{p.error ? ` — ${p.error}` : ''}
        </span>
      )}
    </div>
  );
}

export function DirectionLabPanel() {
  const [symbol, setSymbol] = useState('NQZ6');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);

  const invoke = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch('/api/direction/compute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: symbol.trim().toUpperCase() }),
      });
      const j = await r.json().catch(() => null);
      if (!r.ok) {
        setError(typeof j?.detail === 'string' ? j.detail : `HTTP ${r.status}`);
        return;
      }
      setRuns((xs) =>
        [{ ...j, at: new Date().toLocaleTimeString() }, ...xs].slice(0, 25));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <PanelShell title="Direction Lab" accent="blue" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
        advisory only — never order-gating
      </span>
    }>
      <div style={{
        padding: 10, display: 'flex', flexDirection: 'column', gap: 10,
        height: '100%', overflow: 'auto',
      }}>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            spellCheck={false}
            style={{
              width: 90, padding: '4px 8px', fontSize: 12,
              fontFamily: 'ui-monospace, monospace',
              background: 'var(--bg-secondary)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border-default)', borderRadius: 4,
            }}
          />
          <button
            onClick={() => void invoke()}
            disabled={busy}
            style={{
              padding: '4px 14px', fontSize: 12, fontWeight: 700,
              border: 'none', borderRadius: 4, cursor: busy ? 'wait' : 'pointer',
              background: 'var(--accent-blue)', color: '#fff',
              opacity: busy ? 0.6 : 1,
            }}
          >
            {busy ? 'Computing…' : 'Compute direction'}
          </button>
        </div>
        {error && (
          <div style={{ color: 'var(--accent-red)', fontSize: 12 }}>{error}</div>
        )}
        {runs.length === 0 && !error && (
          <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            Press Compute to classify the last 30min of {symbol.toUpperCase()}.
            The engine must be sampling the symbol (settings.yaml →
            direction_symbols); LLM rows read “off” until their API keys
            are in .env.
          </div>
        )}
        {runs.map((run, i) => (
          <div key={`${run.at}-${i}`} style={{
            border: '1px solid var(--border-default)', borderRadius: 4,
            padding: 8, display: 'flex', flexDirection: 'column', gap: 6,
            flexShrink: 0,
          }}>
            <div style={{
              display: 'flex', gap: 10, fontSize: 10,
              color: 'var(--text-muted)',
            }}>
              <span>{run.at}</span>
              <span>{run.symbol}</span>
              <span>{run.samples} samples · {run.closes} closes</span>
            </div>
            <div style={{
              display: 'flex', gap: 8, alignItems: 'baseline',
              fontFamily: 'ui-monospace, monospace', fontSize: 12,
            }}>
              <span style={{
                width: 44, color: 'var(--text-muted)', fontSize: 10,
                letterSpacing: '0.1em',
              }}>LOC</span>
              <span style={{
                fontWeight: 700, color: dirColor(run.local?.dir),
              }}>
                {run.local?.status === 'warmup' ? '… warmup' : glyph(run.local?.dir)}
              </span>
              {run.local?.status === 'ok' && (
                <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
                  {run.local.slope_ticks_per_min} t/min
                  {run.local.accel !== 'STEADY' ? ` · ${run.local.accel}` : ''}
                </span>
              )}
            </div>
            <ProviderRow tag="GROK" p={run.grok} />
            <ProviderRow tag="JEV" p={run.jev} />
            {run.prompt && (
              <details style={{ fontSize: 10 }}>
                <summary style={{ color: 'var(--text-muted)', cursor: 'pointer' }}>
                  prompt sent
                </summary>
                <code style={{
                  whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                  color: 'var(--text-secondary)',
                }}>{run.prompt}</code>
              </details>
            )}
          </div>
        ))}
      </div>
    </PanelShell>
  );
}
