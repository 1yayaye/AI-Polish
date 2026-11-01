import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 生产环境由 Nginx 伺服 dist/ 并反代 /api；此处 proxy 仅服务于本地 npm run dev。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
