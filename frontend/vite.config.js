import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      '@locales': path.resolve(__dirname, '../locales')
    }
  },
  server: {
    port: 3000,
    open: process.env.MIROFISH_OPEN_BROWSER === '1',
    proxy: {
      '/api/ai': {
        target: 'http://localhost:5001',
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/api\/ai/, '/api')
      },
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true,
        secure: false
      }
    }
  }
})
