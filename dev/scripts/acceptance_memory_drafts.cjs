// Synthetic browser check for blank memory drafts and the existing inline editor.
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), http = require('node:http')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../web_frontend/dist'), delay = ms => new Promise(resolve => setTimeout(resolve, ms))
const inventory = { kits: [], social: [], experiences: [], observations: Array.from({ length: 7 }, (_, i) => ({
  note_id: 'note-' + i, text: '见闻 ' + i + '\n' + '这是观察依据与推测的区分。'.repeat(30), source: 'reflection', record_time: '2026-10-10T14:00:00' })) }
let failKitCreate = false, kitPosts = 0
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
      if (kind === 'maintenance') return route.fulfill({ json: { items: [], requests: [], failure_reasons: [] } })
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
        if (kind === 'kits') { kitPosts++; if (failKitCreate) { failKitCreate = false; return route.fulfill({ status: 503, json: { error: '模拟保存失败，稍后重试' } }) } }
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

    const memory = page.locator('.memory-dialog')
    const openMemory = () => page.getByTitle('记忆 (Character / Group / FuzzyRole)', { exact: true }).click()
    const tab = name => memory.locator('.memory-tab').filter({ hasText: new RegExp('^' + name) }).click()
    async function start(kind) {
      if (!kind) await memory.getByRole('button', { name: '新建', exact: true }).click()
      else {
        await memory.locator('.el-dropdown').getByRole('button').nth(1).click()
        await page.getByRole('menuitem', { name: '新建' + kind, exact: true }).click()
      }
      assert.equal(await page.locator('.memory-create-dialog').count(), 0)
    }
    async function edit(label, value) {
      const input = memory.getByLabel(label, { exact: true })
      if (!await input.count()) await memory.getByRole('button', { name: '修改' + label, exact: true }).click()
      await input.fill(value)
      await memory.getByRole('button', { name: '保存', exact: true }).click()
    }
    await openMemory()
    await check('blank social entry opens inline; optional fields are retained', async () => {
      await start()
      await memory.getByLabel('名称', { exact: true }).waitFor()
      assert.equal(inventory.social.length, 0)
      await edit('名称', '草稿角色')
      await edit('印象', '可靠且耐心')
      assert.equal(inventory.social.length, 0)
      await edit('关联会话', 'private_123')
      await memory.getByRole('button', { name: '修改name', exact: true }).waitFor()
      assert.equal(inventory.social.length, 1)
      assert.equal(inventory.social[0].impression, '可靠且耐心')
      assert.equal(inventory.social[0].session_id, 'private_123')
      await start('群组'); await edit('名称', '草稿群组'); await edit('关联会话', 'group_456')
      await memory.getByRole('button', { name: '修改name', exact: true }).waitFor()
      await start('模糊角色'); await edit('名称', '老师'); await edit('内容', '提及的角色'); await edit('来源会话', 'private_123')
      await memory.getByRole('button', { name: '修改name', exact: true }).waitFor()
      assert.equal(inventory.social.length, 3)
    })
    await check('incomplete kit survives tab changes and page reload', async () => {
      await tab('锦囊'); await start()
      await edit('建议正文', '先确认资源。\n再联系相关的人。')
      assert.equal(inventory.kits.length, 0)
      await tab('经验'); await tab('锦囊')
      await memory.locator('.memory-item').filter({ hasText: '未命名锦囊' }).click()
      assert.match(await memory.getByRole('button', { name: '修改建议正文', exact: true }).textContent(), /先确认资源/)
      await page.reload(); await openMemory(); await tab('锦囊')
      await memory.locator('.memory-item').filter({ hasText: '未命名锦囊' }).click()
      assert.match(await memory.getByRole('button', { name: '修改建议正文', exact: true }).textContent(), /先确认资源/)
    })
    await check('completed kit retries without losing draft and then uses normal editing', async () => {
      await edit('作用困境', '缺少现实联系人信息')
      failKitCreate = true
      await edit('困境分类', '资源盘点')
      await memory.getByText('模拟保存失败，稍后重试').first().waitFor()
      assert.equal(inventory.kits.length, 0)
      assert.equal(kitPosts, 1)
      await memory.getByRole('button', { name: '保存', exact: true }).click()
      await memory.getByRole('button', { name: '修改困境分类', exact: true }).waitFor()
      assert.equal(inventory.kits.length, 1)
      assert.equal(kitPosts, 2)
      await edit('建议正文', '检查通讯录，再确认联系方式。')
      await memory.getByRole('button', { name: '修改建议正文', exact: true }).waitFor()
      assert.equal(inventory.kits[0].advice, '检查通讯录，再确认联系方式。')
    })
    await check('experience and observation create through inline fields only', async () => {
      await tab('经验'); await start(); await edit('标题', '新的经验')
      assert.equal(inventory.experiences.length, 0)
      await edit('摘要', '保留完整内容与换行。')
      await memory.getByRole('button', { name: '修改摘要', exact: true }).waitFor()
      assert.equal(inventory.experiences.length, 1)
      await tab('见闻录'); await start(); await edit('见闻正文', '一段真实观察与依据。')
      await memory.getByRole('button', { name: '修改见闻正文', exact: true }).waitFor()
      assert.equal(inventory.observations.length, 8)
    })
    await check('blank draft can be removed without creating a server record', async () => {
      await tab('锦囊'); await start()
      await memory.getByRole('button', { name: '删除', exact: true }).click()
      assert.equal(inventory.kits.length, 1)
      assert.equal(kitPosts, 2)
      assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('abyss:memory-drafts:v1') || '[]')), [])
      await memory.locator('.memory-item').first().click()
      await page.screenshot({ path: path.resolve(__dirname, '../../cache/tests/memory-drafts-complete.png') })
      assert.deepEqual(errors, [])
    })
    console.log(passed + ' memory draft browser checks passed, synthetic data only.')
  } finally { await browser.close(); server.close() }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1 })
