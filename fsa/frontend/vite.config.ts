import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Префикс, под которым интерфейс отдаётся снаружи. В единой выкладке он
// равен '/fsa/', потому что корень занят интерфейсом прайсов; локально —
// корень, чтобы dev-режим и тесты не требовали настройки.
const base = process.env.VITE_BASE_PATH || '/'

export default defineConfig({
  base,
  plugins: [react()],
  server: {
    port: 5183,
    // Бэкенд слушает 8100; прокси избавляет от CORS в dev.
    proxy: { '/v1': 'http://127.0.0.1:8100', '/health': 'http://127.0.0.1:8100' },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
})
