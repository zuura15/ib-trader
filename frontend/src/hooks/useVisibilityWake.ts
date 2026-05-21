import { useEffect, useRef } from 'react';

/**
 * Fire ``onWake`` whenever the document transitions to ``visible``.
 *
 * Browsers (Chrome especially) throttle JS execution in background tabs
 * and may discard the page entirely after a few minutes of inactivity.
 * Long-lived WebSocket subscriptions, polled HTTP endpoints, and
 * historical-data caches all go stale during that time. Components that
 * own a data source register what to refresh on return-to-visible.
 *
 * Behavior:
 *  - Fires on every ``hidden → visible`` transition.
 *  - Does NOT fire on initial mount; the component handles its own
 *    initial fetch / subscribe.
 *  - Debounced (500 ms): rapid tab toggles collapse to one wake.
 *  - Holds onto the latest ``onWake`` via a ref so callers can pass an
 *    inline closure without re-binding the document listener on every
 *    render.
 */
export function useVisibilityWake(onWake: () => void): void {
  const cbRef = useRef(onWake);
  // Sync the latest callback into the ref *after* render so the
  // document listener (bound once) always invokes the freshest closure.
  // Assigning during render trips the react-hooks/refs lint and risks
  // tearing during concurrent renders.
  useEffect(() => {
    cbRef.current = onWake;
  });

  useEffect(() => {
    let lastFire = 0;
    const handler = () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      if (now - lastFire < 500) return;
      lastFire = now;
      cbRef.current();
    };
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  }, []);
}
