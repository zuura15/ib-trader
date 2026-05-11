import { defineConfig, createLogger } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import basicSsl from '@vitejs/plugin-basic-ssl'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const apiTarget = process.env.VITE_API_URL || 'http://localhost:8000'
const wsTarget = apiTarget.replace('http', 'ws')

// Locally-trusted dev cert. ``frontend/scripts/setup-certs.sh`` runs
// mkcert against this box's hostnames + LAN IP and drops the PEM here.
// When present we use it directly so browsers with the mkcert root
// CA installed get a green padlock — no "your connection is not
// private" prompt on every reload, and no cold-tab TLS handshake
// hangs (the symptom that prompted this cleanup on local-prod).
//
// Fallback: ``@vitejs/plugin-basic-ssl`` keeps a fresh checkout
// usable over HTTPS until the setup script is run. HTTPS is required
// by the Web Speech API on non-localhost origins (the LAN-mobile
// flow).
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const certPath = path.join(__dirname, 'certs', 'dev.pem')
const keyPath = path.join(__dirname, 'certs', 'dev-key.pem')
const hasMkcertCert = fs.existsSync(certPath) && fs.existsSync(keyPath)

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

// mkcert certs go directly to ``server.https``; the basic-ssl plugin
// is the fallback for fresh checkouts.
const plugins = hasMkcertCert
  ? [react(), tailwindcss()]
  : [react(), tailwindcss(), basicSsl()]
const https = hasMkcertCert
  ? { cert: fs.readFileSync(certPath), key: fs.readFileSync(keyPath) }
  : undefined

if (hasMkcertCert) {
  // Surface which cert the dev server is using so a stale cert is
  // obvious if the box's LAN IP changes.
  console.info(`[vite] using mkcert dev cert: ${certPath}`)
}

export default defineConfig({
  customLogger: logger,
  plugins,
  server: {
    host: true,
    https,
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
