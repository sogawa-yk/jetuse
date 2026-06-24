import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // リリースバージョンをフッター表示(feedback 20260620 #13)。npm が package.json の version を渡す
  define: {
    __APP_VERSION__: JSON.stringify(process.env.npm_package_version ?? '0.0.0'),
  },
  // ローカルAPI(uvicorn)への開発プロキシ。VITE_API_PROXYで上書き可(VOICE-01)
  server: {
    host: true,
    proxy: { '/api': process.env.VITE_API_PROXY ?? 'http://localhost:8000' },
  },
  // 本番ビルド(dist)をローカル検証する preview でも /api をローカルAPIへプロキシ
  preview: {
    host: true,
    proxy: { '/api': process.env.VITE_API_PROXY ?? 'http://localhost:8000' },
  },
})
