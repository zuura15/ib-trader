import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import basicSsl from '@vitejs/plugin-basic-ssl'

const apiTarget = process.env.VITE_API_URL || 'http://localhost:8000'
const wsTarget = apiTarget.replace('http', 'ws')

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
      },
      '/ws': {
        target: wsTarget,
        ws: true,
        secure: false,
      },
    },
  },
})
