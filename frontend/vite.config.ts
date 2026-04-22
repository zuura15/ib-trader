import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import basicSsl from '@vitejs/plugin-basic-ssl'

const apiTarget = process.env.VITE_API_URL || 'http://localhost:8000'
const wsTarget = apiTarget.replace('http', 'ws')

// Noisy-but-benign proxy errors we want to downgrade. These fire when a
// browser tab closes mid-request or the HMR socket is torn down — no
// impact on users or data integrity.
const BENIGN_PROXY_ERRORS = new Set([
  'ECONNRESET',
  'ERR_STREAM_WRITE_AFTER_END',
  'EPIPE',
])
function isBenignProxyError(err: NodeJS.ErrnoException): boolean {
  if (err.code && BENIGN_PROXY_ERRORS.has(err.code)) return true
  const msg = err.message || ''
  // Socket ended / write after FIN — classic closed-tab teardown.
  return /socket has been ended|write after end|ECONNRESET/i.test(msg)
}

export default defineConfig({
  // basic-ssl generates a self-signed cert so the dev server runs over HTTPS.
  // Required for Web Speech API on non-localhost origins (e.g. LAN access from
  // a phone at https://192.168.x.x:5173). Accept the cert warning once.
  plugins: [react(), tailwindcss(), basicSsl()],
  server: {
    host: true,
    // Move Vite's HMR WebSocket to a different path so it doesn't
    // conflict with our app's /ws proxy.
    hmr: {
      path: '/__vite_hmr',
    },
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        secure: false,
        configure: (proxy) => {
          proxy.on('error', (err) => {
            if (isBenignProxyError(err)) {
              console.info(`[vite] api proxy: ${err.message}`)
            } else {
              console.warn('[vite] api proxy error:', err)
            }
          })
        },
      },
      '/ws': {
        target: wsTarget,
        ws: true,
        secure: false,
        configure: (proxy) => {
          proxy.on('error', (err) => {
            if (isBenignProxyError(err)) {
              console.info(`[vite] ws proxy: ${err.message}`)
            } else {
              console.warn('[vite] ws proxy error:', err)
            }
          })
        },
      },
    },
  },
})
