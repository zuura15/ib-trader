import { useState, useEffect, useRef, useCallback } from 'react';

const MAX_SYMBOLS = 50;

export function WatchlistConfig({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [input, setInput] = useState('');
  const [saving, setSaving] = useState(false);
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    fetch('/api/watchlist/symbols')
      .then(r => r.json())
      .then(d => setSymbols(d.symbols || []))
      .catch(() => {});
  }, [open]);

  const addSymbol = () => {
    const sym = input.trim().toUpperCase();
    if (!sym || symbols.includes(sym) || symbols.length >= MAX_SYMBOLS) return;
    setSymbols([...symbols, sym]);
    setInput('');
  };

  const removeSymbol = (idx: number) => {
    setSymbols(symbols.filter((_, i) => i !== idx));
  };

  // --- Drag-to-reorder (pointer events for mobile + desktop) ---
  const dragStartY = useRef(0);
  const dragStartIdx = useRef(0);
  const rowHeight = useRef(44);

  const onPointerDown = useCallback((e: React.PointerEvent, idx: number) => {
    e.preventDefault();
    setDragIdx(idx);
    setOverIdx(idx);
    dragStartY.current = e.clientY;
    dragStartIdx.current = idx;

    // Measure row height from the target
    const row = (e.target as HTMLElement).closest('[data-row]');
    if (row) rowHeight.current = row.getBoundingClientRect().height;

    const onMove = (ev: globalThis.PointerEvent) => {
      const delta = ev.clientY - dragStartY.current;
      const offset = Math.round(delta / rowHeight.current);
      const newIdx = Math.max(0, Math.min(symbols.length - 1, dragStartIdx.current + offset));
      setOverIdx(newIdx);
    };

    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);

      setDragIdx(null);
      setOverIdx(null);

      // Apply the reorder
      setSymbols(prev => {
        const fromIdx = dragStartIdx.current;
        const delta = (document as any).__lastOverIdx ?? fromIdx;
        if (fromIdx === delta) return prev;
        const next = [...prev];
        const [moved] = next.splice(fromIdx, 1);
        next.splice(delta, 0, moved);
        return next;
      });
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  }, [symbols.length]);

  // Store overIdx in a ref accessible from the onUp closure
  useEffect(() => {
    (document as any).__lastOverIdx = overIdx;
  }, [overIdx]);

  const save = async () => {
    setSaving(true);
    try {
      await fetch('/api/watchlist/symbols', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbols }),
      });
      onClose();
    } catch { /* ignore */ }
    setSaving(false);
  };

  if (!open) return null;

  // Build display order: if dragging, show where the item would land
  const displaySymbols = [...symbols];
  let draggedSymbol: string | null = null;
  if (dragIdx !== null && overIdx !== null && dragIdx !== overIdx) {
    draggedSymbol = displaySymbols[dragIdx];
    displaySymbols.splice(dragIdx, 1);
    displaySymbols.splice(overIdx, 0, draggedSymbol);
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 10000,
      background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }} onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--bg-primary)',
          border: '1px solid var(--border-default)',
          borderRadius: 8,
          width: Math.min(400, window.innerWidth - 32),
          maxHeight: '80vh',
          display: 'flex',
          flexDirection: 'column',
          boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
        }}
      >
        {/* Header */}
        <div style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--border-default)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>
            Watchlist Symbols
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {symbols.length} / {MAX_SYMBOLS}
          </span>
        </div>

        {/* Symbol list */}
        <div ref={listRef} style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
          {displaySymbols.map((sym, idx) => {
            const isDragged = dragIdx !== null && sym === draggedSymbol && idx === overIdx;
            const isBeingDragged = dragIdx !== null && idx === dragIdx && overIdx === dragIdx;
            return (
              <div
                key={`${sym}-${idx}`}
                data-row
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  padding: '8px 16px',
                  gap: 10,
                  minHeight: 44,
                  background: isDragged ? 'var(--bg-hover)' : 'transparent',
                  opacity: isBeingDragged ? 0.4 : 1,
                  borderTop: isDragged ? '2px solid var(--accent-blue)' : '2px solid transparent',
                  transition: dragIdx !== null ? 'none' : 'background 0.15s',
                }}
              >
                {/* Drag handle */}
                <span
                  onPointerDown={(e) => onPointerDown(e, symbols.indexOf(sym))}
                  style={{
                    cursor: 'grab',
                    color: 'var(--text-muted)',
                    fontSize: 14,
                    padding: '4px 2px',
                    touchAction: 'none',
                    userSelect: 'none',
                  }}
                >
                  ☰
                </span>

                <span className="font-mono" style={{
                  flex: 1, fontSize: 13, fontWeight: 600, color: 'var(--text-primary)',
                }}>
                  {sym}
                </span>

                <button
                  onClick={() => removeSymbol(symbols.indexOf(sym))}
                  style={{
                    background: 'none', border: 'none',
                    color: 'var(--accent-red)', fontSize: 13,
                    padding: '4px 6px', cursor: 'pointer',
                    minWidth: 28, minHeight: 28,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}
                  title="Remove"
                >
                  ✕
                </button>
              </div>
            );
          })}
          {symbols.length === 0 && (
            <div style={{ padding: '20px 16px', color: 'var(--text-muted)', fontSize: 12, textAlign: 'center' }}>
              No symbols — add one below
            </div>
          )}
        </div>

        {/* Add input */}
        <div style={{
          padding: '8px 16px',
          borderTop: '1px solid var(--border-default)',
          display: 'flex',
          gap: 8,
        }}>
          <input
            value={input}
            onChange={e => setInput(e.target.value.toUpperCase())}
            onKeyDown={e => e.key === 'Enter' && addSymbol()}
            placeholder="Add symbol..."
            className="font-mono"
            style={{
              flex: 1,
              background: 'var(--bg-secondary)',
              border: '1px solid var(--border-default)',
              borderRadius: 4,
              padding: '6px 10px',
              color: 'var(--text-primary)',
              fontSize: 13,
              outline: 'none',
            }}
          />
          <button onClick={addSymbol} disabled={!input.trim() || symbols.length >= MAX_SYMBOLS}
            style={{
              background: 'var(--accent-blue)',
              color: '#000',
              border: 'none',
              borderRadius: 4,
              padding: '6px 14px',
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
              opacity: !input.trim() ? 0.5 : 1,
            }}>
            Add
          </button>
        </div>

        {/* Footer */}
        <div style={{
          padding: '10px 16px',
          borderTop: '1px solid var(--border-default)',
          display: 'flex',
          justifyContent: 'flex-end',
          gap: 8,
        }}>
          <button onClick={onClose} style={{
            background: 'var(--bg-secondary)',
            color: 'var(--text-secondary)',
            border: '1px solid var(--border-default)',
            borderRadius: 4,
            padding: '6px 16px',
            fontSize: 12,
            cursor: 'pointer',
          }}>
            Cancel
          </button>
          <button onClick={save} disabled={saving} style={{
            background: 'var(--accent-green)',
            color: '#000',
            border: 'none',
            borderRadius: 4,
            padding: '6px 16px',
            fontSize: 12,
            fontWeight: 600,
            cursor: 'pointer',
          }}>
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
