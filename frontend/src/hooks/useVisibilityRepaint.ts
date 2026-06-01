import { useEffect } from 'react';
import { recordDiag } from '../app/diagnostics';

/**
 * Force a layout + repaint whenever the tab returns to ``visible``.
 *
 * Symptom this fixes: after switching browser tabs and coming back,
 * the page sometimes renders white. Tooltips paint on mouse-move and
 * the screen restores on F12 (which fires a window resize). Cause:
 * Chrome aggressively pauses rAF-driven render loops in hidden tabs
 * — flexlayout-react's measurement loop and lightweight-charts'
 * canvas draw both stop. When the tab returns, React state is intact
 * but the compositor never gets the "paint a new frame" trigger
 * until some user input fires one.
 *
 * Layered fix:
 *   1. ``document.body.offsetHeight`` read forces a synchronous reflow
 *      (so any pending style invalidation is resolved BEFORE the
 *      resize listeners run, which may early-out on stale measurements).
 *   2. ``dispatchEvent(new Event('resize'))`` kicks flexlayout's
 *      onResize and lightweight-charts' size watcher; both schedule a
 *      redraw and the browser flushes a frame.
 *   3. ``requestAnimationFrame`` chained twice — first frame realises
 *      the resize, second commits the new paint. Belt + suspenders for
 *      browsers that batch the first paint behind a pending layout.
 *
 * Mount once at App root. Idempotent. Logs to the diag ring so the
 * Ctrl+Alt+D overlay shows when these wake-up nudges fired.
 */
export function useVisibilityRepaint(): void {
  useEffect(() => {
    let lastFireMs = 0;
    const handler = () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      // Debounce: rapid tab-flips should not stack resize storms onto
      // flexlayout, which re-measures every pane on each event.
      if (now - lastFireMs < 250) return;
      lastFireMs = now;

      try {
        // Force sync reflow so pending style mutations resolve first.
        // The void is to silence the unused-expression lint.
        void document.body.offsetHeight;
      } catch {
        // Defensive — body should always exist post-hydration.
      }

      const fire = () => {
        try {
          window.dispatchEvent(new Event('resize'));
        } catch {
          // Defensive — older browsers / strict CSP edge cases.
        }
      };

      fire();
      requestAnimationFrame(() => {
        fire();
        requestAnimationFrame(fire);
      });

      recordDiag({
        kind: 'watchdog',
        message: 'visibility→visible: dispatched repaint nudge',
        source: 'useVisibilityRepaint',
      });
    };
    document.addEventListener('visibilitychange', handler);
    return () => document.removeEventListener('visibilitychange', handler);
  }, []);
}
