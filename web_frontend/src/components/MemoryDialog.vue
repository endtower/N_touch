<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
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
  EditPen,
} from '@element-plus/icons-vue'
import type {
  SocialNode,
  SocialContent,
  SocialRelationship,
  ExperienceItem,
  MemorySocialUpdateRequest,
  ExperienceUpdateRequest,
} from '../types'
import { chat } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

// =====================================================================
// 状态
// =====================================================================

const searchKeyword = ref('')
const selectedId = ref<string | null>(null)
const collapsedGroups = ref<Record<string, boolean>>({})

// 视图切换: 社交实体 / 经验
const activeView = ref<'social' | 'experience'>('social')
// 经验视图状态
const selectedExpId = ref<string | null>(null)
const expCollapsed = ref<Record<string, boolean>>({})

const snap = computed(() => chat.state.memorySocialSnapshot)

// =====================================================================
// 分组列表
// =====================================================================

interface GroupDef {
  key: SocialNode['type']
  title: string
  icon: any
  color: string
}

const GROUP_DEFS: GroupDef[] = [
  { key: 'Character', title: '角色', icon: User, color: '#6366f1' },
  { key: 'Group', title: '群组', icon: OfficeBuilding, color: '#14b8a6' },
  { key: 'FuzzyRole', title: '模糊角色', icon: UserFilled, color: '#a855f7' },
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
  value: string
  multiline?: boolean
}

const editingField = ref<string | null>(null)
const editText = ref('')
const editTags = ref<string[]>([])
const inlineSavingField = ref<string | null>(null)
const editError = ref('')

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
  if (n.type === 'Character') {
    return [
      { key: 'name', label: 'name', value: n.name ?? '' },
      { key: 'aliases', label: '别名', value: (n.aliases ?? []).map(a => a.name).filter(Boolean).join('、') },
      { key: 'impression', label: '印象', value: firstContent(n.impressions), multiline: true },
      { key: 'familiarity', label: '熟悉度', value: firstContent(n.familiarities) },
      { key: 'relationship', label: '与 00000000 关系', value: firstType(n.relationships_with_default) },
    ]
  }
  if (n.type === 'Group') {
    return [
      { key: 'name', label: 'name', value: n.name ?? '' },
      { key: 'aliases', label: '别名', value: (n.aliases ?? []).map(a => a.name).filter(Boolean).join('、') },
      { key: 'impression', label: '印象', value: firstContent(n.impressions), multiline: true },
      { key: 'familiarity', label: '熟悉度', value: firstContent(n.familiarities) },
      { key: 'member_impression', label: '成员印象', value: firstContent(n.member_impressions), multiline: true },
      { key: 'self_image', label: '群组自我形象', value: firstType(n.relationships_with_default) },
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

function beginEdit(key: string) {
  const n = selectedNode.value
  if (!n) return
  editingField.value = key
  editError.value = ''
  if (key === 'name') editText.value = n.name ?? ''
  else if (key === 'aliases') editTags.value = (n.aliases ?? []).map(a => a.name).filter(Boolean)
  else if (key === 'impression') editText.value = firstContent(n.impressions)
  else if (key === 'familiarity') editText.value = firstContent(n.familiarities)
  else if (key === 'relationship') editText.value = firstType(n.relationships_with_default)
  else if (key === 'member_impression') editText.value = firstContent(n.member_impressions)
  else if (key === 'self_image') editText.value = firstType(n.relationships_with_default)
  else if (key === 'content') editText.value = n.content ?? ''
}

function cancelEdit() {
  editingField.value = null
  editError.value = ''
}

function onInlineEnter(key: string) {
  const row = editRows.value.find(r => r.key === key)
  if (row?.multiline) return // 文本域回车换行，不触发保存
  saveInline(key)
}

async function saveInline(key: string) {
  const n = selectedNode.value
  if (!n) return
  const entityType =
    n.type === 'Character' ? 'character' : n.type === 'Group' ? 'group' : 'fuzzy_role'
  const p: MemorySocialUpdateRequest = {}
  if (key === 'name') {
    const v = editText.value.trim()
    if (!v) {
      editError.value = '名称不能为空'
      return
    }
    p.name = v
  } else if (key === 'aliases') {
    p.aliases = editTags.value.map(x => x.trim()).filter(Boolean)
  } else if (key === 'impression') p.impression = editText.value.trim()
  else if (key === 'familiarity') p.familiarity = editText.value.trim()
  else if (key === 'relationship') p.relationship = editText.value.trim()
  else if (key === 'member_impression') p.member_impression = editText.value.trim()
  else if (key === 'self_image') p.self_image = editText.value.trim()
  else if (key === 'content') p.content = editText.value.trim()

  inlineSavingField.value = key
  editError.value = ''
  try {
    await chat.updateSocialMemory(entityType, n.uuid, p)
    editingField.value = null
    ElMessage.success('已保存')
  } catch (e: any) {
    editError.value = e?.message || String(e)
  } finally {
    inlineSavingField.value = null
  }
}

// ---- 经验视图 ----
const expList = computed(() => chat.state.memoryExperiences?.experiences ?? [])

const filteredExperiences = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return expList.value
  return expList.value.filter(e =>
    (e.title || '').toLowerCase().includes(kw) ||
    (e.summary || '').toLowerCase().includes(kw) ||
    (e.apply_scenario || '').toLowerCase().includes(kw),
  )
})

const EXP_GROUPS = [
  { key: 'decision', title: '决策阶段', color: '#6366f1' },
  { key: 'execution', title: '执行阶段', color: '#14b8a6' },
  { key: 'other', title: '其他', color: '#a855f7' },
]

const expGroups = computed<{ def: typeof EXP_GROUPS[number]; items: ExperienceItem[] }[]>(() => {
  return EXP_GROUPS
    .map(def => ({
      def,
      items: filteredExperiences.value.filter(e =>
        def.key === 'other'
          ? !['decision', 'execution'].includes(e.apply_scenario)
          : e.apply_scenario === def.key,
      ),
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

function scenarioName(s: string): string {
  if (s === 'decision') return '决策阶段'
  if (s === 'execution') return '执行阶段'
  return s || '未指定'
}

function scenarioColor(s: string): string {
  if (s === 'decision') return '#6366f1'
  if (s === 'execution') return '#14b8a6'
  return '#a855f7'
}

function scenarioBadgeClass(s: string): string {
  if (s === 'decision') return 'decision'
  if (s === 'execution') return 'execution'
  return 'other'
}

// ---- 经验/嘱托内联编辑 ----
const expEditField = ref<string | null>(null)
const expEditText = ref('')
const expSavingField = ref<string | null>(null)
const expEditError = ref('')

function expBeginEdit(key: 'title' | 'summary' | 'apply_scenario') {
  const e = selectedExp.value
  if (!e) return
  expEditField.value = key
  expEditError.value = ''
  if (key === 'title') expEditText.value = e.title ?? ''
  else if (key === 'summary') expEditText.value = e.summary ?? ''
  else if (key === 'apply_scenario') expEditText.value = e.apply_scenario ?? ''
}

function expCancelEdit() {
  expEditField.value = null
  expEditError.value = ''
}

async function expSave(key: 'title' | 'summary' | 'apply_scenario') {
  const e = selectedExp.value
  if (!e) return
  const p: ExperienceUpdateRequest = {}
  if (key === 'title') {
    const v = expEditText.value.trim()
    if (!v) {
      expEditError.value = '标题不能为空'
      return
    }
    p.title = v
  } else if (key === 'summary') p.summary = expEditText.value.trim()
  else if (key === 'apply_scenario') p.apply_scenario = expEditText.value.trim()

  expSavingField.value = key
  expEditError.value = ''
  try {
    await chat.updateExperience(e.bequest_id, p)
    expEditField.value = null
    ElMessage.success('已保存')
  } catch (err: any) {
    expEditError.value = err?.message || String(err)
  } finally {
    expSavingField.value = null
  }
}

// 切换到经验视图且无数据时自动拉取
watch(activeView, (v) => {
  if (v === 'experience' && chat.state.memoryExperiences === null && !chat.state.memoryExperiencesLoading) {
    chat.loadExperiences(200)
  }
})

async function reload() {
  clearSel()
  clearSelExp()
  if (activeView.value === 'experience') {
    await chat.loadExperiences(200)
  } else {
    await chat.loadMemorySocial(500)
  }
}

function switchView(v: 'social' | 'experience') {
  if (activeView.value === v) return
  activeView.value = v
  clearSel()
  clearSelExp()
}

// 每次打开对话框时按当前视图拉取数据
watch(() => props.visible, (v) => {
  if (v) reload()
})

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
    case 'Group': return '#14b8a6'
    case 'FuzzyRole': return '#a855f7'
    case 'Root': return '#ec4899'
    default: return '#6366f1'
  }
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :width="'86vw'"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
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
          </div>
          <span v-if="activeView === 'social' && snap" class="memory-counts">
            {{ snap.character_count }} 角色 · {{ snap.group_count }} 群组 ·
            {{ snap.fuzzy_role_count }} 模糊角色 · {{ snap.edges.length }} 关系
          </span>
          <span v-else-if="activeView === 'experience'" class="memory-counts">{{ expTotal }} 条经验</span>
        </div>
        <div class="memory-header-right">
          <el-input
            v-model="searchKeyword"
            size="small"
            :placeholder="activeView === 'experience' ? '搜索经验…' : '搜索角色 / 群组 / 模糊角色…'"
            clearable
            style="width: 220px;"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :icon="Refresh" size="small" @click="reload">刷新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

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
                      <span class="memory-item-name">{{ n.label }}</span>
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
          </div>

          <div class="memory-detail-body">
            <dl class="kv">
              <dt>uuid</dt><dd><code>{{ selectedNode.uuid }}</code></dd>

              <template v-for="row in editRows" :key="row.key">
                <dt>{{ row.label }}</dt>
                <dd>
                  <template v-if="editingField === row.key">
                    <template v-if="row.key === 'aliases'">
                      <el-select
                        v-model="editTags"
                        multiple
                        filterable
                        allow-create
                        default-first-option
                        :reserve-keyword="false"
                        size="small"
                        placeholder="输入别名后回车，标签右侧 × 删除"
                        style="width: 100%;"
                      />
                    </template>
                    <template v-else>
                      <el-input
                        v-model="editText"
                        size="small"
                        :type="row.multiline ? 'textarea' : 'text'"
                        :rows="row.multiline ? 3 : 1"
                        :autosize="row.multiline ? { minRows: 2, maxRows: 8 } : false"
                        @keyup.enter="onInlineEnter(row.key)"
                        @keyup.esc="cancelEdit"
                      />
                    </template>
                    <div class="kv-edit-actions">
                      <el-button size="small" type="primary" :loading="inlineSavingField === row.key" @click="saveInline(row.key)">保存</el-button>
                      <el-button size="small" @click="cancelEdit">取消</el-button>
                    </div>
                    <div v-if="editError" class="kv-edit-error">{{ editError }}</div>
                  </template>
                  <template v-else>
                    <span class="kv-value" :class="{ empty: !row.value }" title="点击修改" @click="beginEdit(row.key)">
                      {{ row.value || '—' }}
                      <el-icon class="kv-edit-icon"><EditPen /></el-icon>
                    </span>
                  </template>
                </dd>
              </template>

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

      <!-- 经验视图 -->
      <template v-else>
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
                  <span class="memory-item-dot" :style="{ background: g.def.color }"></span>
                  <div class="memory-item-main">
                    <div class="memory-item-label">
                      <span class="memory-item-name">{{ e.title || '（未命名经验）' }}</span>
                      <span class="memory-scenario-tag" :class="scenarioBadgeClass(e.apply_scenario)">{{ scenarioName(e.apply_scenario) }}</span>
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
              <span class="memory-type-badge" :class="scenarioBadgeClass(selectedExp.apply_scenario)">{{ scenarioName(selectedExp.apply_scenario) }}</span>
              <span class="memory-detail-name">{{ selectedExp.title || '（未命名经验）' }}</span>
              <span class="memory-edit-tip">点击字段值即可修改</span>
            </div>
            <div class="memory-detail-body">
              <dl class="kv">
                <dt>bequest_id</dt><dd><code>{{ selectedExp.bequest_id }}</code></dd>

                <dt>标题</dt>
                <dd>
                  <template v-if="expEditField === 'title'">
                    <el-input
                      v-model="expEditText"
                      size="small"
                      @keyup.enter="expSave('title')"
                      @keyup.esc="expCancelEdit"
                    />
                    <div class="kv-edit-actions">
                      <el-button size="small" type="primary" :loading="expSavingField === 'title'" @click="expSave('title')">保存</el-button>
                      <el-button size="small" @click="expCancelEdit">取消</el-button>
                    </div>
                    <div v-if="expEditError" class="kv-edit-error">{{ expEditError }}</div>
                  </template>
                  <template v-else>
                    <span class="kv-value" :class="{ empty: !selectedExp.title }" title="点击修改" @click="expBeginEdit('title')">
                      {{ selectedExp.title || '—' }}
                      <el-icon class="kv-edit-icon"><EditPen /></el-icon>
                    </span>
                  </template>
                </dd>

                <dt>摘要</dt>
                <dd class="kv-multiline">
                  <template v-if="expEditField === 'summary'">
                    <el-input
                      v-model="expEditText"
                      type="textarea"
                      :rows="3"
                      :autosize="{ minRows: 2, maxRows: 8 }"
                      @keyup.esc="expCancelEdit"
                    />
                    <div class="kv-edit-actions">
                      <el-button size="small" type="primary" :loading="expSavingField === 'summary'" @click="expSave('summary')">保存</el-button>
                      <el-button size="small" @click="expCancelEdit">取消</el-button>
                    </div>
                    <div v-if="expEditError" class="kv-edit-error">{{ expEditError }}</div>
                  </template>
                  <template v-else>
                    <span class="kv-value" :class="{ empty: !selectedExp.summary }" title="点击修改" @click="expBeginEdit('summary')">
                      {{ selectedExp.summary || '—' }}
                      <el-icon class="kv-edit-icon"><EditPen /></el-icon>
                    </span>
                  </template>
                </dd>

                <dt>应用场景</dt>
                <dd>
                  <template v-if="expEditField === 'apply_scenario'">
                    <el-input
                      v-model="expEditText"
                      size="small"
                      placeholder="decision / execution / 其他"
                      @keyup.enter="expSave('apply_scenario')"
                      @keyup.esc="expCancelEdit"
                    />
                    <div class="kv-edit-actions">
                      <el-button size="small" type="primary" :loading="expSavingField === 'apply_scenario'" @click="expSave('apply_scenario')">保存</el-button>
                      <el-button size="small" @click="expCancelEdit">取消</el-button>
                    </div>
                    <div v-if="expEditError" class="kv-edit-error">{{ expEditError }}</div>
                  </template>
                  <template v-else>
                    <span class="kv-value" :class="{ empty: !selectedExp.apply_scenario }" title="点击修改" @click="expBeginEdit('apply_scenario')">
                      {{ selectedExp.apply_scenario || '—' }}
                      <el-icon class="kv-edit-icon"><EditPen /></el-icon>
                    </span>
                  </template>
                </dd>

                <dt>开始时间</dt><dd>{{ timeShort(selectedExp.start_time) }}</dd>
                <dt>更新时间</dt><dd>{{ timeShort(selectedExp.updated_at) }}</dd>
              </dl>
            </div>
          </template>
        </section>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
.memory-dialog :deep(.el-dialog__header) { padding: 14px 20px !important; }
.memory-dialog :deep(.el-dialog__body) { padding: 0 !important; }

.memory-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 16px;
  padding-right: 8px;
}
.memory-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
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
}
.memory-tabs {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-2);
  border-radius: 999px;
  padding: 2px;
  flex-shrink: 0;
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
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
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
  height: 78vh;
  min-height: 560px;
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
  background: rgba(236, 72, 153, 0.08);
  border: 1px solid rgba(236, 72, 153, 0.25);
  margin-bottom: 8px;
}
.memory-root-dot {
  width: 12px;
  height: 12px;
  min-width: 12px;
  border-radius: 50%;
  background: #ec4899;
  box-shadow: 0 0 8px rgba(236, 72, 153, 0.5);
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
.memory-scenario-tag.decision { background: rgba(99, 102, 241, 0.15); color: #6366f1; }
.memory-scenario-tag.execution { background: rgba(20, 184, 166, 0.15); color: #14b8a6; }
.memory-scenario-tag.other { background: rgba(168, 85, 247, 0.15); color: #a855f7; }
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
.memory-type-badge.group { background: rgba(20, 184, 166, 0.15); color: #14b8a6; }
.memory-type-badge.fuzzyrole { background: rgba(168, 85, 247, 0.15); color: #a855f7; }
.memory-type-badge.root { background: rgba(236, 72, 153, 0.15); color: #ec4899; }
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

/* ---- 内联编辑 ---- */
.memory-edit-tip {
  font-size: 11px;
  color: var(--text-3);
  font-style: italic;
  margin-left: 4px;
}
.kv-value {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
  border-radius: 5px;
  padding: 1px 5px;
  margin-left: -5px;
  transition: background 0.12s;
  min-height: 20px;
}
.kv-value:hover { background: var(--bg-subtle); }
.kv-value.empty { color: var(--text-3); font-style: italic; }
.kv-edit-icon {
  opacity: 0;
  transition: opacity 0.12s;
  color: var(--text-3);
  flex-shrink: 0;
}
.kv-value:hover .kv-edit-icon { opacity: 1; }
.kv-edit-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.kv-edit-error {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: var(--danger);
  font-size: 11.5px;
  padding: 5px 9px;
  border-radius: 6px;
  margin-top: 6px;
  word-break: break-all;
}
</style>
