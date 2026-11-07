<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, ChatDotRound, RefreshRight, Check } from '@element-plus/icons-vue'

const props = defineProps<{ visible: boolean; canRestart: boolean }>()
type Draft = { ONEBOT_ENABLED: boolean; ONEBOT_WS_PORT: number; ONEBOT_WS_PATH: string; BRIDGE_ENABLED: boolean; BRIDGE_BASE_URL: string }
interface Live { available: boolean; onebot_enabled: boolean; onebot_connected: boolean; bridge: { enabled: boolean; ready?: boolean; blocked?: boolean; last_error?: string; unclaimed_sends?: number; inbox?: Record<string, number>; outbox?: Record<string, number> }; apply?: { running: boolean; phase: string; error: string } }
const initial: Draft = { ONEBOT_ENABLED: true, ONEBOT_WS_PORT: 5001, ONEBOT_WS_PATH: '/onebot/v11/ws', BRIDGE_ENABLED: false, BRIDGE_BASE_URL: 'http://127.0.0.1:8770' }
const draft = ref<Draft>({ ...initial }), saved = ref<Draft>({ ...initial })
const token = ref(''), qqToken = ref(''), tokenSet = ref(false), qqTokenSet = ref(false), clearToken = ref(false)
const loading = ref(false), loaded = ref(false), saving = ref(false), checking = ref(false), applying = ref(false)
const error = ref(''), checkResult = ref(''), checkOK = ref(false), pending = ref(false), advanced = ref(false)
const live = ref<Live | null>(null)
let timer: number | null = null
let revision = 0
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(saved.value) || !!token.value || !!qqToken.value || clearToken.value)
const bridgeLabel = computed(() => !live.value?.available ? '接入服务未连接' : !live.value.bridge.enabled ? '未启用' : live.value.bridge.ready ? '已连接' : live.value.bridge.blocked ? '需要处理' : '等待连接')
const waiting = computed(() => Object.entries(live.value?.bridge.outbox || {}).filter(([key]) => ['queued', 'actioned', 'unknown', 'submitting'].includes(key)).reduce((total, [, count]) => total + count, 0))

async function jsonRequest(path: string, options?: RequestInit) {
  const response = await fetch(path, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`)
  return body
}
async function load() {
  const current = ++revision
  loading.value = true; error.value = ''
  try {
    const body = await jsonRequest('/system/config')
    if (current !== revision) return
    const fields = body.fields || {}
    draft.value = { ONEBOT_ENABLED: fields.ONEBOT_ENABLED !== false, ONEBOT_WS_PORT: Number(fields.ONEBOT_WS_PORT) || 5001, ONEBOT_WS_PATH: fields.ONEBOT_WS_PATH || initial.ONEBOT_WS_PATH, BRIDGE_ENABLED: fields.BRIDGE_ENABLED === true, BRIDGE_BASE_URL: fields.BRIDGE_BASE_URL || initial.BRIDGE_BASE_URL }
    saved.value = { ...draft.value }; token.value = ''; qqToken.value = ''; clearToken.value = false
    tokenSet.value = !!fields.BRIDGE_TOKEN_SET; qqTokenSet.value = !!fields.ONEBOT_ACCESS_TOKEN_SET
    loaded.value = true
  } catch (e: any) { error.value = e.message || '无法读取接入配置' }
  finally { if (current === revision) loading.value = false }
}
async function poll() {
  try {
    live.value = await jsonRequest('/system/connections')
    if (applying.value && !live.value?.apply?.running) {
      applying.value = false
      if (live.value?.apply?.error) error.value = live.value.apply.error
      else if (live.value?.apply?.phase === '已应用') { pending.value = false; ElMessage.success('平台接入配置已应用') }
    }
  } catch { live.value = null }
}
watch(() => props.visible, visible => {
  if (timer !== null) { window.clearInterval(timer); timer = null }
  if (visible) { if (!loaded.value) load(); poll(); timer = window.setInterval(poll, 4000) }
}, { immediate: true })
watch([token, () => draft.value.BRIDGE_BASE_URL], () => { checkResult.value = '' })
onBeforeUnmount(() => { revision++; if (timer !== null) window.clearInterval(timer) })

async function save() {
  saving.value = true; error.value = ''
  try {
    const fields: Record<string, unknown> = {}
    for (const key of Object.keys(draft.value) as (keyof Draft)[]) if (draft.value[key] !== saved.value[key]) fields[key] = draft.value[key]
    if (token.value) fields.BRIDGE_TOKEN = token.value
    if (clearToken.value) fields.BRIDGE_TOKEN = ''
    if (qqToken.value) fields.ONEBOT_ACCESS_TOKEN = qqToken.value
    await jsonRequest('/system/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fields }) })
    saved.value = { ...draft.value }
    if (token.value) tokenSet.value = true
    if (clearToken.value) tokenSet.value = false
    if (qqToken.value) qqTokenSet.value = true
    token.value = ''; qqToken.value = ''; clearToken.value = false; pending.value = true
    ElMessage.success('已保存，应用后生效')
  } catch (e: any) { error.value = e.message || '保存失败，修改已保留' }
  finally { saving.value = false }
}
async function checkConnection() {
  checking.value = true; checkResult.value = ''; checkOK.value = false
  try {
    const fields: Record<string, string> = { BRIDGE_BASE_URL: draft.value.BRIDGE_BASE_URL }
    if (token.value) fields.BRIDGE_TOKEN = token.value
    if (clearToken.value) fields.BRIDGE_TOKEN = ''
    const result = await jsonRequest('/system/connections/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields) })
    checkOK.value = true; checkResult.value = result.message
  } catch (e: any) { checkResult.value = e.message || '检查失败' }
  finally { checking.value = false }
}
async function apply() {
  try { await ElMessageBox.confirm('将依次停止 Core、重启消息接入，再启动 Core。期间平台消息接收会短暂中断。', '应用平台接入配置', { confirmButtonText: '应用并重启', cancelButtonText: '取消' }) }
  catch { return }
  applying.value = true; error.value = ''
  try { await jsonRequest('/system/connections/apply', { method: 'POST' }) }
  catch (e: any) { applying.value = false; error.value = e.message }
}
async function confirmClose() {
  if (!dirty.value) return true
  try { await ElMessageBox.confirm('平台接入还有未保存的修改，关闭后放弃这些修改？', '未保存的修改', { confirmButtonText: '放弃修改', cancelButtonText: '继续编辑' }); loaded.value = false; await load(); return true }
  catch { return false }
}
function discard() { draft.value = { ...saved.value }; token.value = ''; qqToken.value = ''; clearToken.value = false; checkResult.value = ''; error.value = '' }
defineExpose({ confirmClose })
</script>

<template>
  <section class="pc-panel" aria-label="平台接入设置" v-loading="loading">
    <header class="pc-heading"><div class="pc-eyebrow">CONNECTIONS</div><h2>平台接入</h2><p>选择你使用的平台。QQ 与通用桥接可以同时开启。</p></header>
    <div v-if="error" class="pc-notice pc-error" role="alert">{{ error }} <el-button v-if="!loaded" text @click="load">重试</el-button></div>
    <div v-if="loaded" class="pc-cards">
      <article class="pc-card">
        <div class="pc-card-head"><div class="pc-platform"><span class="pc-icon"><el-icon :size="21"><ChatDotRound /></el-icon></span><div><h3>SnowLuma <span class="pc-tag">QQ</span></h3><p>连接 QQ 私聊与群聊</p></div></div><el-switch v-model="draft.ONEBOT_ENABLED" aria-label="启用 SnowLuma 接入" /></div>
        <div class="pc-status"><span :class="['pc-dot', { connected: live?.onebot_connected }]" />{{ !live?.available ? '接入服务未连接' : live.onebot_connected ? '已连接' : live.onebot_enabled ? '等待 SnowLuma 连接' : '未启用' }}</div>
        <button class="pc-details" :aria-expanded="advanced" @click="advanced = !advanced">{{ advanced ? '收起连接参数' : '编辑连接参数' }}</button>
        <div v-if="advanced" class="pc-fields">
          <div class="pc-grid"><label>监听端口<el-input-number v-model="draft.ONEBOT_WS_PORT" :min="1" :max="65535" :controls="false" aria-label="SnowLuma 监听端口" /></label><label>连接路径<el-input v-model="draft.ONEBOT_WS_PATH" aria-label="SnowLuma 连接路径" /></label></div>
          <label>访问令牌<el-input v-model="qqToken" type="password" show-password autocomplete="new-password" :placeholder="qqTokenSet ? '已保存 · 留空保留现有令牌' : '填写与 SnowLuma 一致的令牌'" aria-label="SnowLuma 访问令牌" /></label>
          <p class="pc-help">连接参数需与 SnowLuma 中的 OneBot 配置保持一致。开启接入不会替你启动 SnowLuma。</p>
        </div>
      </article>
      <article class="pc-card">
        <div class="pc-card-head"><div class="pc-platform"><span class="pc-icon"><el-icon :size="21"><Connection /></el-icon></span><div><h3>通用桥接 <span class="pc-tag">微信 · 文字</span></h3><p>连接符合接入契约的外部桥接程序</p></div></div><el-switch v-model="draft.BRIDGE_ENABLED" aria-label="启用通用桥接" /></div>
        <div class="pc-status"><span :class="['pc-dot', { connected: live?.bridge.ready }]" />{{ bridgeLabel }}</div>
        <div class="pc-fields">
          <label>服务地址<el-input v-model="draft.BRIDGE_BASE_URL" placeholder="http://127.0.0.1:8770" aria-label="桥接服务地址" /></label>
          <label>访问令牌<el-input v-model="token" type="password" show-password autocomplete="new-password" :placeholder="clearToken ? '保存后清除' : tokenSet ? '已保存 · 留空保留现有令牌' : '填写桥接程序的访问令牌'" aria-label="桥接访问令牌" @input="clearToken = false" /></label>
          <div class="pc-check-row"><el-button :icon="Check" :loading="checking" :disabled="saving" @click="checkConnection">检查连接</el-button><el-button v-if="tokenSet" text :disabled="draft.BRIDGE_ENABLED" @click="clearToken = !clearToken; token = ''">{{ clearToken ? '撤销清除' : '清除令牌' }}</el-button><span class="pc-help">只检查状态，不发送消息</span></div>
          <p v-if="checkResult" :class="['pc-result', { success: checkOK }]" role="status">{{ checkResult }}</p>
          <p v-if="live?.bridge.last_error" class="pc-result" role="status">{{ live.bridge.last_error }}</p>
          <div v-if="waiting || live?.bridge.inbox?.quarantined" class="pc-notice"><span v-if="waiting">{{ waiting }} 条发送待确认。结果不明时不要重复发送。</span><span v-if="live?.bridge.inbox?.quarantined"> {{ live.bridge.inbox.quarantined }} 条不确定来源的消息已保留，未触发 AI。</span></div>
          <p class="pc-help">第一阶段支持私聊与群聊文字。微信桥自行管理 1–2 分钟的会话锁，期间其他会话的新正文可能延后到达。</p>
        </div>
      </article>
    </div>
    <footer v-if="loaded" class="pc-footer"><div class="pc-help">{{ applying ? '正在应用，服务恢复后将更新状态…' : pending ? '已保存，待重启接入服务和 Core 后生效' : dirty ? '有未保存的修改' : '配置由用户管理，修改不会立即影响运行中的连接' }}</div><div class="pc-footer-actions"><el-button v-if="dirty" text :disabled="saving" @click="discard">撤销修改</el-button><el-button v-if="pending" :icon="RefreshRight" :loading="applying" :disabled="dirty || !canRestart" @click="apply">应用并重启</el-button><el-button type="primary" :disabled="!dirty || applying" :loading="saving" @click="save">保存修改</el-button></div></footer>
  </section>
</template>

<style scoped>
.pc-panel { height: 100%; overflow-y: auto; padding: 28px; box-sizing: border-box; color: var(--text-primary); }
.pc-heading { margin-bottom: 24px; }.pc-eyebrow { font-size: var(--text-xs); letter-spacing: .14em; color: var(--text-tertiary); }.pc-heading h2 { margin: 8px 0; font-size: var(--text-2xl); }.pc-heading p,.pc-platform p { margin: 6px 0 0; color: var(--text-tertiary); font-size: var(--text-md); line-height: 1.6; }
.pc-cards { display: grid; gap: 16px; }.pc-card { border: 1px solid var(--border-default); border-radius: 16px; padding: 20px; background: var(--bg-surface); }.pc-card-head,.pc-platform,.pc-check-row,.pc-footer,.pc-footer-actions { display: flex; align-items: center; gap: 12px; }.pc-card-head { justify-content: space-between; }.pc-platform { min-width: 0; }.pc-platform h3 { margin: 0; font-size: var(--text-lg); }.pc-icon { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 12px; background: var(--bg-elevated); flex-shrink: 0; }.pc-tag { display: inline-block; margin-left: 6px; padding: 2px 6px; border: 1px solid var(--border-default); color: var(--text-tertiary); font-size: var(--text-xs); font-weight: 400; border-radius: 5px; }
.pc-status { display: flex; align-items: center; gap: 7px; margin: 17px 0 0; font-size: var(--text-sm); color: var(--text-secondary); }.pc-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-tertiary); }.pc-dot.connected { background: var(--tag-success); }.pc-details { padding: 0; margin-top: 16px; cursor: pointer; background: transparent; color: var(--text-secondary); border: 0; font: inherit; font-size: var(--text-sm); }.pc-details:hover { color: var(--text-primary); }.pc-details:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
.pc-fields { display: grid; gap: 15px; margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--border-subtle); }.pc-fields label { display: grid; gap: 8px; font-size: var(--text-md); }.pc-grid { display: grid; grid-template-columns: 120px 1fr; gap: 14px; }.pc-grid :deep(.el-input-number) { width: 100%; }.pc-help { font-size: var(--text-sm); line-height: 1.7; color: var(--text-tertiary); margin: 0; }.pc-check-row { flex-wrap: wrap; }.pc-result { font-size: var(--text-sm); line-height: 1.7; margin: 0; color: var(--text-secondary); overflow-wrap: anywhere; }.pc-result.success { color: var(--tag-success); }.pc-notice { padding: 12px; border: 1px solid var(--border-default); border-radius: 10px; background: var(--bg-elevated); font-size: var(--text-sm); line-height: 1.7; }.pc-error { margin-bottom: 16px; }
.pc-footer { position: sticky; bottom: -28px; background: var(--bg-surface); border-top: 1px solid var(--border-default); margin: 24px -28px -28px; padding: 16px 28px; justify-content: space-between; flex-wrap: wrap; }.pc-footer-actions { margin-left: auto; gap: 8px; }.pc-footer-actions :deep(.el-button + .el-button) { margin-left: 0; }.pc-footer-actions :deep(.el-button--primary.is-disabled) { background: var(--bg-elevated) !important; color: var(--text-disabled) !important; border-color: var(--border-default) !important; }.pc-panel :deep(.el-switch) { --el-switch-on-color: var(--accent); }
@media (max-width: 600px) { .pc-panel { padding: 18px; }.pc-card { padding: 16px; }.pc-icon { display: none; }.pc-tag { margin-left: 2px; }.pc-platform h3 { font-size: var(--text-base); }.pc-grid { grid-template-columns: 1fr; }.pc-footer { margin: 20px -18px -18px; padding: 14px 18px; bottom: -18px; }.pc-check-row { gap: 8px; } }
</style>
