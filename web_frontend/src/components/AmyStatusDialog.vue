<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { MagicStick, Refresh, ChatLineSquare, Sunny, Lightning, Clock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { AmyState, AmyThought, AmyActEntry, AmyDescriptionBand } from '../types'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

const data = ref<AmyState | null>(null)
const loading = ref(false)
const lastFetchedAt = ref<number>(0)
const expandedAct = ref<string | null>(null)
let pollTimer: number | null = null

watch(
  () => props.visible,
  (v) => {
    if (v) {
      fetchData()
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
    if (props.visible) fetchData(true)
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

function actCategoryLabel(c: AmyThought['category']) {
  switch (c) {
    case 'external':    return { label: '外部互动', color: 'var(--accent-2)' }
    case 'evaluation':   return { label: '评价',     color: 'var(--accent)' }
    case 'self_action':  return { label: '自身行为', color: 'var(--warn)' }
    case 'system':       return { label: '系统',     color: 'var(--text-3)' }
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
  const m = new Map<string, AmyActEntry>()
  for (const list of Object.values(data.value.acts)) {
    for (const e of list as AmyActEntry[]) m.set(e.name, e)
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

function showAct(name: string) {
  expandedAct.value = expandedAct.value === name ? null : name
}
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
          <span class="amy-sub">Amy · NE / DA · 只读 · 3s 自动刷新</span>
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

      </section>

      <!-- LLM 实际看到的描述 -->
      <section v-if="data.amy_description" class="amy-prompt-card">
        <div class="amy-section-head">
          <span><el-icon :size="13"><ChatLineSquare /></el-icon> LLM 当前看到的 prompt 描述</span>
        </div>
        <pre class="amy-prompt-body">{{ data.amy_description }}</pre>
      </section>

      <!-- 活跃 thought 列表 -->
      <section class="amy-section">
        <div class="amy-section-head">
          <span>活跃想法池 (active thoughts) · {{ data.thoughts.all_active_count }} 条</span>
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
              <span class="amy-thought-time">{{ timeShort(t.create_time) }}</span>
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
              <div class="amy-act-cat-head" :style="{ color: actCategoryLabel(cat as AmyThought['category']).color }">
                {{ actCategoryLabel(cat as AmyThought['category']).label }} · {{ items.length }} 条
              </div>
              <div
                v-for="a in items"
                :key="a.name"
                class="amy-act-row"
                @click="showAct(a.name)"
              >
                <span class="amy-act-name">{{ a.name }}</span>
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
          </template>
        </div>
      </section>
    </div>
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
</style>