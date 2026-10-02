import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 端口分布：
//   5000 - FastAPI main.py  （你的主服务，目前不用）
//   5002 - WebChannel       （WebSocket + 静态 /media，给浏览器当消息通道）
//   5173 - Vite dev server  （这个文件）
//
// Vite 代理把 /web/ws 和 /media 转发到 5002，避免浏览器跨域。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      // WebSocket 通道
      '/web': {
        target: 'http://127.0.0.1:5002',
        ws: true,
        changeOrigin: true,
      },
      // 会话记录调试查看器（HTTP，直接读 chat_history 存储）
      '/sessions': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      '/messages': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 静态媒体文件
      '/media': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // LLM 自存表情包 / 图片 (memory/llm_save/...) — 同 dev server 转发到 5002 的 /llm-save
      '/llm-save': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 表情包管理 HTTP API（/stickers/list、/stickers/vectorize、/stickers/{filename}）
      '/stickers': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 健康检查
      '/health': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 图编辑器 HTTP API
      '/graph': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 长期事件列表 HTTP API
      '/events': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 上下文 HTTP API
      '/context': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      '/amy': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 记事本笔记 HTTP API（/notes）
      '/notes': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // 记忆社交实体 HTTP API（/memory/social）
      '/memory': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
      // Token 用量统计 HTTP API（/token-stats）
      '/token-stats': {
        target: 'http://127.0.0.1:5002',
        changeOrigin: true,
      },
    },
  },
})
