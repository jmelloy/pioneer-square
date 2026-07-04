import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

const backendUrl = process.env.BACKEND_URL ?? 'http://localhost:8000'
const backendWsUrl = backendUrl.replace(/^http/, 'ws')

// When the SPA is reached through the backend dev proxy (e.g. localhost:8056),
// the backend does not forward Vite's HMR WebSocket. Point the HMR client at
// the directly-exposed Vite port instead. Configurable via HMR_CLIENT_PORT.
const hmrClientPort = process.env.HMR_CLIENT_PORT
  ? Number(process.env.HMR_CLIENT_PORT)
  : undefined

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    allowedHosts: ['localhost', 'frontend', 'pioneer-square.melloy.life'],
    ...(hmrClientPort
      ? { hmr: { protocol: 'ws', host: 'localhost', clientPort: hmrClientPort } }
      : {}),
    // Forward backend routes when running `npm run dev` so the SPA can
    // call /auth, /guilds, /ws, /discord against the dev server. In
    // production these are all served from the same FastAPI origin as
    // the built SPA (see backend/main.py) — there is no separate reverse
    // proxy in front of the backend.
    proxy: {
      '/auth':    { target: backendUrl,   changeOrigin: true },
      '/guilds':  { target: backendUrl,   changeOrigin: true },
      // Intentionally proxies all /discord/* paths (not just /discord/interactions)
      // so any future Discord routes are covered automatically in dev.
      '/discord': { target: backendUrl,   changeOrigin: true },
      '/ws':      { target: backendWsUrl, ws: true, changeOrigin: true },
    },
  },
  preview: {
    allowedHosts: ['localhost', 'pioneer-square.melloy.life'],
  },
})
