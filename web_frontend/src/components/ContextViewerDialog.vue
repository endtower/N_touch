<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Document, Refresh, ArrowDown, Connection, Fold, Expand, Share } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { ChainFileItem, ChainInfo, ChainsResponse, ContextEntry, ContextFileContent } from '../types'
import { channel } from '../api/channel'
import ResizableSplit from './ResizableSplit.vue'
import { useReadingScroll } from '../composables/useReadingScroll'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

// ===== 数据: 事件链文件夹树 + 当前查看的文件 =====
const chainsData = ref<ChainsResponse | null>(null)
const loadingFiles = ref(false)
const loadingContent = ref(false)
const currentFile = ref<ContextFileContent | null>(null)
// 当前选中文件来源 (链 ID 或 'unlinked') —— 侧栏高亮与轮询 reload 的依据
const currentSource = ref<{ chain: string; name: string } | null>(null)
const currentError = ref<string>('')
const listRef = ref<HTMLElement | null>(null)
const splitRef = ref<InstanceType<typeof ResizableSplit> | null>(null)
const reading = useReadingScroll(listRef)
const { following } = reading
let loadedRevision = ''
let pollBusy = false
let lifecycle = 0

const chains = computed<ChainInfo[]>(() => chainsData.value?.chains ?? [])
const unlinkedFiles = computed<ChainFileItem[]>(() => chainsData.value?.unlinked?.files ?? [])

// ===== 自动轮询 (弹窗打开期间, 每 3s 检测后端 event_chains 变化) =====
const POLL_INTERVAL_MS = 3000
let pollTimer: number | null = null
// loadFile token: 用于区分用户主动加载 vs 轮询触发, 防止乱序响应覆盖当前文件
let loadToken = 0

// ===== WS 即时刷新: context_updated -> 300ms debounce 触发 pollOnce (轮询仅作兜底) =====
let wsDebounceTimer: number | null = null
function schedulePollFromWs() {
  if (!props.visible) return
  if (wsDebounceTimer !== null) clearTimeout(wsDebounceTimer)
  wsDebounceTimer = window.setTimeout(() => {
    wsDebounceTimer = null
    void pollOnce()
  }, 300)
}
const unsubscribeContextUpdated = channel.onMessage((msg) => {
  if (msg.type === 'context_updated') schedulePollFromWs()
})

function startPolling() {
  stopPolling()
  // 首次立即跑一次, 后面按 interval 周期跑
  void pollOnce()
  pollTimer = window.setInterval(pollOnce, POLL_INTERVAL_MS)
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// 链树快照: 判断轮询结果是否有变化 (链增删 / 活跃状态 / 文件增删 / 大小 / mtime)
function chainsSnapshot(d: ChainsResponse): string {
  const parts: string[] = []
  for (const c of d.chains) {
    parts.push(`${c.chain_id}|${c.is_active ? 1 : 0}|` + c.files.map(f => `${f.name}:${f.size}:${f.mtime}`).join(','))
  }
  parts.push('unlinked|' + (d.unlinked?.files ?? []).map(f => `${f.name}:${f.size}:${f.mtime}`).join(','))
  return parts.join(';')
}

// 在链树中定位文件元数据
function findFileMeta(d: ChainsResponse, chain: string, name: string): ChainFileItem | null {
  if (chain === 'unlinked') return (d.unlinked?.files ?? []).find(f => f.name === name) ?? null
  return d.chains.find(c => c.chain_id === chain)?.files.find(f => f.name === name) ?? null
}

// 自动选中: 第一个链的第一个文件 (进行中的链排在最前), 否则 unlinked 第一个
function autoSelect() {
  if (currentSource.value) return
  const d = chainsData.value
  if (!d) return
  const first = d.chains.find(chain => chain.files.length > 0)
  if (first?.files.length) {
    void loadFile(first.chain_id, first.files[0].name)
    return
  }
  const firstUnlinked = d.unlinked?.files?.[0]
  if (firstUnlinked) void loadFile('unlinked', firstUnlinked.name)
}

async function pollOnce() {
  if (!props.visible || pollBusy || loadingContent.value) return
  pollBusy = true
  const cycle = lifecycle
  // 轮询时不显示 loading spinner, 用户感知不到
  try {
    const res = await fetch('/context/chains')
    if (!res.ok) return
    const data = (await res.json()) as ChainsResponse
    if (!props.visible || cycle !== lifecycle) return
    const old = chainsData.value
    // 链树未变无需替换；上次失败的文件读取仍可在下方重试。
    if (!old || chainsSnapshot(old) !== chainsSnapshot(data)) chainsData.value = data
    // 2) 当前正在看的文件变了 (mtime/size 变了), 自动 reload
    const src = currentSource.value
    if (!src) {
      autoSelect()
      return
    }
    const oldMeta = old ? findFileMeta(old, src.chain, src.name) : null
    const newMeta = findFileMeta(data, src.chain, src.name)
    if (!newMeta) {
      // 文件被清理掉了 -> 清空并自动选下一个
      currentSource.value = null
      currentFile.value = null
      ++loadToken
      autoSelect()
      return
    }
    if (!oldMeta || loadedRevision !== `${newMeta.mtime}:${newMeta.size}`) {
      await loadFile(src.chain, src.name)
    }
  } catch {
    // 静默, 避免噪音
  } finally { pollBusy = false }
}

onBeforeUnmount(() => {
  ++lifecycle
  ++loadToken
  stopPolling()
  unsubscribeContextUpdated()
  if (wsDebounceTimer !== null) clearTimeout(wsDebounceTimer)
})

watch(
  () => props.visible,
  async (v) => {
    const cycle = ++lifecycle
    if (v) {
      await loadChains()
      if (!props.visible || cycle !== lifecycle) return
      // 没有已选中的文件 -> 自动选第一个链的第一个文件
      if (!currentSource.value) autoSelect()
      startPolling()
    } else {
      ++loadToken
      loadingContent.value = false
      stopPolling()
      if (wsDebounceTimer !== null) { clearTimeout(wsDebounceTimer); wsDebounceTimer = null }
    }
  },
)

async function loadChains(showError = true) {
  const cycle = lifecycle
  loadingFiles.value = true
  try {
    const res = await fetch('/context/chains')
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    const data = (await res.json()) as ChainsResponse
    if (cycle === lifecycle && props.visible) chainsData.value = data
  } catch (e: any) {
    if (showError) ElMessage.error(`列出事件链失败: ${e?.message || e}`)
  } finally {
    loadingFiles.value = false
  }
}

async function loadFile(chain: string, name: string) {
  const myToken = ++loadToken
  const preserve = currentSource.value?.chain === chain && currentSource.value?.name === name && !!currentFile.value
  const meta = chainsData.value ? findFileMeta(chainsData.value, chain, name) : null
  const revision = meta ? `${meta.mtime}:${meta.size}` : ''
  if (!preserve) {
    currentFile.value = null
    reasoningOverride.value = {}
    loadedRevision = ''
    splitRef.value?.closeOnNarrow()
  }
  // 乐观更新来源 -> 侧栏高亮即时
  currentSource.value = { chain, name }
  loadingContent.value = true
  const previousPosition = preserve ? reading.capture() : null
  currentError.value = ''
  if (preserve) void reading.restore(previousPosition, 'preserve', () => myToken === loadToken)
  try {
    const res = await fetch(`/context/file?chain=${encodeURIComponent(chain)}&name=${encodeURIComponent(name)}`)
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    // 在拿到响应前用户又触发了新的 loadFile -> 丢弃本次结果
    const content = (await res.json()) as ContextFileContent
    if (myToken !== loadToken || !props.visible) return
    const position = reading.capture()
    currentFile.value = content
    loadedRevision = revision
    await reading.restore(position, preserve ? 'preserve' : 'top', () => myToken === loadToken && props.visible)
  } catch (e: any) {
    if (myToken !== loadToken) return
    const position = reading.capture()
    currentError.value = e?.message || String(e)
    if (!preserve) currentFile.value = null
    else await reading.restore(position, 'preserve', () => myToken === loadToken)
  } finally {
    if (myToken === loadToken) loadingContent.value = false
  }
}

async function refreshAll() {
  await loadChains()
  if (currentSource.value) {
    await loadFile(currentSource.value.chain, currentSource.value.name)
  }
}

// ===== 展示辅助 =====

// 各 agent 上下文文件的 kind 标签 (后端从文件名解析; null 时按原始文件名展示)
type Kind = 'chat' | 'pfca' | 'proactive' | 'social_chat' | 'quest'
const KIND_META: Record<Kind, { label: string; color: string }> = {
  chat: { label: '聊天', color: 'var(--accent-2)' },
  pfca: { label: 'PFCA', color: 'var(--accent)' },
  proactive: { label: '主动', color: 'var(--warn)' },
  social_chat: { label: 'Social', color: 'var(--accent)' },
  quest: { label: '任务', color: 'var(--warn)' },
}

function kindLabel(f: ChainFileItem): string {
  return f.kind && f.kind in KIND_META ? KIND_META[f.kind as Kind].label : f.name.replace(/\.json$/, '')
}

function kindColor(f: ChainFileItem): string {
  return f.kind && f.kind in KIND_META ? KIND_META[f.kind as Kind].color : 'var(--text-3)'
}

// 链内条目编号: 同 kind 按创建顺序编号（后端已按 mtime 正序），一次唤醒 = 一个条目
// 返回 null 表示 kind 未知（保留原始文件名展示）
function chainFileSeq(chain: ChainInfo, f: ChainFileItem): number | null {
  if (!f.kind) return null
  let n = 0
  for (const other of chain.files) {
    if (other.kind === f.kind) {
      n++
      if (other.name === f.name) return n
    }
  }
  return null
}

// 链头显示: event_chain_20260912_223001_ab12 -> 2026-09-12 22:30:01 · ab12
function formatChainTitle(cid: string): string {
  const s = cid.startsWith('event_chain_') ? cid.slice('event_chain_'.length) : cid
  const m = s.match(/^(\d{8})_(\d{6})(?:_(.+))?$/)
  if (!m) return s
  const d = m[1]
  const t = m[2]
  const date = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`
  const time = `${t.slice(0, 2)}:${t.slice(2, 4)}:${t.slice(4, 6)}`
  return m[3] ? `${date} ${time} · ${m[3]}` : `${date} ${time}`
}

function selectChainFile(chain: string, item: ChainFileItem) {
  if (currentSource.value?.chain === chain && currentSource.value?.name === item.name) return
  void loadFile(chain, item.name)
}

function fileSize(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

// 直接显示原始 content (不再做"情绪/记忆/策略"分段染色);
// 只保留 pair 配对的 LLM → Tool 调用树视图.

const parsedEntries = computed(() => {
  if (!currentFile.value) return []

  type Interaction =
    | { kind: 'single'; entry: ContextEntry }
    | {
        kind: 'pair'
        assistant: ContextEntry
        tools: ContextEntry[]   // 紧跟其后的一个或多个 tool 响应
      }

  const raw = currentFile.value.entries
  const out: Interaction[] = []

  // LLM 调工具 + 响应配对: 用 tool_call_id 匹配
  let i = 0
  while (i < raw.length) {
    const e = raw[i]
    const isAssistantToolCaller =
      e.role === 'assistant' && Array.isArray(e.tool_calls) && e.tool_calls.length > 0

    if (!isAssistantToolCaller) {
      out.push({ kind: 'single', entry: e })
      i++
      continue
    }

    // 收集这次调用的预期 tool_call_id
    const expectedIds = new Set(e.tool_calls!.map(tc => tc.id))
    const tools: ContextEntry[] = []
    let j = i + 1
    while (j < raw.length && tools.length < expectedIds.size) {
      const next = raw[j]
      if (next.role === 'tool' && next.tool_call_id && expectedIds.has(next.tool_call_id)) {
        tools.push(next)
        j++
        continue
      }
      // 不属于这次的 tool 响应 (比如上一次的尾巴) -> 当作独立
      break
    }

    out.push({ kind: 'pair', assistant: e, tools })
    i = j
  }

  const occurrences = new Map<string, number>()
  return out.map(item => {
    const entry = item.kind === 'single' ? item.entry : item.assistant
    // Context files have no message IDs. Use tool IDs or an unchanged content prefix, plus duplicate order.
    const base = JSON.stringify([entry.role, entry.tool_call_id, entry.tool_calls?.map(tc => tc.id), entry.content?.slice(0, 120)])
    const count = occurrences.get(base) ?? 0
    occurrences.set(base, count + 1)
    return { ...item, key: `${base}:${count}` }
  })
})

// ===== 思考链折叠 (全局一键开关 + 单条覆盖) =====
const REASONING_COLLAPSE_KEY = 'ctx-viewer:reasoning-collapsed'
const reasoningCollapsed = ref(localStorage.getItem(REASONING_COLLAPSE_KEY) === '1')
watch(reasoningCollapsed, v => {
  localStorage.setItem(REASONING_COLLAPSE_KEY, v ? '1' : '0')
})
// 单条覆盖: 条目索引 -> 是否折叠; 未设置的条目跟随全局状态
const reasoningOverride = ref<Record<string, boolean>>({})

function isReasoningCollapsed(idx: string): boolean {
  return reasoningOverride.value[idx] ?? reasoningCollapsed.value
}

function toggleReasoning(idx: string) {
  const position = reading.capture()
  reasoningOverride.value[idx] = !isReasoningCollapsed(idx)
  void reading.restore(position)
}

function toggleAllReasoning() {
  const position = reading.capture()
  reasoningCollapsed.value = !reasoningCollapsed.value
  reasoningOverride.value = {}
  void reading.restore(position)
}

function assistantReasoning(e: ContextEntry): string {
  return (e.reasoning_content ?? '').trim()
}

function reasoningPreview(e: ContextEntry): string {
  const t = assistantReasoning(e)
  const firstLine = t.split('\n').find(l => l.trim()) || ''
  return firstLine.length > 80 ? `${firstLine.slice(0, 80)}…` : firstLine
}

function assistantHasText(e: ContextEntry): boolean {
  return !!e.content && e.content.trim().length > 0
}

function toolArgsPreview(argsJson: string): string {
  try {
    const obj = JSON.parse(argsJson)
    return JSON.stringify(obj, null, 2)
  } catch {
    return argsJson
  }
}

function roleMeta(role: string) {
  const r = (role || '').toLowerCase()
  if (r === 'system') return { label: 'SYSTEM', bg: 'var(--accent-soft)', color: 'var(--accent)' }
  if (r === 'assistant') return { label: 'ASSISTANT', bg: 'rgba(52, 211, 153, 0.18)', color: 'var(--accent-2)' }
  if (r === 'user') return { label: 'USER (PROMPT)', bg: 'rgba(245, 158, 11, 0.18)', color: 'var(--warn)' }
  return { label: (role || 'UNKNOWN').toUpperCase(), bg: 'var(--bg-subtle)', color: 'var(--text-3)' }
}

function close() {
  emit('update:visible', false)
}

function scrollToBottom() {
  void reading.latest()
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    width="90vw"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
    class="ctx-dialog reading-dialog"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <template #header>
      <div class="ctx-header">
        <div class="ctx-title">
          <el-icon :size="18" :color="'var(--accent)'"><Document /></el-icon>
          <span>LLM 上下文</span>
          <span class="ctx-sub">只读 · 不修改 memory/</span>
        </div>
        <div class="ctx-actions">
          <span class="ctx-poll-indicator" title="打开期间每 3s 自动检测后端文件变化">
            <span class="ctx-poll-dot" />
            <span>自动刷新</span>
          </span>
          <el-button
            size="small"
            :icon="reasoningCollapsed ? Expand : Fold"
            @click="toggleAllReasoning"
          >{{ reasoningCollapsed ? '展开思考链' : '折叠思考链' }}</el-button>
          <el-button :icon="Refresh" size="small" @click="refreshAll">刷新</el-button>
          <el-button :icon="ArrowDown" size="small" @click="scrollToBottom">最新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <ResizableSplit ref="splitRef" class="ctx-body" storage-key="abyss:context-sidebar" label="上下文文件">
      <template #sidebar>
      <!-- 左侧: 事件链文件夹树 (一个任务链 = 一个文件夹, 内含各 agent 的上下文文件) -->
      <aside class="ctx-sidebar">
        <div class="ctx-tab-desc">
          按任务链文件夹查看一个任务涉及的全部 agent 上下文（写入即落位, 归档不重命名）;
          <span class="ctx-desc-live">进行中</span>的链随 LLM 运行实时刷新; 底部"未关联"为链外写入与历史归档
        </div>

        <div class="ctx-file-list">
          <!-- 每个任务链一个文件夹组 -->
          <div
            v-for="c in chains"
            :key="c.chain_id"
            :class="['ctx-chain-group', { active: currentSource?.chain === c.chain_id }]"
          >
            <div class="ctx-chain-head">
              <el-icon :size="13" :color="c.is_active ? 'var(--accent-2)' : 'var(--text-3)'">
                <Share />
              </el-icon>
              <span class="ctx-chain-title" :title="c.chain_id">{{ formatChainTitle(c.chain_id) }}</span>
              <el-tag
                v-if="c.is_active"
                size="small"
                type="success"
                effect="dark"
                round
              >进行中</el-tag>
              <span class="ctx-chain-count">{{ c.files.length }}</span>
            </div>
            <div
              v-for="f in c.files"
              :key="c.chain_id + '/' + f.name"
              :class="['ctx-chain-file', { active: currentSource?.chain === c.chain_id && currentSource?.name === f.name }]"
              @click="selectChainFile(c.chain_id, f)"
            >
              <span class="ctx-chain-file-kind" :style="{ color: kindColor(f) }">{{ kindLabel(f) }}</span>
              <span
                class="ctx-chain-file-name"
                :title="chainFileSeq(c, f) !== null ? f.name : undefined"
              >{{ chainFileSeq(c, f) !== null ? `上下文${chainFileSeq(c, f)}` : f.name.replace(/\.json$/, '') }}</span>
              <span class="ctx-chain-file-size">{{ fileSize(f.size) }}</span>
            </div>
            <div v-if="!c.files.length" class="ctx-chain-file-empty">（该链暂无上下文文件）</div>
          </div>

          <!-- 未关联区: 链外写入 + legacy 迁移, 平铺展示, 不归属任何链 -->
          <div v-if="unlinkedFiles.length" class="ctx-chain-group">
            <div class="ctx-chain-head">
              <el-icon :size="13" color="var(--text-3)">
                <Document />
              </el-icon>
              <span class="ctx-chain-title">未关联</span>
              <span class="ctx-chain-sub">链外写入 / 历史归档</span>
              <span class="ctx-chain-count">{{ unlinkedFiles.length }}</span>
            </div>
            <div
              v-for="f in unlinkedFiles"
              :key="'unlinked/' + f.name"
              :class="['ctx-chain-file', { active: currentSource?.chain === 'unlinked' && currentSource?.name === f.name }]"
              @click="selectChainFile('unlinked', f)"
            >
              <span class="ctx-chain-file-kind" :style="{ color: kindColor(f) }">{{ kindLabel(f) }}</span>
              <span class="ctx-chain-file-name">{{ f.name }}</span>
              <span class="ctx-chain-file-size">{{ fileSize(f.size) }}</span>
            </div>
          </div>

          <div v-if="!chains.length && !unlinkedFiles.length && !loadingFiles" class="ctx-file-empty">
            暂无事件链
          </div>
        </div>
      </aside>
      </template>

      <!-- 右侧: 内容 -->
      <main class="ctx-main">
        <div v-if="loadingContent && !currentFile" class="ctx-loading reading-state">加载中…</div>
        <div v-else-if="currentError && !currentFile" class="ctx-error reading-state">读取失败: {{ currentError }}</div>
        <div v-else-if="!currentFile" class="ctx-empty reading-state">从左侧选一个文件查看</div>
        <div v-show="currentFile" ref="listRef" class="ctx-list reading-scroll" @scroll.passive="reading.onScroll">
          <template v-if="currentFile">
          <div class="ctx-file-banner">
            <div>
              <span class="ctx-file-banner-name">{{ currentFile.name }}</span>
              <el-tag
                v-if="currentFile.chain_id"
                size="small"
                type="warning"
                effect="plain"
                round
                class="ctx-banner-chain"
              >{{ formatChainTitle(currentFile.chain_id) }}</el-tag>
              <el-tag
                v-else
                size="small"
                effect="plain"
                round
              >未关联</el-tag>
            </div>
            <div class="ctx-file-banner-meta">
              <span>{{ currentFile.entries.length }} 条</span>
              <span>·</span>
              <span>{{ fileSize(currentFile.raw_size) }}</span>
              <span>· {{ loadingContent ? '更新中' : following ? '跟随最新' : '保留阅读位置' }}</span>
            </div>
          </div>
          <div v-if="currentError" class="reading-error" role="status">更新失败，保留上次内容：{{ currentError }}</div>

          <!-- 单条 / 配对 统一循环, 内部 v-if 分支 -->
          <template v-for="(item, idx) in parsedEntries" :key="item.key">

            <!-- 单条 entry: user / system / 没有 tool_call 的 assistant / 孤立的 tool -->
            <div
              v-if="item.kind === 'single'"
              class="ctx-entry"
              :data-scroll-key="item.key"
            >
              <div class="ctx-entry-header">
                <span
                  class="ctx-role-badge"
                  :style="{
                    background: roleMeta(item.entry.role).bg,
                    color: roleMeta(item.entry.role).color,
                  }"
                >{{ roleMeta(item.entry.role).label }}</span>
                <span class="ctx-entry-idx">#{{ idx + 1 }}</span>
              </div>
              <!-- 直接显示原始 prompt 正文 -->
              <pre class="ctx-entry-raw">{{ item.entry.content ?? '' }}</pre>
            </div>

            <!-- 配对大卡: LLM (调工具) + Tool 响应, 两份并排 -->
            <div
              v-else
              class="ctx-interaction"
              :data-scroll-key="item.key"
            >
              <div class="ctx-interaction-header">
                <span class="ctx-interaction-title">
                  <el-icon :size="14" color="var(--accent)"><Connection /></el-icon>
                  <span>第 {{ idx + 1 }} 轮 · LLM 调用 → Tool 响应</span>
                </span>
                <span class="ctx-interaction-sub">
                  <span>LLM 调 {{ item.assistant.tool_calls!.length }} 个</span>
                  <span>·</span>
                  <span>Tool 回 {{ item.tools.length }} 个</span>
                </span>
              </div>
              <div class="ctx-interaction-body">
                <!-- 左: LLM 侧 -->
                <section class="ctx-side ctx-side-llm">
                  <div class="ctx-side-header">
                    <span
                      class="ctx-role-badge"
                      :style="{ background: roleMeta('assistant').bg, color: roleMeta('assistant').color }"
                    >{{ roleMeta('assistant').label }}</span>
                    <span class="ctx-side-sub">调了 {{ item.assistant.tool_calls!.length }} 个工具</span>
                  </div>

                  <!-- 思考链 (标签行可点击: 折叠/展开单条; 头部按钮可一键折叠全部) -->
                  <div
                    v-if="assistantReasoning(item.assistant)"
                    class="ctx-reasoning"
                  >
                    <div
                      class="ctx-reasoning-label clickable"
                      :title="isReasoningCollapsed(item.key) ? '展开此条思考链' : '折叠此条思考链'"
                      @click="toggleReasoning(item.key)"
                    >
                      <el-icon :size="12">
                        <Expand v-if="isReasoningCollapsed(item.key)" />
                        <Fold v-else />
                      </el-icon>
                      <span>💭 思考链</span>
                      <span class="ctx-reasoning-hint">
                        {{ isReasoningCollapsed(item.key)
                          ? `已折叠 · ${assistantReasoning(item.assistant).length} 字`
                          : '点击折叠' }}
                      </span>
                    </div>
                    <div
                      v-if="isReasoningCollapsed(item.key)"
                      class="ctx-reasoning-preview"
                    >{{ reasoningPreview(item.assistant) }}</div>
                    <div
                      v-show="!isReasoningCollapsed(item.key)"
                      class="ctx-reasoning-body"
                    >{{ assistantReasoning(item.assistant) }}</div>
                  </div>

                  <!-- assistant 输出文本 (有 content 时; 直接 raw 输出) -->
                  <pre
                    v-if="assistantHasText(item.assistant)"
                    class="ctx-entry-raw"
                  >{{ item.assistant.content }}</pre>

                  <!-- 工具调用清单 -->
                  <div class="ctx-tool-calls">
                    <div class="ctx-reasoning-label">🔧 调用的工具</div>
                    <div
                      v-for="tc in item.assistant.tool_calls"
                      :key="tc.id"
                      class="ctx-tool-call"
                    >
                      <div class="ctx-tool-call-head">
                        <span class="ctx-tool-call-name">{{ tc.function.name }}</span>
                        <span class="ctx-tool-call-id">{{ tc.id }}</span>
                      </div>
                      <pre class="ctx-tool-call-args">{{ toolArgsPreview(tc.function.arguments) }}</pre>
                    </div>
                  </div>
                </section>

                <!-- 右: Tool 侧 -->
                <section class="ctx-side ctx-side-tool">
                  <div
                    v-if="item.tools.length === 0"
                    class="ctx-tool-empty"
                  >
                    <div class="ctx-tool-empty-text">(本轮没有 Tool 响应记录, 可能 tool 还没回执)</div>
                  </div>
                  <template v-else>
                    <div
                      v-for="t in item.tools"
                      :key="t.tool_call_id"
                      class="ctx-tool-response"
                    >
                      <div class="ctx-side-header">
                        <span
                          class="ctx-role-badge"
                          :style="{ background: roleMeta('tool').bg, color: roleMeta('tool').color }"
                        >{{ roleMeta('tool').label }}</span>
                        <span class="ctx-side-sub">{{ t.name || '(未知函数)' }}</span>
                        <span class="ctx-tool-id-tail">{{ t.tool_call_id }}</span>
                      </div>
                      <pre class="ctx-entry-raw">{{ t.content }}</pre>
                    </div>
                  </template>
                </section>
              </div>
            </div>

          </template>
          </template>
        </div>
      </main>
    </ResizableSplit>
  </el-dialog>
</template>

<style scoped>
/* —— 弹窗整体 —— */
:global(.ctx-dialog .el-dialog__header) { padding: 0 !important; }

.ctx-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-surface);
  flex-wrap: wrap;
  gap: 10px;
}
.ctx-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-1);
  flex-wrap: wrap;
  min-width: 0;
}
.ctx-sub {
  font-weight: 400;
  font-size: 11.5px;
  color: var(--text-3);
  margin-left: 8px;
}
.ctx-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

/* —— 自动刷新指示器 —— */
.ctx-poll-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-3);
  padding: 0 6px;
  user-select: none;
}
.ctx-poll-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent-2);
  box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6);
  animation: ctx-poll-pulse 2s ease-in-out infinite;
}
@keyframes ctx-poll-pulse {
  0%, 100% {
    box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.55);
    transform: scale(1);
  }
  50% {
    box-shadow: 0 0 0 6px rgba(52, 211, 153, 0);
    transform: scale(1.15);
  }
}

/* —— 主体两栏 —— */
.ctx-body {
  height: min(76vh, calc(100dvh - 160px));
  background: var(--bg-app);
}

/* —— 左侧 sidebar —— */
.ctx-sidebar {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border-1);
  background: var(--bg-surface);
  min-height: 0;
  flex: 1;
  min-width: 0;
}
.ctx-tab-desc {
  margin: 6px 12px 10px 12px;
  padding: 8px 10px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  font-size: 11px;
  color: var(--text-3);
  line-height: 1.5;
}
.ctx-desc-live {
  color: var(--accent-2);
  font-weight: 600;
}
.ctx-file-list {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 4px 10px 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ctx-file-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--text-3);
}

/* —— 任务链分组 —— */
.ctx-chain-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px;
  margin-bottom: 4px;
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  background: var(--bg-app);
  flex-shrink: 0;
}
.ctx-chain-group:has(.ctx-chain-file.active) {
  border-color: var(--accent);
}
.ctx-chain-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 4px 4px 4px;
}
.ctx-chain-title {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctx-chain-sub {
  flex-shrink: 0;
  font-size: 10.5px;
  color: var(--text-3);
}
.ctx-chain-count {
  flex-shrink: 0;
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-3);
  background: var(--bg-subtle);
  padding: 1px 6px;
  border-radius: 999px;
}
.ctx-chain-file-size {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ctx-chain-file-empty {
  padding: 2px 6px 6px 14px;
  font-size: 10.5px;
  color: var(--text-3);
  font-style: italic;
}
.ctx-chain-file {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px 4px 10px;
  margin-left: 8px;
  border-left: 2px solid var(--border-1);
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s;
}
.ctx-chain-file:hover {
  background: var(--bg-subtle);
}
.ctx-chain-file.active {
  background: var(--bg-subtle);
  border-left-color: var(--accent);
}
.ctx-chain-file-kind {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-3);
  background: var(--bg-subtle);
  padding: 1px 5px;
  border-radius: 4px;
  white-space: nowrap;
}
.ctx-chain-file.active .ctx-chain-file-kind {
  color: var(--accent);
  background: transparent;
}
.ctx-chain-file-name {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* —— 右侧主区 —— */
.ctx-main {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  background: var(--bg-app);
  container-type: inline-size;
  container-name: context-pane;
}
.ctx-loading,
.ctx-empty,
.ctx-error {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-size: 13px;
  color: var(--text-3);
}
.ctx-error { color: var(--danger); }

.ctx-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  isolation: isolate;
  padding: 16px 20px 28px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.ctx-file-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--bg-surface);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  font-size: 12px;
  position: sticky;
  top: 0;
  z-index: 1;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 8px;
  overflow-wrap: anywhere;
}
.ctx-file-banner-name {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text-1);
}
.ctx-banner-chain { margin-left: 8px; }
.ctx-file-banner-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  flex-wrap: wrap;
}

/* —— 单条 entry —— */
.ctx-entry {
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius);
  overflow: hidden;
  /* 关键: 让每个 entry 形成独立层, 防止相邻的 entry 视觉覆盖 */
  position: relative;
  z-index: 0;
  /* 让 entry 自身的所有 section + padding 都完整可见, 不被父 overflow 切掉 */
  flex-shrink: 0;
}
.ctx-entry-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-subtle);
}
.ctx-role-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 999px;
}
.ctx-entry-idx {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
/* —— 单条 entry 的原始正文: 直接渲染, 不分段不染色 —— */
.ctx-entry-raw {
  margin: 0;
  padding: 12px 14px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-1);
  background: transparent;
}

/* ===== LLM→Tool 配对大卡 ===== */
.ctx-interaction {
  position: relative;
  z-index: 0;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  overflow: hidden;
}
.ctx-interaction-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--bg-subtle);
  border-bottom: 1px solid var(--border-2);
}
.ctx-interaction-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-1);
}
.ctx-interaction-sub {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

.ctx-interaction-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  min-height: 0;
}
.ctx-side {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 12px 14px;
  gap: 10px;
}
.ctx-side-llm {
  border-right: 1px solid var(--border-1);
  background: rgba(129, 140, 248, 0.04);  /* 淡紫底, 标识 LLM 侧 */
}
.ctx-side-tool {
  background: rgba(52, 211, 153, 0.04);   /* 淡绿底, 标识 Tool 侧 */
}
.ctx-side-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}
.ctx-side-sub {
  margin-left: 4px;
  font-size: 11px;
  color: var(--text-3);
}

/* 思考链 / Tool 响应 类 */
.ctx-reasoning {
  background: var(--bg-subtle);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
.ctx-reasoning-label {
  font-size: 10.5px;
  font-weight: 700;
  color: var(--text-2);
  margin-bottom: 4px;
  letter-spacing: 0.02em;
}
.ctx-reasoning-label.clickable {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
  transition: color 0.12s ease;
}
.ctx-reasoning-label.clickable:hover { color: var(--accent); }
.ctx-reasoning-hint {
  margin-left: auto;
  font-weight: 400;
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.02em;
}
.ctx-reasoning-preview {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-3);
  font-style: italic;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctx-reasoning-body {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  font-style: italic;
}

/* 工具调用清单 */
.ctx-tool-calls {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ctx-tool-call {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
.ctx-tool-call-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.ctx-tool-call-name {
  font-family: var(--font-mono);
  font-size: 11.5px;
  font-weight: 700;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 1px 6px;
  border-radius: 4px;
}
.ctx-tool-call-id {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  word-break: break-all;
}
.ctx-tool-call-args {
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-1);
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: 4px;
  padding: 6px 8px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 180px;
  overflow-y: auto;
}

/* Tool 响应 */
.ctx-tool-response {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
}
.ctx-tool-id-tail {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  word-break: break-all;
}
.ctx-tool-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 120px;
  padding: 20px;
}
.ctx-tool-empty-text {
  font-size: 12px;
  color: var(--text-3);
  text-align: center;
  font-style: italic;
}

@container context-pane (max-width: 700px) {
  .ctx-interaction-body { grid-template-columns: minmax(0, 1fr); }
  .ctx-side-llm { border-right: 0; border-bottom: 1px solid var(--border-1); }
  .ctx-interaction-header { flex-wrap: wrap; gap: 6px; }
  .ctx-list { padding: 12px; }
}
</style>
