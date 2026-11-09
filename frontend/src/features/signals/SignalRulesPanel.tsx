/**
 * Recommendation rules editor (GH #102) — the three rule groups, every
 * rule individually switchable with its parameters. Rule parameters are
 * rendered from the registry's schemas, so new rules show up here
 * without UI changes. Edits apply at once and auto-save (global).
 */
import { useEffect, useRef, useState } from 'react';
import {
  applySignalConfigLocal, getSignalConfig, saveSignalConfig, useSignalConfig,
} from './configStore';
import { defaultSignalConfig } from './defaults';
import { PRIMARY_RULES, SECONDARY_RULES } from './rules';
import type {
  ParamDef, ParamValue, RuleDef, SignalConfig, TimeEffect, TimeEvent, TimeRange,
} from './types';

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SignalRulesPanel({ open, onClose }: Props) {
  const cfg = useSignalConfig();
  const [status, setStatus] = useState<string>('');
  const saveTimer = useRef<number | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  // Flush a pending save when the panel unmounts.
  useEffect(() => () => {
    if (saveTimer.current != null) {
      window.clearTimeout(saveTimer.current);
      void saveSignalConfig(getSignalConfig());
    }
  }, []);

  // Apply locally right away (charts follow live); debounce the PUT so
  // typing in a field doesn't fire one request per keystroke.
  const update = (fn: (c: SignalConfig) => SignalConfig) => {
    applySignalConfigLocal(fn(structuredClone(getSignalConfig())));
    setStatus('saving…');
    if (saveTimer.current != null) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(async () => {
      saveTimer.current = null;
      const err = await saveSignalConfig(getSignalConfig());
      setStatus(err ?? 'saved');
    }, 700);
  };

  if (!open) return null;
  const c = cfg;

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 9000, background: 'rgba(0,0,0,0.4)',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: 40, overflowY: 'auto',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        data-testid="signal-rules-panel"
        style={{
          width: 760, maxWidth: 'calc(100% - 32px)', marginBottom: 40,
          background: 'var(--panel-bg, #fff)', color: 'var(--text-primary)',
          border: '1px solid var(--border-default)', borderRadius: 6,
          boxShadow: '0 10px 30px rgba(0,0,0,0.18)',
        }}
      >
        <header style={{
          display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px',
          borderBottom: '1px solid var(--border-default)',
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>
            Trade recommendation rules
          </span>
          <label style={{ fontSize: 12, display: 'flex', gap: 6, alignItems: 'center' }}>
            <Check
              checked={c.enabled}
              onChange={(v) => update((x) => ({ ...x, enabled: v }))}
            />
            Show on charts
          </label>
          <span style={{ fontSize: 11, color: 'var(--text-muted)', minWidth: 60 }}>{status}</span>
          <button onClick={onClose} title="Close (Esc)" style={iconBtn}>×</button>
        </header>

        <div style={{ padding: '12px 14px' }}>
          <Intro />

          <Section title="General">
            <Grid>
              <NumField label="Buy / Sell at score ≥" value={c.thresholds.buy} min={0.05} max={1} step={0.05}
                onChange={(v) => update((x) => ({ ...x, thresholds: { ...x.thresholds, buy: v } }))} />
              <NumField label="Strong at score ≥" value={c.thresholds.strong} min={0.1} max={1} step={0.05}
                onChange={(v) => update((x) => ({ ...x, thresholds: { ...x.thresholds, strong: v } }))} />
              <NumField label="Ignore signals weaker than" value={c.thresholds.minStrength} min={0} max={1} step={0.05}
                onChange={(v) => update((x) => ({ ...x, thresholds: { ...x.thresholds, minStrength: v } }))} />
              <NumField label="ATR period" unit="bars" value={c.analysis.atrPeriod} min={2} max={200} step={1}
                onChange={(v) => update((x) => ({ ...x, analysis: { ...x.analysis, atrPeriod: v } }))} />
              <NumField label="Swing point: bars left" value={c.analysis.pivotLeft} min={1} max={20} step={1}
                onChange={(v) => update((x) => ({ ...x, analysis: { ...x.analysis, pivotLeft: v } }))} />
              <NumField label="Swing point: bars right" unit="lag" value={c.analysis.pivotRight} min={1} max={20} step={1}
                help="A swing point is only known this many bars later — larger is steadier but slower."
                onChange={(v) => update((x) => ({ ...x, analysis: { ...x.analysis, pivotRight: v } }))} />
              <BoolField label="Badge on chart" value={c.display.badge}
                onChange={(v) => update((x) => ({ ...x, display: { ...x.display, badge: v } }))} />
              <BoolField label="Triangles where the call changed" value={c.display.history}
                onChange={(v) => update((x) => ({ ...x, display: { ...x.display, history: v } }))} />
              <NumField label="Show triangles for the last" unit="min" value={c.display.historyMinutes} min={5} max={1440} step={5}
                help="Solid = strong, outline = plain Buy/Sell. Hover a triangle for the reasons."
                onChange={(v) => update((x) => ({ ...x, display: { ...x.display, historyMinutes: v } }))} />
              <TextField label="Time zone for time rules" value={c.timeZone}
                onChange={(v) => update((x) => ({ ...x, timeZone: v }))} />
            </Grid>
          </Section>

          <Section title="1 · Primary signals — make the call"
            note="Each votes a direction and a strength. No vote → Neutral; groups 2 and 3 can't create a call.">
            {PRIMARY_RULES.map((r) => (
              <RuleCard key={r.id} def={r} cfg={c} update={update} showWeight />
            ))}
          </Section>

          <Section title="2 · Reinforce / detract"
            note="Move a call up or down one grade, or veto it to Neutral. Never flips a call.">
            {SECONDARY_RULES.map((r) => (
              <RuleCard key={r.id} def={r} cfg={c} update={update} />
            ))}
          </Section>

          <Section title="3 · Time rules"
            note={`In ${c.timeZone}. "No trade" blocks the call; "weak" caps it at plain Buy/Sell.`}>
            <TimeEventsEditor
              events={c.timeEvents}
              onChange={(ev) => update((x) => ({ ...x, timeEvents: ev }))}
            />
            <div style={{ ...card, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              <Check checked={c.hourRollover.enabled}
                onChange={(v) => update((x) => ({ ...x, hourRollover: { ...x.hourRollover, enabled: v } }))} />
              <span style={{ fontSize: 12, fontWeight: 600 }}>Every hour rollover</span>
              <MiniNum value={c.hourRollover.before} unit="min before"
                onChange={(v) => update((x) => ({ ...x, hourRollover: { ...x.hourRollover, before: v } }))} />
              <MiniNum value={c.hourRollover.after} unit="min after"
                onChange={(v) => update((x) => ({ ...x, hourRollover: { ...x.hourRollover, after: v } }))} />
              <EffectSelect value={c.hourRollover.effect}
                onChange={(v) => update((x) => ({ ...x, hourRollover: { ...x.hourRollover, effect: v } }))} />
            </div>
            <TimeRangesEditor
              ranges={c.timeRanges}
              onChange={(rs) => update((x) => ({ ...x, timeRanges: rs }))}
            />
          </Section>
        </div>

        <footer style={{
          padding: '8px 14px', borderTop: '1px solid var(--border-default)',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          fontSize: 11, color: 'var(--text-muted)', background: 'var(--bg-secondary, transparent)',
        }}>
          <span>Global — applies to every chart and device. Label only; never places orders.</span>
          <button
            onClick={() => {
              if (window.confirm('Reset every recommendation rule to its default?')) {
                update(() => defaultSignalConfig());
              }
            }}
            style={smallBtn}
          >
            Reset to defaults
          </button>
        </footer>
      </div>
    </div>
  );
}

function Intro() {
  return (
    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 10, lineHeight: 1.5 }}>
      The chart badge goes Strong Sell → Sell → Neutral → Buy → Strong Buy, or <b>No trade</b> during
      a blocked time. Primary signals are added up (direction × strength × weight) into a score;
      group 2 adjusts that call; group 3 can block or cap it. Tolerances are in ATR (average bar range).
    </div>
  );
}

function RuleCard({
  def, cfg, update, showWeight,
}: {
  def: RuleDef;
  cfg: SignalConfig;
  update: (fn: (c: SignalConfig) => SignalConfig) => void;
  showWeight?: boolean;
}) {
  const rc = cfg.rules[def.id];
  if (!rc) return null;
  const setRule = (patch: Partial<typeof rc>) =>
    update((x) => ({ ...x, rules: { ...x.rules, [def.id]: { ...x.rules[def.id], ...patch } } }));
  const setParam = (k: string, v: ParamValue) =>
    update((x) => ({
      ...x,
      rules: {
        ...x.rules,
        [def.id]: { ...x.rules[def.id], params: { ...x.rules[def.id].params, [k]: v } },
      },
    }));
  return (
    <div style={{ ...card, opacity: rc.enabled ? 1 : 0.6 }} data-testid={`rule-card-${def.id}`}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Check checked={rc.enabled} onChange={(v) => setRule({ enabled: v })} />
        <span style={{ fontSize: 12, fontWeight: 600, flex: 1 }}>{def.label}</span>
        {showWeight && (
          <MiniNum value={rc.weight} unit="weight" step={0.1} min={0} max={5}
            onChange={(v) => setRule({ weight: v })} />
        )}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', margin: '2px 0 6px 24px' }}>
        {def.description}
      </div>
      {rc.enabled && (
        <Grid>
          {def.params.map((p) => (
            <ParamField key={p.key} def={p} value={rc.params[p.key] ?? p.default}
              onChange={(v) => setParam(p.key, v)} />
          ))}
        </Grid>
      )}
    </div>
  );
}

function ParamField({
  def, value, onChange,
}: { def: ParamDef; value: ParamValue; onChange: (v: ParamValue) => void }) {
  if (def.type === 'boolean') {
    return <BoolField label={def.label} value={value === true} help={def.help} onChange={onChange} />;
  }
  return (
    <NumField label={def.label} unit={def.unit} help={def.help} value={Number(value)}
      min={def.min} max={def.max} step={def.step} onChange={onChange} />
  );
}

let idSeq = 0;
const newId = (p: string) => `${p}${Date.now().toString(36)}${(idSeq++).toString(36)}`;

function TimeEventsEditor({
  events, onChange,
}: { events: TimeEvent[]; onChange: (e: TimeEvent[]) => void }) {
  const set = (i: number, patch: Partial<TimeEvent>) =>
    onChange(events.map((e, j) => (j === i ? { ...e, ...patch } : e)));
  const sorted = [...events.keys()].sort((a, b) => events[a].time.localeCompare(events[b].time));
  return (
    <div style={card}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Specific times</div>
      {sorted.map((i) => {
        const e = events[i];
        return (
          <div key={e.id} style={rowStyle(e.enabled)}>
            <Check checked={e.enabled} onChange={(v) => set(i, { enabled: v })} />
            <input type="time" value={e.time} onChange={(ev) => set(i, { time: ev.target.value })} style={inputStyle(90)} />
            <input value={e.label} onChange={(ev) => set(i, { label: ev.target.value })}
              style={{ ...inputStyle(0), flex: 1, minWidth: 120 }} placeholder="label" />
            <MiniNum value={e.before} unit="before" onChange={(v) => set(i, { before: v })} />
            <MiniNum value={e.after} unit="after" onChange={(v) => set(i, { after: v })} />
            <EffectSelect value={e.effect} onChange={(v) => set(i, { effect: v })} />
            <button title="Remove" style={iconBtn} onClick={() => onChange(events.filter((_, j) => j !== i))}>×</button>
          </div>
        );
      })}
      <button style={{ ...smallBtn, marginTop: 4 }}
        onClick={() => onChange([...events, {
          id: newId('t'), time: '09:00', label: 'New time', before: 5, after: 0,
          effect: 'block', enabled: true,
        }])}>
        + Add time
      </button>
    </div>
  );
}

function TimeRangesEditor({
  ranges, onChange,
}: { ranges: TimeRange[]; onChange: (r: TimeRange[]) => void }) {
  const set = (i: number, patch: Partial<TimeRange>) =>
    onChange(ranges.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div style={card}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Time ranges</div>
      {ranges.map((r, i) => (
        <div key={r.id} style={rowStyle(r.enabled)}>
          <Check checked={r.enabled} onChange={(v) => set(i, { enabled: v })} />
          <input type="time" value={r.start} onChange={(ev) => set(i, { start: ev.target.value })} style={inputStyle(90)} />
          <span style={{ fontSize: 11 }}>to</span>
          <input type="time" value={r.end} onChange={(ev) => set(i, { end: ev.target.value })} style={inputStyle(90)} />
          <input value={r.label} onChange={(ev) => set(i, { label: ev.target.value })}
            style={{ ...inputStyle(0), flex: 1, minWidth: 120 }} placeholder="label" />
          <EffectSelect value={r.effect} onChange={(v) => set(i, { effect: v })} />
          <button title="Remove" style={iconBtn} onClick={() => onChange(ranges.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
      <button style={{ ...smallBtn, marginTop: 4 }}
        onClick={() => onChange([...ranges, {
          id: newId('r'), start: '09:00', end: '10:00', label: 'New range',
          effect: 'weak', enabled: true,
        }])}>
        + Add range
      </button>
    </div>
  );
}

// ── Small controls ───────────────────────────────────────────────────

function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{
        fontSize: 10, letterSpacing: '0.18em', textTransform: 'uppercase',
        color: 'var(--text-muted)', margin: '6px 0 2px',
      }}>{title}</div>
      {note && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>{note}</div>}
      {children}
    </div>
  );
}

function Grid({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
      gap: '6px 14px', marginLeft: 24,
    }}>{children}</div>
  );
}

function NumField({
  label, value, onChange, min, max, step, unit, help,
}: {
  label: string; value: number; onChange: (v: number) => void;
  min?: number; max?: number; step?: number; unit?: string; help?: string;
}) {
  return (
    <label title={help} style={{ fontSize: 11, display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ color: 'var(--text-secondary)' }}>{label}{help ? ' ⓘ' : ''}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <input type="number" value={value} min={min} max={max} step={step ?? 1}
          onChange={(e) => {
            const n = Number(e.target.value);
            if (e.target.value === '' || !Number.isFinite(n)) return;
            onChange(Math.max(min ?? -Infinity, Math.min(max ?? Infinity, n)));
          }}
          style={inputStyle(80)} />
        {unit && <span style={{ color: 'var(--text-muted)' }}>{unit}</span>}
      </span>
    </label>
  );
}

function BoolField({
  label, value, onChange, help,
}: { label: string; value: boolean; onChange: (v: boolean) => void; help?: string }) {
  return (
    <label title={help} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
      <Check checked={value} onChange={onChange} />
      <span style={{ color: 'var(--text-secondary)' }}>{label}{help ? ' ⓘ' : ''}</span>
    </label>
  );
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label style={{ fontSize: 11, display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
      <input value={value} onChange={(e) => onChange(e.target.value)} style={inputStyle(180)} />
    </label>
  );
}

function MiniNum({
  value, unit, onChange, step = 1, min = 0, max = 240,
}: { value: number; unit: string; onChange: (v: number) => void; step?: number; min?: number; max?: number }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, fontSize: 11 }}>
      <input type="number" value={value} step={step} min={min} max={max}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (e.target.value !== '' && Number.isFinite(n)) onChange(Math.max(min, Math.min(max, n)));
        }}
        style={inputStyle(52)} />
      <span style={{ color: 'var(--text-muted)' }}>{unit}</span>
    </span>
  );
}

function EffectSelect({ value, onChange }: { value: TimeEffect; onChange: (v: TimeEffect) => void }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value as TimeEffect)} style={inputStyle(96)}>
      <option value="block">No trade</option>
      <option value="weak">Weak</option>
    </select>
  );
}

function Check({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
      style={{ width: 15, height: 15, cursor: 'pointer', accentColor: 'var(--accent-blue)' }} />
  );
}

const card: React.CSSProperties = {
  border: '1px solid var(--border-default)', borderRadius: 4,
  padding: '8px 10px', marginBottom: 8,
};
const rowStyle = (enabled: boolean): React.CSSProperties => ({
  display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
  padding: '3px 0', opacity: enabled ? 1 : 0.55,
});
const inputStyle = (w: number): React.CSSProperties => ({
  ...(w ? { width: w } : {}), fontSize: 12, padding: '2px 4px',
  border: '1px solid var(--border-default)', borderRadius: 3,
  background: 'var(--panel-bg, #fff)', color: 'var(--text-primary)',
  fontVariantNumeric: 'tabular-nums',
});
const smallBtn: React.CSSProperties = {
  background: 'transparent', border: '1px solid var(--border-default)',
  color: 'var(--text-secondary)', fontSize: 11, padding: '2px 8px',
  borderRadius: 3, cursor: 'pointer',
};
const iconBtn: React.CSSProperties = {
  background: 'transparent', border: 'none', fontSize: 16,
  color: 'var(--text-muted)', cursor: 'pointer', lineHeight: 1,
};
