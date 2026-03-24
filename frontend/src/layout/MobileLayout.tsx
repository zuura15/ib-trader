import { useRef, useState, useEffect, useCallback, type PointerEvent as RPointerEvent } from 'react';
import { MobileHeader } from '../features/header/MobileHeader';
import { CommandConsole } from '../features/console/CommandConsole';
import { PositionsPanel } from '../features/positions/PositionsPanel';
import { OrdersPanel } from '../features/orders/OrdersPanel';
import { TradesPanel } from '../features/trades/TradesPanel';
import { AlertsPanel } from '../features/alerts/AlertsPanel';
import { LogStream } from '../features/logs/LogStream';
const TABS = ['Trade', 'Orders', 'Logs'] as const;

// ---------------------------------------------------------------------------
// Vertical resize handle for the mobile Trade tab split
// ---------------------------------------------------------------------------

const CONSOLE_MIN_H = 80;
const CONSOLE_MAX_RATIO = 0.6; // never more than 60% of viewport

function useVerticalResize(initialVh: number) {
  const [height, setHeight] = useState<number | null>(null);
  const startY = useRef(0);
  const startH = useRef(0);

  const initHeight = () => {
    if (height === null) return (window.innerHeight * initialVh) / 100;
    return height;
  };

  // Attach move/up listeners on window so dragging works even when the
  // finger moves outside the narrow handle strip.
  const onPointerDown = useCallback((e: RPointerEvent) => {
    e.preventDefault();
    startY.current = e.clientY;
    startH.current = height ?? (window.innerHeight * initialVh) / 100;

    const onMove = (ev: globalThis.PointerEvent) => {
      const delta = ev.clientY - startY.current;
      const maxH = window.innerHeight * CONSOLE_MAX_RATIO;
      setHeight(Math.max(CONSOLE_MIN_H, Math.min(maxH, startH.current + delta)));
    };

    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  }, [height, initialVh]);

  return { height: initHeight(), onPointerDown };
}
type Tab = (typeof TABS)[number];

/**
 * Detect iOS Safari via user-agent.
 *
 * Checks for iPhone/iPod explicitly. Avoids the `navigator.platform === 'MacIntel'`
 * + `maxTouchPoints` heuristic that false-positives on macOS with Magic Trackpad.
 * iPad detection uses the 'iPad' token in the UA string.
 *
 * TODO: iOS Safari support — scroll-snap + nested overflow-y conflicts need
 * directional gesture locking. See GitHub issue #39.
 */
function isIOSSafari(): boolean {
  const ua = navigator.userAgent;
  const isIOS = /iPad|iPhone|iPod/.test(ua);
  const isSafari = /Safari/.test(ua) && !/CriOS|FxiOS|OPiOS|EdgiOS|Chrome/.test(ua);
  return isIOS && isSafari;
}

export function MobileLayout() {
  // Data initialization (WS / mock) is handled by App.tsx — not duplicated here.

  const resize = useVerticalResize(30);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [activeTab, setActiveTab] = useState<Tab>('Trade');
  const [unsupported] = useState(isIOSSafari);
  const programmaticScrollRef = useRef(false);

  // Sync tab indicator with scroll position via scroll event.
  // More reliable than IntersectionObserver during fast swipes —
  // simply compute which page is closest to the scroll offset.
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;

    let ticking = false;
    const onScroll = () => {
      // Suppress scroll handler during programmatic scrollTo to prevent
      // the tab indicator from flickering through intermediate positions.
      if (ticking || programmaticScrollRef.current) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        const pageWidth = container.offsetWidth;
        if (pageWidth === 0) return;
        const idx = Math.round(container.scrollLeft / pageWidth);
        const clamped = Math.max(0, Math.min(idx, TABS.length - 1));
        setActiveTab(TABS[clamped]);
      });
    };

    container.addEventListener('scroll', onScroll, { passive: true });
    return () => container.removeEventListener('scroll', onScroll);
  }, []);

  // Re-snap to the active tab on orientation change / container resize.
  // Without this, rotating the device leaves scrollLeft at the old width's
  // offset, which doesn't align with the new page width.
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;

    const observer = new ResizeObserver(() => {
      const idx = TABS.indexOf(activeTab);
      // Instant snap (no smooth scroll) — we just need alignment, not animation.
      container.scrollTo({ left: idx * container.offsetWidth, behavior: 'instant' });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [activeTab]);

  const scrollToTab = useCallback((tab: Tab) => {
    const container = scrollRef.current;
    if (!container) return;
    const idx = TABS.indexOf(tab);

    // Suppress the scroll event handler during programmatic scroll to prevent
    // tab indicator jitter from intermediate scroll positions.
    programmaticScrollRef.current = true;
    setActiveTab(tab);
    container.scrollTo({ left: idx * container.offsetWidth, behavior: 'smooth' });

    // Use `scrollend` event (Chrome 109+) as the primary signal that the
    // smooth scroll completed. Fall back to a timeout for older browsers.
    const onScrollEnd = () => {
      programmaticScrollRef.current = false;
      container.removeEventListener('scrollend', onScrollEnd);
    };

    if ('onscrollend' in container) {
      container.addEventListener('scrollend', onScrollEnd, { once: true });
    } else {
      setTimeout(() => { programmaticScrollRef.current = false; }, 500);
    }
  }, []);

  if (unsupported) {
    return (
      <div
        className="flex items-center justify-center h-screen w-screen p-8"
        style={{ background: 'var(--bg-root)', color: 'var(--text-primary)' }}
      >
        <div className="text-center" style={{ maxWidth: 400 }}>
          <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 12 }}>
            Browser Not Supported
          </div>
          <div style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            The mobile trading interface is not supported on iOS Safari.
            Please use Chrome on Android, or access the desktop version
            from a computer.
          </div>
          {/* TODO: iOS Safari support — scroll-snap + nested overflow-y
              conflicts need directional gesture locking or a JS-driven
              swipe implementation. See GitHub issue #39. */}
        </div>
      </div>
    );
  }

  return (
    <div
      className="flex flex-col h-screen w-screen overflow-hidden"
      style={{ background: 'var(--bg-root)' }}
    >
      <MobileHeader />

      {/* Tab bar */}
      <div
        className="flex shrink-0 border-b"
        style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}
        role="tablist"
      >
        {TABS.map((tab) => (
          <button
            key={tab}
            id={`tab-${tab}`}
            role="tab"
            aria-selected={activeTab === tab}
            aria-controls={`tabpanel-${tab}`}
            onClick={() => scrollToTab(tab)}
            className="flex-1 border-none cursor-pointer transition-colors"
            style={{
              fontSize: 17,
              fontWeight: activeTab === tab ? 600 : 400,
              color: activeTab === tab ? 'var(--accent-blue)' : 'var(--text-muted)',
              background: 'transparent',
              borderBottom: activeTab === tab
                ? '2px solid var(--accent-blue)'
                : '2px solid transparent',
              padding: '12px 0',
              minHeight: 44,
            }}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Swipeable pages — CSS scroll-snap for native-feeling swipe.
          TODO: iOS Safari has conflicts with nested overflow-y children.
          If iOS support is added, consider overscroll-behavior or a
          JS-driven swipe with directional locking. */}
      <div
        ref={scrollRef}
        className="flex flex-1 overflow-y-hidden mobile-swipe-container"
        style={{
          overflowX: 'auto',
          scrollSnapType: 'x mandatory',
          scrollbarWidth: 'none',       /* Firefox */
        }}
      >
        {/* Tab 1: Trade — order entry + positions, vertically resizable */}
        <div
          id="tabpanel-Trade"
          role="tabpanel"
          aria-labelledby="tab-Trade"
          className="flex flex-col shrink-0 w-screen h-full"
          style={{ scrollSnapAlign: 'start', overflow: 'hidden' }}
        >
          <div style={{ height: resize.height, minHeight: CONSOLE_MIN_H, overflow: 'hidden' }}>
            <CommandConsole compact />
          </div>

          {/* Drag handle + separator */}
          <div
            onPointerDown={resize.onPointerDown}
            style={{
              height: 40,
              flexShrink: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'row-resize',
              touchAction: 'none',
              background: 'var(--bg-root)',
              borderTop: '1px solid var(--border-default)',
              borderBottom: '1px solid var(--border-default)',
            }}
          >
            <div style={{
              width: 48,
              height: 5,
              borderRadius: 3,
              background: 'var(--text-muted)',
              opacity: 0.5,
            }} />
          </div>

          <div className="flex-1 overflow-y-auto">
            <PositionsPanel compact />
          </div>
        </div>

        {/* Tab 2: Orders — trades + open orders */}
        <div
          id="tabpanel-Orders"
          role="tabpanel"
          aria-labelledby="tab-Orders"
          className="flex flex-col shrink-0 w-screen h-full overflow-y-auto"
          style={{ scrollSnapAlign: 'start' }}
        >
          <div className="flex-1">
            <TradesPanel compact />
          </div>
          <div className="flex-1">
            <OrdersPanel compact />
          </div>
        </div>

        {/* Tab 3: Logs — alerts + log stream */}
        <div
          id="tabpanel-Logs"
          role="tabpanel"
          aria-labelledby="tab-Logs"
          className="flex flex-col shrink-0 w-screen h-full overflow-y-auto"
          style={{ scrollSnapAlign: 'start' }}
        >
          <div style={{ minHeight: 100 }}>
            <AlertsPanel />
          </div>
          <div className="flex-1">
            <LogStream maxLines={200} />
          </div>
        </div>
      </div>
    </div>
  );
}
