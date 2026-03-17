import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/app.css'
import { App } from './app/App'

// Apply saved theme before first render to prevent flash
const savedTheme = localStorage.getItem('ib-theme');
if (savedTheme) {
  document.documentElement.setAttribute('data-theme', savedTheme);
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
