// 实际 manager.exe + 生产构建页面；所有用户数据是独立临时夹具，不启动真实 core。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { DatabaseSync } = require('node:sqlite')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..')
const output = path.join(root, 'build', 'console-verification')
const dataRoot = path.join(output, `fixture-${Date.now()}`)
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
let managerExe = process.env.CONSOLE_TEST_EXE || path.join(root, 'dist/console-build/manager/manager.exe')
async function freePort() {
  const socket = net.createServer()
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve))
  const port = socket.address().port
  await new Promise(resolve => socket.close(resolve))
  return port
}

;(async () => {
  fs.mkdirSync(path.join(dataRoot, 'memory/event_chains/offline-fixture'), { recursive: true })
  fs.mkdirSync(path.join(dataRoot, 'memory/chat_history'), { recursive: true })
  const entries = Array.from({ length: 90 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user',
    content: `离线上下文 ${i}\n` + '验证 core 停止时的阅读位置。'.repeat(35) }))
  fs.writeFileSync(path.join(dataRoot, 'memory/event_chains/offline-fixture/conversation.json'), JSON.stringify(entries))
  const db = new DatabaseSync(path.join(dataRoot, 'memory/chat_history/chat_history.db'))
  db.exec(`CREATE TABLE sessions (session_id TEXT, session_name TEXT, last_update_time REAL);
    CREATE TABLE messages (seq INTEGER, session_id TEXT, message_id TEXT, user_id TEXT, user_name TEXT, timestamp REAL, content_text TEXT, content TEXT);
    INSERT INTO sessions VALUES ('private_123','离线验证会话',1);`)
  const insert = db.prepare('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)')
  for (let i = 0; i < 55; i++) insert.run(i, 'private_123', `m${i}`, '123', '验证用户', 1700000000 + i, `离线消息 ${i}`, '[]')
  db.close()
  const port = await freePort()
  fs.writeFileSync(path.join(dataRoot, '.env.dev'), `WEB_WS_PORT=${await freePort()}\nONEBOT_WS_PORT=${await freePort()}\n`)
  const base = `http://127.0.0.1:${port}`
  const managerEnv = { ...process.env, ABYSS_DATA_DIR: dataRoot }
  if (process.env.CONSOLE_TEST_DATA_DESCRIPTOR === '1') {
    // 覆盖双击 EXE 的路径：没有环境变量，也不依赖工作目录。
    const appRoot = path.join(output, `program-${Date.now()}`)
    fs.mkdirSync(appRoot, { recursive: true })
    fs.cpSync(path.join(path.dirname(managerExe), 'manager_internal'), path.join(appRoot, 'manager_internal'), { recursive: true })
    fs.copyFileSync(managerExe, path.join(appRoot, 'manager.exe'))
    fs.writeFileSync(path.join(appRoot, 'abyss-data-dir.txt'), path.relative(appRoot, dataRoot))
    managerExe = path.join(appRoot, 'manager.exe')
    delete managerEnv.ABYSS_DATA_DIR
  }
  const processHandle = spawn(managerExe, ['--port', String(port)], {
    cwd: output, env: managerEnv, windowsHide: true, stdio: 'ignore',
  })
  let browser
  try {
    let token
    for (let i = 0; i < 60; i++) {
      await delay(250)
      try {
        token = fs.readFileSync(path.join(dataRoot, 'manager/service_token.txt'), 'utf8').trim()
        const response = await fetch(`${base}/v1/info`, { headers: { 'X-Manager-Token': token } })
        if (response.ok) {
          assert.equal(path.resolve((await response.json()).instance), path.resolve(dataRoot))
          break
        }
      } catch {}
      if (i === 59) throw new Error('manager.exe 未就绪')
    }
    browser = await chromium.launch({ headless: true, channel: 'msedge' })
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const errors = []
    let wsCount = 0
    page.on('pageerror', error => errors.push(error.message))
    page.on('websocket', socket => { if (socket.url().includes('/web/ws')) wsCount++ })
    await page.goto(base)
    await page.locator('input[type=password]').fill(token)
    await page.getByRole('button', { name: '连接', exact: true }).click()
    await page.getByText('Core 未运行', { exact: true }).waitFor()
    assert.equal(await page.locator('.session-name').first().innerText(), '离线验证会话')
    const split = page.getByRole('separator', { name: '调整会话列表宽度' })
    await split.focus()
    await page.keyboard.press('ArrowRight')
    const savedWidth = await page.evaluate(() => localStorage.getItem('abyss:chat-sidebar'))
    assert.ok(savedWidth)
    await page.getByRole('button', { name: 'Truck', exact: true }).click()
    const truck = page.locator('.truck-item').filter({ hasText: '(task_truck)' })
    await truck.locator('.el-switch').click()
    await page.getByText('启动配置已保存', { exact: true }).waitFor()
    await page.locator('.el-checkbox').filter({ hasText: '启动 Core 时暂停自动任务（开发）' }).click()
    await page.waitForTimeout(350)
    const settings = JSON.parse(fs.readFileSync(path.join(dataRoot, 'manager/trucks.json'), 'utf8'))
    assert.equal(settings.enabled.task_truck, true)
    assert.equal(settings.startup_paused, true)
    assert.equal(await page.getByRole('button', { name: '应用到当前运行' }).isDisabled(), true)
    await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
    await page.getByRole('button', { name: '上下文', exact: true }).click()
    await page.locator('.ctx-chain-file').first().click()
    await page.locator('.ctx-entry-raw').first().waitFor()
    const list = page.locator('.ctx-list')
    await list.evaluate(el => { el.scrollTop = 900; el.dispatchEvent(new Event('scroll')) })
    await page.waitForTimeout(100)
    const readingTop = await list.evaluate(el => el.scrollTop)
    await page.waitForTimeout(3300)
    assert.ok(Math.abs(await list.evaluate(el => el.scrollTop) - readingTop) < 3, '离线自动刷新应保留阅读位置')
    await page.screenshot({ path: path.join(output, 'offline-context.png') })
    await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
    await page.getByRole('button', { name: '启动 Core', exact: true }).click()
    // 测试位置没有 core.exe；实际启动失败必须只影响 core 状态。
    await page.getByText(/spawn 失败/).waitFor()
    await page.getByRole('button', { name: 'Truck', exact: true }).click()
    await page.locator('.truck-item').first().waitFor()
    assert.equal(await truck.getByRole('switch').getAttribute('aria-checked'), 'true')
    assert.equal(await page.getByRole('checkbox', { name: '启动 Core 时暂停自动任务（开发）' }).isChecked(), true)
    await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click()
    await page.reload()
    await page.getByRole('button', { name: 'Truck', exact: true }).waitFor()
    assert.equal(await page.evaluate(() => localStorage.getItem('abyss:chat-sidebar')), savedWidth)
    assert.equal(wsCount, 0, '未启动 core 时不应尝试消息通道')
    assert.deepEqual(errors, [], '页面不能出现运行时错误')
    const targets = await (await fetch(`${base}/v1/targets`, { headers: { 'X-Manager-Token': token } })).json()
    assert.equal(targets.targets.find(t => t.name === 'core').pid, null)
    console.log('PASS actual EXE: offline page, context, saved Truck config, paused startup, layout persistence, failed core isolation; no core process or message socket.')
  } finally {
    if (browser) await browser.close()
    processHandle.kill()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
