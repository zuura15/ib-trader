import type { ReactNode, MouseEvent } from 'react';

const accentColors: Record<string, string> = {
  blue: '#67b7ff',
  green: '#44d89e',
  amber: '#f7bd5c',
  red: '#ff6b6b',
  purple: '#bc8cff',
};

/**
 * Stop mousedown from propagating to flexlayout's drag handler,
 * but allow interactive elements (buttons, inputs, links) to work normally.
 */
function stopDrag(e: MouseEvent) {
  const target = e.target as HTMLElement;
  const tag = target.tagName;
  // Don't interfere with buttons, inputs, links, or elements inside them
  if (tag === 'BUTTON' || tag === 'INPUT' || tag === 'A' || tag === 'SELECT' ||
      target.closest('button') || target.closest('a') || target.closest('input')) {
    return;
  }
  e.stopPropagation();
}

export function PanelShell({
  title,
  accent = 'blue',
  children,
  right,
}: {
  title: string;
  accent?: 'blue' | 'green' | 'amber' | 'red' | 'purple';
  children: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="panel-shell">
      <div className="panel-header">
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span className="accent-dot" style={{ background: accentColors[accent] }} />
          <span className="panel-title">{title}</span>
        </div>
        {right}
      </div>
      <div className="panel-content" onMouseDown={stopDrag}>{children}</div>
    </div>
  );
}
