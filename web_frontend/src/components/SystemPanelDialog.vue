<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Monitor, VideoPlay, VideoPause, RefreshRight, Link, SwitchButton } from '@element-plus/icons-vue'

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
const source = ref<'core' | 'core_out' | 'snowluma'>('core')
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
      openLogStream()
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
  ws.onclose = () => { ws = null }
}

function closeLogStream() {
  if (ws) {
    try { ws.close() } catch { /* ignore */ }
    ws = null
  }
}

watch(source, () => openLogStream())
watch(follow, (v) => { if (v) scrollBottom() })

const logLines = computed(() => raw.value.split('\n').slice(-400))

function scrollBottom() {
  if (!follow.value) return
  nextTick(() => {
    if (logView.value) logView.value.scrollTop = logView.value.scrollHeight
  })
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
    await ElMessageBox.confirm('重启 Core？页面会断开几秒，respawn 工人将在端口释放后拉起新实例。', '确认', { type: 'warning' })
  } catch { return }
  acting.value = true
  try {
    await post('/system/core/restart')
    ElMessage.success('Core 重启中，页面恢复后状态会自动刷新')
  } catch (e: any) { ElMessage.error(`重启失败: ${e?.message || e}`) } finally { acting.value = false }
}

async function shutdownCore() {
  try {
    await ElMessageBox.confirm('优雅关闭 Core？SystemCore 将走完整收尾流程，之后需要 manager.py 或托盘重新启动。', '确认', { type: 'warning' })
  } catch { return }
  acting.value = true
  try {
    await post('/system/core/shutdown')
    ElMessage.warning('Core 关闭中……重新启动请用 manager.py 或托盘')
  } catch (e: any) { ElMessage.error(`关闭失败: ${e?.message || e}`) } finally { acting.value = false }
}

function openSnowlumaWebui() {
  if (info.value?.snowluma.webui_url) window.open(info.value.snowluma.webui_url, '_blank')
}

function close() { emit('update:visible', false) }

function fmtUptime(sec: number | null): string {
  if (sec === null) return '—'
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60)
  const h = Math.floor(m / 60)
  return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    width="860px"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
    class="sp-dialog"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <template #header>
      <div class="sp-header">
        <div class="sp-title">
          <el-icon :size="18" color="var(--accent)"><Monitor /></el-icon>
          <span>系统面板</span>
          <span class="sp-sub">进程管理 · 实时日志 · 优雅重启</span>
        </div>
        <el-button size="small" @click="close">关闭</el-button>
      </div>
    </template>

    <div class="sp-body">
      <!-- 进程卡片 -->
      <section class="sp-processes">
        <div class="sp-card">
          <div class="sp-card-head">
            <span class="sp-dot" :class="info?.core ? 'ok' : 'bad'" />
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
  </el-dialog>
</template>

<style scoped>
.sp-dialog :deep(.el-dialog__body) { padding: 0; }
.sp-dialog :deep(.el-dialog__header) { padding: 0; margin: 0; }

.sp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-surface);
}
.sp-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-1);
}
.sp-sub {
  font-weight: 400;
  font-size: 11.5px;
  color: var(--text-3);
  margin-left: 8px;
}

.sp-body {
  padding: 16px 20px 20px 20px;
  background: var(--bg-app);
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 78vh;
}

.sp-processes {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.sp-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sp-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.sp-name {
  font-weight: 700;
  font-size: 14px;
  color: var(--text-1);
}
.sp-badge {
  font-size: 10.5px;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--border-1);
  color: var(--text-3);
}
.sp-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  flex-shrink: 0;
}
.sp-dot.ok { background: var(--accent-2); box-shadow: 0 0 6px var(--accent-2); }
.sp-dot.bad { background: var(--text-3); }

.sp-meta {
  font-size: 11.5px;
  color: var(--text-3);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.sp-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.sp-hint {
  font-size: 11px;
  color: var(--text-3);
  margin-left: auto;
}

.sp-log-section {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
  min-height: 0;
}
.sp-log-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}
.sp-log-view {
  height: 320px;
  overflow-y: auto;
  background: var(--bg-subtle);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.55;
}
.sp-log-line {
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-2);
}
.sp-log-empty {
  color: var(--text-3);
  text-align: center;
  padding: 40px 0;
}
</style>
