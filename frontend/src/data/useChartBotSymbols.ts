import { useEffect, useState } from 'react';
import { getBots } from '../api/client';

/**
 * Map of chart-bot slot (1..N) → its resolved contract symbol (e.g.
 * ``CLU6``), from ``/api/bots``. Used to label chart tabs with the actual
 * symbol instead of a static description, and refreshed so a contract
 * roll (CLQ6 → CLU6) updates the tab without a redeploy.
 */
export function useChartBotSymbols(): Record<number, string> {
  const [map, setMap] = useState<Record<number, string>>({});
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const bots = await getBots();
        if (cancelled) return;
        const next: Record<number, string> = {};
        for (const b of bots) {
          const m = /^chart-bot-(\d+)$/.exec(b.id ?? '');
          if (!m) continue;
          let sym = '';
          try {
            const arr = b.symbols_json ? JSON.parse(b.symbols_json) : [];
            sym = Array.isArray(arr) && arr.length ? String(arr[0]) : '';
          } catch { /* malformed — leave blank */ }
          if (sym) next[Number(m[1])] = sym.toUpperCase();
        }
        setMap(next);
      } catch { /* transient — keep the last good map */ }
    };
    load();
    const id = window.setInterval(load, 60_000);  // pick up contract rolls
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);
  return map;
}
