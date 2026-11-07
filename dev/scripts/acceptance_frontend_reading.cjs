// Start web_frontend's Vite server on 127.0.0.1:5178 first (or set FRONTEND_TEST_URL for a production preview).
// Run with Playwright available via NODE_PATH. All API/WS traffic uses synthetic fixtures.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const { chromium } = require('playwright')
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
const sessions = ['alpha', 'beta'].map((id, i) => ({ session_id: id, scope: 'private', user_id: id,
  user_name: `验证用户 ${id}`, session_name: `验证会话 ${id}`, role: null, created_at: 100 - i }))
const entry = i => ({ role: i % 2 ? 'assistant' : 'user', content: `上下文 ${i}\n` + '用于验证阅读位置的模拟内容。'.repeat(28) })
let entries = Array.from({ length: 70 }, (_, i) => entry(i))
entries.push({ role: 'assistant', content: '工具调用', reasoning_content: '模拟思考过程。'.repeat(40),
  tool_calls: [{ id: 'validation-tool', type: 'function', function: { name: 'validation', arguments: '{"read_only":true}' } }] },
  { role: 'tool', name: 'validation', tool_call_id: 'validation-tool', content: '模拟工具结果。'.repeat(40) })
let version = 1, fileDelay = 0, failFile = false, ws
const messages = (id, count) => Array.from({ length: count }, (_, i) => ({ message_id: `${id}-${i}`, user_id: id,
  user_name: `验证用户 ${id}`, timestamp: 1700000000 + i, content_text: `记录 ${id}-${i}\n` + '模拟会话内容。'.repeat(25), content: [], images: [] }))
let passed = 0
async function check(name, fn) { await fn(); passed++; console.log(`PASS ${name}`) }
async function position(locator) {
  return locator.evaluate(el => {
    const top = el.getBoundingClientRect().top
    const anchor = [...el.querySelectorAll('[data-scroll-key]')].find(item => item.getBoundingClientRect().bottom > top + 1)
    return { scroll: el.scrollTop, end: el.scrollHeight - el.clientHeight - el.scrollTop,
      key: anchor?.dataset.scrollKey, offset: anchor ? anchor.getBoundingClientRect().top - top : null }
  })
}
async function scroll(locator, top) { await locator.evaluate((el, value) => { el.scrollTop = value; el.dispatchEvent(new Event('scroll')) }, top); await delay(80) }
async function sameAnchor(locator, before) {
  const offset = await locator.evaluate((el, key) => {
    const item = [...el.querySelectorAll('[data-scroll-key]')].find(item => item.dataset.scrollKey === key)
    return item?.getBoundingClientRect().top - el.getBoundingClientRect().top
  }, before.key)
  assert.ok(Math.abs(offset - before.offset) < 3, `anchor moved: ${before.offset} -> ${offset}`)
}

;(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' })
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/console/status', route => route.fulfill({ json: { manager_available: false, core_ready: true } }))
    await page.route('**/console/trucks', route => route.fulfill({ json: { trucks: [], startup_paused: false, core_ready: true } }))
    await page.route('**/context/**', async route => {
      const url = new URL(route.request().url())
      if (url.pathname === '/context/chains') return route.fulfill({ json: { chains: [{ chain_id: 'validation-chain', is_active: true,
        files: ['a.json', 'b.json'].map(name => ({ name, size: version * 1000, mtime: version, kind: 'chat' })) }], unlinked: { files: [] } } })
      const name = url.searchParams.get('name')
      const data = { kind: 'chat', name, chain_id: 'validation-chain', raw_size: 10000,
        entries: name === 'b.json' ? [{ role: 'system', content: 'SECOND FILE' }] : [...entries] }
      await delay(name === 'b.json' ? 400 : fileDelay)
      return route.fulfill(failFile ? { status: 500, body: 'simulated refresh failure' } : { json: data })
    })
    await page.route('**/sessions?**', route => route.fulfill({ json: { sessions } }))
    await page.route('**/sessions', route => route.fulfill({ json: { sessions } }))
    await page.route('**/messages?**', async route => {
      const url = new URL(route.request().url()), id = url.searchParams.get('id'), all = url.searchParams.get('limit') === '0'
      const data = messages(id, 100)
      await delay(id === 'beta' ? 350 : 40)
      return route.fulfill({ json: { session_id: id, scope: 'private', total: 100, messages: all ? data : data.slice(50) } })
    })
    await page.routeWebSocket('**/web/ws', socket => {
      ws = socket
      socket.send(JSON.stringify({ type: 'hello', sessions }))
      socket.onMessage(message => {
        const request = JSON.parse(message.toString())
        if (request.type === 'get_messages') socket.send(JSON.stringify({ type: 'messages', session_id: request.session_id,
          messages: messages(request.session_id, 60), total: 60, has_more: false }))
      })
    })
    await page.goto(process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5178')
    await page.locator('.session-item').first().waitFor()
    await check('main sidebar keyboard resize and storage', async () => {
      const handle = page.getByRole('separator', { name: '调整会话列表宽度' })
      await handle.focus(); await page.keyboard.press('ArrowRight')
      assert.equal(await handle.getAttribute('aria-valuenow'), '296')
      assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('abyss:chat-sidebar')).width), 296)
    })
    await check('incoming chat preserves reading position and follows at end', async () => {
      const chatList = page.locator('.chat-window')
      await delay(180)
      await scroll(chatList, 700)
      const before = await chatList.evaluate(el => el.scrollTop)
      ws.send(JSON.stringify({ type: 'llm_message', session_id: 'alpha', message_id: 'new-1', text: '模拟新消息', segments: [], timestamp: 1700100000 }))
      await delay(220)
      assert.ok(Math.abs((await chatList.evaluate(el => el.scrollTop)) - before) < 4)
      await page.getByRole('button', { name: '跳到底部' }).click(); await delay(600)
      ws.send(JSON.stringify({ type: 'llm_message', session_id: 'alpha', message_id: 'new-2', text: '模拟最新消息', segments: [], timestamp: 1700100001 }))
      await delay(200)
      assert.ok((await position(chatList)).end < 85)
    })
    await page.getByRole('button', { name: '上下文', exact: true }).click()
    const dialog = page.locator('.ctx-dialog'), list = dialog.locator('.ctx-list')
    await dialog.locator('.ctx-entry').nth(60).waitFor()
    await check('refresh preserves list and anchor during reading', async () => {
      await scroll(list, 1300)
      const before = await position(list)
      await list.evaluate(el => { window.__readingList = el })
      entries = [entry(1000), ...entries]; version++; fileDelay = 250
      await dialog.getByRole('button', { name: '刷新', exact: true }).click()
      await delay(80)
      assert.equal(await list.evaluate(el => window.__readingList === el), true)
      await delay(350); await sameAnchor(list, before)
    })
    await check('WS update preserves reading position', async () => {
      fileDelay = 0
      const before = await position(list)
      entries.push(entry(1001)); version++
      ws.send(JSON.stringify({ type: 'context_updated' }))
      await delay(500); await sameAnchor(list, before)
    })
    await check('latest follows append at bottom', async () => {
      await dialog.getByRole('button', { name: '最新', exact: true }).click()
      entries.push(entry(1002)); version++
      ws.send(JSON.stringify({ type: 'context_updated' }))
      await delay(500)
      assert.ok((await position(list)).end < 3)
    })
    await check('refresh failure keeps readable content', async () => {
      await scroll(list, 1000); const before = await position(list)
      failFile = true
      await dialog.getByRole('button', { name: '刷新', exact: true }).click(); await delay(200)
      assert.ok(await dialog.locator('.reading-error').isVisible())
      assert.ok(await dialog.locator('.ctx-entry').count() > 60)
      await sameAnchor(list, before); failFile = false
    })
    await check('fast file switch discards stale response', async () => {
      await dialog.locator('.ctx-chain-file').nth(1).click()
      await dialog.locator('.ctx-chain-file').nth(0).click()
      await delay(550)
      assert.ok((await list.innerText()).includes('上下文'))
      assert.ok(!(await list.innerText()).includes('SECOND FILE'))
    })
    await check('pointer resize clamps sidebar and keeps content usable', async () => {
      const handle = dialog.getByRole('separator')
      const bounds = await handle.boundingBox()
      const beforeWidth = Number(await handle.getAttribute('aria-valuenow'))
      const hit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.className,
        { x: bounds.x + 3, y: bounds.y + 120 })
      await page.mouse.move(bounds.x + 3, bounds.y + 120); await page.mouse.down()
      await page.mouse.move(bounds.x + 160, bounds.y + 120); await page.mouse.up()
      assert.ok(Number(await handle.getAttribute('aria-valuenow')) > 350,
        JSON.stringify({ beforeWidth, afterWidth: await handle.getAttribute('aria-valuenow'), hit, bounds }))
      assert.ok((await dialog.locator('.ctx-main').boundingBox()).width >= 360)
    })
    await check('tool cards stack in narrow content and keep collapse state', async () => {
      await dialog.getByRole('separator').focus(); await page.keyboard.press('End'); await delay(80)
      assert.equal(await dialog.locator('.ctx-interaction-body').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length), 1)
      await dialog.getByRole('button', { name: '最新', exact: true }).click()
      await dialog.locator('.ctx-reasoning-label.clickable').click()
      assert.ok(await dialog.locator('.ctx-reasoning-preview').isVisible())
      version++
      await dialog.getByRole('button', { name: '刷新', exact: true }).click(); await delay(180)
      assert.ok(await dialog.locator('.ctx-reasoning-preview').isVisible())
    })
    await check('narrow context uses drawer and fits viewport', async () => {
      await page.setViewportSize({ width: 480, height: 640 }); await delay(150)
      assert.equal(await dialog.getByRole('separator').count(), 0)
      await dialog.getByRole('button', { name: '展开上下文文件' }).click()
      assert.ok(await dialog.locator('.is-drawer').isVisible())
      await dialog.getByRole('button', { name: '收起上下文文件' }).first().click()
      const bounds = await dialog.boundingBox()
      assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 641, JSON.stringify(bounds))
    })
    await page.setViewportSize({ width: 1280, height: 820 })
    await dialog.getByRole('button', { name: '关闭', exact: true }).click()
    await page.getByRole('button', { name: '会话记录', exact: true }).click()
    const records = page.locator('.session-chat-dialog'), recordList = records.locator('.schat-msg-list')
    await records.locator('.schat-item').first().click(); await delay(180)
    await check('load older records preserves visible message', async () => {
      await scroll(recordList, 1000); const before = await position(recordList)
      await records.getByRole('button', { name: '加载全部' }).click(); await delay(200)
      await sameAnchor(recordList, before)
    })
    await check('fast session switch ignores stale messages', async () => {
      await records.locator('.schat-item').nth(1).click()
      await records.locator('.schat-item').nth(0).click(); await delay(500)
      assert.ok((await recordList.innerText()).includes('alpha-'))
      assert.ok(!(await recordList.innerText()).includes('beta-'))
    })
    await check('narrow records and app do not overflow horizontally', async () => {
      await page.setViewportSize({ width: 480, height: 640 }); await delay(150)
      const bounds = await records.boundingBox()
      assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 641, JSON.stringify(bounds))
      await records.getByRole('button', { name: '关闭', exact: true }).click()
      assert.ok(await page.locator('.app-shell').evaluate(el => el.scrollWidth <= el.clientWidth + 1))
    })
    await page.setViewportSize({ width: 1180, height: 760 })
    await page.getByRole('button', { name: '上下文', exact: true }).click(); await delay(500)
    const out = path.resolve(__dirname, '../docs/_audit_frontend')
    fs.mkdirSync(out, { recursive: true })
    await page.screenshot({ path: path.join(out, 'context-reader.png') })
    await check('hard refresh bypasses cached HTML without clearing login cookie', async () => {
      let revision = 1
      const server = http.createServer((request, response) => {
        if (request.url !== '/') { response.writeHead(204); response.end(); return }
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'max-age=3600',
          'Set-Cookie': 'validation_login=retained; Path=/' })
        response.end(`<html><body data-revision="${revision}">Cached page fixture ${revision}</body></html>`)
      })
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
      const isolated = await browser.newContext()
      try {
        const cached = await isolated.newPage()
        await cached.goto(`http://127.0.0.1:${server.address().port}/`)
        assert.equal(await cached.locator('body').getAttribute('data-revision'), '1')
        revision = 2
        const protocol = await isolated.newCDPSession(cached)
        await protocol.send('Page.reload', { ignoreCache: true })
        await cached.waitForFunction(() => document.body?.dataset.revision === '2')
        assert.ok((await isolated.cookies()).some(cookie => cookie.name === 'validation_login' && cookie.value === 'retained'))
      } finally { await isolated.close(); await new Promise(resolve => server.close(resolve)) }
    })
    assert.deepEqual(errors, [], 'browser runtime errors')
    console.log(`${passed} browser checks passed; all data and service traffic mocked.`)
  } finally { await browser.close() }
})().catch(error => { console.error(error); process.exitCode = 1 })
