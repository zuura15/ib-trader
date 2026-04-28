import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/app.css'
import { App } from './app/App'

// Apply saved theme before first render to prevent flash. Default to light
// when nothing is persisted — matches store.ts initial value.
const savedTheme = localStorage.getItem('ib-theme') || 'light';
document.documentElement.setAttribute('data-theme', savedTheme);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
