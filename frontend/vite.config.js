import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务器把 /api 与 /health 反代到后端（默认 9900），
// 从而在浏览器里免去跨域配置，直连 FastAPI。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:9900',
        changeOrigin: true
      },
      '/health': {
        target: 'http://localhost:9900',
        changeOrigin: true
      }
    }
  }
})
