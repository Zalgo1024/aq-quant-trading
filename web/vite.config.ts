import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时把 /api 代理到本地 FastAPI，避免跨域配置
export default defineConfig({
  plugins: [react()],
  resolve: {
    // 必须与 tsconfig.json 的 compilerOptions.paths 保持一致，
    // 否则 tsc 能过、vite build 会在 Rollup 阶段报 "failed to resolve import @/..."
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // 单包 2.3MB 太肥（antd + echarts 全量）。手动分包把「很少变动的三方库」
    // 拆出去，浏览器可长期缓存，改业务代码时用户只需重下业务包。
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          antd: ['antd', '@ant-design/icons'],
          echarts: ['echarts', 'echarts-for-react'],
          vendor: ['axios', 'dayjs', 'zustand', '@tanstack/react-query'],
        },
      },
    },
  },
})
