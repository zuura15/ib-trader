import { Fragment, useEffect, useState } from 'react';
import {
  getDiagRing, subscribeDiag, recordDiag, recordBoot,
  type DiagEvent,
} from './diagnostics';

/**
 * Always-mounted diagnostic surface. Three modes:
 *
 *   - Hidden:  no UI, just sits in the tree.
 *   - Hotkey:  Ctrl+Alt+D opens the full overlay (and toggles it shut).
 *   - Watchdog: 3 s after mount we sniff the page. If the layout container
 *     is 0×0 OR the document body has no visually-rendered children, we
 *     auto-promote to the full overlay so the user doesn't sit on a blank
 *     screen wondering what happened. (ErrorBoundary already handles render
 *     exceptions; this catches the "nothing crashed but nothing painted"
 *     case — the flexlayout-zero-height symptom.)
 *
 * Also records page boot vitals to the diagnostic ring on mount: protocol,
 * viewport, mobile detection, document.readyState — the values most often
 * implicated in white-screen-on-first-load symptoms.
 */
export function DiagOverlay() {
  const [open, setOpen] = useState(false);
  const [, force] = useState(0);

  // Record boot vitals once, and arm the white-screen watchdog.
  useEffect(() => {
    const vp = `${window.innerWidth}x${window.innerHeight}`;
    recordBoot('react-mounted', {
      proto: window.location.protocol,
      host: window.location.host,
      viewport: vp,
      isMobile: window.matchMedia('(max-width: 767px)').matches,
      readyState: document.readyState,
    });

    const checkVisual = () => {
      const root = document.getElementById('root');
      const layout = document.querySelector('.flexlayout__layout');
      const rootRect = root?.getBoundingClientRect();
      const layoutRect = (layout as HTMLElement | null)?.getBoundingClientRect();
      const rootOk = !!rootRect && rootRect.width > 50 && rootRect.height > 50;
      const layoutOk = !!layoutRect && layoutRect.width > 50 && layoutRect.height > 50;
      const styleSheets = document.styleSheets.length;
      const headerPresent = !!document.querySelector('header');

      recordBoot('watchdog-check', {
        rootRect: rootRect ? `${Math.round(rootRect.width)}x${Math.round(rootRect.height)}` : null,
        layoutRect: layoutRect ? `${Math.round(layoutRect.width)}x${Math.round(layoutRect.height)}` : null,
        styleSheets,
        headerPresent,
      });

      const suspicious =
        !root || !rootOk ||
        // We expect either the workstation's <header> (desktop) or the
        // mobile layout to have rendered something visible by now.
        (!layoutOk && !headerPresent);
      if (suspicious) {
        recordDiag({
          kind: 'watchdog',
          message: 'white-screen suspected — auto-opening diagnostic overlay',
          source: 'watchdog',
        });
        setOpen(true);
      }
    };

    // Two checks: 3 s (fast feedback if it's broken from the start) and
    // 8 s (catches slower-loading regressions that the 3 s check missed).
    const t1 = setTimeout(checkVisual, 3000);
    const t2 = setTimeout(checkVisual, 8000);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, []);

  // Live-refresh when the diag ring changes (so the overlay shows new events).
  useEffect(() => subscribeDiag(() => force(n => n + 1)), []);

  // Ctrl+Alt+D toggles the overlay (anytime, not just on white-screen).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.altKey && e.key.toLowerCase() === 'd') {
        e.preventDefault();
        setOpen(v => !v);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  if (!open) return null;
  return <FullOverlay onClose={() => setOpen(false)} ring={getDiagRing()} />;
}

function FullOverlay({ onClose, ring }: { onClose: () => void; ring: DiagEvent[] }) {
  const copy = async () => {
    const payload = formatRing(ring);
    try {
      await navigator.clipboard.writeText(payload);
    } catch {
      // eslint-disable-next-line no-console
      console.error('[diag] copy failed; payload below\n', payload);
    }
  };
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 100001,
      background: 'rgba(11, 15, 23, 0.96)',
      color: '#e2e8f0',
      padding: 24,
      overflow: 'auto',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 12,
    }}>
      <div style={{ maxWidth: 1100, margin: '0 auto' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#f7bd5c' }}>
            ⚑ Diagnostics
          </div>
          <div style={{ color: '#94a3b8', flex: 1 }}>
            {ring.length} event{ring.length === 1 ? '' : 's'} · Ctrl+Alt+D toggles ·
            {' '}auto-opens on white-screen
          </div>
          <button onClick={copy} style={btn}>Copy</button>
          <button onClick={() => window.location.reload()} style={btn}>Reload</button>
          <button onClick={onClose} style={btn}>Close</button>
        </div>

        <Vitals />

        <div style={{ marginTop: 16 }}>
          {ring.length === 0 ? (
            <div style={{ color: '#94a3b8' }}>(no events yet)</div>
          ) : ring.slice().reverse().map((e, i) => (
            <div key={i} style={{
              marginBottom: 10,
              padding: 8,
              border: '1px solid #2a313d',
              borderRadius: 4,
              background: 'rgba(0,0,0,0.25)',
            }}>
              <div style={{ color: kindColor(e.kind) }}>
                [{e.ts}] +{e.bootElapsedMs ?? '?'}ms · {e.kind} · {e.source || ''}
              </div>
              <div style={{ color: '#e2e8f0', marginTop: 2 }}>{e.message}</div>
              {e.stack && <pre style={preStyle}>{e.stack}</pre>}
              {e.componentStack && <pre style={preStyle}>{e.componentStack}</pre>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Vitals() {
  const proto = window.location.protocol;
  const host = window.location.host;
  const vp = `${window.innerWidth}x${window.innerHeight}`;
  const root = document.getElementById('root');
  const layout = document.querySelector('.flexlayout__layout');
  const rootR = root?.getBoundingClientRect();
  const layoutR = (layout as HTMLElement | null)?.getBoundingClientRect();
  const cells: Array<[string, string]> = [
    ['protocol', proto],
    ['host', host],
    ['viewport', vp],
    ['readyState', document.readyState],
    ['root', rootR ? `${Math.round(rootR.width)}x${Math.round(rootR.height)}` : '(missing)'],
    ['flexlayout', layoutR ? `${Math.round(layoutR.width)}x${Math.round(layoutR.height)}` : '(missing)'],
    ['styleSheets', String(document.styleSheets.length)],
    ['UA', navigator.userAgent],
  ];
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '160px 1fr',
      gap: '4px 12px',
      padding: 12,
      border: '1px solid #2a313d',
      borderRadius: 4,
      background: 'rgba(255,255,255,0.03)',
    }}>
      {cells.map(([k, v]) => (
        <Fragment key={k}>
          <div style={{ color: '#94a3b8' }}>{k}</div>
          <div style={{ color: '#e2e8f0', wordBreak: 'break-all' }}>{v}</div>
        </Fragment>
      ))}
    </div>
  );
}

function kindColor(kind: DiagEvent['kind']): string {
  switch (kind) {
    case 'react':     return '#ef4444';
    case 'error':     return '#ef4444';
    case 'rejection': return '#f7bd5c';
    case 'watchdog':  return '#f7bd5c';
    case 'boot':      return '#3b82f6';
    default:          return '#94a3b8';
  }
}

function formatRing(ring: DiagEvent[]): string {
  const head = [
    '# Frontend diagnostic dump',
    `time: ${new Date().toISOString()}`,
    `url: ${window.location.href}`,
    `userAgent: ${navigator.userAgent}`,
    `viewport: ${window.innerWidth}x${window.innerHeight}`,
    '',
  ].join('\n');
  const events = ring.map((e, i) =>
    `${i + 1}. [${e.ts}] +${e.bootElapsedMs ?? '?'}ms ${e.kind} ${e.source || ''}\n` +
    `   ${e.message}` +
    (e.stack ? `\n   stack:\n${e.stack.split('\n').map(l => '   ' + l).join('\n')}` : '') +
    (e.componentStack ? `\n   componentStack:\n${e.componentStack.split('\n').map(l => '   ' + l).join('\n')}` : ''),
  ).join('\n\n');
  return head + events;
}

const preStyle: React.CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  background: 'rgba(0,0,0,0.35)',
  border: '1px solid #2a313d',
  borderRadius: 4,
  padding: 6,
  margin: '6px 0 0 0',
  fontSize: 11,
  lineHeight: 1.45,
};

const btn: React.CSSProperties = {
  background: 'transparent',
  color: '#e2e8f0',
  border: '1px solid #2a313d',
  borderRadius: 4,
  padding: '4px 10px',
  cursor: 'pointer',
  fontSize: 12,
};
