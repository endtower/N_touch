// Built webpage with synthetic data only: shared memory lists, editors and creation forms.
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), http = require('node:http')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../web_frontend/dist'), delay = ms => new Promise(resolve => setTimeout(resolve, ms))
const inventory = { kits: [], social: [], experiences: [], observations: Array.from({ length: 7 }, (_, i) => ({
  note_id: 'note-' + i, text: '见闻 ' + i + '\n' + '这是观察依据与推测的区分。'.repeat(30), source: 'reflection', record_time: '2026-10-10T14:00:00' })) }
let nextId = 1, searchMode = 'matched', passed = 0
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost')
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : decodeURIComponent(url.pathname)))
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
    page.on('pageerror', err => errors.push(err.message))
    await page.route('**/console/**', route => route.fulfill({ json: { manager_available: false, core_ready: true, trucks: [], startup_paused: false } }))
    await page.route('**/sessions**', route => route.fulfill({ json: { sessions: [] } }))
    await page.routeWebSocket('**/web/ws', socket => socket.send(JSON.stringify({ type: 'hello', sessions: [] })))
    await page.route('**/memory/**', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method(), parts = url.pathname.split('/'), kind = parts[2], list = inventory[kind]
      if (url.pathname === '/memory/observations/search') {
        assert.equal(url.searchParams.get('query'), '缺少现实联系人信息')
        if (searchMode === 'error') return route.fulfill({ status: 502, json: { error: '模拟嵌入服务不可用' } })
        const matched = searchMode === 'disabled' ? [] : list.map((item, i) => ({ ...item, similarity: 0.95 - i * 0.05 }))
        return route.fulfill({ json: { status: searchMode, observations: matched, count: matched.length, total: list.length,
          hint: searchMode === 'disabled' ? '向量检索已停用，未进行见闻匹配。' : '全部见闻按相似度排序，不代表事实可信度。' } })
      }
      if (!list) return route.fulfill({ status: 404, json: { error: 'fixture not found' } })
      if (method === 'GET') {
        if (kind === 'social') return route.fulfill({ json: { nodes: [{ id: '00000000', uuid: '00000000', type: 'Root', label: '00000000' }, ...list], edges: [], root_id: '00000000',
          character_count: list.filter(item => item.type === 'Character').length, group_count: list.filter(item => item.type === 'Group').length, fuzzy_role_count: list.filter(item => item.type === 'FuzzyRole').length } })
        return route.fulfill({ json: { [kind]: list, total: list.length } })
      }
      const body = ['POST', 'PATCH'].includes(method) ? request.postDataJSON() : null
      const idKey = { kits: 'kit_id', social: 'uuid', experiences: 'bequest_id', observations: 'note_id' }[kind]
      const resultKey = { kits: 'kit', social: 'node', experiences: 'experience', observations: 'observation' }[kind]
      if (method === 'POST') {
        if (kind === 'kits') assert.deepEqual(Object.keys(body).sort(), ['advice', 'category', 'dilemma'])
        if (kind === 'observations') assert.deepEqual(Object.keys(body), ['text'])
        const id = kind + '-' + nextId++, item = { ...body, [idKey]: id, created_at: '2026-10-10T14:00:00', start_time: '2026-10-10T14:00:00', record_time: '2026-10-10T14:00:00', updated_at: '2026-10-10T14:00:00' }
        if (kind === 'observations') item.source = 'human'
        if (kind === 'social') Object.assign(item, { id, label: body.name, type: { character: 'Character', group: 'Group', fuzzy_role: 'FuzzyRole' }[parts[3]], aliases: [{ name: body.name }], impressions: [{ content: body.impression || '' }], contacts: body.session_id ? [{ session_id: body.session_id }] : [] })
        list.push(item); return route.fulfill({ status: 201, json: { [resultKey]: item } })
      }
      const item = list.find(item => item[idKey] === parts.at(-1))
      if (item && method === 'PATCH') {
        if (Object.values(body).some(value => typeof value === 'string' && !value.trim())) return route.fulfill({ status: 400, json: { error: '内容不能为空' } })
        Object.assign(item, body)
        if (kind === 'social') { item.label = item.name; if (body.aliases) item.aliases = body.aliases.map(name => ({ name })) }
        return route.fulfill({ json: { [resultKey]: item, updated: true } })
      }
      if (item && method === 'DELETE') { inventory[kind] = list.filter(old => old !== item); return route.fulfill({ json: { deleted: true } }) }
      return route.fulfill({ status: 404, json: { error: 'not found' } })
    })
    await page.goto('http://127.0.0.1:' + server.address().port)
    const memory = page.locator('.memory-dialog'), create = page.locator('.memory-create-dialog')
    await page.getByTitle('记忆 (Character / Group / FuzzyRole)', { exact: true }).click()
    const tab = name => memory.locator('.memory-tab').filter({ hasText: new RegExp('^' + name) }).click()
    async function add(values, entityType) {
      await memory.getByRole('button', { name: '添加', exact: true }).click()
      if (entityType) { await create.locator('.el-select__wrapper').first().click(); await page.getByRole('option', { name: entityType, exact: true }).click() }
      for (const [label, value] of Object.entries(values)) await create.getByLabel(label, { exact: true }).fill(value)
      await create.getByRole('button', { name: '添加', exact: true }).click(); await create.waitFor({ state: 'hidden' })
    }
    async function edit(label, value) {
      await memory.getByRole('button', { name: '修改' + label, exact: true }).click()
      await memory.getByLabel(label, { exact: true }).fill(value)
      await memory.getByRole('button', { name: '保存', exact: true }).click()
      await memory.getByRole('button', { name: '修改' + label, exact: true }).waitFor()
    }
    await check('social creation and shared inline editor', async () => {
      await add({ 名称: '测试角色', 印象: '可靠', 关联会话: 'private_123' })
      await edit('name', '改名后的角色')
      assert.equal(inventory.social[0].name, '改名后的角色')
      await add({ 名称: '测试群组', 关联会话: 'group_456' }, '群组')
      await add({ 名称: '老师', 内容: '提及的角色', 来源会话: 'private_123' }, '模糊角色')
      assert.equal(await memory.locator('.memory-item').count(), 3)
      assert.equal(inventory.social[2].source_session_id, 'private_123')
    })
    await check('experience creation and shared multiline editor', async () => {
      await tab('经验'); await add({ 标题: '测试经验', 摘要: '经验正文' })
      await edit('摘要', '修改后的经验\n保留换行。')
      assert.equal(inventory.experiences[0].summary, '修改后的经验\n保留换行。')
    })
    const advice = '先检查项目角色和通讯录。\n再确认现有通信渠道。' + '保留完整建议正文。'.repeat(30)
    await check('kits use same creation form and exactly three fields', async () => {
      await tab('锦囊'); await memory.getByRole('button', { name: '添加', exact: true }).click()
      assert.equal(await create.locator('input, textarea').count(), 3)
      await create.getByRole('button', { name: '添加', exact: true }).click()
      await create.getByText('请填写建议正文').waitFor(); assert.equal(inventory.kits.length, 0)
      for (const [label, value] of Object.entries({ 建议正文: advice, 作用困境: '缺少现实联系人信息', 困境分类: '资源盘点' })) await create.getByLabel(label).fill(value)
      await create.getByRole('button', { name: '添加', exact: true }).click(); await create.waitFor({ state: 'hidden' })
      assert.equal(await memory.getByRole('button', { name: '修改建议正文', exact: true }).textContent().then(value => value.trim()), advice)
      await memory.getByRole('button', { name: '刷新', exact: true }).click()
      await memory.getByRole('button', { name: '修改建议正文', exact: true }).waitFor()
      await memory.getByRole('button', { name: '关闭', exact: true }).click()
      await page.getByTitle('记忆 (Character / Group / FuzzyRole)', { exact: true }).click()
      await memory.getByRole('button', { name: '修改建议正文', exact: true }).waitFor()
    })
    await check('kit field editing, category grouping and filtering', async () => {
      await edit('困境分类', '信息缺口'); await edit('作用困境', '需要确认联系方式'); await edit('建议正文', '检查通讯录，再向主人确认。')
      assert.equal(inventory.kits[0].category, '信息缺口')
      await memory.getByPlaceholder('搜索锦囊…').fill('不存在')
      await memory.getByText('没有匹配的条目', { exact: true }).waitFor()
      await memory.getByPlaceholder('搜索锦囊…').fill('通讯录'); await memory.locator('.memory-item').waitFor()
      await memory.getByPlaceholder('搜索锦囊…').fill('')
      await page.locator('.el-message').last().waitFor({ state: 'hidden' })
      await page.screenshot({ path: path.resolve(__dirname, '../../cache/tests/decision-memory-kit.png') })
    })
    await check('kit deletion cancel and confirm', async () => {
      await memory.getByRole('button', { name: '删除', exact: true }).click()
      await page.locator('.el-message-box').getByRole('button', { name: '取消', exact: true }).click(); assert.equal(inventory.kits.length, 1)
      await memory.getByRole('button', { name: '删除', exact: true }).click()
      await page.locator('.el-message-box').getByRole('button', { name: '删除', exact: true }).click()
      await memory.getByText('记忆里还没有锦囊', { exact: true }).waitFor(); assert.equal(inventory.kits.length, 0)
    })
    await check('observation creation, full detail and editing', async () => {
      await tab('见闻录'); await add({ 见闻正文: '人工观察\n标明事实依据与推测。' })
      await memory.getByText('人工记录', { exact: true }).waitFor()
      await edit('见闻正文', '修订后的人工观察\n依据保持完整。')
      assert.equal(inventory.observations.at(-1).text, '修订后的人工观察\n依据保持完整。')
      assert.equal(await memory.locator('.memory-item').count(), 8)
    })
    await check('all ranked observations and failure recovery', async () => {
      await memory.getByLabel('当前困境').fill('缺少现实联系人信息')
      await memory.getByRole('button', { name: '查找见闻' }).click(); await memory.getByRole('button', { name: '显示全部', exact: true }).waitFor()
      await memory.locator('.memory-item').first().click()
      assert.ok((await memory.locator('.kv').textContent()).includes('0.950'))
      assert.equal(await memory.getByRole('button', { name: '修改见闻正文', exact: true }).textContent().then(value => value.trim()), inventory.observations[0].text)
      searchMode = 'error'; await memory.getByRole('button', { name: '查找见闻' }).click(); await memory.getByText('模拟嵌入服务不可用').waitFor()
      assert.equal(await memory.locator('.memory-item').count(), 8)
      searchMode = 'disabled'; await memory.getByRole('button', { name: '查找见闻' }).click(); await memory.getByText('向量检索已停用，未进行见闻匹配。').waitFor()
      assert.equal(await memory.locator('.memory-item').count(), 8)
      searchMode = 'matched'; await memory.getByRole('button', { name: '查找见闻' }).click(); await memory.getByRole('button', { name: '显示全部', exact: true }).click()
    })
    await check('observation deletion and shared details', async () => {
      await memory.locator('.memory-item').last().click()
      await memory.getByRole('button', { name: '删除', exact: true }).click()
      await page.locator('.el-message-box').getByRole('button', { name: '删除', exact: true }).click()
      await page.waitForFunction(() => document.querySelectorAll('.memory-dialog .memory-item').length === 7)
      assert.equal(inventory.observations.length, 7)
      await memory.locator('.memory-item').first().click()
    })
    await check('narrow common layout without clipping or browser errors', async () => {
      await page.locator('.el-message').last().waitFor({ state: 'hidden' })
      await page.setViewportSize({ width: 760, height: 820 }); await delay(120)
      const bounds = await memory.boundingBox()
      for (const button of await memory.locator('.memory-tab, .memory-header-right button').all()) {
        const box = await button.boundingBox(); assert.ok(box.x >= bounds.x && box.x + box.width <= bounds.x + bounds.width + 1)
      }
      assert.ok(await memory.locator('.memory-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1))
      const name = memory.getByRole('button', { name: '修改见闻正文', exact: true })
      assert.equal(await name.evaluate(el => el.scrollWidth <= el.clientWidth + 1), true)
      await page.screenshot({ path: path.resolve(__dirname, '../../cache/tests/decision-memory-narrow.png') })
      await page.setViewportSize({ width: 1280, height: 900 }); await delay(100)
      await page.screenshot({ path: path.resolve(__dirname, '../../cache/tests/decision-memory-ledger.png') })
      assert.deepEqual(errors, [])
    })
    console.log(passed + ' browser checks passed; all requests used synthetic fixtures.')
  } finally { await browser.close(); server.close() }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1 })
