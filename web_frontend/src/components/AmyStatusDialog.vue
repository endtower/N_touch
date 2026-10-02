<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { MagicStick, Refresh, ChatLineSquare, Sunny, Lightning, Clock, Plus, Edit, Delete, Star } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { AmyState, AmyThought, AmyActEntry, AmyFinalEvaluationEntry, AmyDescriptionBand, NotesResponse } from '../types'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

const data = ref<AmyState | null>(null)
const loading = ref(false)
const lastFetchedAt = ref<number>(0)
let pollTimer: number | null = null

// ===== 记事本笔记（PFCA 视角手册 + 全部未归档条目） =====
const notes = ref<NotesResponse | null>(null)
const notesLoading = ref(false)
const notesOpen = ref<number[]>([])

// ===== 想法池编辑（调试/测试） =====
const createDialogVisible = ref(false)
const createForm = ref({ act: '', desc: '', intensity: 1.0, duration: 300 })
const editDialogVisible = ref(false)
const editTarget = ref<AmyThought | null>(null)
const editForm = ref({ desc: '', act: '', intensity: 1.0, duration: 300 })
const saving = ref(false)

// ===== 最终评价登记（自我评价记录 + 想法入池） =====
const finalEvalDialogVisible = ref(false)
const finalEvalForm = ref({ name: '', title: '' })

watch(
  () => props.visible,
  (v) => {
    if (v) {
      fetchData()
      fetchNotes()
      startPolling()
    } else {
      stopPolling()
    }
  },
)

onBeforeUnmount(stopPolling)

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(() => {
    if (props.visible) {
      fetchData(true)
      fetchNotes(true)
    }
  }, 3000)
}
function stopPolling() {
  if (pollTimer != null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function fetchData(silent = false) {
  loading.value = !silent
  try {
    const res = await fetch('/amy/state')
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    data.value = (await res.json()) as AmyState
    lastFetchedAt.value = data.value.fetched_at || Date.now() / 1000
  } catch (e: any) {
    if (!silent) ElMessage.error(`加载 Amy 状态失败: ${e?.message || e}`)
  } finally {
    loading.value = false
  }
}

async function fetchNotes(silent = false) {
  notesLoading.value = !silent
  try {
    const res = await fetch('/notes')
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    notes.value = (await res.json()) as NotesResponse
  } catch (e: any) {
    if (!silent) ElMessage.error(`加载记事本笔记失败: ${e?.message || e}`)
  } finally {
    notesLoading.value = false
  }
}

async function apiJson(url: string, opts: RequestInit) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts })
  let body: any = null
  try { body = await res.json() } catch { /* 忽略非 JSON 响应 */ }
  if (!res.ok) throw new Error(body?.error || `HTTP ${res.status}`)
  return body
}

/** 可编辑的 act 候选（排除系统内置类，仅编辑对话框用） */
const allActNames = computed(() => {
  if (!data.value) return [] as string[]
  const names: string[] = []
  for (const cat of ['external', 'evaluation', 'self_action'] as const) {
    for (const e of data.value.acts[cat]) names.push(e.name)
  }
  return names
})

function openCreateWithAct(act: string) {
  createForm.value = { act, desc: '', intensity: 1.0, duration: 300 }
  createDialogVisible.value = true
}

/** 最终评价登记（自我评价记录 + 想法入池）：弹标题输入 → POST /amy/final-evaluation */
function openFinalEval(name: string) {
  finalEvalForm.value = { name, title: '' }
  finalEvalDialogVisible.value = true
}

async function saveFinalEval() {
  const title = finalEvalForm.value.title.trim()
  if (!title) {
    ElMessage.warning('请输入本次事件简易标题（≤15 字）')
    return
  }
  if (title.length > 15) {
    ElMessage.warning('标题不能超过 15 字')
    return
  }
  saving.value = true
  try {
    await apiJson('/amy/final-evaluation', {
      method: 'POST',
      body: JSON.stringify({ final_evaluation: finalEvalForm.value.name, title }),
    })
    ElMessage.success('已登记最终评价（想法入池）')
    finalEvalDialogVisible.value = false
    await fetchData()
  } catch (e: any) {
    ElMessage.error(`登记失败: ${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

async function saveCreate() {
  if (!createForm.value.act || !createForm.value.desc.trim()) {
    ElMessage.warning('act 与描述必填')
    return
  }
  saving.value = true
  try {
    await apiJson('/amy/thoughts', {
      method: 'POST',
      body: JSON.stringify({ ...createForm.value, desc: createForm.value.desc.trim() }),
    })
    ElMessage.success('已新增 thought')
    createDialogVisible.value = false
    await fetchData()
  } catch (e: any) {
    ElMessage.error(`新增失败: ${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

function openEdit(t: AmyThought) {
  editTarget.value = t
  editForm.value = {
    desc: t.desc,
    act: t.act,
    intensity: t.intensity,
    duration: Math.max(1, t.remaining_seconds),
  }
  editDialogVisible.value = true
}

async function saveUpdate() {
  if (!editTarget.value) return
  if (!editForm.value.desc.trim()) {
    ElMessage.warning('描述不能为空')
    return
  }
  saving.value = true
  try {
    const body: Record<string, unknown> = {
      desc: editForm.value.desc.trim(),
      act: editForm.value.act === editTarget.value.act ? undefined : editForm.value.act,
      intensity: editForm.value.intensity,
      duration: editForm.value.duration || undefined,
    }
    await apiJson(`/amy/thoughts/${editTarget.value.id}`, { method: 'PATCH', body: JSON.stringify(body) })
    ElMessage.success('已更新 thought')
    editDialogVisible.value = false
    await fetchData()
  } catch (e: any) {
    ElMessage.error(`更新失败: ${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

async function removeThought(t: AmyThought) {
  try {
    await ElMessageBox.confirm(`删除 thought「${t.desc.slice(0, 30)}…」？删除后立即生效（system 类会在重启时恢复）。`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await apiJson(`/amy/thoughts/${t.id}`, { method: 'DELETE' })
    ElMessage.success('已删除 thought')
    await fetchData()
  } catch (e: any) {
    ElMessage.error(`删除失败: ${e?.message || e}`)
  }
}

function close() {
  emit('update:visible', false)
}

// ===== 视觉辅助 =====

function timeShort(ts?: number | null) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// ===== 记事本笔记显示辅助 =====
// 后端 ISO 时间 (2026-09-20T17:06:05.123) -> "2026-09-20 17:06:05"
function noteTime(ts?: string) {
  if (!ts) return '—'
  return ts.replace('T', ' ').slice(0, 19)
}
function noteSourceLabel(src: string) {
  switch (src) {
    case 'reflection':  return '反思'
    case 'conversation': return '对话'
    case 'social_chat': return '社交'
    case 'pfca':        return 'PFCA'
    default:            return src || '—'
  }
}
function durationShort(sec: number) {
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
  return `${Math.floor(sec / 86400)}d${Math.floor((sec % 86400) / 3600)}h`
}

// 心情数值 -> 视觉化 (进度条 + 色阶)
// Amy 描述带都在 0-100 区间, 用户视角也只看"激活强度".
// 用 Math.abs() 把 -100..+100 映射到 0..100, 然后按强度上色.
function moodTone(v: number) {
  const abs = Math.abs(v)
  if (abs >= 60) return { color: 'var(--danger)', bg: 'rgba(239, 68, 68, 0.18)', label: '极度' }
  if (abs >= 40) return { color: 'var(--warn)',    bg: 'rgba(245, 158, 11, 0.18)', label: '偏强' }
  if (abs >= 20) return { color: 'var(--accent-2)', bg: 'rgba(52, 211, 153, 0.18)', label: '正向' }
  if (abs >= 10) return { color: 'var(--text-2)', bg: 'var(--bg-subtle)', label: '微弱' }
  return { color: 'var(--text-3)', bg: 'var(--bg-subtle)', label: '平静' }
}

function actCategoryLabel(c: AmyThought['category'] | string) {
  switch (c) {
    case 'external':    return { label: '外部互动', color: 'var(--accent-2)' }
    case 'evaluation':   return { label: '评价',     color: 'var(--accent)' }
    case 'self_action':  return { label: '自身行为', color: 'var(--warn)' }
    case 'system':       return { label: '系统',     color: 'var(--text-3)' }
    case 'final':        return { label: '最终评价', color: '#a78bfa' }
    default:             return { label: '未知',     color: 'var(--text-3)' }
  }
}

function bandTextFor(value: number, bands: AmyDescriptionBand[] | undefined) {
  if (!bands || !bands.length) return null
  // Amy 描述带都定义在 0-100 区间 (10..90), 但底层 NE/DA 实际可以是 -100..+100.
  // 用户视角: NE/DA 表示"激活强度", 用绝对值匹配描述带.
  const v = Math.abs(value)
  const b = bands.find(b => v >= b.lo && v < b.hi)
  return b || bands[0] || null
}

// 计算每个 thought 对当前 mood 的实际贡献 (用后端给的 act 表)
const ACT_INDEX = computed(() => {
  if (!data.value) return new Map<string, AmyActEntry>()
  // 只索引参与 mood 的常规 ACT；final 词表不产生 NE/DA 增量, 不进入索引
  const m = new Map<string, AmyActEntry>()
  for (const cat of ['external', 'evaluation', 'self_action', 'system'] as const) {
    for (const e of data.value.acts[cat]) m.set(e.name, e)
  }
  return m
})

function thoughtContribution(t: AmyThought): { ne: number; da: number } {
  const a = ACT_INDEX.value.get(t.act)
  if (!a) return { ne: 0, da: 0 }
  return {
    ne: +(t.intensity * a.ne_delta).toFixed(1),
    da: +(t.intensity * a.da_delta).toFixed(1),
  }
}

// 进度条 (单方向 0..100, 用 Math.abs 把 -100..+100 映射到 0..100)
const MOOD_RANGE = 100
function gaugeWidth(v: number) {
  return Math.min(Math.abs(v), MOOD_RANGE) / MOOD_RANGE * 100
}

// 存在感：0-100 直接显示（非 NE/DA 绝对值语义），默认 50
function presenceTone(v: number) {
  if (v >= 60) return { color: 'var(--accent-2)', bg: 'rgba(52, 211, 153, 0.18)', label: '充盈' }
  if (v >= 40) return { color: 'var(--warn)',    bg: 'rgba(245, 158, 11, 0.18)', label: '正常' }
  if (v >= 20) return { color: 'var(--danger)',  bg: 'rgba(239, 68, 68, 0.18)', label: '较低' }
  return { color: 'var(--danger)',                bg: 'rgba(239, 68, 68, 0.32)', label: '贫乏' }
}
function presenceWidth(v: number) { return Math.min(Math.max(v, 0), 100) }
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    width="90vw"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
    class="amy-dialog"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <template #header>
      <div class="amy-header">
        <div class="amy-title">
          <el-icon :size="18" color="var(--accent)"><MagicStick /></el-icon>
          <span>LLM 情绪状态</span>
          <span class="amy-sub">Amy · NE / DA · 想法池可编辑 · 3s 自动刷新</span>
        </div>
        <div class="amy-actions">
          <span class="amy-fetched">最后更新 {{ timeShort(lastFetchedAt) }}</span>
          <el-button :icon="Refresh" size="small" :loading="loading" @click="fetchData()">刷新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <div v-if="!data" class="amy-loading">{{ loading ? '加载中…' : '等待数据' }}</div>
    <div v-else-if="!data.initialized" class="amy-loading amy-error">
      Amy 尚未初始化: {{ data.error || 'system_core 还没就绪' }}
    </div>
    <div v-else class="amy-body">
      <!-- 顶部三联仪表 -->
      <section class="amy-gauge-row">
        <!-- NE 仪表 -->
        <div class="amy-gauge-card">
          <div class="amy-gauge-head">
            <span class="amy-gauge-label">
              <el-icon :size="13"><Lightning /></el-icon>
              去甲肾上腺素 NE
            </span>
            <span class="amy-gauge-value" :style="{ color: moodTone(data.mood.norepinephrine).color }">
              {{ Math.abs(data.mood.norepinephrine).toFixed(1) }}
            </span>
          </div>
          <div class="amy-gauge-bar">
            <div class="amy-gauge-track">
              <div
                class="amy-gauge-fill"
                :class="data.mood.norepinephrine >= 0 ? 'pos' : 'neg'"
                :style="{
                  width: gaugeWidth(data.mood.norepinephrine) + '%',
                  background: moodTone(data.mood.norepinephrine).color,
                }"
              />
            </div>
            <div class="amy-gauge-scale">
              <span>0</span><span>20</span><span>40</span><span>60</span><span>80</span><span>100</span>
            </div>
          </div>
          <div class="amy-gauge-band" v-if="bandTextFor(data.mood.norepinephrine, data.amy_descriptions?.norepinephrine)">
            <span class="amy-gauge-band-label">{{ moodTone(data.mood.norepinephrine).label }}</span>
            <span class="amy-gauge-band-text">{{ bandTextFor(data.mood.norepinephrine, data.amy_descriptions?.norepinephrine)?.text }}</span>
          </div>
        </div>

        <!-- DA 仪表 -->
        <div class="amy-gauge-card">
          <div class="amy-gauge-head">
            <span class="amy-gauge-label">
              <el-icon :size="13"><Sunny /></el-icon>
              多巴胺 DA
            </span>
            <span class="amy-gauge-value" :style="{ color: moodTone(data.mood.dopamine).color }">
              {{ Math.abs(data.mood.dopamine).toFixed(1) }}
            </span>
          </div>
          <div class="amy-gauge-bar">
            <div class="amy-gauge-track">
              <div
                class="amy-gauge-fill"
                :class="data.mood.dopamine >= 0 ? 'pos' : 'neg'"
                :style="{
                  width: gaugeWidth(data.mood.dopamine) + '%',
                  background: moodTone(data.mood.dopamine).color,
                }"
              />
            </div>
            <div class="amy-gauge-scale">
              <span>0</span><span>20</span><span>40</span><span>60</span><span>80</span><span>100</span>
            </div>
          </div>
          <div class="amy-gauge-band" v-if="bandTextFor(data.mood.dopamine, data.amy_descriptions?.dopamine)">
            <span class="amy-gauge-band-label">{{ moodTone(data.mood.dopamine).label }}</span>
            <span class="amy-gauge-band-text">{{ bandTextFor(data.mood.dopamine, data.amy_descriptions?.dopamine)?.text }}</span>
          </div>
        </div>

        <!-- 存在感仪表 -->
        <div class="amy-gauge-card" v-if="data.presence">
          <div class="amy-gauge-head">
            <span class="amy-gauge-label">
              <el-icon :size="13"><Star /></el-icon>
              存在感
            </span>
            <span class="amy-gauge-value" :style="{ color: presenceTone(data.presence.value).color }">
              {{ data.presence.value.toFixed(1) }}
            </span>
          </div>
          <div class="amy-gauge-bar">
            <div class="amy-gauge-track">
              <div
                class="amy-gauge-fill pos"
                :style="{
                  width: presenceWidth(data.presence.value) + '%',
                  background: presenceTone(data.presence.value).color,
                }"
              />
            </div>
            <div class="amy-gauge-scale">
              <span>0</span><span>25</span><span>50</span><span>75</span><span>100</span>
            </div>
          </div>
          <div class="amy-gauge-band">
            <span class="amy-gauge-band-label">{{ presenceTone(data.presence.value).label }}</span>
            <span class="amy-gauge-band-text">默认 50 · 优秀 +20 / 尚可 +10 / 无反馈 -10 / 欠佳 -20</span>
          </div>
        </div>

      </section>

      <!-- LLM 实际看到的描述 -->
      <section v-if="data.amy_description" class="amy-prompt-card">
        <div class="amy-section-head">
          <span><el-icon :size="13"><ChatLineSquare /></el-icon> LLM 当前看到的 prompt 描述</span>
        </div>
        <pre class="amy-prompt-body">{{ data.amy_description }}</pre>
      </section>

      <!-- 存在感归因 prompt 监控（注入 PFCA / 人格系统的原文） -->
      <section v-if="data.presence" class="amy-prompt-card">
        <div class="amy-section-head amy-section-head-ops">
          <span><el-icon :size="13"><Star /></el-icon> 存在感归因 prompt 监控（注入 PFCA / 人格系统）</span>
          <span v-if="data.presence.debts.length" class="amy-presence-count">
            {{ data.presence.debts.length }} 笔未偿 · 合计
            {{ data.presence.debts.reduce((s, d) => s + d.score, 0) }} 点
          </span>
        </div>
        <pre class="amy-prompt-body">{{ data.presence.attribution_prompt }}</pre>
        <div v-if="data.presence.debts.length" class="amy-presence-debts">
          <div v-for="(d, i) in data.presence.debts" :key="i" class="amy-presence-debt">
            <span class="amy-presence-debt-title">{{ d.title }}</span>
            <span class="amy-presence-debt-kind">{{ d.kind }}</span>
            <span class="amy-presence-debt-score neg">{{ d.score }}</span>
          </div>
        </div>
      </section>

      <!-- 活跃 thought 列表 -->
      <section class="amy-section">
        <div class="amy-section-head amy-section-head-ops">
          <span>活跃想法池 (active thoughts) · {{ data.thoughts.all_active_count }} 条</span>
          <span class="amy-add-hint"><el-icon :size="12"><Plus /></el-icon> 点击下方 ACT 表条目添加</span>
        </div>
        <div v-if="!data.thoughts.all_active.length" class="amy-empty">无活跃 thought</div>
        <div v-else class="amy-thought-grid">
          <div
            v-for="t in data.thoughts.all_active"
            :key="t.id"
            class="amy-thought-card"
            :style="{ borderLeftColor: actCategoryLabel(t.category).color }"
          >
            <div class="amy-thought-head">
              <span
                class="amy-cat-tag"
                :style="{ color: actCategoryLabel(t.category).color, borderColor: actCategoryLabel(t.category).color }"
              >{{ actCategoryLabel(t.category).label }}</span>
              <span class="amy-act-name">{{ t.act }}</span>
              <span class="amy-thought-remaining">
                <el-icon :size="11"><Clock /></el-icon>
                {{ durationShort(t.remaining_seconds) }}
              </span>
            </div>
            <div class="amy-thought-desc">{{ t.desc }}</div>
            <div class="amy-thought-foot">
              <span>
                强度 <strong>{{ t.intensity.toFixed(2) }}</strong>
                <span class="amy-contrib" :class="thoughtContribution(t).ne >= 0 ? 'pos' : 'neg'">
                  NE {{ thoughtContribution(t).ne >= 0 ? '+' : '' }}{{ thoughtContribution(t).ne }}
                </span>
                <span class="amy-contrib" :class="thoughtContribution(t).da >= 0 ? 'pos' : 'neg'">
                  DA {{ thoughtContribution(t).da >= 0 ? '+' : '' }}{{ thoughtContribution(t).da }}
                </span>
              </span>
              <span class="amy-thought-ops">
                <el-button v-if="t.category !== 'system'" :icon="Edit" size="small" text type="primary" @click="openEdit(t)">编辑</el-button>
                <el-button :icon="Delete" size="small" text type="danger" @click="removeThought(t)">删除</el-button>
                <span class="amy-thought-time">{{ timeShort(t.create_time) }}</span>
              </span>
            </div>
          </div>
        </div>
      </section>

      <!-- ACT 表 -->
      <section class="amy-section">
        <div class="amy-section-head">
          <span>ACT 表 (全部注册的 act)</span>
        </div>
        <div class="amy-act-list">
          <template v-for="(items, cat) in data.acts" :key="cat">
            <div v-if="items.length" class="amy-act-cat">
              <div class="amy-act-cat-head" :style="{ color: actCategoryLabel(cat as string).color }">
                {{ actCategoryLabel(cat as string).label }} · {{ items.length }} 条
              </div>
              <!-- 最终评价词表（PFCA 收尾）：无 NE/DA 增量，仅登记为想法；点击行 = 添加为想法 -->
              <div v-if="cat === 'final'">
                <div
                  v-for="f in items as AmyFinalEvaluationEntry[]"
                  :key="f.name"
                  class="amy-act-row is-addable"
                  @click="openCreateWithAct(f.name)"
                >
                  <span class="amy-act-name">
                    {{ f.name }}
                    <el-icon class="amy-act-add-ico" :size="12"><Plus /></el-icon>
                  </span>
                </div>
              </div>
              <!-- 常规 ACT（NE/DA 增量 + 堆积/时长）：点击行 = 添加为想法（system 类除外） -->
              <div v-else>
                <div
                  v-for="a in items as AmyActEntry[]"
                  :key="a.name"
                  class="amy-act-row"
                  :class="{ 'is-addable': cat !== 'system' }"
                  @click="cat !== 'system' && openCreateWithAct(a.name)"
                >
                  <span class="amy-act-name">
                    {{ a.name }}
                    <el-icon v-if="cat !== 'system'" class="amy-act-add-ico" :size="12"><Plus /></el-icon>
                  </span>
                  <span class="amy-act-stats">
                    <span class="amy-act-delta" :class="a.ne_delta >= 0 ? 'pos' : 'neg'">
                      NE {{ a.ne_delta >= 0 ? '+' : '' }}{{ a.ne_delta }}
                    </span>
                    <span class="amy-act-delta" :class="a.da_delta >= 0 ? 'pos' : 'neg'">
                      DA {{ a.da_delta >= 0 ? '+' : '' }}{{ a.da_delta }}
                    </span>
                    <span class="amy-act-meta">max {{ a.max_stack }} · {{ durationShort(a.duration) }}</span>
                  </span>
                </div>
              </div>
            </div>
          </template>
        </div>
      </section>

      <!-- 记事本笔记：PFCA 当前可见手册 + 全部未归档条目 -->
      <section class="amy-section">
        <div class="amy-section-head amy-section-head-ops">
          <span>记事本笔记 · PFCA 视角 / 未归档条目</span>
          <span v-if="notes" class="amy-notes-summary">
            未归档 {{ notes.all.length }} 篇 · 未处理 {{ notes.unprocessed_chars }} 字符
          </span>
        </div>
        <div v-if="notesLoading && !notes" class="amy-empty">加载中…</div>
        <div v-else-if="!notes" class="amy-empty">暂无数据</div>
        <div v-else class="amy-notes-wrap">
          <!-- PFCA 视角手册（render_manual 同源出口） -->
          <div class="amy-notes-block">
            <div class="amy-notes-block-title">
              PFCA 当前可见（render_manual · 1000 字符预算）
            </div>
            <pre v-if="notes.pfca_manual" class="amy-notes-manual">{{ notes.pfca_manual }}</pre>
            <div v-else class="amy-empty amy-empty-inline">PFCA 当前无可见笔记</div>
          </div>

          <!-- 全部未归档（活动池）笔记 -->
          <div class="amy-notes-block">
            <div class="amy-notes-block-title">全部未归档笔记 · {{ notes.all.length }} 篇</div>
            <div v-if="!notes.all.length" class="amy-empty amy-empty-inline">无未归档笔记</div>
            <el-collapse v-else v-model="notesOpen" class="amy-note-collapse">
              <el-collapse-item v-for="(doc, i) in notes.all" :key="doc.chain_id || i" :name="i">
                <template #title>
                  <div class="amy-note-title">
                    <span class="amy-note-name">{{ doc.title || '(无标题)' }}</span>
                    <span class="amy-note-meta">
                      {{ noteTime(doc.finalize_time) }} · {{ (doc.notes || []).length }} 条
                    </span>
                  </div>
                </template>
                <div class="amy-note-body">
                  <div v-if="doc.trigger" class="amy-note-line"><b>起因</b> {{ doc.trigger }}</div>
                  <div v-if="doc.summary" class="amy-note-line"><b>总结</b> {{ doc.summary }}</div>
                  <div v-if="doc.final_evaluation" class="amy-note-line"><b>最终评价</b> {{ doc.final_evaluation }}</div>
                  <div v-if="(doc.notes || []).length" class="amy-note-entries">
                    <div v-for="(n, j) in doc.notes" :key="n.note_id || j" class="amy-note-entry">
                      <span class="amy-note-src">{{ noteSourceLabel(n.source) }}</span>
                      <span class="amy-note-text">{{ n.text }}</span>
                    </div>
                  </div>
                  <div v-else class="amy-note-line amy-note-empty">该笔记无条目记录</div>
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </div>
      </section>
    </div>
  </el-dialog>

  <!-- 最终评价登记（自我评价记录 + 想法入池，时长 4 小时） -->
  <el-dialog v-model="finalEvalDialogVisible" :title="`最终评价 · ${finalEvalForm.name}`" width="480px" append-to-body>
    <el-form label-width="90px" @submit.prevent>
      <el-form-item label="评价">
        <span class="amy-act-fixed">{{ finalEvalForm.name }}</span>
      </el-form-item>
      <el-form-item label="事件标题">
        <el-input v-model="finalEvalForm.title" maxlength="15" show-word-limit placeholder="本次事件简易标题（必填，≤15 字）"
          @keyup.enter="saveFinalEval" />
      </el-form-item>
      <div class="amy-finale-hint">
        登记后：按格子步进结算反馈维度，并想法「因为 {标题} 任务中，导致的{上升/降低}」入池（时长 4 小时），供决策侧原因段与记事本消费。与「新增 thought」不同，这里是 PFCA 收尾的自我评价记录。
      </div>
    </el-form>
    <template #footer>
      <el-button @click="finalEvalDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="saveFinalEval">登记</el-button>
    </template>
  </el-dialog>

  <!-- 新建 thought（调试/测试）：从 ACT 表行点击进入，act 已锁定 -->
  <el-dialog v-model="createDialogVisible" :title="`新增 thought · ${createForm.act}`" width="480px" append-to-body>
    <el-form label-width="70px" @submit.prevent>
      <el-form-item label="act">
        <span class="amy-act-fixed">{{ createForm.act }}</span>
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="createForm.desc" type="textarea" :rows="3" placeholder="thought 内容（必填）" />
      </el-form-item>
      <el-form-item label="强度">
        <el-input-number v-model="createForm.intensity" :min="0" :max="10" :step="0.5" />
      </el-form-item>
      <el-form-item label="时长(s)">
        <el-input-number v-model="createForm.duration" :min="1" :max="86400" :step="30" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="createDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="saveCreate">新增</el-button>
    </template>
  </el-dialog>

  <!-- 编辑 thought（调试/测试） -->
  <el-dialog v-model="editDialogVisible" title="编辑 thought" width="480px" append-to-body>
    <el-form label-width="70px" @submit.prevent>
      <el-form-item label="act">
        <el-select v-model="editForm.act" filterable allow-create default-first-option placeholder="选择或输入 act 名">
          <el-option v-for="n in allActNames" :key="n" :label="n" :value="n" />
        </el-select>
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="editForm.desc" type="textarea" :rows="3" />
      </el-form-item>
      <el-form-item label="强度">
        <el-input-number v-model="editForm.intensity" :min="0" :max="10" :step="0.5" />
      </el-form-item>
      <el-form-item label="时长(s)">
        <el-input-number v-model="editForm.duration" :min="1" :max="86400" :step="30" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="editDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="saveUpdate">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.amy-dialog :deep(.el-dialog__body) { padding: 0; }
.amy-dialog :deep(.el-dialog__header) { padding: 0; margin: 0; }

.amy-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-surface);
}
.amy-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-1);
}
.amy-sub {
  font-weight: 400;
  font-size: 11.5px;
  color: var(--text-3);
  margin-left: 8px;
}
.amy-actions { display: flex; align-items: center; gap: 8px; }
.amy-fetched {
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  margin-right: 4px;
}

.amy-loading {
  padding: 80px 20px;
  text-align: center;
  color: var(--text-3);
}
.amy-loading.amy-error { color: var(--danger); }

.amy-body {
  height: 80vh;
  min-height: 600px;
  overflow-y: auto;
  padding: 16px 20px 28px 20px;
  background: var(--bg-app);
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* —— 顶部三联仪表 —— */
.amy-gauge-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
}
.amy-gauge-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.amy-gauge-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.amy-gauge-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-3);
  font-weight: 600;
  letter-spacing: 0.02em;
}
.amy-gauge-value {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
}
.amy-gauge-bar {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.amy-gauge-track {
  position: relative;
  height: 12px;
  background: var(--bg-subtle);
  border-radius: 6px;
  overflow: hidden;
}
.amy-gauge-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  border-radius: 6px 6px 6px 6px;
  transition: width 0.3s ease;
}
.amy-gauge-scale {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
  margin-top: 2px;
}
.amy-gauge-scale .zero { color: var(--text-2); font-weight: 600; }
.amy-gauge-band {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 8px;
  background: var(--bg-subtle);
  border-radius: var(--radius-sm);
}
.amy-gauge-band-label {
  font-size: 10px;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.amy-gauge-band-text {
  font-size: 11.5px;
  color: var(--text-1);
  line-height: 1.5;
}
/* —— 状态卡片 —— */
.amy-state-banner {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  font-weight: 600;
}
.amy-state-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px 12px;
  font-size: 11.5px;
  color: var(--text-2);
}
.amy-state-grid > div { display: flex; align-items: baseline; gap: 4px; }
.amy-state-grid span { color: var(--text-3); font-size: 10.5px; }
.amy-state-grid strong {
  font-family: var(--font-mono);
  color: var(--text-1);
}
.amy-state-grid em {
  color: var(--text-3);
  font-style: normal;
  font-size: 10px;
}
.amy-ci.success { color: var(--accent-2); }
.amy-ci.warning { color: var(--warn); }
.amy-ci.danger  { color: var(--danger); }
.amy-straw {
  padding: 6px 8px;
  background: rgba(239, 68, 68, 0.08);
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  color: var(--danger);
  line-height: 1.5;
}

/* —— prompt 描述 —— */
.amy-prompt-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 12px 16px;
}
.amy-section-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  margin-bottom: 8px;
}
/* section 头带操作按钮时，标题与按钮两端对齐 */
.amy-section-head-ops {
  justify-content: space-between;
}
.amy-prompt-body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-1);
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  max-height: 220px;
  overflow-y: auto;
}

/* —— 存在感归因 prompt 监控 —— */
.amy-presence-count {
  font-size: 11px;
  color: var(--warn);
  font-variant-numeric: tabular-nums;
}
.amy-presence-debts {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}
.amy-presence-debt {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  font-size: 12px;
}
.amy-presence-debt-title {
  font-weight: 600;
  color: var(--text-1);
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.amy-presence-debt-kind {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.amy-presence-debt-score {
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.amy-presence-debt-score.neg { color: var(--danger); }

/* —— thought 卡片 —— */
.amy-section {
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  padding: 12px 16px;
}
.amy-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--text-3);
}
.amy-thought-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 8px;
}
.amy-thought-card {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-left: 3px solid;
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.amy-thought-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.amy-cat-tag {
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 999px;
  border: 1px solid;
}
.amy-act-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
}
.amy-thought-remaining {
  font-size: 10.5px;
  color: var(--text-3);
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-variant-numeric: tabular-nums;
}
.amy-thought-desc {
  font-size: 12px;
  color: var(--text-2);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
.amy-thought-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.amy-thought-foot strong { color: var(--text-1); }
.amy-contrib {
  display: inline-block;
  margin-left: 8px;
  font-family: var(--font-mono);
  font-weight: 600;
}
.amy-contrib.pos { color: var(--accent-2); }
.amy-contrib.neg { color: var(--danger); }
.amy-thought-time { font-family: var(--font-mono); }
/* thought 卡片底部操作区（编辑/删除按钮 + 时间） */
.amy-thought-ops {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

/* —— ACT 表 —— */
.amy-act-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.amy-act-cat {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
.amy-act-cat-head {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  margin-bottom: 6px;
  text-transform: uppercase;
}
.amy-add-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  color: var(--text-3);
  cursor: default;
}
.amy-act-fixed {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
}
.amy-finale-hint {
  padding: 8px 10px;
  margin-left: 90px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-3);
  background: var(--bg-subtle);
  border-radius: 6px;
}
.amy-act-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  cursor: default;
  transition: background 0.1s ease;
}
.amy-act-row:hover { background: var(--bg-subtle); }
.amy-act-row.is-addable {
  cursor: pointer;
}
.amy-act-row.is-addable:hover {
  background: var(--bg-active, var(--bg-subtle));
}
.amy-act-row.is-addable:hover .amy-act-add-ico {
  opacity: 1;
  transform: translateX(0);
}
.amy-act-add-ico {
  vertical-align: -2px;
  margin-left: 5px;
  color: var(--primary);
  opacity: 0.45;
  transition: opacity 0.12s ease, transform 0.12s ease;
  transform: translateX(-2px);
}
.amy-act-row .amy-act-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-1);
  font-weight: 500;
}
.amy-act-stats {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.amy-act-delta {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
}
.amy-act-delta.pos { color: var(--accent-2); background: rgba(52, 211, 153, 0.12); }
.amy-act-delta.neg { color: var(--danger);    background: rgba(239, 68, 68, 0.12); }
.amy-act-meta {
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
}

/* —— 记事本笔记（PFCA 视角手册 + 未归档条目） —— */
.amy-notes-summary {
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.amy-notes-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.amy-notes-block {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}
.amy-notes-block-title {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--text-2);
  margin-bottom: 6px;
  text-transform: uppercase;
}
.amy-notes-manual {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-1);
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  max-height: 240px;
  overflow-y: auto;
}
.amy-empty-inline {
  padding: 14px 12px;
}
.amy-note-collapse {
  border-top: none;
}
.amy-note-collapse :deep(.el-collapse-item__header) {
  background: transparent;
  border-bottom: 1px solid var(--border-1);
  font-size: 12.5px;
  color: var(--text-1);
  height: 36px;
  line-height: 36px;
}
.amy-note-collapse :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-bottom: 1px solid var(--border-1);
}
.amy-note-collapse :deep(.el-collapse-item__content) {
  padding: 8px 10px 12px 10px;
}
.amy-note-title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
  padding-right: 8px;
}
.amy-note-name {
  font-weight: 600;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 0 1 auto;
  min-width: 0;
}
.amy-note-meta {
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
  flex-shrink: 0;
}
.amy-note-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.amy-note-line {
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-2);
  word-break: break-word;
}
.amy-note-line b {
  color: var(--text-3);
  font-weight: 600;
  margin-right: 4px;
}
.amy-note-empty { color: var(--text-3); }
.amy-note-entries {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 2px;
}
.amy-note-entry {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 3px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  font-size: 12px;
}
.amy-note-src {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  color: var(--accent);
  padding: 1px 5px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  line-height: 1.4;
}
.amy-note-text {
  color: var(--text-1);
  word-break: break-word;
  line-height: 1.5;
}
</style>