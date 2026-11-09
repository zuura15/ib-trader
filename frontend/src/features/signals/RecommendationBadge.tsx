import { GRADE_LABEL } from './evaluate';
import type { Recommendation } from './types';

/** Chart badge for the current recommendation (GH #102); hover lists
 *  every rule that voted or adjusted, and any time rule in force. */
export function RecommendationBadge({
  rec, hover, onHover,
}: {
  rec: Recommendation;
  hover: boolean;
  onHover: (h: boolean) => void;
}) {
  const blocked = !!rec.blocked;
  const label = blocked ? 'No trade' : GRADE_LABEL[rec.grade];
  const bg = blocked ? 'var(--accent-yellow, #ca8a04)'
    : rec.grade > 0 ? 'var(--accent-green, #16a34a)'
    : rec.grade < 0 ? 'var(--accent-red, #dc2626)'
    : 'var(--bg-secondary, #555)';
  const fg = blocked || rec.grade !== 0 ? '#fff' : 'var(--text-secondary, #ccc)';
  const strong = Math.abs(rec.grade) === 2 && !blocked;
  return (
    <div
      data-testid="recommendation-badge"
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
      style={{
        position: 'absolute', top: 6, left: '50%', transform: 'translateX(-50%)',
        zIndex: 24, fontFamily: 'ui-monospace, monospace',
      }}
    >
      <div
        style={{
          padding: '2px 8px', borderRadius: 3, fontSize: 11,
          fontWeight: strong ? 800 : 700, letterSpacing: 0.5,
          textTransform: 'uppercase', whiteSpace: 'nowrap', cursor: 'default',
          background: bg, color: fg,
          boxShadow: strong ? '0 0 0 2px rgba(255,255,255,0.35) inset' : undefined,
        }}
      >
        {label}
        {rec.capped && !blocked && <span style={{ opacity: 0.8 }}> · capped</span>}
      </div>
      {hover && (
        <div
          style={{
            position: 'absolute', top: 'calc(100% + 4px)', left: '50%',
            transform: 'translateX(-50%)', minWidth: 260, maxWidth: 380,
            padding: '6px 8px', borderRadius: 4, fontSize: 11, lineHeight: 1.5,
            background: 'var(--bg-primary)', color: 'var(--text-primary)',
            border: '1px solid var(--border-default)',
            boxShadow: '0 4px 12px rgba(0,0,0,0.35)',
          }}
        >
          <div style={{ color: 'var(--text-muted)' }}>
            Score {rec.score >= 0 ? '+' : ''}{rec.score.toFixed(2)}
            {' · '}signals {GRADE_LABEL[rec.baseGrade]}
          </div>
          {rec.contributions.length === 0 && (
            <div style={{ color: 'var(--text-muted)' }}>No primary signal.</div>
          )}
          {rec.contributions.map((c, i) => (
            <div key={`c${i}`} style={{ color: c.direction > 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
              {c.direction > 0 ? '▲' : '▼'} {c.label}: {c.detail} ({c.strength.toFixed(2)})
            </div>
          ))}
          {rec.adjustments.map((a, i) => (
            <div key={`a${i}`}>
              {a.effect === 'veto' ? '⛔ veto' : a.effect === 'up' ? '↑ upgrade' : '↓ downgrade'}
              {' — '}{a.label}: {a.detail}
            </div>
          ))}
          {rec.blocked && <div>⏸ No trade — {rec.blocked.label}</div>}
          {rec.capped && <div>⏸ Capped at Buy/Sell — {rec.capped.label}</div>}
          <div style={{ color: 'var(--text-muted)', marginTop: 2 }}>
            Label only — never places orders.
          </div>
        </div>
      )}
    </div>
  );
}
