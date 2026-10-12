/**
 * 宿主（WPF 启动器）↔ 网页 的主题联动。
 *
 * 仅在被 WebView2 内嵌时生效；直接用浏览器打开时 window.chrome?.webview
 * 不存在，这里全部退化成空操作，不影响独立运行。
 *
 * 协议只有一条消息，且只有一个主题名：
 *   { type: 'abyss-theme', value: 'dark' | 'light' }
 * 不含管理接口、访问令牌或任何执行能力 —— 启动器侧也只对自家的 Core 页
 * 挂这条桥，第三方页（SnowLuma）不挂（见任务书 §7）。
 */
export type HostTheme = 'dark' | 'light'

interface WebViewHost {
  postMessage: (message: unknown) => void
  addEventListener: (type: 'message', listener: (e: { data: unknown }) => void) => void
}

function host(): WebViewHost | null {
  const w = window as unknown as { chrome?: { webview?: WebViewHost } }
  return w.chrome?.webview ?? null
}

export const hostTheme = {
  /** 是否运行在启动器内嵌的 WebView2 里 */
  available(): boolean {
    return host() !== null
  },

  /** 把网页里选的明暗推给启动器 */
  push(theme: HostTheme): void {
    host()?.postMessage({ type: 'abyss-theme', value: theme })
  },

  /**
   * 监听启动器推来的主题。返回取消订阅函数。
   * 注意：回调里不要再 push 回去，否则两边会来回回声。
   */
  listen(onTheme: (theme: HostTheme) => void): () => void {
    const h = host()
    if (!h) return () => {}
    const handler = (e: { data: unknown }) => {
      const data = e.data as { type?: string; value?: string } | null
      if (!data || data.type !== 'abyss-theme') return
      if (data.value === 'dark' || data.value === 'light') onTheme(data.value)
    }
    h.addEventListener('message', handler)
    // WebView2 的 addEventListener 没有对应的移除接口；页面生命周期与宿主一致，无需解绑
    return () => {}
  },
}
