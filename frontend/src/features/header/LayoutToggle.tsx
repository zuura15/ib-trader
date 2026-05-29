import { useStore } from '../../data/store';

const LABELS: Record<'auto' | 'mobile' | 'desktop', string> = {
  auto: 'Auto',
  mobile: 'Mobile',
  desktop: 'Desktop',
};

const ORDER: Array<'auto' | 'mobile' | 'desktop'> = ['auto', 'mobile', 'desktop'];

/**
 * Manual override for layout mode (auto / mobile / desktop).
 *
 * The auto mode defers to ``window.matchMedia('(max-width: 767px)')`` —
 * which works correctly on Firefox and Safari but is regularly defeated
 * on Chrome Android by the per-origin "Desktop site" preference and by
 * UA-Client-Hints caching that survives "Clear site data". This toggle
 * is the escape hatch.
 *
 * Cycles auto → mobile → desktop → auto on click. Persisted to
 * localStorage via the store's setter.
 */
export function LayoutToggle({ size = 'sm' }: { size?: 'sm' | 'xs' }) {
  const layoutOverride = useStore((s) => s.layoutOverride);
  const setLayoutOverride = useStore((s) => s.setLayoutOverride);

  const cycle = () => {
    const idx = ORDER.indexOf(layoutOverride);
    setLayoutOverride(ORDER[(idx + 1) % ORDER.length]);
  };

  const fontSize = size === 'xs' ? 10 : 11;
  const padding = size === 'xs' ? '2px 6px' : '3px 8px';

  return (
    <button
      type="button"
      onClick={cycle}
      title="Cycle layout: auto / mobile / desktop"
      style={{
        background: layoutOverride === 'auto'
          ? 'transparent'
          : 'var(--badge-blue-bg, rgba(59,130,246,0.15))',
        color: layoutOverride === 'auto'
          ? 'var(--text-muted)'
          : 'var(--accent-blue)',
        border: '1px solid var(--border-default)',
        borderRadius: 4,
        padding,
        fontSize,
        cursor: 'pointer',
        minHeight: 28,
        whiteSpace: 'nowrap',
      }}
    >
      {LABELS[layoutOverride]}
    </button>
  );
}
