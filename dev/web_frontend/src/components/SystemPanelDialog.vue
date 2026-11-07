<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ModelConfigPanel from './ModelConfigPanel.vue'
import PlatformConfigPanel from './PlatformConfigPanel.vue'
import { Monitor, VideoPlay, VideoPause, RefreshRight, Link, SwitchButton, Setting, Connection } from '@element-plus/icons-vue'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

interface CoreInfo {
  running: boolean
  pid: number
  uptime_sec: number | null
  web_port: number
  onebot_port: number
  mode: string
  graceful_shutdown: boolean
}
interface SnowlumaInfo {
  running: boolean
  pid: number | null
  ports: number[]
  webui_url: string
}
interface SystemStatus {
  core: CoreInfo
  snowluma: SnowlumaInfo
  hint: string
}

const info = ref<SystemStatus | null>(null)
const loading = ref(false)
const acting = ref(false)
let pollTimer: number | null = null

// —— 日志流 ——
const source = ref<'core' | 'core_out' | 'snowluma' | 'ingress'>('core')
const follow = ref(true)
const raw = ref('')
const logView = ref<HTMLDivElement | null>(null)
let ws: WebSocket | null = null

watch(
  () => props.visible,
  (v) => {
    if (v) {
      fetchStatus()
      pollTimer = window.setInterval(fetchStatus, 5000)
      if (activeTab.value === 'proc') openLogStream()
    } else {
      cleanup()
    }
  },
)

onBeforeUnmount(cleanup)

function cleanup() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
  closeLogStream()
}

async function fetchStatus() {
  loading.value = true
  try {
    const res = await fetch('/system/status')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    info.value = (await res.json()) as SystemStatus
  } catch (e: any) {
    // 面板打开期间 Core 挂了/重启中：不打扰，状态点会自己变红
    info.value = null
    if (props.visible && !acting.value) console.warn('system status fetch failed', e)
  } finally {
    loading.value = false
  }
}

function openLogStream() {
  closeLogStream()
  raw.value = ''
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/system/ws/logs?name=${source.value}&lines=200`)
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'snapshot') raw.value = (msg.lines || []).join('\n')
      else if (msg.type === 'append') raw.value = (raw.value + msg.text).slice(-262144)
      scrollBottom()
    } catch { /* 忽略坏帧 */ }
  }
  const stream = ws
  ws.onclose = () => { if (ws === stream) ws = null }
}

function closeLogStream() {
  if (ws) {
    try { ws.close() } catch { /* ignore */ }
    ws = null
  }
}

watch(source, () => { if (props.visible && activeTab.value === 'proc') openLogStream() })
watch(follow, (v) => { if (v) scrollBottom() })

const logLines = computed(() => raw.value.split('\n').slice(-400))

function scrollBottom() {
  if (!follow.value) return
  nextTick(() => {
    if (logView.value) logView.value.scrollTop = logView.value.scrollHeight
  })
}

const activeTab = ref<'proc' | 'config' | 'platform'>('config')
const modelPanel = ref<InstanceType<typeof ModelConfigPanel> | null>(null)
const platformPanel = ref<InstanceType<typeof PlatformConfigPanel> | null>(null)
watch(activeTab, tab => {
  if (!props.visible) return
  if (tab === 'proc') openLogStream()
  else closeLogStream()
})
async function close() {
  if (modelPanel.value && !await modelPanel.value.confirmClose()) return
  if (platformPanel.value && !await platformPanel.value.confirmClose()) return
  emit('update:visible', false)
}

// —— 动作 ——
async function post(path: string): Promise<any> {
  const res = await fetch(path, { method: 'POST' })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

async function startSnowluma() {
  acting.value = true
  try {
    const r = await post('/system/snowluma/start')
    ElMessage.success(r.started ? `SnowLuma 已拉起 (pid=${r.pid})` : r.reason || '已在运行')
    fetchStatus()
  } catch (e: any) { ElMessage.error(`启动失败: ${e?.message || e}`) } finally { acting.value = false }
}

async function stopSnowluma() {
  try {
    await ElMessageBox.confirm('停止 SnowLuma？当前 QQ 登录会话将中断。', '确认', { type: 'warning' })
  } catch { return }
  acting.value = true
  try {
    const r = await post('/system/snowluma/stop')
    ElMessage.success(r.stopped ? 'SnowLuma 已停止' : r.reason || '未在运行')
    fetchStatus()
  } catch (e: any) { ElMessage.error(`停止失败: ${e?.message || e}`) } finally { acting.value = false }
}

async function restartCore() {
  try {
    await ElMessageBox.confirm('重启 Core？实时消息会暂时断开，管理页面和历史记录仍可使用。', '确认', { type: 'warning' })
  } catch { return }
  acting.value = true
  try {
    await post('/system/core/restart')
    modelPanel.value?.markApplied()
    ElMessage.success('Core 重启中，页面恢复后状态会自动刷新')
  } catch (e: any) { ElMessage.error(`重启失败: ${e?.message || e}`) } finally { acting.value = false }
}

async function shutdownCore() {
  try {
    await ElMessageBox.confirm('停止 Core？管理页面仍可使用，也可以从页面再次启动。', '确认', { type: 'warning' })
  } catch { return }
  acting.value = true
  try {
    await post('/system/core/shutdown')
    ElMessage.success('Core 已停止，管理页面仍可使用')
  } catch (e: any) { ElMessage.error(`关闭失败: ${e?.message || e}`) } finally { acting.value = false }
}

function openSnowlumaWebui() {
  if (info.value?.snowluma.webui_url) window.open(info.value.snowluma.webui_url, '_blank')
}



function fmtUptime(sec: number | null): string {
  if (sec === null) return '—'
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60)
  const h = Math.floor(m / 60)
  return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`
}
</script>

<template>
  <AppDialog size="panel" :model-value="props.visible" :close-on-click-modal="false"
             align-center class="sp-dialog" :before-close="close"
    title="系统设置"
  >
    <template #header><div class="sp-header"><div class="sp-title"><el-icon :size="20"><Setting /></el-icon><span>系统设置</span></div></div></template>
    <div class="sp-layout">
      <nav class="sp-nav" aria-label="系统设置导航">
        <div class="sp-nav-caption">工作空间</div>
        <button :class="['sp-nav-item', { active: activeTab === 'config' }]" :aria-current="activeTab === 'config' ? 'page' : undefined" @click="activeTab = 'config'"><el-icon><Connection /></el-icon><span>模型接入</span></button>
        <button :class="['sp-nav-item', { active: activeTab === 'platform' }]" :aria-current="activeTab === 'platform' ? 'page' : undefined" @click="activeTab = 'platform'"><el-icon><Link /></el-icon><span>平台接入</span></button>
        <button :class="['sp-nav-item', { active: activeTab === 'proc' }]" :aria-current="activeTab === 'proc' ? 'page' : undefined" @click="activeTab = 'proc'"><el-icon><Monitor /></el-icon><span>进程与日志</span></button>
        <div class="sp-nav-bottom"><span class="sp-dot" :class="info?.core.running ? 'ok' : 'bad'" />Core {{ info?.core.running ? '运行中' : '未连接' }}</div>
      </nav>
      <main class="sp-main">
        <ModelConfigPanel v-show="activeTab === 'config'" ref="modelPanel" :visible="props.visible" :can-restart="!!info?.core.graceful_shutdown" :restarting="acting" @restart="restartCore" />
        <PlatformConfigPanel v-show="activeTab === 'platform'" ref="platformPanel" :visible="props.visible && activeTab === 'platform'" :can-restart="!!info?.core.graceful_shutdown" />
        <div v-show="activeTab === 'proc'" class="sp-scroll">
          <header class="sp-page-heading"><div><div class="sp-eyebrow">SYSTEM ACTIVITY</div><h2>进程与日志</h2><p>管理运行中的服务，查看实时输出。</p></div><el-button text :icon="RefreshRight" :loading="loading" @click="fetchStatus">刷新状态</el-button></header>
            <section class="sp-processes">
              <div class="sp-card">
                <div class="sp-card-head">
                  <span class="sp-dot" :class="info?.core.running ? 'ok' : 'bad'" />
                  <span class="sp-name">Abyss Core</span>
                  <span class="sp-badge">{{ info ? (info.core.mode === 'prod' ? '生产' : '开发') : '—' }}</span>
                </div>
                <div class="sp-meta">
                  pid {{ info?.core.pid ?? '—' }} · 端口 {{ info?.core.web_port ?? '—' }}(Web) /
                  {{ info?.core.onebot_port ?? '—' }}(OneBot) · 运行 {{ fmtUptime(info?.core.uptime_sec ?? null) }}
                </div>
                <div class="sp-actions">
                  <el-button size="small" :icon="RefreshRight" :disabled="acting || !info?.core.graceful_shutdown"
                             @click="restartCore">重启</el-button>
                  <el-button size="small" type="danger" plain :icon="SwitchButton"
                             :disabled="acting || !info?.core.graceful_shutdown" @click="shutdownCore">优雅关闭</el-button>
                  <span v-if="info && !info.core.graceful_shutdown" class="sp-hint">dev 模式请在终端 Ctrl+C</span>
                </div>
              </div>

              <div class="sp-card">
                <div class="sp-card-head">
                  <span class="sp-dot" :class="info?.snowluma.running ? 'ok' : 'bad'" />
                  <span class="sp-name">SnowLuma</span>
                </div>
                <div class="sp-meta">
                  {{ info?.snowluma.running ? `pid ${info.snowluma.pid}` : '未运行' }} ·
                  端口 {{ info?.snowluma.ports.join(' / ') ?? '—' }}
                </div>
                <div class="sp-actions">
                  <el-button size="small" type="primary" plain :icon="VideoPlay" :disabled="acting || !!info?.snowluma.running"
                             @click="startSnowluma">启动</el-button>
                  <el-button size="small" type="danger" plain :icon="VideoPause" :disabled="acting || !info?.snowluma.running"
                             @click="stopSnowluma">停止</el-button>
                  <el-button size="small" :icon="Link" :disabled="!info" @click="openSnowlumaWebui">打开 WebUI</el-button>
                </div>
              </div>
            </section>

            <!-- 日志 -->
            <section class="sp-log-section">
              <div class="sp-log-toolbar">
                <el-select v-model="source" size="small" style="width: 190px">
                  <el-option label="Core 主日志 (app_*.log)" value="core" />
                  <el-option label="Core 启动输出 (stdout)" value="core_out" />
                  <el-option label="消息接入输出" value="ingress" />
                  <el-option label="SnowLuma 输出" value="snowluma" />
                </el-select>
                <el-switch v-model="follow" active-text="跟随" size="small" />
                <span class="sp-hint">{{ logLines.length }} 行 · 每 0.5s 推送</span>
              </div>
              <div ref="logView" class="sp-log-view">
                <div v-for="(line, i) in logLines" :key="i" class="sp-log-line">{{ line }}</div>
                <div v-if="!logLines.length" class="sp-log-empty">（暂无输出）</div>
              </div>
            </section>

        </div>
      </main>
    </div>
  </AppDialog>
</template>

<style scoped>
.sp-header { display: flex; align-items: center; justify-content: space-between;  }
.sp-title { display: flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.sp-title .el-icon { color: var(--text-tertiary); }
.sp-layout { display: grid; grid-template-columns: 190px minmax(0, 1fr); flex: 1; min-height: 0; }
.sp-nav { padding: 26px 12px 18px; background: var(--bg-surface); border-right: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 6px; }
.sp-nav-caption { padding: 0 12px 12px; font-size: 11px; color: var(--text-tertiary); letter-spacing: .08em; }
.sp-nav-item { display: flex; align-items: center; gap: 10px; padding: 13px 12px; border: 0; border-radius: 9px; background: transparent; color: var(--text-secondary); font: inherit; text-align: left; cursor: pointer; }
.sp-nav-item:hover { background: var(--bg-hover); }
.sp-nav-item.active { color: var(--text-primary); background: var(--bg-active); font-weight: 600; }
.sp-nav-item .el-icon { font-size: 17px; }
.sp-nav-bottom { margin-top: auto; display: flex; align-items: center; gap: 8px; padding: 14px 12px 0; color: var(--text-tertiary); font-size: 12px; }
.sp-main { min-width: 0; min-height: 0; overflow: hidden; }
.sp-scroll { height: 100%; box-sizing: border-box; overflow-y: auto; padding: 30px 34px; }
.sp-page-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 30px; }
.sp-eyebrow { font-size: 10px; font-weight: 600; letter-spacing: .14em; color: var(--text-tertiary); margin-bottom: 10px; }
.sp-page-heading h2 { margin: 0; font-size: 25px; font-weight: 600; color: var(--text-primary); }
.sp-page-heading p { margin: 10px 0 0; color: var(--text-tertiary); font-size: 13px; line-height: 1.7; }
.sp-processes { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 24px; }
.sp-card { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 12px; padding: 18px; display: flex; flex-direction: column; gap: 14px; }
.sp-card-head { display: flex; align-items: center; gap: 8px; color: var(--text-primary); }
.sp-name { font-size: 14px; font-weight: 600; }
.sp-badge { font-size: 10px; padding: 2px 6px; border-radius: 5px; background: var(--bg-elevated); color: var(--text-tertiary); }
.sp-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.sp-dot.ok { background: var(--tag-success); }
.sp-dot.bad { background: var(--text-tertiary); }
.sp-meta { font-size: 11px; line-height: 1.8; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.sp-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.sp-actions .el-button + .el-button { margin-left: 0; }
.sp-hint { color: var(--text-tertiary); font-size: 10px; margin-left: auto; }
.sp-log-section { border: 1px solid var(--border-default); border-radius: 12px; overflow: hidden; }
.sp-log-toolbar { display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--border-default); }
.sp-log-view { height: 280px; overflow-y: auto; background: var(--bg-sunken); padding: 14px; font-family: var(--font-mono); font-size: 11px; line-height: 1.7; }
.sp-log-line { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text-secondary); }
.sp-log-empty { padding: 40px 0; color: var(--text-tertiary); text-align: center; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 760px) {
  .sp-layout { grid-template-columns: 145px minmax(0, 1fr); }
  .sp-scroll { padding: 24px 18px; }
  .sp-processes { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .sp-layout { display: flex; flex-direction: column; min-height: 0; }
  .sp-nav { flex-direction: row; padding: 6px 10px; border-right: 0; border-bottom: 1px solid var(--border-subtle); }
  .sp-nav-item { flex: 1; min-width: 0; justify-content: center; padding: 10px 6px; gap: 6px; font-size: 12px; white-space: nowrap; }
  .sp-nav-caption, .sp-nav-bottom { display: none; }
  .sp-main { flex: 1; }
  .sp-scroll { padding: 20px 14px; }
  .sp-page-heading h2 { font-size: 22px; }
  .sp-log-toolbar { flex-wrap: wrap; }
}
</style>
