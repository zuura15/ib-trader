/**
 * Typed user-settings store backed by localStorage.
 *
 * Each setting is keyed under the ``ib-trader:settings:`` namespace.
 * The ``useUserSetting`` hook subscribes to changes (cross-tab via
 * the storage event AND in-tab via a custom event) so any consumer
 * re-renders when the user updates a value via the settings modal.
 */
import { useCallback, useSyncExternalStore } from 'react';

export interface UserSettings {
  /** Number of 3-min bars a buy/sell signal stays "active" (full
   *  opacity) after firing. After this elapses the badge dims to
   *  indicate the trade window has closed. Default 2 (= 6 min). */
  signalActiveBars: number;
  /** Minutes back to keep broken S/R lines visible (when the
   *  Broken filter is on). Default 30. */
  brokenLookbackMinutes: number;
}

export const DEFAULT_SETTINGS: UserSettings = {
  signalActiveBars: 2,
  brokenLookbackMinutes: 30,
};

const KEY_PREFIX = 'ib-trader:settings:';
const CHANGE_EVENT = 'ib-trader:settings-change';

function read<K extends keyof UserSettings>(key: K): UserSettings[K] {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + key);
    if (raw == null) return DEFAULT_SETTINGS[key];
    const parsed = JSON.parse(raw);
    // Numeric settings: coerce + clamp. Non-numeric here would mean
    // a corrupted localStorage row; fall back to default.
    if (typeof DEFAULT_SETTINGS[key] === 'number') {
      const n = Number(parsed);
      return (Number.isFinite(n) ? n : DEFAULT_SETTINGS[key]) as UserSettings[K];
    }
    return parsed as UserSettings[K];
  } catch {
    return DEFAULT_SETTINGS[key];
  }
}

export function setUserSetting<K extends keyof UserSettings>(
  key: K, value: UserSettings[K],
): void {
  try {
    localStorage.setItem(KEY_PREFIX + key, JSON.stringify(value));
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { key } }));
  } catch { /* quota exceeded or storage disabled — ignore */ }
}

export function getUserSetting<K extends keyof UserSettings>(
  key: K,
): UserSettings[K] {
  return read(key);
}

/** React hook: subscribes to a single setting. Re-renders the caller
 *  whenever ``setUserSetting`` updates that key (this tab) or another
 *  tab writes the same key. Returns the current value. */
export function useUserSetting<K extends keyof UserSettings>(
  key: K,
): UserSettings[K] {
  const subscribe = useCallback((cb: () => void) => {
    const onChange = (e: Event) => {
      // In-tab change: only fire if it's the matching key. Cross-tab
      // ``storage`` event reports the full key including prefix.
      if (e instanceof CustomEvent && e.detail?.key === key) cb();
      else if (e instanceof StorageEvent && e.key === KEY_PREFIX + key) cb();
    };
    window.addEventListener(CHANGE_EVENT, onChange);
    window.addEventListener('storage', onChange);
    return () => {
      window.removeEventListener(CHANGE_EVENT, onChange);
      window.removeEventListener('storage', onChange);
    };
  }, [key]);
  const getSnapshot = useCallback(() => read(key), [key]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
