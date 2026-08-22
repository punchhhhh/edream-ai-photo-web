import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 后端所有入口(含 /api/media 静态产物)统一在 /api 前缀下
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
