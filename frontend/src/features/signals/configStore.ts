/**
 * Global recommendation config (GH #102), shared by every chart and
 * device: loaded from GET /api/signal-rules, saved with PUT. Re-polled
 * so an edit on another device shows up within a minute.
 */
import { useSyncExternalStore } from 'react';
import { defaultSignalConfig, mergeSignalConfig } from './defaults';
import type { SignalConfig } from './types';

let current: SignalConfig = defaultSignalConfig();
let loaded = false;
let started = false;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

async function load(): Promise<void> {
  try {
    const r = await fetch('/api/signal-rules');
    if (!r.ok) return;
    const body = await r.json();
    const next = mergeSignalConfig(body?.config);
    if (JSON.stringify(next) !== JSON.stringify(current) || !loaded) {
      current = next;
      loaded = true;
      emit();
    }
  } catch { /* offline / API restarting — keep the last config */ }
}

function start() {
  if (started) return;
  started = true;
  void load();
  window.setInterval(load, 60_000);
}

export function getSignalConfig(): SignalConfig {
  return current;
}

/** Apply a config locally (charts update at once) without saving. */
export function applySignalConfigLocal(next: SignalConfig): void {
  current = next;
  emit();
}

/** Save the whole config. Applies locally at once; resolves to an error
 *  message on failure (the local change is kept so the editor isn't lost). */
export async function saveSignalConfig(next: SignalConfig): Promise<string | null> {
  current = next;
  emit();
  try {
    const r = await fetch('/api/signal-rules', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: next }),
    });
    return r.ok ? null : `save failed (HTTP ${r.status})`;
  } catch (e) {
    return `save failed: ${String(e)}`;
  }
}

export function useSignalConfig(): SignalConfig {
  start();
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    () => current,
    () => current,
  );
}
