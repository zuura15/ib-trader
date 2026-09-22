import { useMemo, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { SymbolChart } from '../chart/SymbolChart';
import type { ChartTarget } from '../../data/store';

/**
 * Direction Lab (#100, iteration 3) — isolated playground for the
 * directional callout, now with a live chart. Nothing here runs
 * automatically: the ⟳ button POSTs /api/direction/compute, which
 * reads the engine's live sample buffer, classifies slope/curvature
 * locally, and fires one round per configured LLM provider (Grok's
 * chat path / Jev's typed Choice API). The latest LOC + JEV verdicts
 * sit in an overlay on the chart's top-left quadrant with their
 * strengths (slope ticks/min; Jev's probability + confidence); the
 * run history below keeps raw replies, latency, and the exact prompt
 * so the logic can be iterated without touching the main chart panes.
 */

type ProviderResult = {
  status: string; dir?: string; raw?: string; ms?: number;
  model?: string; error?: string;
  probs?: Record<string, number> | null;
  confidence?: number | null;
};
type Run = {
  at: string; symbol: string;
  local: { status: string; dir: string; slope_ticks_per_min: number;
           accel: string; samples: number };
  grok: ProviderResult; jev: ProviderResult;
  prompt: string | null; closes: number; samples: number;
};

// Futures month-code pattern (root + FGHJKMNQUVXZ + year digit) —
// the lab is free-form, so sec type is inferred for chart resolution.
const FUT_RE = /^[A-Z]{1,3}[FGHJKMNQUVXZ]\d$/;

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

/** One overlay verdict line: dir glyph + strength readout. */
function OverlayRow({ tag, dir, strength, dim }: {
  tag: string; dir?: string; strength: string; dim?: boolean;
}) {
  return (
    <div style={{
      display: 'flex', gap: 8, alignItems: 'baseline',
      fontFamily: 'ui-monospace, monospace',
    }}>
      <span style={{
        width: 30, color: 'var(--text-muted)', fontSize: 9,
        letterSpacing: '0.1em',
      }}>{tag}</span>
      <span style={{
        fontWeight: 700, fontSize: 14,
        color: dim ? 'var(--text-muted)' : dirColor(dir),
      }}>
        {dim ? '—' : glyph(dir)}
      </span>
      <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
        {strength}
      </span>
    </div>
  );
}

export function DirectionLabPanel() {
  const [symbol, setSymbol] = useState('NQZ6');
  const [chartSymbol, setChartSymbol] = useState('NQZ6');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);

  const target = useMemo<ChartTarget>(() => ({
    symbol: chartSymbol,
    secType: FUT_RE.test(chartSymbol) ? 'FUT' : 'STK',
    conId: null,
  }), [chartSymbol]);

  const invoke = async () => {
    if (busy) return;
    const sym = symbol.trim().toUpperCase();
    setChartSymbol(sym);
    setBusy(true);
    setError(null);
    try {
      const r = await fetch('/api/direction/compute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym }),
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

  // Latest verdicts drive the chart overlay; stale-run guard: only a
  // run for the currently charted symbol is shown on the chart.
  const latest = runs.find((r) => r.symbol === chartSymbol) ?? null;
  const loc = latest?.local ?? null;
  const jev = latest?.jev ?? null;
  const jevPct = jev?.probs && jev.dir != null && jev.probs[jev.dir] != null
    ? Math.round(jev.probs[jev.dir] * 100) : null;

  return (
    <PanelShell title="Direction Lab" accent="blue" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
        advisory only — never order-gating
      </span>
    }>
      <div style={{
        display: 'flex', flexDirection: 'column', height: '100%',
        minHeight: 0,
      }}>
        {/* ── Chart + top-left verdict overlay ─────────────────────── */}
        <div style={{ position: 'relative', flex: 1, minHeight: 0 }}>
          <SymbolChart
            target={target}
            enableSr={false}
            showRsi={false}
            suppressAutoSignals
          />
          <div style={{
            position: 'absolute', top: 8, left: 8, zIndex: 30,
            display: 'flex', flexDirection: 'column', gap: 5,
            padding: '7px 10px', borderRadius: 6,
            background: 'var(--bg-secondary)', opacity: 0.94,
            border: '1px solid var(--border-default)',
          }}>
            <OverlayRow
              tag="LOC"
              dir={loc?.dir}
              dim={!loc || loc.status !== 'ok'}
              strength={
                !loc ? 'on-demand'
                : loc.status === 'warmup' ? `warmup ${loc.samples}/30`
                : `${loc.slope_ticks_per_min} t/min`
                  + (loc.accel !== 'STEADY' ? ` · ${loc.accel}` : '')
              }
            />
            <OverlayRow
              tag="JEV"
              dir={jev?.dir}
              dim={!jev || (jev.status !== 'ok' && jev.status !== 'unparsed')}
              strength={
                !jev ? 'on-demand'
                : jev.status === 'ok' || jev.status === 'unparsed'
                  ? (jevPct != null ? `${jevPct}%` : '')
                    + (jev.confidence != null
                       ? ` · conf ${jev.confidence}` : '')
                  : jev.status
              }
            />
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') void invoke(); }}
                spellCheck={false}
                style={{
                  width: 62, padding: '2px 6px', fontSize: 11,
                  fontFamily: 'ui-monospace, monospace',
                  background: 'var(--bg-primary)',
                  color: 'var(--text-primary)',
                  border: '1px solid var(--border-default)', borderRadius: 4,
                }}
              />
              <button
                onClick={() => void invoke()}
                disabled={busy}
                style={{
                  padding: '2px 10px', fontSize: 11, fontWeight: 700,
                  border: 'none', borderRadius: 4,
                  cursor: busy ? 'wait' : 'pointer',
                  background: 'var(--accent-blue)', color: '#fff',
                  opacity: busy ? 0.6 : 1,
                }}
              >
                {busy ? '…' : '⟳ compute'}
              </button>
            </div>
            {error && (
              <div style={{
                color: 'var(--accent-red)', fontSize: 10, maxWidth: 240,
              }}>{error}</div>
            )}
          </div>
        </div>

        {/* ── Run history (raw replies, latency, prompt) ───────────── */}
        {runs.length > 0 && (
          <div style={{
            maxHeight: '38%', overflow: 'auto', padding: 8,
            display: 'flex', flexDirection: 'column', gap: 8,
            borderTop: '1px solid var(--border-default)', flexShrink: 0,
          }}>
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
                    {run.local?.status === 'warmup'
                      ? '… warmup' : glyph(run.local?.dir)}
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
                    <summary style={{
                      color: 'var(--text-muted)', cursor: 'pointer',
                    }}>
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
        )}
      </div>
    </PanelShell>
  );
}
