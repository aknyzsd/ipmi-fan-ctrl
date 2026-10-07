import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 相对路径 base，Python 伺服静态文件时不依赖域名根
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
  },
  server: {
    port: 5173,
    // 开发时把 /api 代理到 Python 后端（默认 8089）
    proxy: {
      '/api': {
        target: 'http://localhost:8089',
        changeOrigin: true,
      },
    },
  },
})
