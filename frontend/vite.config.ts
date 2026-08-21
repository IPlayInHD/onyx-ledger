import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

const API_TARGET = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  build: {
    // Source maps are NOT emitted for production: they would republish readable
    // application internals to anyone who opens devtools on the deployed site.
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          query: ['@tanstack/react-query'],
        },
      },
    },
  },
  server: {
    proxy: { '/api': { target: API_TARGET, changeOrigin: true } },
  },
  // The preview server proxies too, so the production build under test talks to
  // the real backend instead of having an API origin compiled into it.
  preview: {
    proxy: { '/api': { target: API_TARGET, changeOrigin: true } },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    css: true,
    exclude: ['**/node_modules/**', '**/e2e/**'],
  },
} as never)
