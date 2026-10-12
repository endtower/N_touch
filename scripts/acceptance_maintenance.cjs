// Built UI, synthetic HTTP fixtures only. Never connects to the running backend.
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), http = require('node:http')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../web_frontend/dist')
const personas = [{ persona_name: 'normal', scene: '普通交流', kernel_prompt: '保持自己的判断。' }]
const experience = { bequest_id: 'exp1', title: '说明求助背景', summary: '先说明现有资源。', category: 'speech', updated_at: '2026-10-11T01:00:00' }
const requests = [{ request_id: 'r1', topic: '求助背景专题', reason: '希望主人校准适用边界', status: 'pending', created_at: 1791648000, conclusion: '', items: [{ kind: 'experience', item_id: 'exp1', title: experience.title }] }]
const versions = { persona: 'p1', experience: 'e1' }, confirmed = { persona: false, experience: false }
let reviews = [], passed = 0, stale = false
const server = http.createServer((req, res) => {
  const file = path.resolve(dist, '.' + (new URL(req.url, 'http://local').pathname === '/' ? '/index.html' : decodeURIComponent(new URL(req.url, 'http://local').pathname)))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end() }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html')
  fs.createReadStream(file).pipe(res)
})
async function check(name, fn) { await fn(); passed++; console.log('PASS ' + name) }
;(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch({ headless: true, channel: 'msedge' })
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/console/**', route => route.fulfill({ json: { manager_available: false, core_ready: true, trucks: [] } }))
    await page.route('**/sessions**', route => route.fulfill({ json: { sessions: [] } }))
    await page.routeWebSocket('**/web/ws', socket => socket.send(JSON.stringify({ type: 'hello', sessions: [] })))
    await page.route('**/memory/**', async route => {
      const request = route.request(), url = new URL(request.url()), body = ['POST', 'PATCH'].includes(request.method()) ? request.postDataJSON() : null
      const reply = (json, status = 200) => route.fulfill({ json, status })
      if (url.pathname === '/memory/social') return reply({ nodes: [], edges: [], character_count: 0, group_count: 0, fuzzy_role_count: 0 })
      if (url.pathname === '/memory/experiences') return reply({ experiences: [experience], total: 1 })
      if (url.pathname === '/memory/personas') return reply({ personas })
      if (url.pathname === '/memory/personas/normal') {
        Object.assign(personas[0], body); versions.persona = 'p2'; confirmed.persona = true
        return reply({ persona: personas[0] })
      }
      if (url.pathname === '/memory/maintenance/items') {
        const kind = url.searchParams.get('kind')
        return reply({ items: [{ kind, item_id: url.searchParams.get('item_id'), revision: versions[kind],
          maintenance: { human_confirmed: confirmed[kind], changed_since_review: !confirmed[kind] && versions[kind] !== 'p1' && versions[kind] !== 'e1', last_review: reviews.at(-1) },
          usage: kind === 'experience' ? { vector_returned: { count: 7, last_used_at: 1791648000 }, injected: { count: 2, last_used_at: 1791648000 } } : { selected: { count: 3, last_used_at: 1791648000 } } }] })
      }
      if (url.pathname === '/memory/maintenance/reviews') {
        assert.equal(body.revision, versions[body.kind])
        if (stale) { stale = false; versions[body.kind] = 'p3'; confirmed[body.kind] = false; return reply({ error: '内容已变更，请刷新后重新检查' }, 400) }
        confirmed[body.kind] = true; reviews.push({ review_id: String(reviews.length), title: 'normal', reviewed_at: 1791648000, action: 'review' })
        return reply({ reviewed: true })
      }
      if (url.pathname === '/memory/maintenance') return reply({ requests, reviews, eligibility: {} })
      if (url.pathname === '/memory/maintenance/requests/r1') {
        Object.assign(requests[0], body, { defer_until: 1791648000 + body.defer_hours * 3600 })
        return reply({ status: body.status })
      }
      return reply({ error: 'unhandled fixture: ' + url.pathname }, 404)
    })
    await page.goto('http://127.0.0.1:' + server.address().port)
    await page.getByTitle('记忆 (Character / Group / FuzzyRole)', { exact: true }).click()
    const memory = page.locator('.memory-dialog'), tab = name => memory.locator('.memory-tab').filter({ hasText: new RegExp('^' + name) }).click()
    await check('persona uses existing editor and saves human confirmation', async () => {
      await tab('人格'); await memory.locator('.memory-item').filter({ hasText: 'normal' }).click()
      await memory.getByText('当前版本尚未人工确认', { exact: true }).waitFor()
      assert.equal(await memory.getByRole('button', { name: '删除', exact: true }).count(), 0)
      await memory.getByRole('button', { name: '修改人格正文', exact: true }).click()
      await memory.getByLabel('人格正文', { exact: true }).fill('人工校准后的正文。')
      await memory.getByRole('button', { name: '保存', exact: true }).click()
      await memory.getByText('当前版本已人工确认', { exact: true }).waitFor()
      assert.equal(personas[0].kernel_prompt, '人工校准后的正文。')
    })
    await check('stale review fails and reloads current version', async () => {
      stale = true
      await memory.getByRole('button', { name: '已检查，无需修改', exact: true }).click()
      await memory.getByText('人工确认后内容已变更', { exact: true }).waitFor()
      await memory.getByRole('button', { name: '已检查，无需修改', exact: true }).click()
      await memory.getByText('当前版本已人工确认', { exact: true }).waitFor()
    })
    await check('experience shows vector returns distinct from injection', async () => {
      await tab('经验'); await memory.locator('.memory-item').filter({ hasText: experience.title }).click()
      await memory.getByText(/向量候选返回：7 次/).waitFor()
      await memory.getByText(/注入上下文：2 次/).waitFor()
    })
    await check('maintenance request opens existing item and records owner decision', async () => {
      await tab('维护申请'); await memory.locator('.request-item').click()
      await memory.getByRole('button', { name: experience.title, exact: true }).click()
      await memory.getByText(/向量候选返回：7 次/).waitFor()
      await tab('维护申请'); await memory.locator('.request-item').click()
      await memory.getByLabel('维护处理说明').fill('旅行中，稍后处理')
      await memory.getByRole('button', { name: '暂缓', exact: true }).click()
      await memory.getByText(/到期后仍保留原申请/).waitFor()
      assert.equal(requests[0].status, 'deferred')
      await memory.getByRole('button', { name: '不采纳', exact: true }).click()
      await memory.getByRole('button', { name: '开始维护', exact: true }).waitFor({ state: 'hidden' })
      await memory.getByText('处理结论：旅行中，稍后处理', { exact: true }).waitFor()
      assert.equal(await memory.getByRole('button', { name: '开始维护', exact: true }).count(), 0)
      assert.equal(requests[0].status, 'rejected')
    })
    await check('narrow window keeps tabs and controls within dialog', async () => {
      await page.setViewportSize({ width: 800, height: 800 })
      await memory.getByRole('button', { name: '刷新', exact: true }).click()
      const dialog = await memory.boundingBox()
      for (const locator of [memory.locator('.memory-tabs'), memory.getByRole('button', { name: '关闭', exact: true })]) {
        const rect = await locator.boundingBox(); assert.ok(rect.x >= dialog.x && rect.x + rect.width <= dialog.x + dialog.width + 2)
      }
      fs.mkdirSync(path.resolve(__dirname, '../tmp'), { recursive: true })
      await page.screenshot({ path: path.resolve(__dirname, '../tmp/maintenance-ui.png') })
      assert.deepEqual(errors, [])
    })
    console.log(`${passed} maintenance UI checks passed`)
  } finally { await browser.close() }
})().catch(error => { console.error(error); process.exitCode = 1 }).finally(() => server.close())
