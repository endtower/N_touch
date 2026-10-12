// WebChannel 客户端：单例 WebSocket
import type { ClientMessage, ServerMessage } from '../types'

type Listener = (msg: ServerMessage) => void

class WebChannelClient {
  private ws: WebSocket | null = null
  private listeners: Set<Listener> = new Set()
  private reconnectTimer: number | null = null
  private statusListeners: Set<(connected: boolean) => void> = new Set()

  /** 打开连接（同源 /web/ws，经 Vite 代理到 5002） */
  connect(url: string = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/web/ws`) {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return
    }
    console.log('[WS] connecting to', url)
    const ws = new WebSocket(url)
    this.ws = ws

    ws.onopen = () => {
      console.log('[WS] connected')
      this.notifyStatus(true)
    }
    ws.onclose = () => {
      console.warn('[WS] disconnected, will retry in 2s')
      this.ws = null
      this.notifyStatus(false)
      this.scheduleReconnect(url)
    }
    ws.onerror = (e) => {
      console.warn('[WS] error', e)
    }
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as ServerMessage
        for (const cb of this.listeners) cb(data)
      } catch (e) {
        console.warn('[WS] bad message', ev.data, e)
      }
    }
  }

  private scheduleReconnect(url: string) {
    if (this.reconnectTimer != null) return
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect(url)
    }, 2000)
  }

  send(msg: ClientMessage) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error('WS not connected')
    }
    this.ws.send(JSON.stringify(msg))
  }

  /** Core 停止时保留页面，取消消息通道的自动重连。 */
  disconnect() {
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      this.ws.onclose = null
      this.ws.close()
      this.ws = null
    }
    this.notifyStatus(false)
  }

  /** 主动断开并重连（切换存储库后重新拉取会话/历史用）。 */
  reconnect() {
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      try {
        this.ws.onclose = null
        this.ws.close()
      } catch {
        // ignore
      }
      this.ws = null
      this.notifyStatus(false)
    }
    this.connect()
  }

  onMessage(cb: Listener): () => void {
    this.listeners.add(cb)
    return () => this.listeners.delete(cb)
  }

  onStatus(cb: (connected: boolean) => void): () => void {
    this.statusListeners.add(cb)
    // 立刻通知当前状态
    cb(this.ws?.readyState === WebSocket.OPEN)
    return () => this.statusListeners.delete(cb)
  }

  private notifyStatus(c: boolean) {
    for (const cb of this.statusListeners) cb(c)
  }
}

export const channel = new WebChannelClient()
