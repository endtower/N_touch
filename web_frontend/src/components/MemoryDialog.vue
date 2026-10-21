<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Connection,
  Refresh,
  Search,
  User,
  OfficeBuilding,
  UserFilled,
  Plus,
  ArrowRight,
  Collection,
  Delete,
  Tools,
} from '@element-plus/icons-vue'
import type {
  SocialNode,
  SocialContent,
  SocialRelationship,
  ExperienceItem,
} from '../types'
import { chat } from '../stores/chat'
import MemoryFields from './MemoryFields.vue'
import MaintenanceStatus from './MaintenanceStatus.vue'
import MaintenanceRequests from './MaintenanceRequests.vue'
import { memoryRequest } from '../api/memory'
import { memoryDrafts, memoryDraft, newMemoryDraft, updateMemoryDraft, removeMemoryDraft, draftFields, draftNames, draftComplete, type MemoryDraft, type MemoryDraftKind } from '../stores/memoryDrafts'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

// =====================================================================
// 状态
// =====================================================================

const searchKeyword = ref('')
const selectedId = ref<string | null>(null)
const collapsedGroups = ref<Record<string, boolean>>({})

// 视图切换: 社交实体 / 经验
const activeView = ref<'social' | 'experience' | 'kits' | 'observations' | 'personas' | 'maintenance'>('social')
const maintenancePanel = ref<InstanceType<typeof MaintenanceRequests> | null>(null)
// 经验视图状态
const selectedExpId = ref<string | null>(null)
const expCollapsed = ref<Record<string, boolean>>({})

const snap = computed(() => {
  const original = chat.state.memorySocialSnapshot
  const drafts: SocialNode[] = memoryDrafts.value.filter(item => ['character', 'group', 'fuzzy_role'].includes(item.kind)).map(item => ({
    id: item.id, uuid: item.id, type: item.kind === 'character' ? 'Character' : item.kind === 'group' ? 'Group' : 'FuzzyRole',
    label: item.fields.name || `未命名${draftNames[item.kind]}`, name: item.fields.name || '', content: item.fields.content || '',
    impressions: [{ content: item.fields.impression || '' }], source_session_id: item.fields.source_session_id || '', created_at: item.createdAt,
  }))
  if (!original && !drafts.length) return null
  const base = original ?? { nodes: [], edges: [], root_id: '00000000', character_count: 0, group_count: 0, fuzzy_role_count: 0 }
  return { ...base, nodes: [...drafts, ...base.nodes], character_count: base.character_count + drafts.filter(n => n.type === 'Character').length,
    group_count: base.group_count + drafts.filter(n => n.type === 'Group').length, fuzzy_role_count: base.fuzzy_role_count + drafts.filter(n => n.type === 'FuzzyRole').length }
})

// =====================================================================
// 分组列表
// =====================================================================

interface GroupDef {
  key: SocialNode['type']
  title: string
  icon: any
  color: string
}

// 记忆实体配色：全部取自 CMF 标签五色（源：design/tokens.json），不再写死色值。
//   Character → tag-info / Group → tag-success / FuzzyRole → tag-special / Root → tag-warning
const GROUP_DEFS: GroupDef[] = [
  { key: 'Character', title: '角色', icon: User, color: 'var(--tag-info)' },
  { key: 'Group', title: '群组', icon: OfficeBuilding, color: 'var(--tag-success)' },
  { key: 'FuzzyRole', title: '模糊角色', icon: UserFilled, color: 'var(--tag-special)' },
]

function matchKeyword(n: SocialNode): boolean {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return true
  return (
    (n.label || '').toLowerCase().includes(kw) ||
    (n.name || '').toLowerCase().includes(kw) ||
    (n.content || '').toLowerCase().includes(kw) ||
    (n.source_label || '').toLowerCase().includes(kw)
  )
}

const groups = computed<{ def: GroupDef; nodes: SocialNode[] }[]>(() => {
  const all = snap.value?.nodes ?? []
  const filtered = all.filter(matchKeyword)
  const mapping = GROUP_DEFS
    .map(def => ({ def, nodes: filtered.filter(n => n.type === def.key) }))
    .filter(g => g.nodes.length > 0)
  return mapping
})

const totalVisible = computed(() =>
  groups.value.reduce((s, g) => s + g.nodes.length, 0),
)

// =====================================================================
// 选中 + 详情
// =====================================================================

const selectedNode = computed<SocialNode | null>(() => {
  if (!selectedId.value || !snap.value) return null
  return snap.value.nodes.find(n => n.id === selectedId.value) ?? null
})

const nodeById = computed<Record<string, SocialNode>>(() => {
  const map: Record<string, SocialNode> = {}
  for (const n of snap.value?.nodes ?? []) map[n.id] = n
  return map
})

const selectedRelated = computed<SocialNode[]>(() => {
  if (!selectedId.value || !snap.value) return []
  const ids = snap.value.edges
    .filter(e => e.source === selectedId.value || e.target === selectedId.value)
    .map(e => (e.source === selectedId.value ? e.target : e.source))
    .filter(id => id !== selectedId.value)
  const uniq = [...new Set(ids)]
  return uniq.map(id => nodeById.value[id]).filter(Boolean) as SocialNode[]
})

function select(id: string) { selectedId.value = id }
function clearSel() { selectedId.value = null }
function close() { emit('update:visible', false) }
function toggleGroup(key: string) {
  collapsedGroups.value[key] = !collapsedGroups.value[key]
}
function isCollapsed(key: string) {
  return !!collapsedGroups.value[key]
}

// ---- 记忆内容内联编辑 ----

interface EditRow {
  key: string
  label: string
  value: string | string[] | boolean
  multiline?: boolean
  tags?: boolean
  boolean?: boolean
  options?: { value: string; label: string }[]
}

function firstContent(items?: SocialContent[]): string {
  return items?.[0]?.content ?? ''
}
function firstType(items?: SocialRelationship[]): string {
  return items?.[0]?.type ?? ''
}

// 可编辑字段行（按实体类型动态生成，供详情面板内联编辑渲染）
const editRows = computed<EditRow[]>(() => {
  const n = selectedNode.value
  if (!n) return []
  if (memoryDraft(n.id)) return draftRows(n.id)
  if (n.type === 'Character') {
    return [
      { key: 'name', label: 'name', value: n.name ?? '' },
      { key: 'aliases', label: '别名', value: (n.aliases ?? []).map(a => a.name).filter(Boolean), tags: true },
      { key: 'impression', label: '印象', value: firstContent(n.impressions), multiline: true },
      { key: 'familiarity', label: '熟悉度', value: firstContent(n.familiarities) },
      { key: 'relationship', label: '与 00000000 关系', value: firstType(n.relationships_with_default) },
      { key: 'allow_conversation', label: '允许 conversation', value: !!n.allow_conversation, boolean: true },
    ]
  }
  if (n.type === 'Group') {
    return [
      { key: 'name', label: 'name', value: n.name ?? '' },
      { key: 'aliases', label: '别名', value: (n.aliases ?? []).map(a => a.name).filter(Boolean), tags: true },
      { key: 'impression', label: '印象', value: firstContent(n.impressions), multiline: true },
      { key: 'familiarity', label: '熟悉度', value: firstContent(n.familiarities) },
      { key: 'member_impression', label: '成员印象', value: firstContent(n.member_impressions), multiline: true },
      { key: 'self_image', label: '群组自我形象', value: firstType(n.relationships_with_default) },
      { key: 'allow_conversation', label: '允许 conversation', value: !!n.allow_conversation, boolean: true },
    ]
  }
  if (n.type === 'FuzzyRole') {
    return [
      { key: 'name', label: 'name', value: n.name ?? '' },
      { key: 'content', label: '内容', value: n.content ?? '', multiline: true },
    ]
  }
  return []
})

async function saveSocialField(key: string, value: string | string[] | boolean) {
  const node = selectedNode.value
  if (!node) throw new Error('请先选择实体')
  if (memoryDraft(node.id)) {
    if (typeof value !== 'string') throw new Error('请填写文字')
    return saveDraftField(node.id, key, value)
  }
  if (key === 'name' && (!value || (typeof value !== 'boolean' && !value.length))) throw new Error('名称不能为空')
  const type = node.type === 'Character' ? 'character' : node.type === 'Group' ? 'group' : 'fuzzy_role'
  await chat.updateSocialMemory(type, node.uuid, { [key]: value })
  ElMessage.success('已保存')
}

// 删除当前社交实体（二次确认后调用后端；角色/群组会级联删除其名下模糊角色）
const socialDeleting = ref(false)

async function socialDelete() {
  const n = selectedNode.value
  if (!n || socialDeleting.value) return
  if (memoryDraft(n.id)) { removeMemoryDraft(n.id); clearSel(); return }
  const cascade = n.type === 'FuzzyRole' ? '' : '，锚定到它的模糊角色也会一并删除'
  try {
    await ElMessageBox.confirm(
      `确定删除「${n.label || n.uuid}」吗？其别名、联系方式与关系会一并清除${cascade}。删除后不可恢复。`,
      '删除社交实体',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  socialDeleting.value = true
  const type = n.type === 'Character' ? 'character' : n.type === 'Group' ? 'group' : 'fuzzy_role'
  try {
    await chat.deleteSocialMemory(type, n.uuid)
    clearSel()
    ElMessage.success('已删除')
  } catch (err: any) {
    ElMessage.error(err?.message || String(err))
  } finally {
    socialDeleting.value = false
  }
}

// ---- 经验视图 ----
const expList = computed<ExperienceItem[]>(() => [
  ...memoryDrafts.value.filter(item => item.kind === 'experience').map(item => ({ bequest_id: item.id, title: item.fields.title || '',
    summary: item.fields.summary || '', category: item.fields.category || 'speech', start_time: item.createdAt })),
  ...(chat.state.memoryExperiences?.experiences ?? []),
])

const filteredExperiences = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return expList.value
  return expList.value.filter(e =>
    (e.title || '').toLowerCase().includes(kw) ||
    (e.summary || '').toLowerCase().includes(kw),
  )
})

const EXP_GROUPS = [
  { key: 'speech', title: '话术经验', color: 'var(--tag-info)' },
  { key: 'decision', title: '决策经验', color: 'var(--tag-special)' },
]

const expGroups = computed<{ def: typeof EXP_GROUPS[number]; items: ExperienceItem[] }[]>(() => {
  return EXP_GROUPS
    .map(def => ({
      def,
      items: filteredExperiences.value.filter(e => e.category === def.key),
    }))
    .filter(g => g.items.length > 0)
})

const expTotal = computed(() => filteredExperiences.value.length)

const selectedExp = computed<ExperienceItem | null>(() => {
  if (!selectedExpId.value) return null
  return expList.value.find(e => e.bequest_id === selectedExpId.value) ?? null
})

function selectExp(id: string) { selectedExpId.value = id }
function clearSelExp() { selectedExpId.value = null }
function toggleExp(key: string) {
  expCollapsed.value[key] = !expCollapsed.value[key]
}
function expCollapsedGet(key: string) {
  return !!expCollapsed.value[key]
}

const expBatchBusy = ref(false)
async function reviewAllExperiences() {
  const count = chat.state.memoryExperiences?.experiences.length ?? 0
  if (!count) { ElMessage.warning('当前没有可维护的经验'); return }
  try {
    await ElMessageBox.confirm(
      `将 ${count} 条经验各记录一次人工维护？检索次数不受影响。`,
      '一键维护全部经验',
      { confirmButtonText: '全部维护', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }
  expBatchBusy.value = true
  try {
    const data = await memoryRequest('/memory/maintenance/reviews/all', 'POST', {})
    await chat.loadExperiences(200)
    ElMessage.success(`已维护 ${data.reviewed} 条经验`)
  } catch (err: any) { ElMessage.error(err.message || String(err)) }
  finally { expBatchBusy.value = false }
}

function scenarioName(s: string): string {
  if (s === 'speech') return '话术经验'
  if (s === 'decision') return '决策经验'
  return s || '未指定'
}

function scenarioColor(s: string): string {
  if (s === 'speech') return 'var(--tag-info)'
  if (s === 'decision') return 'var(--tag-special)'
  return 'var(--tag-special)'
}

function scenarioBadgeClass(s: string): string {
  if (s === 'speech') return 'speech'
  if (s === 'decision') return 'decision'
  return 'other'
}

const experienceRows = computed<EditRow[]>(() => memoryDraft(selectedExpId.value) ? draftRows(selectedExpId.value!) : selectedExp.value ? [
  { key: 'title', label: '标题', value: selectedExp.value.title },
  { key: 'summary', label: '摘要', value: selectedExp.value.summary, multiline: true },
] : [])
async function saveExperienceField(key: string, value: string | string[] | boolean) {
  const item = selectedExp.value
  if (!item || typeof value !== 'string') throw new Error('请先选择经验')
  if (memoryDraft(item.bequest_id)) return saveDraftField(item.bequest_id, key, value)
  if (key === 'title' && !value) throw new Error('标题不能为空')
  await chat.updateExperience(item.bequest_id, { [key]: value })
  ElMessage.success('已保存')
}

// 删除当前经验（二次确认后调用后端，成功后本地移除并清除选中）
const expDeleting = ref(false)

async function expDelete() {
  const e = selectedExp.value
  if (!e || expDeleting.value) return
  if (memoryDraft(e.bequest_id)) { removeMemoryDraft(e.bequest_id); clearSelExp(); return }
  try {
    await ElMessageBox.confirm(
      `确定删除经验「${e.title || '（未命名）'}」吗？删除后不可恢复。`,
      '删除经验',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  expDeleting.value = true
  try {
    await chat.deleteExperience(e.bequest_id)
    clearSelExp()
    ElMessage.success('已删除')
  } catch (err: any) {
    ElMessage.error(err?.message || String(err))
  } finally {
    expDeleting.value = false
  }
}

// 锦囊与见闻复用实体/经验的分类列表、详情与字段编辑。
interface MemoryRecord { id: string; name: string; category: string; snippet: string; raw: Record<string, any> }
const kitItems = ref<Record<string, any>[]>([]), observationItems = ref<Record<string, any>[]>([]), personaItems = ref<Record<string, any>[]>([])
const selectedRecordId = ref(''), recordLoading = ref(false), recordError = ref(''), recordDeleting = ref(false)
const recordCollapsed = ref<Record<string, boolean>>({})
const dilemmaQuery = ref(''), matchedObservations = ref<Record<string, any>[] | null>(null)
const matching = ref(false), matchHint = ref(''), matchError = ref('')
// 见闻录列表筛选：显示条数与时间段（限制一次性全量渲染，2026-10-14）
const obsLimit = ref(20), obsRange = ref<[Date, Date] | null>(null), observationTotal = ref(0)
let recordVersion = 0, matchVersion = 0
const recordLabel = computed(() => activeView.value === 'kits' ? '锦囊' : activeView.value === 'personas' ? '人格' : '见闻')
const records = computed<MemoryRecord[]>(() => {
  const drafts = memoryDrafts.value.filter(item => activeView.value === 'kits' ? item.kind === 'kit' : activeView.value === 'observations' && item.kind === 'observation')
    .map(item => ({ id: item.id, name: item.fields.dilemma || item.fields.text?.split('\n')[0] || `未命名${draftNames[item.kind]}`,
      category: item.kind === 'kit' ? item.fields.category || '未分类' : '见闻录', snippet: item.fields.advice || item.fields.text || '', raw: item.fields }))
  return [...drafts, ...(activeView.value === 'kits'
  ? kitItems.value.map(item => ({ id: item.kit_id, name: item.dilemma, category: item.category, snippet: item.advice, raw: item }))
  : activeView.value === 'personas' ? personaItems.value.map(item => ({ id: item.persona_name, name: item.display_name || item.persona_name, category: item.user_only ? '系统人格' : item.persona_name === 'common' ? '基本说话风格' : '内心人格', snippet: item.scene, raw: item }))
  : (matchedObservations.value ?? observationItems.value).map(item => ({ id: item.note_id, name: item.text.split('\n')[0] || '见闻', category: '见闻录', snippet: item.text, raw: item })))]
})
const selectedRecord = computed(() => records.value.find(item => item.id === selectedRecordId.value))
const recordGroups = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  const list = records.value.filter(item => [item.name, item.category, item.snippet].some(value => value.toLowerCase().includes(keyword)))
  return [...new Set(list.map(item => item.category))].map(category => ({ category, items: list.filter(item => item.category === category) }))
})
const recordRows = computed<EditRow[]>(() => {
  if (memoryDraft(selectedRecordId.value)) return draftRows(selectedRecordId.value)
  const raw = selectedRecord.value?.raw
  if (!raw) return []
  return activeView.value === 'kits' ? [
    { key: 'advice', label: '建议正文', value: raw.advice, multiline: true },
    { key: 'dilemma', label: '作用困境', value: raw.dilemma, multiline: true },
    { key: 'category', label: '困境分类', value: raw.category },
  ] : activeView.value === 'personas' && raw.user_only ? [
    { key: 'kernel_prompt', label: '补充人格设定', value: raw.kernel_prompt, multiline: true },
  ] : activeView.value === 'personas' ? [
    { key: 'scene', label: '适配场景', value: raw.scene, multiline: true },
    { key: 'kernel_prompt', label: '人格正文', value: raw.kernel_prompt, multiline: true },
  ] : [{ key: 'text', label: '见闻正文', value: raw.text, multiline: true }]
})
// 本地时间戳（见闻录时间段用本地时间提交，避免 toISOString 的时区偏移）
function localStamp(date: Date, endOfDay: boolean): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  return `${day}T${endOfDay ? '23:59:59' : '00:00:00'}`
}
async function loadRecords() {
  const version = ++recordVersion, view = activeView.value
  recordLoading.value = true; recordError.value = ''
  try {
    let path = view === 'kits' ? '/memory/kits' : view === 'personas' ? '/memory/personas' : '/memory/observations'
    if (view === 'observations') {
      const params = new URLSearchParams({ limit: String(obsLimit.value) })
      if (obsRange.value && obsRange.value.length === 2) {
        params.set('since', localStamp(obsRange.value[0], false))
        params.set('until', localStamp(obsRange.value[1], true))
      }
      path += `?${params.toString()}`
    }
    const data = await memoryRequest(path)
    if (version !== recordVersion || view !== activeView.value) return
    if (view === 'kits') kitItems.value = data.kits
    else if (view === 'personas') personaItems.value = data.personas
    else { observationItems.value = data.observations; observationTotal.value = data.total ?? data.observations.length }
  } catch (err: any) { if (version === recordVersion) recordError.value = err.message || String(err) }
  finally { if (version === recordVersion) recordLoading.value = false }
}
function changeObservationFilter() { clearMatches(); loadRecords() }
async function saveRecordField(key: string, value: string | string[] | boolean) {
  const item = selectedRecord.value, view = activeView.value
  if (item && memoryDraft(item.id)) {
    if (typeof value !== 'string') throw new Error('请填写文字')
    return saveDraftField(item.id, key, value)
  }
  const canClear = view === 'personas' && item?.raw.user_only && key === 'kernel_prompt'
  if (!item || typeof value !== 'string' || (!value && !canClear)) throw new Error('内容不能为空')
  const data = await memoryRequest(`/memory/${view === 'kits' ? 'kits' : view === 'personas' ? 'personas' : 'observations'}/${encodeURIComponent(item.id)}`, 'PATCH', { [key]: value })
  if (view === 'kits') kitItems.value = kitItems.value.map(old => old.kit_id === item.id ? data.kit : old)
  else if (view === 'personas') personaItems.value = personaItems.value.map(old => old.persona_name === item.id ? data.persona : old)
  else {
    observationItems.value = observationItems.value.map(old => old.note_id === item.id ? data.observation : old)
    // 正文改变后旧相似度不再适用，回到全部见闻，等待显式重新匹配。
    if (view === activeView.value) { ++matchVersion; matching.value = false; matchedObservations.value = null; matchHint.value = '' }
  }
  ElMessage.success('已保存')
}
async function deleteRecord() {
  const item = selectedRecord.value, view = activeView.value
  if (!item || recordDeleting.value) return
  if (memoryDraft(item.id)) { removeMemoryDraft(item.id); selectedRecordId.value = ''; return }
  try { await ElMessageBox.confirm(`确定删除这条${recordLabel.value}吗？删除后不可恢复。`, `删除${recordLabel.value}`, { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }) } catch { return }
  recordDeleting.value = true
  try {
    await memoryRequest(`/memory/${view === 'kits' ? 'kits' : 'observations'}/${encodeURIComponent(item.id)}`, 'DELETE')
    if (view === 'kits') kitItems.value = kitItems.value.filter(old => old.kit_id !== item.id)
    else {
      observationItems.value = observationItems.value.filter(old => old.note_id !== item.id)
      ++matchVersion; matching.value = false
      if (matchedObservations.value) matchedObservations.value = matchedObservations.value.filter(old => old.note_id !== item.id)
    }
    if (view === activeView.value) selectedRecordId.value = ''
    ElMessage.success('已删除')
  } catch (err: any) { ElMessage.error(err.message || String(err)) }
  finally { recordDeleting.value = false }
}
async function findObservations() {
  if (!dilemmaQuery.value.trim()) { ElMessage.warning('请描述当前遇到的困境'); return }
  const version = ++matchVersion
  matching.value = true; matchError.value = ''; matchHint.value = ''
  try {
    const data = await memoryRequest(`/memory/observations/search?query=${encodeURIComponent(dilemmaQuery.value.trim())}`)
    if (version !== matchVersion) return
    matchedObservations.value = data.status === 'matched' ? data.observations : null
    matchHint.value = data.hint || ''
  } catch (err: any) { if (version === matchVersion) matchError.value = err.message || String(err) }
  finally { if (version === matchVersion) matching.value = false }
}
function clearMatches() { ++matchVersion; matching.value = false; matchedObservations.value = null; matchHint.value = ''; matchError.value = '' }
async function reload() {
  clearMatches()
  if (activeView.value === 'maintenance') await maintenancePanel.value?.reload()
  else if (activeView.value === 'kits' || activeView.value === 'observations' || activeView.value === 'personas') await loadRecords()
  else if (activeView.value === 'experience') await chat.loadExperiences(200)
  else await chat.loadMemorySocial(500)
}
function switchView(view: typeof activeView.value) {
  if (view === activeView.value) return
  activeView.value = view
  searchKeyword.value = ''; selectedRecordId.value = ''; clearSel(); clearSelExp()
  ++recordVersion; clearMatches(); reload()
}
watch(() => props.visible, visible => { if (visible) reload() })
async function openMaintenanceItem(kind: string, itemId: string) {
  activeView.value = kind === 'experience' ? 'experience' : kind === 'persona' ? 'personas' : 'kits'
  searchKeyword.value = ''; clearMatches()
  await reload()
  if (kind === 'experience') selectedExpId.value = itemId
  else selectedRecordId.value = itemId
}
onBeforeUnmount(() => { ++recordVersion; ++matchVersion })

// 空白条目直接复用详情编辑器；未完成内容保留在本地，正式保存后替换草稿编号。
const publishingDrafts = new Set<string>()
const draftErrors = ref<Record<string, string>>({})
function draftRows(id: string): EditRow[] {
  const draft = memoryDraft(id)
  return draft ? draftFields(draft.kind).map(field => ({ ...field, value: draft.fields[field.key] || '' })) : []
}
function firstDraftField(id: string) {
  const draft = memoryDraft(id)
  return draft ? draftFields(draft.kind).find(field => !draft.fields[field.key])?.key : undefined
}
const activeDraft = computed(() => memoryDraft(activeView.value === 'social' ? selectedId.value : activeView.value === 'experience' ? selectedExpId.value : selectedRecordId.value))
async function saveDraftField(id: string, key: string, value: string | string[]) {
  if (typeof value !== 'string') throw new Error('请填写文字')
  updateMemoryDraft(id, key, value)
  const draft = memoryDraft(id)!
  // 角色/群组保留填写可选关联会话的机会；离开条目时也会自动提交。
  if (draftComplete(draft) && (!['character', 'group'].includes(draft.kind) || 'session_id' in draft.fields)) await publishDraft(draft)
}
function openCreate(kind?: MemoryDraftKind) {
  const target = kind || (activeView.value === 'social' ? 'character' : activeView.value === 'experience' ? 'experience' : activeView.value === 'kits' ? 'kit' : 'observation')
  try {
    const item = newMemoryDraft(target)
    searchKeyword.value = ''; clearMatches()
    if (['character', 'group', 'fuzzy_role'].includes(target)) {
      selectedId.value = item.id
      collapsedGroups.value[target === 'character' ? 'Character' : target === 'group' ? 'Group' : 'FuzzyRole'] = false
    } else if (target === 'experience') { selectedExpId.value = item.id; expCollapsed.value.speech = false }
    else { selectedRecordId.value = item.id; recordCollapsed.value[target === 'kit' ? '未分类' : '见闻录'] = false }
  } catch (err: any) { ElMessage.error(err.message || '无法保存空白条目') }
}
async function publishDraft(draft: MemoryDraft) {
  if (!draftComplete(draft) || publishingDrafts.has(draft.id)) return
  publishingDrafts.add(draft.id)
  delete draftErrors.value[draft.id]
  try {
    const body = Object.fromEntries(draftFields(draft.kind).map(field => [field.key, (draft.fields[field.key] || '').trim()]))
    const paths = { experience: '/memory/experiences', kit: '/memory/kits', observation: '/memory/observations' }
    const path = draft.kind in paths ? paths[draft.kind as keyof typeof paths] : `/memory/social/${draft.kind}`
    const data = await memoryRequest(path, 'POST', body)
    if (data.node) {
      const snapshot = chat.state.memorySocialSnapshot ?? { nodes: [], edges: [], root_id: '00000000', character_count: 0, group_count: 0, fuzzy_role_count: 0 }
      snapshot.nodes = [data.node, ...snapshot.nodes.filter(item => item.id !== data.node.id)]
      const count = data.node.type === 'Character' ? 'character_count' : data.node.type === 'Group' ? 'group_count' : 'fuzzy_role_count'
      snapshot[count] += 1
      chat.state.memorySocialSnapshot = snapshot
      if (selectedId.value === draft.id) selectedId.value = data.node.id
    } else if (data.experience) {
      const current = chat.state.memoryExperiences ?? { experiences: [], total: 0 }
      current.experiences = [data.experience, ...current.experiences]; current.total++
      chat.state.memoryExperiences = current
      if (selectedExpId.value === draft.id) selectedExpId.value = data.experience.bequest_id
    } else if (data.kit) {
      kitItems.value = [data.kit, ...kitItems.value]; recordCollapsed.value[data.kit.category] = false
      if (selectedRecordId.value === draft.id) selectedRecordId.value = data.kit.kit_id
    } else if (data.observation) {
      observationItems.value = [data.observation, ...observationItems.value]; observationTotal.value++
      if (selectedRecordId.value === draft.id) selectedRecordId.value = data.observation.note_id
    }
    removeMemoryDraft(draft.id)
    ElMessage.success({ message: '已保存条目', grouping: true })
  } catch (err: any) {
    draftErrors.value[draft.id] = err.message || String(err)
    throw err
  } finally { publishingDrafts.delete(draft.id) }
}
watch(() => [selectedId.value, selectedExpId.value, selectedRecordId.value, activeView.value, props.visible] as const, (current, previous) => {
  for (const id of previous.slice(0, 3)) {
    const draft = memoryDraft(typeof id === 'string' ? id : null)
    if (draft && (current[3] !== previous[3] || !current[4] || !current.slice(0, 3).includes(id))) {
      void publishDraft(draft).catch(() => ElMessage.error('条目暂未入库，草稿已保留，可稍后重试'))
    }
  }
})
async function retryDraft() { if (activeDraft.value) await publishDraft(activeDraft.value).catch(() => {}) }

// =====================================================================
// 展示辅助
// =====================================================================

function timeShort(s?: string | null) {
  if (!s) return '—'
  return s.length >= 16 ? s.slice(0, 16).replace('T', ' ') : s
}

function listText(items?: { name?: string; content?: string; type?: string }[]): string {
  if (!items || items.length === 0) return '—'
  return items.map(i => i.name ?? i.content ?? i.type ?? '').filter(Boolean).join('、')
}

function aliasText(n: SocialNode): string {
  const a = n.aliases?.map(x => x.name).filter(Boolean).join('、')
  return a || ''
}

// 副标题只保留有信息量的印象/内容，不再回退到 uuid/name
function nodeSnippet(n: SocialNode): string {
  if (n.content) return n.content
  if (n.impressions?.length) return listText(n.impressions)
  return ''
}

function typeBadgeClass(n: SocialNode): string {
  return n.type.toLowerCase()
}

function iconFor(n: SocialNode): any {
  switch (n.type) {
    case 'Group': return OfficeBuilding
    case 'FuzzyRole': return UserFilled
    case 'Root': return Connection
    default: return User
  }
}

function colorFor(n: SocialNode): string {
  switch (n.type) {
    case 'Group': return 'var(--tag-success)'
    case 'FuzzyRole': return 'var(--tag-special)'
    case 'Root': return 'var(--tag-warning)'
    default: return 'var(--tag-info)'
  }
}
</script>

<template>
  <AppDialog size="panel"
    :model-value="props.visible"
    :close-on-click-modal="false"
    align-center
    title="记忆"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
    class="memory-dialog"
  >
    <template #header>
      <div class="memory-header">
        <div class="memory-header-left">
          <el-icon :size="16" color="var(--accent)"><Connection /></el-icon>
          <span class="memory-title">记忆</span>
          <div class="memory-tabs">
            <button
              type="button"
              class="memory-tab"
              :class="{ active: activeView === 'social' }"
              @click="switchView('social')"
            >社交实体</button>
            <button
              type="button"
              class="memory-tab"
              :class="{ active: activeView === 'experience' }"
              @click="switchView('experience')"
            >经验<span v-if="chat.state.memoryExperiences" class="memory-tab-count">{{ chat.state.memoryExperiences.total }}</span></button>
            <button type="button" class="memory-tab" :class="{ active: activeView === 'kits' }" @click="switchView('kits')">锦囊</button>
            <button type="button" class="memory-tab" :class="{ active: activeView === 'observations' }" @click="switchView('observations')">见闻录</button>
            <button type="button" class="memory-tab" :class="{ active: activeView === 'personas' }" @click="switchView('personas')">人格</button>
            <button type="button" class="memory-tab" :class="{ active: activeView === 'maintenance' }" @click="switchView('maintenance')">维护申请</button>
          </div>
          <span v-if="activeView === 'social' && snap" class="memory-counts">
            {{ snap.character_count }} 角色 · {{ snap.group_count }} 群组 ·
            {{ snap.fuzzy_role_count }} 模糊角色 · {{ snap.edges.length }} 关系
          </span>
          <span v-else-if="activeView === 'experience'" class="memory-counts">{{ expTotal }} 条经验</span>
          <span v-else-if="activeView === 'observations'" class="memory-counts">已显示 {{ observationItems.length }} / 共 {{ observationTotal }} 条</span>
        </div>
        <div class="memory-header-right">
          <el-input
            v-if="activeView !== 'maintenance'"
            v-model="searchKeyword"
            size="small"
            :placeholder="activeView === 'experience' ? '搜索经验…' : activeView === 'kits' ? '搜索锦囊…' : activeView === 'observations' ? '搜索见闻…' : activeView === 'personas' ? '搜索人格…' : '搜索角色 / 群组 / 模糊角色…'"
            clearable
            style="width: 220px;"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-dropdown v-if="activeView === 'social'" split-button size="small" @click="openCreate()" @command="openCreate">
            新建
            <template #dropdown><el-dropdown-menu><el-dropdown-item command="character">新建角色</el-dropdown-item><el-dropdown-item command="group">新建群组</el-dropdown-item><el-dropdown-item command="fuzzy_role">新建模糊角色</el-dropdown-item></el-dropdown-menu></template>
          </el-dropdown>
          <el-button v-else-if="activeView !== 'personas' && activeView !== 'maintenance'" :icon="Plus" size="small" @click="openCreate()">新建</el-button>
          <el-button v-if="activeView === 'experience'" :icon="Tools" size="small" :loading="expBatchBusy" :disabled="!expTotal" @click="reviewAllExperiences">全部维护</el-button>
          <el-button :icon="Refresh" size="small" @click="reload">刷新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <div v-if="activeDraft" class="memory-draft-note">
      <span>草稿 · 逐项编辑即可，未完成内容会保留。</span>
      <el-button v-if="draftComplete(activeDraft)" size="small" @click="retryDraft">保存条目</el-button>
      <span v-if="draftErrors[activeDraft.id]" role="alert">{{ draftErrors[activeDraft.id] }}</span>
    </div>
    <div class="memory-body">
      <template v-if="activeView === 'social'">
      <!-- 左：分组实体列表 -->
      <aside class="memory-list">
        <div v-if="chat.state.memorySocialLoading && !snap" class="memory-empty">
          <span>加载中…</span>
        </div>
        <div v-else-if="chat.state.memorySocialError" class="memory-empty error">
          <div class="memory-empty-title">无法连接记忆数据库</div>
          <div class="memory-empty-sub">{{ chat.state.memorySocialError }}</div>
          <el-button :icon="Refresh" size="small" style="margin-top: 10px;" @click="chat.loadMemorySocial(500)">重试</el-button>
        </div>
        <div v-else-if="!snap || snap.nodes.length === 0" class="memory-empty">
          <div class="memory-empty-title">记忆里还没有社交实体</div>
          <div class="memory-empty-sub">角色 / 群组 / 模糊角色会在此展示</div>
        </div>
        <template v-else>
          <div class="memory-root-card">
            <span class="memory-root-dot"></span>
            <div class="memory-root-main">
              <span class="memory-root-id">00000000</span>
              <span class="memory-root-note"><el-icon :size="11"><Connection /></el-icon>{{ snap.root_id ? 'LLM 扮演的角色（未命名）' : '' }}</span>
            </div>
          </div>

          <div class="memory-groups">
            <section v-for="g in groups" :key="g.def.key" class="memory-group">
              <button
                class="memory-group-head"
                type="button"
                :aria-expanded="!isCollapsed(g.def.key)"
                @click="toggleGroup(g.def.key)"
              >
                <el-icon
                  :size="10"
                  class="memory-group-chevron"
                  :class="{ open: !isCollapsed(g.def.key) }"
                >
                  <ArrowRight />
                </el-icon>
                <el-icon :size="12" :style="{ color: g.def.color }"><component :is="g.def.icon" /></el-icon>
                <span class="memory-group-title">{{ g.def.title }}</span>
                <span class="memory-group-count">{{ g.nodes.length }}</span>
              </button>
              <div v-if="!isCollapsed(g.def.key)" class="memory-group-items">
                <button
                  v-for="n in g.nodes"
                  :key="n.id"
                  class="memory-item"
                  :class="{ active: n.id === selectedId }"
                  @click="select(n.id)"
                >
                  <span class="memory-item-dot" :style="{ background: g.def.color }"></span>
                  <div class="memory-item-main">
                    <div class="memory-item-label">
                      <span class="memory-item-name">{{ n.label }}</span><span v-if="memoryDraft(n.id)" class="memory-draft-tag">草稿</span>
                      <span v-if="aliasText(n)" class="memory-item-alias">{{ aliasText(n) }}</span>
                    </div>
                    <div v-if="nodeSnippet(n)" class="memory-item-sub">{{ nodeSnippet(n) }}</div>
                  </div>
                  <span v-if="n.usage_count != null && n.usage_count > 0" class="memory-item-count" :title="`使用 ${n.usage_count} 次`">
                    <el-icon :size="11"><Plus /></el-icon>{{ n.usage_count }}
                  </span>
                </button>
              </div>
            </section>
          </div>

          <div v-if="totalVisible === 0" class="memory-empty">
            <div class="memory-empty-title">没有匹配「{{ searchKeyword }}」的实体</div>
          </div>
        </template>
      </aside>

      <!-- 右：详情面板 -->
      <section class="memory-detail">
        <div v-if="!selectedNode" class="memory-detail-empty">
          <el-icon :size="26" color="var(--text-3)"><Search /></el-icon>
          <div style="font-weight: 600; color: var(--text-1); margin-top: 8px;">未选中实体</div>
          <div style="font-size: 12px; color: var(--text-3); margin-top: 4px;">
            点击左侧任一 角色 / 群组 / 模糊角色，查看它在 memory 中的完整信息。
          </div>
        </div>
        <template v-else>
          <div class="memory-detail-head">
            <el-icon :size="13" :style="{ color: colorFor(selectedNode) }"><component :is="iconFor(selectedNode)" /></el-icon>
            <span class="memory-type-badge" :class="typeBadgeClass(selectedNode)">{{ selectedNode.type }}</span>
            <span class="memory-detail-name">{{ selectedNode.label }}</span>
            <span class="memory-edit-tip">点击字段值即可修改</span>
            <el-button
              class="memory-detail-delete"
              type="danger"
              size="small"
              plain
              :icon="Delete"
              :loading="socialDeleting"
              @click="socialDelete"
            >删除</el-button>
          </div>

          <div class="memory-detail-body">
            <dl class="kv">
              <dt>uuid</dt><dd><code>{{ selectedNode.uuid }}</code></dd>

                <MemoryFields :record-id="selectedNode.id" :autofocus-key="firstDraftField(selectedNode.id)" :rows="editRows" :save="saveSocialField" />

              <template v-if="selectedNode.type === 'Character' || selectedNode.type === 'Group'">
                <dt>联系方式</dt>
                <dd>{{ (selectedNode.contacts ?? []).map(c => c.session_id).join('、') || '—' }}</dd>
              </template>

              <template v-if="selectedNode.type === 'FuzzyRole'">
                <dt>来源会话</dt><dd><code>{{ selectedNode.source_session_id || '—' }}</code></dd>
                <dt>依附实体</dt>
                <dd>
                  <template v-if="selectedNode.source_entity_uuid && nodeById[selectedNode.source_entity_uuid]">
                    <span class="related-chip clickable" @click="select(selectedNode.source_entity_uuid!)">
                      {{ nodeById[selectedNode.source_entity_uuid].label }}（{{ nodeById[selectedNode.source_entity_uuid].type }}）
                    </span>
                  </template>
                  <template v-else>—</template>
                </dd>
              </template>

              <dt>usage_count</dt><dd>{{ selectedNode.usage_count ?? 0 }}</dd>
              <dt>last_used_at</dt><dd>{{ timeShort(selectedNode.last_used_at) }}</dd>
              <dt>created_at</dt><dd>{{ timeShort(selectedNode.created_at) }}</dd>
              <dt v-if="selectedNode.updated_at">updated_at</dt>
              <dd v-if="selectedNode.updated_at">{{ timeShort(selectedNode.updated_at) }}</dd>
            </dl>

            <div class="detail-related">
              <div class="kv-label">相关实体（{{ selectedRelated.length }}）</div>
              <div v-if="selectedRelated.length === 0" class="kv-empty">无</div>
              <div v-else class="related-chips">
                <button
                  v-for="r in selectedRelated"
                  :key="r.id"
                  class="related-chip clickable"
                  @click="select(r.id)"
                >
                  <span class="related-dot" :style="{ background: colorFor(r) }"></span>
                  <span class="related-type">{{ r.type }}</span>
                  <span class="related-name">{{ r.label }}</span>
                </button>
              </div>
            </div>
          </div>
        </template>
      </section>
      </template>

      <template v-else-if="activeView === 'maintenance'"><MaintenanceRequests ref="maintenancePanel" @open-item="openMaintenanceItem" /></template>
      <!-- 经验视图 -->
      <template v-else-if="activeView === 'experience'">
        <!-- 左：分组经验列表 -->
        <aside class="memory-list">
          <div v-if="chat.state.memoryExperiencesLoading && !chat.state.memoryExperiences" class="memory-empty">
            <span>加载中…</span>
          </div>
          <div v-else-if="chat.state.memoryExperiencesError" class="memory-empty error">
            <div class="memory-empty-title">无法读取经验</div>
            <div class="memory-empty-sub">{{ chat.state.memoryExperiencesError }}</div>
            <el-button :icon="Refresh" size="small" style="margin-top: 10px;" @click="chat.loadExperiences(200)">重试</el-button>
          </div>
          <div v-else-if="!chat.state.memoryExperiences || expTotal === 0" class="memory-empty">
            <div class="memory-empty-title">记忆里还没有经验</div>
            <div class="memory-empty-sub">决策 / 执行阶段的经验会在此展示</div>
          </div>
          <div v-else class="memory-groups">
            <section v-for="g in expGroups" :key="g.def.key" class="memory-group">
              <button
                class="memory-group-head"
                type="button"
                :aria-expanded="!expCollapsedGet(g.def.key)"
                @click="toggleExp(g.def.key)"
              >
                <el-icon
                  :size="10"
                  class="memory-group-chevron"
                  :class="{ open: !expCollapsedGet(g.def.key) }"
                >
                  <ArrowRight />
                </el-icon>
                <span class="memory-group-dot" :style="{ background: g.def.color }"></span>
                <span class="memory-group-title">{{ g.def.title }}</span>
                <span class="memory-group-count">{{ g.items.length }}</span>
              </button>
              <div v-if="!expCollapsedGet(g.def.key)" class="memory-group-items">
                <button
                  v-for="e in g.items"
                  :key="e.bequest_id"
                  class="memory-item"
                  :class="{ active: e.bequest_id === selectedExpId }"
                  @click="selectExp(e.bequest_id)"
                >
                  <span v-if="!memoryDraft(e.bequest_id)" class="memory-item-stats">
                    <span class="memory-stat is-maintain" :title="`人工维护 ${e.manual_maintenance_count ?? 0} 次`">维护<span class="memory-stat-num">{{ e.manual_maintenance_count ?? 0 }}</span></span>
                    <span class="memory-stat is-retrieval" :title="`被检索 ${e.retrieval_count ?? 0} 次`">检索<span class="memory-stat-num">{{ e.retrieval_count ?? 0 }}</span></span>
                  </span>
                  <span class="memory-item-dot" :style="{ background: g.def.color }"></span>
                  <div class="memory-item-main">
                    <div class="memory-item-label">
                      <span class="memory-item-name">{{ e.title || '（未命名经验）' }}</span><span v-if="memoryDraft(e.bequest_id)" class="memory-draft-tag">草稿</span>
                      <span class="memory-scenario-tag" :class="scenarioBadgeClass(e.category)">{{ scenarioName(e.category) }}</span>
                    </div>
                    <div v-if="e.summary" class="memory-item-sub">{{ e.summary }}</div>
                  </div>
                </button>
              </div>
            </section>
          </div>
        </aside>

        <!-- 右：经验详情 -->
        <section class="memory-detail">
          <div v-if="!selectedExp" class="memory-detail-empty">
            <el-icon :size="26" color="var(--text-3)"><Collection /></el-icon>
            <div style="font-weight: 600; color: var(--text-1); margin-top: 8px;">未选中经验</div>
            <div style="font-size: 12px; color: var(--text-3); margin-top: 4px;">
              点击左侧任一经验，查看其在 memory 中存储的完整内容。
            </div>
          </div>
          <template v-else>
            <div class="memory-detail-head">
              <span class="memory-type-badge" :class="scenarioBadgeClass(selectedExp.category)">{{ scenarioName(selectedExp.category) }}</span>
              <span class="memory-detail-name">{{ selectedExp.title || '（未命名经验）' }}</span>
              <span class="memory-edit-tip">点击字段值即可修改</span>
              <el-button
                class="memory-detail-delete"
                type="danger"
                size="small"
                plain
                :icon="Delete"
                :loading="expDeleting"
                @click="expDelete"
              >删除</el-button>
            </div>
            <div class="memory-detail-body">
              <dl class="kv">
                <dt>编号</dt><dd><code>{{ memoryDraft(selectedExp.bequest_id) ? '草稿' : selectedExp.bequest_id }}</code></dd>

                <MemoryFields :record-id="selectedExp.bequest_id" :autofocus-key="firstDraftField(selectedExp.bequest_id)" :rows="experienceRows" :save="saveExperienceField" />

                <dt>分类</dt>
                <dd><code>{{ selectedExp.category }}（{{ scenarioName(selectedExp.category) }}）</code></dd>

                <dt>开始时间</dt><dd>{{ timeShort(selectedExp.start_time) }}</dd>
                <dt>更新时间</dt><dd>{{ timeShort(selectedExp.updated_at) }}</dd>
              </dl>
              <MaintenanceStatus v-if="!memoryDraft(selectedExp.bequest_id)" kind="experience" :item-id="selectedExp.bequest_id" :version="selectedExp.updated_at" />
            </div>
          </template>
        </section>
      </template>
      <template v-else>
        <aside class="memory-list">
          <div v-if="activeView === 'observations'" class="memory-match-controls">
            <el-input v-model="dilemmaQuery" size="small" aria-label="当前困境" placeholder="描述当前困境，查找相关见闻" @keyup.enter="findObservations" />
            <div class="kv-edit-actions"><el-button size="small" :loading="matching" @click="findObservations">查找见闻</el-button><el-button v-if="matchedObservations !== null" size="small" @click="clearMatches">显示全部</el-button></div>
            <p v-if="matchHint" class="memory-empty-sub">{{ matchHint }}</p>
            <p v-if="matchError" class="kv-edit-error" role="alert">{{ matchError }}</p>
            <div class="obs-filter-row">
              <el-select v-model="obsLimit" size="small" aria-label="显示数量" style="width: 96px;" @change="changeObservationFilter">
                <el-option v-for="n in [20, 50, 100, 200]" :key="n" :value="n" :label="`最近 ${n} 条`" />
              </el-select>
              <el-date-picker v-model="obsRange" size="small" type="daterange" aria-label="时间段" range-separator="至" start-placeholder="起始日期" end-placeholder="结束日期" style="flex: 1 1 auto; min-width: 200px;" @change="changeObservationFilter" />
            </div>
          </div>
          <div v-if="recordLoading" class="memory-empty">加载中…</div>
          <div v-if="recordError" class="memory-empty error"><div class="memory-empty-sub">{{ recordError }}</div><el-button size="small" @click="loadRecords">重试</el-button></div>
          <div v-else-if="!recordLoading && !recordGroups.length" class="memory-empty"><div class="memory-empty-title">{{ records.length ? '没有匹配的条目' : `记忆里还没有${recordLabel}` }}</div><div class="memory-empty-sub">点击“新建”创建空白{{ recordLabel }}</div></div>
          <div v-else class="memory-groups">
            <section v-for="group in recordGroups" :key="group.category" class="memory-group">
              <button type="button" class="memory-group-head" :aria-expanded="!recordCollapsed[group.category]" @click="recordCollapsed[group.category] = !recordCollapsed[group.category]">
                <el-icon :size="10" class="memory-group-chevron" :class="{ open: !recordCollapsed[group.category] }"><ArrowRight /></el-icon>
                <span class="memory-group-dot" style="background: var(--tag-special);"></span><span class="memory-group-title">{{ group.category }}</span><span class="memory-group-count">{{ group.items.length }}</span>
              </button>
              <div v-if="!recordCollapsed[group.category]" class="memory-group-items">
                <button v-for="item in group.items" :key="item.id" class="memory-item" :class="{ active: item.id === selectedRecordId }" @click="selectedRecordId = item.id">
                  <span class="memory-item-dot" style="background: var(--tag-special);"></span>
                  <div class="memory-item-main"><div class="memory-item-label"><span class="memory-item-name">{{ item.name }}</span><span v-if="memoryDraft(item.id)" class="memory-draft-tag">草稿</span></div><div class="memory-item-sub">{{ item.snippet }}</div></div>
                </button>
              </div>
            </section>
          </div>
        </aside>
        <section class="memory-detail">
          <div v-if="!selectedRecord" class="memory-detail-empty"><el-icon :size="26"><Search /></el-icon><div>未选中{{ recordLabel }}</div><div class="memory-empty-sub">点击左侧条目查看详情，点击字段值即可修改。</div></div>
          <template v-else>
            <div class="memory-detail-head"><span class="memory-type-badge">{{ recordLabel }}</span><span class="memory-detail-name">{{ selectedRecord.name }}</span><span class="memory-edit-tip">点击字段值即可修改</span><el-button v-if="activeView !== 'personas'" class="memory-detail-delete" size="small" type="danger" plain :icon="Delete" :loading="recordDeleting" @click="deleteRecord">删除</el-button></div>
            <div class="memory-detail-body">
              <div v-if="activeView === 'personas' && selectedRecord.raw.user_only" class="memory-system-persona-note">
                <strong>仅用户可编辑</strong>
                <p>{{ selectedRecord.raw.scene }}，下一次模型调用生效。留空则不追加内容。</p>
                <p>不参与内心人格候选，也不提供给 LLM 查询或修改的工具。</p>
              </div>
              <dl class="kv">
                <dt>编号</dt><dd><code>{{ memoryDraft(selectedRecord.id) ? '草稿' : selectedRecord.id }}</code></dd>
                <MemoryFields :record-id="selectedRecord.id" :autofocus-key="firstDraftField(selectedRecord.id)" :rows="recordRows" :save="saveRecordField" />
                <template v-if="!selectedRecord.raw.user_only"><dt>记录时间</dt><dd>{{ timeShort(selectedRecord.raw.created_at || selectedRecord.raw.record_time) }}</dd></template>
                <dt v-if="selectedRecord.raw.updated_at">更新时间</dt><dd v-if="selectedRecord.raw.updated_at">{{ timeShort(selectedRecord.raw.updated_at) }}</dd>
                <template v-if="activeView === 'observations'"><dt>来源</dt><dd>{{ selectedRecord.raw.source === 'human' ? '人工记录' : selectedRecord.raw.source === 'reflection' ? '反思记录' : '历史记录' }}</dd><template v-if="selectedRecord.raw.similarity != null"><dt>相似度</dt><dd>{{ selectedRecord.raw.similarity.toFixed(3) }}</dd></template></template>
              </dl>
              <div v-if="activeView === 'kits'" class="memory-match-controls">
                <el-input v-model="dilemmaQuery" size="small" aria-label="当前困境" placeholder="描述当前困境，查找相关见闻" @keyup.enter="findObservations" />
                <div class="kv-edit-actions"><el-button size="small" :loading="matching" @click="findObservations">查找见闻</el-button><el-button v-if="matchedObservations !== null" size="small" @click="activeView = 'observations'; loadRecords()">查看匹配见闻（{{ matchedObservations.length }}）</el-button></div>
                <p v-if="matchHint" class="memory-empty-sub">{{ matchHint }}</p><p v-if="matchError" class="kv-edit-error" role="alert">{{ matchError }}</p>
              </div>
            </div>
          </template>
        </section>
      </template>
    </div>
  </AppDialog>

</template>

<style scoped>
.memory-system-persona-note { margin-bottom: 18px; padding: 14px 16px; border: 1px solid var(--border-default); border-radius: 10px; background: var(--bg-surface); color: var(--text-secondary); font-size: 12px; line-height: 1.8; }
.memory-system-persona-note strong { color: var(--text-primary); font-weight: 600; }
.memory-system-persona-note p { margin: 5px 0 0; color: var(--text-tertiary); }
.memory-draft-note { display: flex; align-items: center; gap: 12px; padding: 8px 20px; border-bottom: 1px solid var(--border-1); color: var(--text-2); font-size: 12px; }
.memory-draft-tag { color: var(--text-3); border: 1px solid var(--border-1); border-radius: 4px; padding: 1px 4px; font-size: 10px; }

.memory-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 16px;
  padding-right: 8px;
  flex-wrap: wrap;
}
.memory-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.memory-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}
.memory-counts {
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 400;
  margin-left: 4px;
}
.memory-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.memory-tabs {
  display: inline-flex;
  flex-wrap: wrap;
  max-width: 100%;
  align-items: center;
  gap: 2px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-2);
  border-radius: 999px;
  padding: 2px;
  flex-shrink: 1;
}
.memory-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: none;
  background: transparent;
  color: var(--text-3);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  padding: 3px 12px;
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
  white-space: nowrap;
}
.memory-tab:hover { color: var(--text-1); }
.memory-tab.active {
  background: var(--bg-surface);
  color: var(--accent);
  box-shadow: var(--shadow-1);
}
.memory-tab-count {
  font-size: 10px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: 999px;
  padding: 0 6px;
  font-variant-numeric: tabular-nums;
}

.memory-body {
  display: flex;
  flex: 1;
  min-height: 0;
  border-top: 1px solid var(--border-1);
  border-bottom: 1px solid var(--border-1);
}

/* ---- 左列表 ---- */
.memory-list {
  flex: 0 0 340px;
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-1);
  min-width: 0;
  overflow-y: auto;
  padding: 12px;
}
.memory-root-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--tag-warning) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--tag-warning) 25%, transparent);
  margin-bottom: 8px;
}
.memory-root-dot {
  width: 12px;
  height: 12px;
  min-width: 12px;
  border-radius: 50%;
  background: var(--tag-warning);
  box-shadow: 0 0 8px color-mix(in srgb, var(--tag-warning) 50%, transparent);
}
.memory-root-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.memory-root-id {
  font-family: var(--font-mono);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-1);
}
.memory-root-note {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--text-3);
}

.memory-groups { display: flex; flex-direction: column; gap: 4px; }
.memory-group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: none;
  background: transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  color: inherit;
  font: inherit;
  text-align: left;
  transition: background 0.12s;
}
.memory-group-head:hover { background: var(--bg-subtle); }
.memory-group-chevron {
  color: var(--text-3);
  transition: transform 0.18s ease;
}
.memory-group-chevron.open { transform: rotate(90deg); }
.memory-group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-1);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.memory-group-count {
  font-size: 11px;
  color: var(--text-3);
  background: var(--bg-subtle);
  border-radius: 999px;
  padding: 1px 8px;
  font-variant-numeric: tabular-nums;
}
.memory-group-dot {
  width: 9px;
  height: 9px;
  min-width: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}
.memory-scenario-tag {
  font-size: 10px;
  font-weight: 600;
  border-radius: 999px;
  padding: 1px 7px;
  white-space: nowrap;
  flex-shrink: 0;
}
.memory-scenario-tag.speech { background: color-mix(in srgb, var(--tag-info) 15%, transparent); color: var(--tag-info); }
.memory-scenario-tag.decision { background: color-mix(in srgb, var(--tag-special) 15%, transparent); color: var(--tag-special); }
.memory-scenario-tag.other { background: color-mix(in srgb, var(--tag-special) 15%, transparent); color: var(--tag-special); }
.memory-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 6px 8px;
  margin-top: 2px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.memory-item:hover { background: var(--bg-subtle); }
.memory-item.active {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.memory-item-dot {
  width: 9px;
  height: 9px;
  min-width: 9px;
  border-radius: 50%;
}
.memory-item-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.memory-item-label {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  font-size: 13px;
}
.memory-item-name {
  color: var(--text-1);
  font-weight: 500;
  flex-shrink: 0;
  white-space: nowrap;
}
.memory-item-alias {
  color: var(--text-3);
  font-weight: 400;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.memory-item-sub {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 1px;
}
.memory-item-count {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  color: var(--text-3);
  background: var(--bg-subtle);
  border-radius: 999px;
  padding: 1px 7px;
  font-variant-numeric: tabular-nums;
}
.memory-item-stats {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex-shrink: 0;
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  line-height: 1.4;
}
.memory-stat {
  display: inline-flex;
  align-items: baseline;
  gap: 2px;
  white-space: nowrap;
}
.memory-stat-num {
  font-size: 12px;
  font-weight: 700;
}
.memory-stat.is-maintain .memory-stat-num { color: var(--tag-warning); }
.memory-stat.is-retrieval .memory-stat-num { color: var(--accent); }

/* ---- 空态 / 错误态 ---- */
.memory-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: var(--text-3);
  padding: 30px 12px;
  gap: 4px;
  font-size: 12.5px;
}
.memory-empty.error { color: var(--danger); }
.memory-empty-title { font-size: 13px; font-weight: 600; color: var(--text-1); }
.memory-empty-sub { font-size: 12px; margin-top: 2px; }

/* ---- 右详情 ---- */
.memory-detail {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  background: var(--bg-app);
  min-width: 0;
}
.memory-detail-empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
}
.memory-detail-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border-1);
  flex-wrap: wrap;
}
.memory-detail-delete {
  margin-left: auto;
}
.memory-type-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  background: var(--accent-soft);
  color: var(--accent);
}
.memory-type-badge.group { background: color-mix(in srgb, var(--tag-success) 15%, transparent); color: var(--tag-success); }
.memory-type-badge.fuzzyrole { background: color-mix(in srgb, var(--tag-special) 15%, transparent); color: var(--tag-special); }
.memory-type-badge.root { background: color-mix(in srgb, var(--tag-warning) 15%, transparent); color: var(--tag-warning); }
.memory-detail-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
  word-break: break-all;
}
.memory-detail-body {
  flex: 1 1 auto;
  padding: 16px 18px;
  overflow-y: auto;
}
.kv {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 6px 12px;
  margin: 0 0 14px;
}
.kv dt {
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  align-self: start;
}
.kv dd {
  font-size: 12.5px;
  color: var(--text-1);
  margin: 0;
  word-break: break-all;
  font-variant-numeric: tabular-nums;
}
.kv dd.kv-multiline { white-space: pre-wrap; line-height: 1.5; }
.kv dd code {
  font-size: 11px;
  font-family: var(--font-mono);
  background: var(--bg-subtle);
  padding: 1px 6px;
  border-radius: 4px;
}

.detail-related { margin-top: 4px; }
.kv-label {
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 6px;
}
.kv-empty { font-size: 12px; color: var(--text-3); }
.related-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.related-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 9px;
  border-radius: 999px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-2);
  font-size: 12px;
  color: var(--text-1);
}
.related-chip.clickable { cursor: pointer; }
.related-chip.clickable:hover { border-color: var(--accent); }
.related-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.related-type {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--text-3);
  letter-spacing: 0.03em;
}
.related-name { word-break: break-all; }

.memory-edit-tip { font-size: 11px; color: var(--text-3); font-style: italic; margin-left: 4px; }
.memory-match-controls { padding: 10px 0; }
.obs-filter-row { display: flex; gap: 6px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
.kv-edit-actions { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.kv-edit-error { color: var(--tag-danger); font-size: 12px; margin-top: 8px; overflow-wrap: anywhere; }
@media (max-width: 760px) {
  .memory-body { flex-direction: column; }
  .memory-list { flex: 0 0 32%; width: auto; border-right: 0; border-bottom: 1px solid var(--border-1); }
  .memory-detail { min-height: 0; }
  .kv { grid-template-columns: 90px minmax(0, 1fr); }
}
/* Keep header controls clear of the shared close button. */
.memory-header, .memory-header-left, .memory-header-right { flex-wrap: wrap; min-width: 0; gap: 8px; }
</style>
