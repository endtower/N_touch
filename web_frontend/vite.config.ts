import { defineConfig, type ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// 开发页面也使用常驻管理入口；令牌由开发服务器读取，不进入浏览器脚本。
function managerProxy(ws = false): ProxyOptions {
  return {
    target: 'http://127.0.0.1:49990', ws, changeOrigin: true,
    configure(proxy) {
      const authorize = (req: any) => {
        try {
          const root = process.env.ABYSS_DATA_DIR || resolve(process.cwd(), '..')
          req.setHeader('X-Manager-Token', readFileSync(resolve(root, 'manager/service_token.txt'), 'utf8').trim())
        } catch { /* 管理服务未启动时保持未登录状态 */ }
      }
      proxy.on('proxyReq', authorize)
      proxy.on('proxyReqWs', authorize)
    },
  }
}

// 端口分布：
//   5000 - FastAPI main.py  （你的主服务，目前不用）
//   5002 - WebChannel       （WebSocket + 静态 /media，给浏览器当消息通道）
//   5173 - Vite dev server  （这个文件）
//
// Vite 将页面 API 转发到常驻 manager（49990），业务请求再转发到 core。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      '/console': managerProxy(),
      '/system': managerProxy(true),
      // WebSocket 通道
      '/web': managerProxy(true),
      // 会话记录调试查看器（HTTP，直接读 chat_history 存储）
      '/sessions': managerProxy(),
      '/messages': managerProxy(),
      // 静态媒体文件
      '/media': managerProxy(),
      // LLM 自存表情包 / 图片 (memory/llm_save/...) — 同 dev server 转发到 5002 的 /llm-save
      '/llm-save': managerProxy(),
      // 表情包管理 HTTP API（/stickers/list、/stickers/vectorize、/stickers/{filename}）
      '/stickers': managerProxy(),
      // 健康检查
      '/health': managerProxy(),
      // 图编辑器 HTTP API
      '/graph': managerProxy(),
      // 长期事件列表 HTTP API
      '/events': managerProxy(),
      // 上下文 HTTP API
      '/context': managerProxy(),
      '/amy': managerProxy(),
      // 记事本笔记 HTTP API（/notes）
      '/notes': managerProxy(),
      // 记忆社交实体 HTTP API（/memory/social）
      '/memory': managerProxy(),
      // Token 用量统计 HTTP API（/token-stats）
      '/token-stats': managerProxy(),
    },
  },
})
