<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowRight, Check, Coin, Connection, Cpu, RefreshRight } from '@element-plus/icons-vue'

const props = defineProps<{ visible: boolean; canRestart: boolean; restarting: boolean }>()
const emit = defineEmits<{ restart: [] }>()
interface Field { key: string; label: string; secret?: boolean }
interface Group { id: string; description: string; optional?: boolean; fields: Field[] }
interface Entry { base_url: string; model: string; key_set: boolean; key_source?: string }
function fields(prefix: string, model: string): Field[] {
  return [{ key: `${prefix}_BASE_URL`, label: 'API 地址' }, { key: model, label: '模型名称' }, { key: `${prefix}_API_KEY`, label: 'API Key', secret: true }]
}
const groups: Group[] = [
  { id: 'common', description: '通用模型 · 所有未单独配置的任务都使用它', fields: fields('FLASH', 'LLM_FLASH') },
  { id: 'embed', description: '向量嵌入 · 用于记忆检索与内容索引', fields: fields('VECTOR', 'LLM_EMBEDDING') },
  { id: 'conversation', description: '会话与社交聊天', optional: true, fields: fields('CONVERSATION', 'LLM_CONVERSATION') },
  { id: 'persona', description: '内心人格', optional: true, fields: fields('PERSONA', 'LLM_PERSONA') },
  { id: 'vision', description: '图片理解', optional: true, fields: fields('VISION', 'LLM_VISION') },
  { id: 'pro', description: '高阶任务 · 地址与 Key 始终使用 common', optional: true, fields: [{ key: 'LLM_PRO', label: '模型名称' }] },
]
const allFields = groups.flatMap(g => g.fields)
const secrets = new Set(allFields.filter(f => f.secret).map(f => f.key))
const loading = ref(false), saving = ref(false), loaded = ref(false), error = ref('')
const values = ref<Record<string, string>>({}), saved = ref<Record<string, string>>({})
const keySet = ref<Record<string, boolean>>({}), effective = ref<Record<string, Entry>>({})
const dirty = ref(new Set<string>()), cleared = ref(new Set<string>())
const expanded = ref<string | null>(null), showOverrides = ref(false), pendingRestart = ref(false)
const defaults = ref({ common: { base_url: 'https://ark.cn-beijing.volces.com/api/plan/v3', model: 'DeepSeek-V4.1-Flash' }, embed: { model: 'doubao-embedding-vision' } })
let generation = 0
const changes = computed(() => new Set([...dirty.value, ...cleared.value]).size)
const overrideCount = computed(() => groups.filter(g => g.optional && custom(g)).length)
const sections = [{ optional: false, groups: groups.filter(g => !g.optional) }, { optional: true, groups: groups.filter(g => g.optional) }]

function hasKey(key: string): boolean { return !cleared.value.has(key) && (!!values.value[key]?.trim() || !!keySet.value[key]) }
function custom(group: Group): boolean { return group.fields.some(f => f.secret ? hasKey(f.key) : !!values.value[f.key]?.trim()) }
function changed(group: Group): boolean { return group.fields.some(f => dirty.value.has(f.key) || cleared.value.has(f.key)) }
function resolved(group: Group): Entry {
  const common = {
    base_url: values.value.FLASH_BASE_URL?.trim() || defaults.value.common.base_url,
    model: values.value.LLM_FLASH?.trim() || defaults.value.common.model,
    key_set: hasKey('FLASH_API_KEY') || effective.value.common?.key_source === 'fallback',
  }
  if (group.id === 'common') return common
  const url = group.fields.find(f => f.key.endsWith('_BASE_URL'))
  const key = group.fields.find(f => f.secret)
  const model = group.fields.find(f => f.key.startsWith('LLM_'))!
  return { base_url: (url && values.value[url.key]?.trim()) || common.base_url,
    model: values.value[model.key]?.trim() || (group.id === 'embed' ? defaults.value.embed.model : common.model),
    key_set: !!(key && hasKey(key.key)) || common.key_set }
}
function placeholder(group: Group, field: Field): string {
  if (field.secret) {
    if (cleared.value.has(field.key)) return group.optional ? '保存后使用 common 的 Key' : '保存后清空'
    if (keySet.value[field.key]) return '已保存 · 留空保留现有 Key'
    return group.optional ? '留空使用 common 的 Key' : '输入 API Key'
  }
  if (group.optional) return `使用 common · ${field.key.startsWith('LLM_') ? resolved(groups[0]).model : resolved(groups[0]).base_url}`
  return field.key.startsWith('LLM_') ? resolved(group).model : 'https://api.example.com/v1'
}
function markDirty(key: string) {
  cleared.value.delete(key)
  const value = values.value[key]?.trim() || ''
  if (secrets.has(key) ? !value : value === (saved.value[key] || '').trim()) dirty.value.delete(key)
  else dirty.value.add(key)
}
function clearKey(field: Field) { values.value[field.key] = ''; dirty.value.delete(field.key); cleared.value.add(field.key) }
function inherit(group: Group) {
  for (const field of group.fields) {
    values.value[field.key] = ''
    if (field.secret) { dirty.value.delete(field.key); if (keySet.value[field.key]) cleared.value.add(field.key) }
    else markDirty(field.key)
  }
}
function cancel(group: Group) {
  for (const field of group.fields) { values.value[field.key] = saved.value[field.key] || ''; dirty.value.delete(field.key); cleared.value.delete(field.key) }
  expanded.value = null
}
async function load() {
  const request = ++generation
  loading.value = true; error.value = ''
  try {
    const response = await fetch('/system/config'), body = await response.json()
    if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`)
    if (request !== generation) return
    if (!body.fields) throw new Error('配置数据不完整')
    const next: Record<string, string> = {}, set: Record<string, boolean> = {}
    for (const field of allFields) { next[field.key] = field.secret ? '' : String(body.fields[field.key] ?? ''); set[field.key] = !!body.fields[`${field.key}_SET`] }
    values.value = next; saved.value = { ...next }; keySet.value = set
    effective.value = body.effective || {}
    if (body.defaults) defaults.value = body.defaults
    dirty.value = new Set(); cleared.value = new Set(); loaded.value = true
  } catch (e: any) { if (request === generation) { loaded.value = false; error.value = `读取失败：${e.message || e}` } }
  finally { if (request === generation) loading.value = false }
}
async function reload() {
  if (changes.value) {
    try { await ElMessageBox.confirm('重新读取会丢弃未保存的修改。', '重新读取配置', { type: 'warning' }) } catch { return }
  }
  expanded.value = null
  await load()
}
async function save() {
  if (!loaded.value || !changes.value || saving.value) return
  const fields: Record<string, string> = {}
  for (const key of cleared.value) fields[key] = ''
  for (const key of dirty.value) { const value = values.value[key]?.trim() || ''; if (!secrets.has(key) || value) fields[key] = value }
  saving.value = true
  try {
    const response = await fetch('/system/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fields }) })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`)
    for (const key of Object.keys(fields)) { keySet.value[key] = fields[key] !== ''; if (secrets.has(key)) values.value[key] = '' }
    saved.value = { ...values.value }; dirty.value = new Set(); cleared.value = new Set()
    if (body.effective) effective.value = body.effective
    expanded.value = null; pendingRestart.value = true
    ElMessage.success('配置已保存，重启 Core 后生效')
  } catch (e: any) { ElMessage.error(`保存失败：${e.message || e}`) }
  finally { saving.value = false }
}
async function confirmClose(): Promise<boolean> {
  if (saving.value) return false
  if (changes.value) {
    try { await ElMessageBox.confirm('有未保存的配置，关闭后将丢弃这些修改。', '关闭系统设置', { confirmButtonText: '丢弃并关闭', cancelButtonText: '继续编辑', type: 'warning' }) } catch { return false }
  }
  generation++
  return true
}
watch(() => props.visible, visible => {
  if (visible) { expanded.value = null; load() }
  else { generation++; for (const key of secrets) values.value[key] = ''; dirty.value = new Set(); cleared.value = new Set() }
}, { immediate: true })
function markApplied() { pendingRestart.value = false }
defineExpose({ confirmClose, markApplied })
</script>

<template>
  <div class="mc-panel">
    <div class="mc-scroll">
      <header class="mc-heading"><div><div class="mc-eyebrow">MODEL CONNECTIONS</div><h2>模型接入</h2><p>配置 common 和 embed，即可开始使用。</p></div><span class="mc-count">2 项基础配置</span></header>
      <div v-if="error" class="mc-empty" role="alert"><p>{{ error }}</p><el-button @click="load">重新读取</el-button></div>
      <div v-else-if="!loaded || loading" class="mc-empty" role="status">正在读取配置…</div>
      <template v-else>
        <div class="mc-section-label">基础接入 <span>配置一次，供整个工作空间使用</span></div>
        <template v-for="section in sections" :key="String(section.optional)">
          <button v-if="section.optional" class="mc-overrides-toggle" :aria-expanded="showOverrides" @click="showOverrides = !showOverrides"><span><strong>按任务单独配置</strong><small>可选 · 未填写的字段自动使用 common</small></span><span class="mc-overrides-end">{{ overrideCount ? `${overrideCount} 项自定义` : '全部使用 common' }}<el-icon><ArrowDown v-if="showOverrides" /><ArrowRight v-else /></el-icon></span></button>
          <div v-if="!section.optional || showOverrides" class="mc-cards">
            <article v-for="group in section.groups" :key="group.id" :class="['mc-card', { editing: expanded === group.id }]" :data-connection="group.id">
              <header class="mc-card-head">
                <div :class="['mc-icon', { embedding: group.id === 'embed' }]"><el-icon :size="20"><Coin v-if="group.id === 'embed'" /><Cpu v-else /></el-icon></div>
                <div class="mc-card-info"><div class="mc-name"><h3>{{ group.id }}</h3><span v-if="changed(group)" class="mc-state changed">未保存</span><span v-else-if="group.optional && !custom(group)" class="mc-state inherited">使用 common</span><span v-else class="mc-state" :class="resolved(group).key_set ? 'configured' : 'missing'">{{ resolved(group).key_set ? '已配置' : '待配置' }}</span></div><p>{{ group.description }}</p></div>
                <el-button :aria-expanded="expanded === group.id" :aria-label="`编辑 ${group.id}`" @click="expanded = expanded === group.id ? null : group.id">{{ expanded === group.id ? '收起' : '编辑' }}<el-icon class="mc-edit-arrow"><ArrowDown v-if="expanded === group.id" /><ArrowRight v-else /></el-icon></el-button>
              </header>
              <div v-if="expanded !== group.id" class="mc-summary"><span>{{ resolved(group).model }}</span><span class="mc-endpoint" :title="resolved(group).base_url">{{ resolved(group).base_url }}</span></div>
              <div v-else class="mc-editor">
                <div class="mc-editor-intro"><span>{{ group.optional ? '自定义覆盖' : 'OpenAI 兼容接入' }}</span><button v-if="group.optional && custom(group)" class="mc-text-button" :disabled="saving" @click="inherit(group)">全部使用 common</button></div>
                <p v-if="group.id === 'common'" class="mc-note">供主力、会话、人格、视觉及高阶任务使用。需要图片理解时，请选择支持图片输入的模型。</p>
                <p v-if="group.id === 'embed'" class="mc-note">请填写支持向量嵌入的模型和 API 地址；模型输出需兼容现有的 2048 维索引。</p>
                <div class="mc-fields">
                  <div v-for="field in group.fields" :key="field.key" :class="['mc-field', { wide: field.secret || group.id === 'pro' }]">
                    <div class="mc-label"><label :for="`mc-${field.key}`">{{ field.label }}</label><span v-if="field.secret && keySet[field.key] && !cleared.has(field.key)" class="mc-key-saved"><el-icon><Check /></el-icon>已保存</span></div>
                    <el-input :id="`mc-${field.key}`" v-model="values[field.key]" :type="field.secret ? 'password' : 'text'" :show-password="field.secret" :placeholder="placeholder(group, field)" :autocomplete="field.secret ? 'new-password' : 'off'" :spellcheck="false" :disabled="saving" @input="markDirty(field.key)" />
                    <div v-if="field.secret" class="mc-secret-help"><span>{{ cleared.has(field.key) ? '清空操作将在保存后生效。' : '已保存的密钥不会回显，留空保留现有密钥。' }}</span><button v-if="cleared.has(field.key)" class="mc-text-button" :disabled="saving" @click="cleared.delete(field.key)">撤销清空</button><button v-else-if="keySet[field.key]" class="mc-text-button" :disabled="saving" @click="clearKey(field)">清空密钥</button></div>
                  </div>
                </div>
                <footer class="mc-editor-footer"><span>{{ group.optional ? '只覆盖已填写字段，其余继承 common。' : '修改完成后，在下方保存更改。' }}</span><el-button :disabled="saving" @click="cancel(group)">取消修改</el-button><el-button type="primary" plain :disabled="saving" @click="expanded = null">完成编辑</el-button></footer>
              </div>
            </article>
          </div>
        </template>
        <div class="mc-inheritance-note"><el-icon><Connection /></el-icon><span>common → 会话 / 人格 / 视觉 / 高阶任务<span class="mc-note-extra"> · embed 单独提供向量嵌入能力</span></span></div>
      </template>
    </div>
    <footer class="mc-savebar">
      <div class="mc-save-status" role="status"><span v-if="changes" class="mc-unsaved-dot" /><el-icon v-else-if="pendingRestart"><Check /></el-icon><span>{{ changes ? `${changes} 项更改待保存` : pendingRestart ? '已保存 · 重启 Core 后生效' : '配置保存后，重启 Core 生效' }}</span></div>
      <div class="mc-save-actions"><el-button text :disabled="saving || loading" :icon="RefreshRight" @click="reload">重新读取</el-button><el-button v-if="pendingRestart && !changes" :disabled="restarting || !canRestart" @click="emit('restart')">重启 Core</el-button><el-button type="primary" :loading="saving" :disabled="!loaded || !!error || !changes || saving || loading" @click="save">保存更改</el-button></div>
    </footer>
  </div>
</template>

<style scoped>
.mc-panel { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.mc-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 30px 34px 26px; scrollbar-gutter: stable; }
.mc-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 30px; }
.mc-eyebrow { margin-bottom: 10px; font-size: 10px; font-weight: 600; letter-spacing: .14em; color: var(--text-tertiary); }
.mc-heading h2 { margin: 0; font-size: 25px; font-weight: 600; letter-spacing: -.04em; color: var(--text-primary); }
.mc-heading p { margin: 10px 0 0; font-size: 13px; line-height: 1.7; color: var(--text-tertiary); }
.mc-count { font-size: 11px; padding: 6px 10px; border: 1px solid var(--border-default); border-radius: 7px; white-space: nowrap; color: var(--text-secondary); }
.mc-section-label { display: flex; gap: 12px; font-size: 12px; font-weight: 600; margin-bottom: 12px; color: var(--text-secondary); }
.mc-section-label span { font-weight: 400; font-size: 11px; color: var(--text-tertiary); }
.mc-cards { display: flex; flex-direction: column; gap: 12px; }
.mc-card { border: 1px solid var(--border-default); border-radius: 13px; background: var(--bg-app); overflow: hidden; transition: border-color .15s; }
.mc-card:hover, .mc-card.editing { border-color: var(--border-strong); }
.mc-card-head { display: flex; align-items: center; gap: 13px; padding: 20px 18px 13px; }
.mc-icon { width: 40px; height: 40px; flex-shrink: 0; border-radius: 11px; background: var(--bg-elevated); color: var(--text-secondary); display: grid; place-items: center; }
.mc-icon.embedding { background: color-mix(in srgb, var(--tag-info) 9%, var(--bg-app)); color: var(--tag-info); }
.mc-card-info { flex: 1; min-width: 0; }
.mc-name { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.mc-name h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--text-primary); }
.mc-card-info p { margin: 7px 0 0; font-size: 11px; line-height: 1.6; color: var(--text-tertiary); }
.mc-state { display: inline-flex; align-items: center; gap: 5px; font-size: 10px; line-height: 1.5; white-space: nowrap; }
.mc-state.configured { color: var(--tag-success); }
.mc-state.configured::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.mc-state.missing, .mc-state.changed { color: var(--tag-warning); }
.mc-state.inherited { padding: 2px 6px; border-radius: 5px; background: var(--bg-elevated); color: var(--text-tertiary); }
.mc-edit-arrow { margin-left: 7px; font-size: 11px; color: var(--text-tertiary); }
.mc-summary { display: flex; align-items: center; gap: 14px; margin: 0 18px 17px 71px; min-width: 0; font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); }
.mc-summary > span:first-child { flex-shrink: 0; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mc-endpoint { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-tertiary); }
.mc-editor { margin: 2px 12px 12px; padding: 18px; border-radius: 9px; background: var(--bg-surface); }
.mc-editor-intro { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 18px; font-size: 12px; font-weight: 500; color: var(--text-secondary); }
.mc-note { margin: -6px 0 18px; font-size: 11px; line-height: 1.8; color: var(--text-tertiary); }
.mc-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 18px 16px; }
.mc-field { min-width: 0; }
.mc-field.wide { grid-column: 1 / -1; }
.mc-label { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-size: 12px; color: var(--text-secondary); }
.mc-key-saved { display: flex; align-items: center; gap: 3px; font-size: 10px; color: var(--text-tertiary); }
.mc-field :deep(.el-input__wrapper) { min-height: 38px; border-radius: 8px; padding: 0 11px; background: var(--bg-app); box-shadow: 0 0 0 1px var(--border-default) inset; }
.mc-field :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--accent) inset, 0 0 0 3px var(--accent-soft); }
.mc-field :deep(.el-input__inner) { font-size: 12px; font-family: var(--font-mono); }
.mc-secret-help { display: flex; justify-content: space-between; gap: 12px; margin-top: 8px; font-size: 10px; line-height: 1.6; color: var(--text-tertiary); }
.mc-text-button { border: 0; padding: 0; background: none; color: var(--text-secondary); font: inherit; font-size: 11px; cursor: pointer; white-space: nowrap; text-decoration: underline; text-underline-offset: 3px; }
.mc-text-button:hover { color: var(--accent); }
.mc-editor-footer { display: flex; align-items: center; gap: 8px; margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border-default); }
.mc-editor-footer > span { flex: 1; font-size: 10px; line-height: 1.6; color: var(--text-tertiary); }
.mc-overrides-toggle { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; padding: 22px 0 16px; background: none; border: 0; color: var(--text-secondary); text-align: left; cursor: pointer; }
.mc-overrides-toggle strong { display: block; font-size: 12px; font-weight: 600; }
.mc-overrides-toggle small { display: block; margin-top: 6px; font-size: 11px; color: var(--text-tertiary); }
.mc-overrides-end { display: flex; align-items: center; gap: 8px; font-size: 10px; color: var(--text-tertiary); white-space: nowrap; }
.mc-inheritance-note { display: flex; align-items: flex-start; gap: 8px; padding: 16px 0 0; border-top: 1px solid var(--border-subtle); font-size: 10px; line-height: 1.8; color: var(--text-tertiary); }
.mc-inheritance-note .el-icon { flex-shrink: 0; margin-top: 3px; }
.mc-savebar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 24px; border-top: 1px solid var(--border-subtle); background: var(--bg-app); }
.mc-save-status { display: flex; align-items: center; gap: 7px; font-size: 11px; color: var(--text-tertiary); }
.mc-unsaved-dot { width: 6px; height: 6px; background: var(--tag-warning); border-radius: 50%; flex-shrink: 0; }
.mc-save-actions { display: flex; align-items: center; gap: 8px; }
.mc-save-actions :deep(.el-button--primary.is-disabled) { background: var(--bg-elevated); color: var(--text-disabled); border-color: var(--border-default); }
.mc-save-actions .el-button + .el-button, .mc-editor-footer .el-button + .el-button { margin-left: 0; }
.mc-empty { padding: 60px 0; text-align: center; color: var(--text-tertiary); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 760px) {
  .mc-scroll { padding: 24px 18px; }
  .mc-count, .mc-section-label span, .mc-note-extra { display: none; }
  .mc-savebar { align-items: flex-start; flex-direction: column; padding: 12px 18px; }
  .mc-save-actions { align-self: flex-end; }
  .mc-fields { grid-template-columns: 1fr; }
  .mc-field.wide { grid-column: auto; }
  .mc-card-head { gap: 10px; padding: 16px 12px; }
  .mc-icon { width: 32px; height: 32px; border-radius: 8px; }
  .mc-summary { flex-direction: column; align-items: flex-start; gap: 6px; margin-left: 54px; }
  .mc-summary > span:first-child { max-width: 100%; }
  .mc-endpoint { max-width: 100%; }
  .mc-editor-footer { flex-wrap: wrap; }
  .mc-editor-footer > span { flex-basis: 100%; }
}
@media (max-width: 520px) {
  .mc-scroll { padding: 20px 14px; }
  .mc-heading { margin-bottom: 22px; }
  .mc-heading h2 { font-size: 22px; }
  .mc-overrides-toggle { align-items: flex-start; }
  .mc-overrides-end { padding-top: 2px; font-size: 9px; }
  .mc-editor { margin: 0 6px 6px; padding: 14px 10px; }
  .mc-card-info p { font-size: 10px; }
}
</style>
