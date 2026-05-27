import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { recordDiag, getDiagRing, type DiagEvent } from './diagnostics';

interface Props {
  /** Label used in the diagnostic event source (e.g. pane name, "root"). */
  label: string;
  /** UI variant. "root" = full-screen takeover; "pane" = inline pane-sized. */
  variant: 'root' | 'pane';
  children: ReactNode;
}

interface State {
  error: Error | null;
  componentStack: string | null;
}

/**
 * Catches render-time exceptions in the React tree.
 *
 * Two flavors:
 *   - `variant="root"` wraps `<App />` in `main.tsx`. Renders a full-screen
 *     panel with the stack, recent diagnostic ring, copy-to-clipboard,
 *     and a Reload button. The last line of defense before a white screen.
 *   - `variant="pane"` wraps each component returned by `componentFactory`
 *     so one crashing pane doesn't take the workstation down — the
 *     surrounding flexlayout grid stays usable.
 *
 * Async throws (promises, event handlers) are NOT caught by React boundaries.
 * `installGlobalErrorHandlers` in `diagnostics.ts` catches those and feeds
 * them into the same ring buffer rendered here.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ componentStack: info.componentStack ?? null });
    recordDiag({
      kind: 'react',
      message: error.message || 'unknown render error',
      stack: error.stack,
      componentStack: info.componentStack ?? undefined,
      source: `react-boundary:${this.props.label}`,
    });
  }

  private reset = () => {
    this.setState({ error: null, componentStack: null });
  };

  private reload = () => {
    window.location.reload();
  };

  private copyDiagnostics = async () => {
    const { error, componentStack } = this.state;
    const ring = getDiagRing();
    const payload = formatDiagnostics({
      label: this.props.label,
      error,
      componentStack,
      ring,
    });
    try {
      await navigator.clipboard.writeText(payload);
    } catch {
      // Clipboard may be denied; surface in console as a fallback.
      // eslint-disable-next-line no-console
      console.error('[diag] copy failed — payload below\n', payload);
    }
  };

  render() {
    if (!this.state.error) return this.props.children;

    if (this.props.variant === 'root') {
      return (
        <RootCrashScreen
          label={this.props.label}
          error={this.state.error}
          componentStack={this.state.componentStack}
          ring={getDiagRing()}
          onReload={this.reload}
          onCopy={this.copyDiagnostics}
        />
      );
    }
    return (
      <PaneCrashScreen
        label={this.props.label}
        error={this.state.error}
        onRetry={this.reset}
        onCopy={this.copyDiagnostics}
      />
    );
  }
}

// ---------------------------------------------------------------------------
// Presentational
// ---------------------------------------------------------------------------

function formatDiagnostics({
  label, error, componentStack, ring,
}: {
  label: string;
  error: Error | null;
  componentStack: string | null;
  ring: DiagEvent[];
}): string {
  const head = [
    `# Frontend diagnostic`,
    `timestamp: ${new Date().toISOString()}`,
    `url: ${typeof window !== 'undefined' ? window.location.href : ''}`,
    `userAgent: ${typeof navigator !== 'undefined' ? navigator.userAgent : ''}`,
    `viewport: ${typeof window !== 'undefined' ? `${window.innerWidth}x${window.innerHeight}` : ''}`,
    `boundary: ${label}`,
    ``,
  ].join('\n');

  const errBlock = error ? [
    `## React render error`,
    `message: ${error.message}`,
    `stack:`,
    error.stack ?? '(no stack)',
    componentStack ? `componentStack:\n${componentStack}` : '',
    ``,
  ].join('\n') : '';

  const ringBlock = ring.length ? [
    `## Diagnostic ring (last ${ring.length})`,
    ...ring.map((e, i) =>
      `${i + 1}. [${e.ts}] ${e.kind}: ${e.message}` +
      (e.source ? `  (source: ${e.source})` : '') +
      (e.stack ? `\n   stack:\n${indent(e.stack, '   ')}` : '') +
      (e.componentStack ? `\n   componentStack:\n${indent(e.componentStack, '   ')}` : ''),
    ),
  ].join('\n\n') : '## Diagnostic ring: empty';

  return [head, errBlock, ringBlock].filter(Boolean).join('\n');
}

function indent(text: string, prefix: string): string {
  return text.split('\n').map(l => prefix + l).join('\n');
}

function RootCrashScreen({
  label, error, componentStack, ring, onReload, onCopy,
}: {
  label: string;
  error: Error;
  componentStack: string | null;
  ring: DiagEvent[];
  onReload: () => void;
  onCopy: () => void;
}) {
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 100000,
      background: 'var(--bg-root, #0b0f17)',
      color: 'var(--text-primary, #e2e8f0)',
      padding: '2rem',
      overflow: 'auto',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 13,
    }}>
      <div style={{ maxWidth: 1100, margin: '0 auto' }}>
        <div style={{
          color: 'var(--accent-red, #ef4444)',
          fontSize: 22, fontWeight: 700, marginBottom: 8,
        }}>
          ✗ Frontend crashed ({label})
        </div>
        <div style={{ color: 'var(--text-muted, #94a3b8)', marginBottom: 20 }}>
          The React tree threw during render. No tabs left to recover into.
          Stack and last {ring.length} diagnostic event{ring.length === 1 ? '' : 's'} below.
        </div>

        <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
          <button onClick={onReload} style={primaryBtn}>Reload page</button>
          <button onClick={onCopy} style={secondaryBtn}>Copy diagnostics</button>
        </div>

        <Section title={`Error: ${error.message}`}>
          <pre style={preStyle}>{error.stack || '(no stack)'}</pre>
          {componentStack && (
            <>
              <div style={{ color: 'var(--text-muted)', marginTop: 12, marginBottom: 4 }}>
                React componentStack:
              </div>
              <pre style={preStyle}>{componentStack}</pre>
            </>
          )}
        </Section>

        <Section title={`Diagnostic ring (${ring.length})`}>
          {ring.length === 0 ? (
            <div style={{ color: 'var(--text-muted)' }}>(empty)</div>
          ) : (
            ring.slice().reverse().map((e, i) => (
              <div key={i} style={{ marginBottom: 12 }}>
                <div style={{ color: 'var(--accent-yellow, #f7bd5c)' }}>
                  [{e.ts}] {e.kind} — {e.source || ''}
                </div>
                <div style={{ color: 'var(--text-primary)' }}>{e.message}</div>
                {e.stack && <pre style={preStyle}>{e.stack}</pre>}
                {e.componentStack && <pre style={preStyle}>{e.componentStack}</pre>}
              </div>
            ))
          )}
        </Section>
      </div>
    </div>
  );
}

function PaneCrashScreen({
  label, error, onRetry, onCopy,
}: {
  label: string;
  error: Error;
  onRetry: () => void;
  onCopy: () => void;
}) {
  return (
    <div style={{
      padding: 12,
      height: '100%',
      overflow: 'auto',
      background: 'var(--bg-panel, #11151c)',
      color: 'var(--text-primary, #e2e8f0)',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 12,
    }}>
      <div style={{
        color: 'var(--accent-red, #ef4444)', fontWeight: 700, marginBottom: 6,
      }}>
        ✗ {label} pane crashed
      </div>
      <div style={{ color: 'var(--text-secondary, #cbd5e1)', marginBottom: 8 }}>
        {error.message}
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
        <button onClick={onRetry} style={smallBtn}>Retry</button>
        <button onClick={onCopy} style={smallBtn}>Copy diag</button>
      </div>
      <details>
        <summary style={{ cursor: 'pointer', color: 'var(--text-muted)' }}>Stack</summary>
        <pre style={{ ...preStyle, marginTop: 6 }}>{error.stack || '(no stack)'}</pre>
      </details>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details open style={{
      border: '1px solid var(--border-default, #2a313d)',
      borderRadius: 6,
      padding: 12,
      marginBottom: 16,
      background: 'var(--bg-panel, #11151c)',
    }}>
      <summary style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--text-primary)' }}>
        {title}
      </summary>
      <div style={{ marginTop: 12 }}>{children}</div>
    </details>
  );
}

const preStyle: React.CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  background: 'rgba(0,0,0,0.25)',
  border: '1px solid var(--border-default, #2a313d)',
  borderRadius: 4,
  padding: 8,
  margin: 0,
  fontSize: 12,
  lineHeight: 1.45,
};

const primaryBtn: React.CSSProperties = {
  background: 'var(--accent-blue, #3b82f6)',
  color: '#fff',
  border: 'none',
  borderRadius: 4,
  padding: '6px 14px',
  cursor: 'pointer',
  fontWeight: 600,
};

const secondaryBtn: React.CSSProperties = {
  background: 'transparent',
  color: 'var(--text-primary, #e2e8f0)',
  border: '1px solid var(--border-default, #2a313d)',
  borderRadius: 4,
  padding: '6px 14px',
  cursor: 'pointer',
};

const smallBtn: React.CSSProperties = {
  background: 'transparent',
  color: 'var(--text-primary, #e2e8f0)',
  border: '1px solid var(--border-default, #2a313d)',
  borderRadius: 4,
  padding: '2px 8px',
  cursor: 'pointer',
  fontSize: 11,
};
