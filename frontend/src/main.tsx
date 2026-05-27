import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/app.css'
import { App } from './app/App'
import { ErrorBoundary } from './app/ErrorBoundary'
import { installGlobalErrorHandlers } from './app/diagnostics'

// Trap uncaught exceptions + unhandled promise rejections before React mounts
// so anything that escapes the render path still lands in the diagnostic ring
// (and prints to console with a [diag:] prefix).
installGlobalErrorHandlers();

// Apply saved theme before first render to prevent flash. Default to light
// when nothing is persisted — matches store.ts initial value.
const savedTheme = localStorage.getItem('ib-theme') || 'light';
document.documentElement.setAttribute('data-theme', savedTheme);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary label="root" variant="root">
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
