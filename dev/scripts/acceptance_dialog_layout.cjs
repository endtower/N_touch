// Browser regression check against the production build; all APIs use synthetic data.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const { chromium } = require('playwright')

const dist = path.resolve(__dirname, '../web_frontend/dist')
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost')
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : decodeURIComponent(url.pathname)))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); return res.end()
  }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html')
  fs.createReadStream(file).pipe(res)
})
const panels = [
  ['Truck 控制', '.truck-dialog'],
  ['记忆 (Character / Group / FuzzyRole)', '.memory-dialog'],
  ['任务管理 (按状态查看 / 编辑 / 删除)', '.evlist-dialog'],
  ['LLM 上下文查看 (memory/chat & memory/pfca)', '.ctx-dialog'],
  ['LLM 当前情绪状态 (Amy: NE/DA, 活跃 thought, ACT 表)', '.amy-dialog'],
  ['会话记录 (调试: 直接查看 chat_history 存储的消息)', '.session-chat-dialog'],
  ['Token 用量统计 (各 Agent / API 提供商 / 模型的消耗与缓存命中)', '.tk-dialog'],
  ['系统设置 (模型接入 / 进程管理 / 实时日志)', '.sp-dialog'],
]
const near = (a, b, label) => assert.ok(Math.abs(a - b) < 2, `${label}: ${a} vs ${b}`)
async function geometry(dialog, viewport, expected) {
  await dialog.evaluate(el => Promise.all(el.getAnimations().map(animation => animation.finished.catch(() => {}))))
  const close = dialog.locator(':scope > .el-dialog__header > .el-dialog__headerbtn')
  await close.waitFor({ state: 'visible' })
  await close.click({ trial: true })
  const box = await dialog.boundingBox(), button = await close.boundingBox()
  assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1, 'window fits viewport')
  assert.ok(button.x >= box.x && button.y >= box.y && button.x + button.width <= box.x + box.width && button.y + button.height <= box.y + box.height, 'X remains inside window')
  assert.equal(await close.evaluate(el => {
    const r = el.getBoundingClientRect()
    return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2))
  }), true, 'X is not covered by toolbar content')
  if (expected) for (const key of ['x', 'y', 'width', 'height']) near(box[key], expected[key], key)
  near(box.x + box.width / 2, viewport.width / 2, 'horizontal center')
  near(box.y + box.height / 2, viewport.height / 2, 'vertical center')
  return box
}

;(async () => {
  let browser
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    browser = await chromium.launch({ headless: true, channel: 'msedge' })
    const page = await browser.newPage(), errors = []
    page.on('pageerror', error => errors.push(error.message))
    const fixtures = {
      '/console/status': { manager_available: false, core_ready: true },
      '/console/trucks': { trucks: [], startup_paused: false },
      '/sessions': { sessions: [] },
      '/memory/social': { nodes: [], edges: [], root_id: '00000000', character_count: 0, group_count: 0, fuzzy_role_count: 0 },
      '/memory/experiences': { experiences: [], total: 0 },
      '/memory/kits': { kits: [], total: 0 },
      '/memory/observations': { observations: [], total: 0 },
      '/memory/personas': { personas: [], total: 0 },
      '/memory/maintenance/requests': { requests: [], failure_reasons: [] },
      '/events/list': { tasks: [], total: 0, status: 'active', limit: 100, offset: 0 },
      '/context/chains': { chains: [], unlinked: { files: [] }, fetched_at: 0 },
      '/amy/state': { initialized: false, error: '模拟未初始化状态' },
      '/notes': { notes: [] },
      '/token-stats': { totals: { call_count: 0 }, by_agent: {}, by_provider: {}, by_model: {}, per_day: [] },
      '/system/status': { core: { running: false }, snowluma: { running: false, ports: [] } },
      '/system/config': { fields: {} },
      '/system/connections': { available: false, bridge: { enabled: false } },
    }
    await page.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.pathname === '/' || url.pathname.startsWith('/assets/')) return route.continue()
      return route.fulfill({ json: fixtures[url.pathname] || {} })
    })
    await page.routeWebSocket('**/web/ws', socket => socket.send(JSON.stringify({ type: 'hello', sessions: [] })))
    const screenshots = process.env.UI_SCREENSHOT_DIR
    if (screenshots) fs.mkdirSync(screenshots, { recursive: true })
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 1366, height: 768 }, { width: 800, height: 480 }, { width: 390, height: 700 }]) {
      await page.setViewportSize(viewport)
      await page.goto('http://127.0.0.1:' + server.address().port)
      let expected
      for (const [title, selector] of panels) {
        const dialog = page.locator(selector)
        await page.getByTitle(title, { exact: true }).click()
        await dialog.waitFor({ state: 'visible' })
        const box = await geometry(dialog, viewport, expected)
        expected ||= box
        if (selector === '.memory-dialog') {
          for (const name of ['经验', '锦囊', '见闻录', '人格', '维护申请', '社交实体']) {
            await dialog.locator('.memory-tab').filter({ hasText: new RegExp('^' + name) }).click()
            await geometry(dialog, viewport, expected)
          }
          if (screenshots) await page.screenshot({ path: path.join(screenshots, `memory-${viewport.width}.png`) })
        }
        if (selector === '.evlist-dialog') {
          await dialog.getByRole('button', { name: '新建', exact: true }).click()
          const child = page.locator('.evlist-create-dialog')
          await child.waitFor({ state: 'visible' })
          await geometry(child, viewport)
          await child.locator('.el-dialog__headerbtn').click()
          await child.waitFor({ state: 'hidden' })
          assert.equal(await dialog.isVisible(), true, 'nested X closes only the child')
        }
        await dialog.locator(':scope > .el-dialog__header > .el-dialog__headerbtn').click()
        await dialog.waitFor({ state: 'hidden' })
        await page.getByTitle(title, { exact: true }).click()
        await dialog.waitFor({ state: 'visible' })
        await geometry(dialog, viewport, expected)
        await page.keyboard.press('Escape')
        await dialog.waitFor({ state: 'hidden' })
      }
      console.log(`PASS ${viewport.width}x${viewport.height}: 8 matching panels; tab switches, reopen, X, Escape and nested close`)
    }
    assert.deepEqual(errors, [], 'no browser runtime errors')
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
