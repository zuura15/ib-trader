/**
 * Frontend diagnostics: global error trapping + a ring buffer of the last
 * few unhandled events so the ErrorBoundary UI can render them even when
 * they originated outside React.
 *
 * `installGlobalErrorHandlers()` is called once from `main.tsx` before the
 * React tree mounts. It catches:
 *
 *   - `window.error`            — synchronous uncaught exceptions.
 *   - `unhandledrejection`      — async rejections React boundaries can't see.
 *
 * Every captured event is also `console.error`-logged with a fixed
 * `[diag]` prefix so it shows up against everything else in DevTools.
 */

export interface DiagEvent {
  kind: 'error' | 'rejection' | 'react';
  message: string;
  stack?: string;
  componentStack?: string;
  source?: string;        // filename or 'react-boundary' / 'window' / 'promise'
  ts: string;             // ISO timestamp
  url: string;
  userAgent: string;
}

const RING_MAX = 25;
const ring: DiagEvent[] = [];
const subscribers = new Set<(events: DiagEvent[]) => void>();

function notify(): void {
  const snap = ring.slice();
  for (const fn of subscribers) {
    try { fn(snap); } catch { /* swallow — never let a subscriber crash diagnostics */ }
  }
}

export function recordDiag(ev: Omit<DiagEvent, 'ts' | 'url' | 'userAgent'>): void {
  const full: DiagEvent = {
    ...ev,
    ts: new Date().toISOString(),
    url: typeof window !== 'undefined' ? window.location.href : '',
    userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : '',
  };
  ring.push(full);
  if (ring.length > RING_MAX) ring.shift();
  try {
    // eslint-disable-next-line no-console
    console.error(`[diag:${ev.kind}]`, ev.message, ev.stack || '', ev.componentStack || '');
  } catch { /* console not available */ }
  notify();
}

export function getDiagRing(): DiagEvent[] {
  return ring.slice();
}

export function subscribeDiag(fn: (events: DiagEvent[]) => void): () => void {
  subscribers.add(fn);
  return () => { subscribers.delete(fn); };
}

export function clearDiagRing(): void {
  ring.length = 0;
  notify();
}

let installed = false;
export function installGlobalErrorHandlers(): void {
  if (installed || typeof window === 'undefined') return;
  installed = true;

  window.addEventListener('error', (event: ErrorEvent) => {
    recordDiag({
      kind: 'error',
      message: event.message || String(event.error ?? 'unknown error'),
      stack: event.error?.stack,
      source: event.filename ? `${event.filename}:${event.lineno}:${event.colno}` : 'window',
    });
  });

  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    const message = reason instanceof Error
      ? reason.message
      : typeof reason === 'string' ? reason : JSON.stringify(reason);
    recordDiag({
      kind: 'rejection',
      message,
      stack: reason instanceof Error ? reason.stack : undefined,
      source: 'promise',
    });
  });
}
