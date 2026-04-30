import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    allowedHosts: ['localhost', 'pioneer-square.melloy.life'],
    // Forward backend routes when running `npm run dev` so the SPA can
    // call /auth, /guilds, /ws against the dev server. In production
    // these are handled by nginx-in-container (see frontend/nginx.conf).
    proxy: {
      '/auth':   { target: 'http://localhost:8000', changeOrigin: true },
      '/guilds': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws':     { target: 'ws://localhost:8000',   ws: true, changeOrigin: true },
    },
  },
  preview: {
    allowedHosts: ['localhost', 'pioneer-square.melloy.life'],
  },
})
