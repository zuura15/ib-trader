import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/app.css'
import { App } from './app/App'
import { ErrorBoundary } from './app/ErrorBoundary'
import { DiagOverlay } from './app/DiagOverlay'
import { installGlobalErrorHandlers, recordBoot } from './app/diagnostics'

// Trap uncaught exceptions + unhandled promise rejections before React mounts
// so anything that escapes the render path still lands in the diagnostic ring
// (and prints to console with a [diag:] prefix).
installGlobalErrorHandlers();
recordBoot('script-loaded');

// Apply saved theme before first render to prevent flash. Default to light
// when nothing is persisted — matches store.ts initial value.
const savedTheme = localStorage.getItem('ib-theme') || 'light';
document.documentElement.setAttribute('data-theme', savedTheme);
recordBoot('theme-applied', { theme: savedTheme });

if (typeof document !== 'undefined') {
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    recordBoot('dom-ready-at-boot', { readyState: document.readyState });
  } else {
    document.addEventListener('DOMContentLoaded', () => recordBoot('dom-content-loaded'));
  }
  window.addEventListener('load', () => recordBoot('window-load'));
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary label="root" variant="root">
      <App />
    </ErrorBoundary>
    {/* DiagOverlay sits outside the ErrorBoundary so it can always render —
        even if <App /> itself failed to mount. Ctrl+Alt+D toggles it. */}
    <DiagOverlay />
  </StrictMode>,
)
recordBoot('react-root-created');
