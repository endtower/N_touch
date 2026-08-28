<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Document, ChatLineRound, MagicStick, Refresh, ArrowDown, Connection, Aim, User, ChatDotRound } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { ContextEntry, ContextFileContent, ContextFileList, ContextFileMeta } from '../types'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

type Kind = 'chat' | 'pfca' | 'proactive' | 'social_agent' | 'social_chat'
const activeTab = ref<Kind>('chat')

// 每个 kind 各缓存一份文件列表
const fileLists = ref<Record<Kind, ContextFileMeta[]>>({
  chat: [],
  pfca: [],
  proactive: [],
  social_agent: [],
  social_chat: [],
})
const loadingFiles = ref(false)
const loadingContent = ref(false)
const currentFile = ref<ContextFileContent | null>(null)
const currentError = ref<string>('')

// ===== 自动轮询 (弹窗打开期间, 每 3s 检测后端 memory/ 目录文件变化) =====
const POLL_INTERVAL_MS = 3000
let pollTimer: number | null = null
// loadFile token: 用于区分用户主动加载 vs 轮询触发, 防止乱序响应覆盖当前文件
let loadToken = 0

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

function isSameList(a: ContextFileMeta[], b: ContextFileMeta[]): boolean {
  if (a.length !== b.length) return false
  const map = new Map(a.map(f => [f.name, `${f.size}:${f.mtime}`]))
  for (const f of b) {
    if (map.get(f.name) !== `${f.size}:${f.mtime}`) return false
  }
  return true
}

async function pollOnce() {
  if (!props.visible) return
  // 轮询时不显示 loading spinner, 用户感知不到
  for (const k of ['chat', 'pfca', 'proactive', 'social_agent', 'social_chat'] as Kind[]) {
    try {
      const res = await fetch(`/context/files?kind=${k}`)
      if (!res.ok) continue
      const data = (await res.json()) as ContextFileList
      const newList = data.files || []
      const oldList = fileLists.value[k]
      // 1) 文件列表无变化 -> 跳过, 不重读 currentFile
      if (isSameList(oldList, newList)) continue
      fileLists.value[k] = newList
      // 2) 当前正在看的文件变了 (mtime/size 变了), 自动 reload
      const cur = currentFile.value
      if (cur && cur.kind === k) {
        const oldMeta = oldList.find(f => f.name === cur.name)
        const newMeta = newList.find(f => f.name === cur.name)
        // 文件被删了 -> 清空 (让 watch(activeTab) 兜底选下一个)
        if (!newMeta) {
          currentFile.value = null
          continue
        }
        const changed =
          !oldMeta ||
          oldMeta.mtime !== newMeta.mtime ||
          oldMeta.size !== newMeta.size
        if (changed) {
          const myToken = ++loadToken
          await loadFile(k, cur.name)
          // 用户在轮询期间主动切了文件, 丢弃这次轮询结果
          if (myToken !== loadToken) continue
        }
      }
    } catch {
      // 静默, 避免噪音
    }
  }
}

onBeforeUnmount(stopPolling)

watch(
  () => props.visible,
  async (v) => {
    if (v) {
      // 只 loadFiles 还空着的 kind (避免重复请求)
      const pending = (['chat', 'pfca', 'proactive', 'social_agent', 'social_chat'] as Kind[]).filter(
        k => !fileLists.value[k].length,
      )
      if (pending.length) await Promise.all(pending.map(k => loadFiles(k)))

      // 1) 如果上次选中的文件还在, 直接复用 (保持上次打开的状态)
      if (currentFile.value && fileLists.value[currentFile.value.kind]?.length) {
        const stillExists = fileLists.value[currentFile.value.kind].some(
          f => f.name === currentFile.value!.name,
        )
        if (stillExists) {
          activeTab.value = currentFile.value.kind
          startPolling()
          return
        }
      }
      // 2) 否则选 activeTab 下的第一个文件
      const first = fileLists.value[activeTab.value]?.[0]
      if (first) await loadFile(activeTab.value, first.name)
      startPolling()
    } else {
      stopPolling()
    }
  },
)

watch(activeTab, async (kind) => {
  if (!props.visible) return
  if (!fileLists.value[kind].length) await loadFiles(kind)
  // 切 tab 时: 如果当前 currentFile 就是这个 kind 且文件还在, 不必重选
  if (currentFile.value?.kind === kind) {
    const stillExists = fileLists.value[kind].some(
      f => f.name === currentFile.value!.name,
    )
    if (stillExists) return
  }
  const first = fileLists.value[kind][0]
  if (first) await loadFile(kind, first.name)
})

async function loadFiles(kind: Kind) {
  loadingFiles.value = true
  try {
    const res = await fetch(`/context/files?kind=${kind}`)
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    const data = (await res.json()) as ContextFileList
    fileLists.value[kind] = data.files || []
  } catch (e: any) {
    ElMessage.error(`列出 ${kind} 文件失败: ${e?.message || e}`)
  } finally {
    loadingFiles.value = false
  }
}

async function loadFile(kind: Kind, name: string) {
  const myToken = ++loadToken
  loadingContent.value = true
  currentError.value = ''
  try {
    const res = await fetch(`/context/file?kind=${kind}&name=${encodeURIComponent(name)}`)
    if (!res.ok) {
      const t = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${t || res.statusText}`)
    }
    // 在拿到响应前用户又触发了新的 loadFile -> 丢弃本次结果
    if (myToken !== loadToken) return
    currentFile.value = (await res.json()) as ContextFileContent
  } catch (e: any) {
    if (myToken !== loadToken) return
    currentError.value = e?.message || String(e)
    currentFile.value = null
  } finally {
    if (myToken === loadToken) loadingContent.value = false
  }
}

async function refreshAll() {
  await Promise.all([
    loadFiles('chat'),
    loadFiles('pfca'),
    loadFiles('proactive'),
    loadFiles('social_agent'),
    loadFiles('social_chat'),
  ])
  if (currentFile.value) {
    await loadFile(currentFile.value.kind, currentFile.value.name)
  }
}

// ===== 展示辅助 =====

const KIND_META: Record<Kind, { label: string; icon: any; color: string; desc: string }> = {
  chat: {
    label: '聊天上下文',
    icon: ChatLineRound,
    color: 'var(--accent-2)',
    desc: 'conversation.json / conversation_archived_*.json — LLM 当前看到的"历史消息"原文',
  },
  pfca: {
    label: 'PFCA 上下文',
    icon: MagicStick,
    color: 'var(--accent)',
    desc: 'active.json / pfca_archived_*.json — PFCA (脑前额) 决策阶段喂给 LLM 的拼接 prompt',
  },
  proactive: {
    label: '主动上下文',
    icon: Aim,
    color: 'var(--warn)',
    desc: 'active.json / proactive_archived_*.json — Proactive (主动发起) 阶段的拼接 prompt + 完整交互',
  },
  social_agent: {
    label: 'Social Agent 上下文',
    icon: User,
    color: 'var(--accent-2)',
    desc: 'social_agent.json / social_agent_archived_*.json — Social Agent (社交策略机) 阶段喂给 LLM 的拼接 prompt',
  },
  social_chat: {
    label: 'Social Chat 上下文',
    icon: ChatDotRound,
    color: 'var(--accent)',
    desc: 'social_chat.json / social_chat_archived_*.json — Social Chat (实际对话) 阶段喂给 LLM 的拼接 prompt + 完整交互',
  },
}

function timeShort(ts?: number | null) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
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

  return out
})

function assistantReasoning(e: ContextEntry): string {
  return (e.reasoning_content ?? '').trim()
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

function selectFile(name: string) {
  if (!currentFile.value || currentFile.value.name === name) return
  loadFile(currentFile.value.kind, name)
}

function close() {
  emit('update:visible', false)
}

function scrollToBottom() {
  // 简单粗暴, 让用户手动也能看到最新条
  const el = document.querySelector('.ctx-list')
  if (el) el.scrollTop = el.scrollHeight
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    width="90vw"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
    class="ctx-dialog"
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
          <el-button :icon="Refresh" size="small" @click="refreshAll">刷新</el-button>
          <el-button :icon="ArrowDown" size="small" @click="scrollToBottom">最新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <div class="ctx-body">
      <!-- 左侧: tabs + 文件列表 -->
      <aside class="ctx-sidebar">
        <div class="ctx-tabs">
          <div
            v-for="(meta, kind) in KIND_META"
            :key="kind"
            :class="['ctx-tab', { active: activeTab === kind }]"
            @click="activeTab = kind as Kind"
          >
            <el-icon :size="14" :color="activeTab === kind ? meta.color : 'var(--text-3)'">
              <component :is="meta.icon" />
            </el-icon>
            <span>{{ meta.label }}</span>
            <span class="ctx-tab-count">{{ fileLists[kind as Kind].length }}</span>
          </div>
        </div>

        <div class="ctx-tab-desc">{{ KIND_META[activeTab].desc }}</div>

        <div class="ctx-file-list">
          <div
            v-for="f in fileLists[activeTab]"
            :key="f.name"
            :class="['ctx-file-item', { active: currentFile?.name === f.name }]"
            @click="selectFile(f.name)"
          >
            <div class="ctx-file-name">
              <el-tag
                v-if="f.is_active"
                size="small"
                type="success"
                effect="dark"
                round
              >ACTIVE</el-tag>
              <el-tag
                v-else
                size="small"
                effect="plain"
                round
              >归档</el-tag>
              <span class="ctx-file-title">{{ f.name }}</span>
            </div>
            <div class="ctx-file-meta">
              <span>{{ fileSize(f.size) }}</span>
              <span>·</span>
              <span>{{ timeShort(f.mtime) }}</span>
            </div>
          </div>
          <div v-if="!fileLists[activeTab].length && !loadingFiles" class="ctx-file-empty">
            暂无文件
          </div>
        </div>
      </aside>

      <!-- 右侧: 内容 -->
      <main class="ctx-main">
        <div v-if="loadingContent" class="ctx-loading">加载中…</div>
        <div v-else-if="currentError" class="ctx-error">读取失败: {{ currentError }}</div>
        <div v-else-if="!currentFile" class="ctx-empty">从左侧选一个文件查看</div>
        <div v-else class="ctx-list">
          <div class="ctx-file-banner">
            <div>
              <span class="ctx-file-banner-name">{{ currentFile.name }}</span>
              <el-tag
                v-if="currentFile.is_active"
                size="small"
                type="success"
                effect="dark"
                round
                style="margin-left: 8px;"
              >ACTIVE</el-tag>
            </div>
            <div class="ctx-file-banner-meta">
              <span>{{ currentFile.entries.length }} 条</span>
              <span>·</span>
              <span>{{ fileSize(currentFile.raw_size) }}</span>
            </div>
          </div>

          <!-- 单条 / 配对 统一循环, 内部 v-if 分支 -->
          <template v-for="(item, idx) in parsedEntries" :key="idx">

            <!-- 单条 entry: user / system / 没有 tool_call 的 assistant / 孤立的 tool -->
            <div
              v-if="item.kind === 'single'"
              class="ctx-entry"
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

                  <!-- 思考链 -->
                  <div
                    v-if="assistantReasoning(item.assistant)"
                    class="ctx-reasoning"
                  >
                    <div class="ctx-reasoning-label">💭 思考链</div>
                    <div class="ctx-reasoning-body">{{ assistantReasoning(item.assistant) }}</div>
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
        </div>
      </main>
    </div>
  </el-dialog>
</template>

<style scoped>
/* —— 弹窗整体 —— */
.ctx-dialog :deep(.el-dialog__body) { padding: 0; }
.ctx-dialog :deep(.el-dialog__header) { padding: 0; margin: 0; }

.ctx-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-surface);
}
.ctx-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  color: var(--text-1);
}
.ctx-sub {
  font-weight: 400;
  font-size: 11.5px;
  color: var(--text-3);
  margin-left: 8px;
}
.ctx-actions { display: flex; gap: 8px; align-items: center; }

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
  display: grid;
  grid-template-columns: 280px 1fr;
  height: 80vh;
  min-height: 600px;
  background: var(--bg-app);
}

/* —— 左侧 sidebar —— */
.ctx-sidebar {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border-1);
  background: var(--bg-surface);
  min-height: 0;
}
.ctx-tabs {
  display: flex;
  flex-direction: column;
  padding: 10px 10px 0 10px;
  gap: 4px;
}
.ctx-tab {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-2);
  transition: background 0.12s ease, color 0.12s ease;
}
.ctx-tab:hover { background: var(--bg-subtle); color: var(--text-1); }
.ctx-tab.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.ctx-tab-count {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-3);
  background: var(--bg-subtle);
  padding: 1px 6px;
  border-radius: 999px;
}
.ctx-tab.active .ctx-tab-count {
  background: var(--accent);
  color: var(--bg-surface);
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

.ctx-file-list {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 4px 10px 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ctx-file-item {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.1s ease, border 0.1s ease;
}
.ctx-file-item:hover { background: var(--bg-subtle); }
.ctx-file-item.active {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.ctx-file-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.ctx-file-title {
  font-family: var(--font-mono);
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctx-file-meta {
  margin-top: 3px;
  margin-left: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.ctx-file-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--text-3);
}

/* —— 右侧主区 —— */
.ctx-main {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  background: var(--bg-app);
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
  /* 强制形成独立滚动层, 防止任何祖先的 transform / overflow 把滚动吞掉 */
  contain: strict;
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
}
.ctx-file-banner-name {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text-1);
}
.ctx-file-banner-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
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
.ctx-entry-body {
  /* 旧版用于多 section 容器, 现在不再使用; 留个兜底 reset */
  margin: 0;
  padding: 0;
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
  grid-template-columns: 1fr 1fr;
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
</style>
