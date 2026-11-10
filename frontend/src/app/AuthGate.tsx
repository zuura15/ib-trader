/** Shows the access-key login screen until ib-api accepts this browser. */
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { AUTH_REQUIRED_EVENT, getSession, installAuthInterceptor, login } from './auth';

type GateState = 'checking' | 'open' | 'login';

/** Marks the login screen as a legitimate render for the white-screen watchdog. */
export const LOGIN_SHELL_CLASS = 'auth-login-shell';

const LIVE = import.meta.env.VITE_DATA_MODE === 'live';

export function AuthGate({ children }: { children: ReactNode }) {
  // Mock-data builds never talk to ib-api, so there is nothing to sign into.
  const [state, setState] = useState<GateState>(LIVE ? 'checking' : 'open');

  useEffect(() => {
    if (!LIVE) return;
    installAuthInterceptor();
    let cancelled = false;
    const onRequired = () => setState('login');
    window.addEventListener(AUTH_REQUIRED_EVENT, onRequired);
    getSession().then(
      (s) => {
        if (!cancelled) setState(!s.auth_required || s.authenticated ? 'open' : 'login');
      },
      (err) => {
        // ib-api unreachable: render the app anyway so it shows its normal
        // disconnected state. The interceptor flips us to login on any 401.
        console.warn('[auth] session check failed', err);
        if (!cancelled) setState('open');
      },
    );
    return () => {
      cancelled = true;
      window.removeEventListener(AUTH_REQUIRED_EVENT, onRequired);
    };
  }, []);

  if (state === 'checking') return <div style={screen} />;
  if (state === 'login') return <LoginScreen onSuccess={() => setState('open')} />;
  return <>{children}</>;
}

function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!key.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (await login(key.trim())) {
        setKey('');
        onSuccess();
      } else {
        setError('That access key was not accepted.');
      }
    } catch (err) {
      setError(`Can't reach the server (${err instanceof Error ? err.message : String(err)}).`);
    } finally {
      setBusy(false);
    }
  };

  return (
    // The class tells the white-screen watchdog (DiagOverlay) that this
    // screen rendered on purpose; without it the overlay pops up over it.
    <div className={LOGIN_SHELL_CLASS} style={screen}>
      <form onSubmit={submit} style={card} aria-labelledby="ibt-login-title">
        <h1 id="ibt-login-title" style={title}>IB Trader</h1>
        <p style={hint}>Enter the access key (API_SECRET_KEY on the server).</p>
        {/* Hidden username so password managers file the key under this site. */}
        <input type="text" name="username" autoComplete="username" value="ib-trader" readOnly hidden />
        <input
          type="password"
          name="password"
          autoComplete="current-password"
          autoFocus
          placeholder="Access key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          style={input}
          aria-invalid={error ? true : undefined}
        />
        {error && <div role="alert" style={errorText}>{error}</div>}
        <button type="submit" disabled={busy || !key.trim()} style={button}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}

const screen: React.CSSProperties = {
  position: 'fixed', inset: 0,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'var(--bg-root)', padding: '16px',
};
const card: React.CSSProperties = {
  width: '100%', maxWidth: 360,
  display: 'flex', flexDirection: 'column', gap: 12,
  padding: '24px', borderRadius: 12,
  background: 'var(--bg-primary)', border: '1px solid var(--border-default)',
  boxShadow: 'var(--shadow-panel)',
};
const title: React.CSSProperties = {
  margin: 0, fontSize: '1.25rem', fontWeight: 700, color: 'var(--text-heading)',
};
const hint: React.CSSProperties = { margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' };
const input: React.CSSProperties = {
  padding: '10px 12px', fontSize: '1rem', borderRadius: 8,
  background: 'var(--bg-secondary)', color: 'var(--text-primary)',
  border: '1px solid var(--border-default)',
};
const errorText: React.CSSProperties = { fontSize: '0.85rem', color: 'var(--accent-red)' };
const button: React.CSSProperties = {
  padding: '10px 12px', fontSize: '1rem', fontWeight: 600, borderRadius: 8,
  border: 'none', cursor: 'pointer',
  background: 'var(--accent-blue)', color: 'var(--bg-root)',
};
