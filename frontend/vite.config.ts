import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

const backendUrl = process.env.BACKEND_URL ?? 'http://localhost:8000'
const backendWsUrl = backendUrl.replace(/^http/, 'ws')

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  // Extra inputs ship the demo/ pages with the production build so the
  // SVG showcase is reachable at /demo/* on the deployed site. They are
  // not linked from the main app — they exist to share specific visual
  // states (bench scenery, robot sprite variants).
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, 'index.html'),
        'demo-index': path.resolve(__dirname, 'demo/index.html'),
        'demo-scenery': path.resolve(__dirname, 'demo/scenery.html'),
        'demo-robots': path.resolve(__dirname, 'demo/robots.html'),
      },
    },
  },
  server: {
    allowedHosts: ['localhost', 'pioneer-square.melloy.life'],
    // Forward backend routes when running `npm run dev` so the SPA can
    // call /auth, /guilds, /ws against the dev server. In production
    // these are handled by nginx-in-container (see frontend/nginx.conf).
    proxy: {
      '/auth':   { target: backendUrl,   changeOrigin: true },
      '/guilds': { target: backendUrl,   changeOrigin: true },
      '/ws':     { target: backendWsUrl, ws: true, changeOrigin: true },
    },
  },
  preview: {
    allowedHosts: ['localhost', 'pioneer-square.melloy.life'],
  },
})
