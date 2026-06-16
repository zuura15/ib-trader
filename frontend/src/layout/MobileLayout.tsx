import { useRef, useState, useEffect, useCallback } from 'react';
import { MobileHeader } from '../features/header/MobileHeader';
import { OrdersPanel } from '../features/orders/OrdersPanel';
import { WatchlistPanel } from '../features/watchlist/WatchlistPanel';
import { TradesPanel } from '../features/trades/TradesPanel';
import { AlertsPanel } from '../features/alerts/AlertsPanel';
import { LogStream } from '../features/logs/LogStream';
import { BotTradesPanel } from '../features/bots/BotTradesPanel';
import { MobileTradeView } from '../features/chart-bot/MobileTradeView';
const TABS = ['Trade', 'Watch', 'Orders', 'Trades', 'Logs'] as const;

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

  const scrollRef = useRef<HTMLDivElement>(null);
  const [activeTab, setActiveTabRaw] = useState<Tab>(() => {
    const saved = localStorage.getItem('ib-mobile-tab');
    return saved && TABS.includes(saved as Tab) ? (saved as Tab) : 'Trade';
  });
  const setActiveTab = useCallback((tab: Tab) => {
    localStorage.setItem('ib-mobile-tab', tab);
    setActiveTabRaw(tab);
  }, []);
  const [unsupported] = useState(isIOSSafari);
  const programmaticScrollRef = useRef(false);

  // Directional gesture locking: when a touch starts inside a data table,
  // detect whether the initial movement is horizontal or vertical.
  // If horizontal → lock out the tab swipe container so the table scrolls.
  // If vertical → let the tab/page scroll normally.
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;

    let startX = 0;
    let startY = 0;
    let locked: 'none' | 'horizontal' | 'vertical' = 'none';

    const isInsideTable = (target: EventTarget | null): boolean => {
      let el = target as HTMLElement | null;
      while (el && el !== container) {
        if (el.classList.contains('data-table') || el.closest('.data-table')) return true;
        el = el.parentElement;
      }
      return false;
    };

    const onTouchStart = (e: TouchEvent) => {
      locked = 'none';
      if (!isInsideTable(e.target)) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
    };

    const onTouchMove = (e: TouchEvent) => {
      if (locked !== 'none') {
        if (locked === 'horizontal') {
          // Already locked horizontal — keep blocking tab swipe
          container.style.overflowX = 'hidden';
          container.style.scrollSnapType = 'none';
        }
        return;
      }
      if (!isInsideTable(e.target)) return;

      const dx = Math.abs(e.touches[0].clientX - startX);
      const dy = Math.abs(e.touches[0].clientY - startY);

      // Need at least 8px of movement to decide direction
      if (dx < 8 && dy < 8) return;

      if (dx > dy) {
        // Horizontal swipe inside table — lock out tab container
        locked = 'horizontal';
        container.style.overflowX = 'hidden';
        container.style.scrollSnapType = 'none';
      } else {
        locked = 'vertical';
      }
    };

    const onTouchEnd = () => {
      if (locked === 'horizontal') {
        container.style.overflowX = 'auto';
        container.style.scrollSnapType = 'x mandatory';
      }
      locked = 'none';
    };

    container.addEventListener('touchstart', onTouchStart, { passive: true });
    container.addEventListener('touchmove', onTouchMove, { passive: true });
    container.addEventListener('touchend', onTouchEnd, { passive: true });
    container.addEventListener('touchcancel', onTouchEnd, { passive: true });
    return () => {
      container.removeEventListener('touchstart', onTouchStart);
      container.removeEventListener('touchmove', onTouchMove);
      container.removeEventListener('touchend', onTouchEnd);
      container.removeEventListener('touchcancel', onTouchEnd);
    };
  }, []);

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
        className="flex items-center justify-center h-dvh w-screen p-8"
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
      className="flex flex-col h-dvh w-screen overflow-hidden"
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
        {/* Tab 1: Trade — two stacked chart rows (Gold/Console/Positions
            over Nasdaq/Micro NQ). See MobileTradeView. */}
        <div
          id="tabpanel-Trade"
          role="tabpanel"
          aria-labelledby="tab-Trade"
          className="flex flex-col shrink-0 w-screen h-full"
          style={{ scrollSnapAlign: 'start', overflow: 'hidden' }}
        >
          <MobileTradeView />
        </div>

        {/* Tab 2: Watch — watchlist */}
        <div
          id="tabpanel-Watch"
          role="tabpanel"
          aria-labelledby="tab-Watch"
          className="flex flex-col shrink-0 w-screen h-full overflow-y-auto"
          style={{ scrollSnapAlign: 'start' }}
        >
          <WatchlistPanel />
        </div>

        {/* Tab 3: Orders — trades + open orders */}
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

        {/* Tab 4: Trades — bot round-trip P&L records */}
        <div
          id="tabpanel-Trades"
          role="tabpanel"
          aria-labelledby="tab-Trades"
          className="flex flex-col shrink-0 w-screen h-full overflow-hidden"
          style={{ scrollSnapAlign: 'start' }}
        >
          <BotTradesPanel compact />
        </div>

        {/* Tab 5: Logs — alerts + log stream */}
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
