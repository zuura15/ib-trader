import { useEffect } from 'react';
import {
  DEFAULT_SETTINGS, setUserSetting, useUserSetting,
} from '../../data/userSettings';

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SettingsModal({ open, onClose }: Props) {
  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const signalActiveBars = useUserSetting('signalActiveBars');
  const brokenLookbackMinutes = useUserSetting('brokenLookbackMinutes');

  if (!open) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 9000,
        background: 'rgba(0,0,0,0.4)',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: 80,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 460, maxWidth: 'calc(100% - 32px)',
          background: 'var(--panel-bg, #fff)',
          color: 'var(--text-primary)',
          border: '1px solid var(--border-default)',
          borderRadius: 6,
          boxShadow: '0 10px 30px rgba(0,0,0,0.18)',
          overflow: 'hidden',
        }}
      >
        <header
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '10px 14px', borderBottom: '1px solid var(--border-default)',
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600 }}>Settings</span>
          <button
            onClick={onClose}
            title="Close (Esc)"
            style={{
              background: 'transparent', border: 'none',
              fontSize: 16, color: 'var(--text-muted)', cursor: 'pointer',
            }}
          >
            ×
          </button>
        </header>
        <div style={{ padding: '12px 14px' }}>
          <SectionTitle>Chart signals</SectionTitle>
          <SettingRow
            label="Buy/Sell signal lifetime"
            help={`Bars the badge stays at full opacity after firing. ${
              signalActiveBars
            } bars × 3 min = ${signalActiveBars * 3} min active. After that the badge dims to mark a stale signal.`}
          >
            <NumberInput
              min={1}
              max={120}
              step={1}
              value={signalActiveBars}
              onChange={(n) => setUserSetting('signalActiveBars', n)}
              suffix="bars"
            />
          </SettingRow>

          <SectionTitle>Support/Resistance</SectionTitle>
          <SettingRow
            label="Broken-line lookback"
            help="When the &quot;Broken&quot; filter is on, broken S/R lines stay drawn for this many minutes after the breach."
          >
            <NumberInput
              min={3}
              max={720}
              step={3}
              value={brokenLookbackMinutes}
              onChange={(n) => setUserSetting('brokenLookbackMinutes', n)}
              suffix="min"
            />
          </SettingRow>
        </div>
        <footer
          style={{
            padding: '8px 14px', borderTop: '1px solid var(--border-default)',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            background: 'var(--bg-secondary, transparent)',
            fontSize: 11, color: 'var(--text-muted)',
          }}
        >
          <span>Saved automatically. Synced across tabs.</span>
          <button
            onClick={() => {
              setUserSetting('signalActiveBars', DEFAULT_SETTINGS.signalActiveBars);
              setUserSetting('brokenLookbackMinutes', DEFAULT_SETTINGS.brokenLookbackMinutes);
            }}
            style={{
              background: 'transparent', border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)', fontSize: 11,
              padding: '2px 8px', borderRadius: 3, cursor: 'pointer',
            }}
          >
            Reset to defaults
          </button>
        </footer>
      </div>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10, letterSpacing: '0.18em', textTransform: 'uppercase',
        color: 'var(--text-muted)', margin: '6px 0 8px',
      }}
    >
      {children}
    </div>
  );
}

function SettingRow({
  label, help, children,
}: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <span style={{ flex: 1, fontSize: 12, fontWeight: 500 }}>{label}</span>
        {children}
      </div>
      {help && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.4 }}>
          {help.replaceAll('&quot;', '"')}
        </div>
      )}
    </div>
  );
}

function NumberInput({
  value, onChange, min, max, step, suffix,
}: {
  value: number; onChange: (n: number) => void;
  min: number; max: number; step: number; suffix?: string;
}) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <input
        type="number"
        min={min} max={max} step={step}
        value={value}
        onChange={(e) => {
          const n = parseInt(e.target.value, 10);
          if (!Number.isFinite(n)) return;
          onChange(Math.max(min, Math.min(max, n)));
        }}
        style={{
          width: 70, fontSize: 12,
          padding: '2px 4px',
          border: '1px solid var(--border-default)',
          borderRadius: 3,
          background: 'var(--panel-bg, #fff)',
          color: 'var(--text-primary)',
          fontVariantNumeric: 'tabular-nums',
        }}
      />
      {suffix && (
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{suffix}</span>
      )}
    </span>
  );
}
