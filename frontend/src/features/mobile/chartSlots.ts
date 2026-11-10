/** Mobile Graphs tab: chart slot order and the remembered open set. */
import { useState } from 'react';

/** Desktop order: gold pair, Nasdaq pair, Dow, crude. */
const SLOT_ORDER = [1, 7, 8, 9, 10, 5];
const OPEN_KEY = 'ib-mobile-open-charts';

/** Known desktop slots first, then any other chart-bot the API reports. */
export function chartSlots(sym: Record<number, string>): number[] {
  const known = Object.keys(sym).length === 0
    ? SLOT_ORDER : SLOT_ORDER.filter((s) => sym[s]);
  const extra = Object.keys(sym).map(Number)
    .filter((s) => !SLOT_ORDER.includes(s)).sort((a, b) => a - b);
  return [...known, ...extra];
}

/** Open-panel set, remembered across reloads. */
export function useOpenCharts(): [number[], (v: number[]) => void] {
  const [open, setOpenRaw] = useState<number[]>(() => {
    try {
      const v = JSON.parse(localStorage.getItem(OPEN_KEY) ?? 'null');
      return Array.isArray(v) ? v.filter((n) => Number.isInteger(n)) : [SLOT_ORDER[0]];
    } catch { return [SLOT_ORDER[0]]; }
  });
  const setOpen = (v: number[]) => {
    setOpenRaw(v);
    try { localStorage.setItem(OPEN_KEY, JSON.stringify(v)); } catch { /* private mode */ }
  };
  return [open, setOpen];
}

