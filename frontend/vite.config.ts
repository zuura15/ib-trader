import { defineConfig, createLogger } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import basicSsl from '@vitejs/plugin-basic-ssl'

const apiTarget = process.env.VITE_API_URL || 'http://localhost:8000'
const wsTarget = apiTarget.replace('http', 'ws')

// Noisy-but-benign proxy errors from browser tab close / HMR socket teardown.
// Node net.js raises `EPIPE` with the message "This socket has been ended by
// the other party" when our proxy tries to pipe more data to a socket the
// client has already FIN'd. No user or data impact.
const BENIGN_PROXY_ERRORS = new Set([
  'ECONNRESET',
  'ERR_STREAM_WRITE_AFTER_END',
  'EPIPE',
])
function isBenignProxyError(err: NodeJS.ErrnoException | undefined): boolean {
  if (!err) return false
  if (err.code && BENIGN_PROXY_ERRORS.has(err.code)) return true
  return /socket has been ended|write after end|ECONNRESET/i.test(err.message ?? '')
}

// Vite registers its own proxy error handlers AFTER the user's `configure`
// callback and logs them at ERROR level (see vite/dist/.../node.js: proxyMiddleware).
// We can't suppress those listeners from `configure`, so we filter at the log
// sink instead: wrap the default logger's `.error` and drop calls that carry
// a benign proxy Error.
const logger = createLogger()
const origError = logger.error.bind(logger)
logger.error = (msg, opts) => {
  const err = opts?.error as NodeJS.ErrnoException | undefined
  const msgStr = typeof msg === 'string' ? msg : ''
  if (isBenignProxyError(err) && /proxy/i.test(msgStr)) {
    // Single tidy line instead of three stack traces per disconnect.
    console.info(`[vite] benign proxy event: ${err!.message}`)
    return
  }
  origError(msg, opts)
}

export default defineConfig({
  customLogger: logger,
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
      },
      '/ws': {
        target: wsTarget,
        ws: true,
        secure: false,
      },
    },
  },
})
