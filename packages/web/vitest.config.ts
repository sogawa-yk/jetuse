import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Vitest設定。本体の vite.config.ts とは分離（test専用フィールドの型を vitest/config から得る）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
