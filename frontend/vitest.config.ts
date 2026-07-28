import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'happy-dom',
      globals: true,
      setupFiles: ['./vitest.setup.ts'],
      include: ['src/**/*.{test,spec}.{ts,js}', 'tests/**/*.{test,spec}.{ts,js}'],
      coverage: {
        provider: 'v8',
        reporter: ['text', 'html'],
        include: ['src/stores/**', 'src/composables/**'],
      },
    },
  }),
)
