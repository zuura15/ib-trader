import { useEffect } from 'react';
import { recordDiag } from '../app/diagnostics';

/**
 * Force a layout + repaint whenever the tab returns to ``visible`` OR
 * the window regains focus OR a pageshow fires.
 *
 * Symptom this fixes: after switching browser tabs and coming back,
 * the page sometimes renders white. Tooltips paint on mouse-move and
 * the screen restores on any keydown / F12 (which fires layout). The
 * DOM is intact and React state is fine — the browser's compositor
 * just hasn't been told to flush a new frame.
 *
 * Previous attempt only listened to ``visibilitychange`` and dispatched
 * a synthetic resize. That wasn't enough — on some setups the resize
 * fires without triggering an actual paint, because lightweight-charts
 * and flexlayout schedule their own work via rAF and the compositor
 * stays asleep until something *moves* in the DOM.
 *
 * The fix that actually wakes the compositor is a forced style mutation
 * that the browser has to repaint to honor: flip ``body.style.opacity``
 * from 0.99999 → 1 across two rAFs. Reading ``offsetHeight`` between
 * them forces the layout flush. We also fire the synthetic resize for
 * flexlayout + lightweight-charts to re-measure, and now listen on
 * ``focus`` + ``pageshow`` in addition to ``visibilitychange`` since
 * some Chrome/Android paths fire focus but not visibilitychange.
 *
 * Logs every trigger to the diag ring (Ctrl+Alt+D) so we can tell
 * post-mortem which event fired AND whether the repaint code ran.
 */
export function useVisibilityRepaint(): void {
  useEffect(() => {
    let lastFireMs = 0;

    const forceRepaint = (trigger: string) => {
      const body = document.body;
      if (!body) return;

      try {
        // 1. Synthetic resize — wakes flexlayout-react onResize and
        // lightweight-charts size watcher. They schedule redraws via
        // their own rAF; not sufficient on its own.
        window.dispatchEvent(new Event('resize'));
      } catch { /* defensive */ }

      // 2. Force a style mutation the browser MUST repaint to honor.
      // Opacity 0.99999 vs 1 is invisible to the eye but counts as a
      // visual change for the compositor — guarantees a frame flush.
      const prev = body.style.opacity;
      body.style.opacity = '0.99999';
      // Force layout flush so the opacity change is committed before
      // the second mutation.
      void body.offsetHeight;
      requestAnimationFrame(() => {
        body.style.opacity = prev || '';
        // Second resize after the opacity-restore frame ensures
        // anything that measured during the 0.99999 frame
        // re-measures against the final state.
        requestAnimationFrame(() => {
          try { window.dispatchEvent(new Event('resize')); } catch { /* defensive */ }
        });
      });

      recordDiag({
        kind: 'watchdog',
        message: `repaint nudge fired (trigger=${trigger})`,
        source: 'useVisibilityRepaint',
      });
    };

    const handler = (trigger: string) => {
      // Visibility-API check still gates focus/pageshow — no point
      // repainting if we're transitioning toward hidden.
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      // Debounce: focus + visibilitychange + pageshow can all fire
      // within milliseconds of each other on a tab return.
      if (now - lastFireMs < 250) return;
      lastFireMs = now;
      forceRepaint(trigger);
    };

    const onVis = () => handler('visibilitychange');
    const onFocus = () => handler('focus');
    const onPageShow = () => handler('pageshow');

    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('focus', onFocus);
    window.addEventListener('pageshow', onPageShow);
    return () => {
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('pageshow', onPageShow);
    };
  }, []);
}
