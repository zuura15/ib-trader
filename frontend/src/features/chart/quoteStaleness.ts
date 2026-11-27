/**
 * "Is this chart's live quote stale?" — drives the grey STALE tag in
 * the chart header.
 *
 * Futures only: CME Globex front months tick every few seconds around
 * the clock, so a minute of silence while Globex is open means the
 * stream stopped (the 2026-11-25 / 11-27 IB farm-blip stalls). Stocks
 * can sit quiet for minutes outside RTH, so they never get the tag.
 */

/** No live tick for this long during Globex hours → stale. */
export const QUOTE_STALE_MS = 60_000;

/** Weekday and hour in US/Central. */
function chicagoParts(d: Date): { weekday: number; hour: number } {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Chicago', weekday: 'short', hour: 'numeric', hourCycle: 'h23',
  }).formatToParts(d);
  const wd = parts.find((p) => p.type === 'weekday')?.value ?? 'Mon';
  const hour = Number(parts.find((p) => p.type === 'hour')?.value ?? '0');
  const weekday = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(wd);
  return { weekday, hour };
}

/** True while CME Globex should be streaming futures (mirrors the
 *  engine's ``futures_ticks_expected``): closed Fri 16:00 CT → Sun
 *  17:00 CT and during the Mon–Thu 16:00–17:00 CT daily break. */
export function futuresTicksExpected(now: Date): boolean {
  const { weekday, hour } = chicagoParts(now);
  if (weekday === 6) return false;                 // Saturday
  if (weekday === 5 && hour >= 16) return false;   // Friday after close
  if (weekday === 0 && hour < 17) return false;    // Sunday before open
  if (weekday >= 1 && weekday <= 4 && hour === 16) return false; // daily break
  return true;
}

/** ``since`` is the last live tick (or when the chart started waiting
 *  for one). */
export function isQuoteStale(
  secType: string | null | undefined, since: number, now: number,
  thresholdMs: number = QUOTE_STALE_MS,
): boolean {
  if (secType !== 'FUT') return false;
  if (now - since <= thresholdMs) return false;
  return futuresTicksExpected(new Date(now));
}
