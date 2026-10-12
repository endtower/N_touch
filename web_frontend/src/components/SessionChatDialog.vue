<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  ChatDotRound,
  Refresh,
  Search,
  ChatLineRound,
} from '@element-plus/icons-vue'
import type { WebSession, FileMessage } from '../types'
import { chat } from '../stores/chat'
import ResizableSplit from './ResizableSplit.vue'
import { useReadingScroll } from '../composables/useReadingScroll'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

// =====================================================================
// 状态
// =====================================================================

const scopeFilter = ref<'all' | 'private' | 'group'>('all')
const searchKeyword = ref('')
// 直接保存选中的会话对象, 避免 private/group 裸 id 相同时头部信息取错
const selectedSession = ref<WebSession | null>(null)
// 展开原始 JSON 的消息 id 集合 (Set 无法直接作为响应式 key, 用 Record)
const rawOpen = ref<Record<string, boolean>>({})
const listRef = ref<HTMLElement | null>(null)
const splitRef = ref<InstanceType<typeof ResizableSplit> | null>(null)
const reading = useReadingScroll(listRef)
const { following } = reading
let resetPosition = false

// =====================================================================
// 会话列表
// =====================================================================

const sessions = computed<WebSession[]>(() => chat.state.sessionDebugList ?? [])

const filteredSessions = computed<WebSession[]>(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  return sessions.value
    .filter(s => scopeFilter.value === 'all' || s.scope === scopeFilter.value)
    .filter(s => {
      if (!kw) return true
      return (
        (s.user_name || '').toLowerCase().includes(kw) ||
        (s.session_name || '').toLowerCase().includes(kw) ||
        (s.user_id || '').toLowerCase().includes(kw) ||
        (s.session_id || '').toLowerCase().includes(kw)
      )
    })
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
})

const privateCount = computed(() => sessions.value.filter(s => s.scope === 'private').length)
const groupCount = computed(() => sessions.value.filter(s => s.scope === 'group').length)

function selectSession(s: WebSession) {
  const changed = selectedSession.value?.scope !== s.scope || selectedSession.value?.session_id !== s.session_id
  if (changed) {
    chat.clearSessionDebugMessages()
    resetPosition = true
    rawOpen.value = {}
  }
  selectedSession.value = s
  chat.loadSessionDebugMessages(s.scope, s.session_id, VIEW_LIMIT)
  splitRef.value?.closeOnNarrow()
}

// =====================================================================
// 消息
// =====================================================================

// 默认只加载最新若干条, 便于快速定位; 可手动加载全部
const VIEW_LIMIT = 50

const messages = computed<FileMessage[]>(() => chat.state.sessionDebugMessages ?? [])
const selectedMessagesError = computed(() => chat.state.sessionDebugMessagesError)

const messageCount = computed(() => messages.value.length)
const messageTotal = computed(() => chat.state.sessionDebugTotal || messages.value.length)
const isAllLoaded = computed(() => messageTotal.value <= messages.value.length)

function fmtTs(ts?: number): string {
  if (!ts) return '—'
  // ts 为秒
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function isRawOpen(id?: string): boolean {
  return !!id && !!rawOpen.value[id]
}

function toggleRaw(id?: string) {
  if (!id) return
  const position = reading.capture()
  rawOpen.value[id] = !rawOpen.value[id]
  void reading.restore(position)
}

// 图片加载失败时隐藏, 防止出现破图占位
function hideBrokenImg(e: Event) {
  const el = e.target as HTMLElement | null
  if (el) el.style.display = 'none'
}

function loadAllMessages() {
  if (!selectedSession.value) return
  chat.loadSessionDebugMessages(selectedSession.value.scope, selectedSession.value.session_id, 0)
}

async function scrollToBottom() {
  await reading.latest()
}

// 消息加载完成后滚动到底部, 直接看到最新几条
watch(messages, () => {
  const position = reading.capture()
  const mode = resetPosition && messages.value.length > 0 ? 'bottom' : 'preserve'
  if (messages.value.length > 0) resetPosition = false
  void reading.restore(position, mode)
}, { flush: 'pre' })

function scopeBadge(scope?: string): string {
  return scope === 'group' ? '群聊' : '私聊'
}

function scopeColor(scope?: string): string {
  return scope === 'group' ? 'var(--tag-success)' : 'var(--tag-info)'
}

// =====================================================================
// 开关
// =====================================================================

function close() { emit('update:visible', false) }

watch(() => props.visible, (v) => {
  if (v) {
    chat.loadSessionDebugList()
  }
})
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :width="'88vw'"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
    class="session-chat-dialog reading-dialog"
  >
    <template #header>
      <div class="schat-header">
        <div class="schat-header-left">
          <el-icon :size="16" color="var(--accent)"><ChatDotRound /></el-icon>
          <span class="schat-title">会话记录</span>
          <span class="schat-sub">调试用 · 直接读取 chat_history 存储 · 只读</span>
        </div>
        <div class="schat-header-right">
          <el-input
            v-model="searchKeyword"
            size="small"
            placeholder="搜索会话 / 用户…"
            clearable
            style="width: 200px;"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :icon="Refresh" size="small" @click="chat.loadSessionDebugList()">刷新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <ResizableSplit ref="splitRef" class="schat-body" storage-key="abyss:records-sidebar" label="会话列表" :initial-width="300">
      <template #sidebar>
      <!-- 左：会话列表 -->
      <aside class="schat-list">
        <div class="schat-scope-tabs">
          <button
            type="button"
            class="schat-scope-tab"
            :class="{ active: scopeFilter === 'all' }"
            @click="scopeFilter = 'all'"
          >全部{{ sessions.length }}</button>
          <button
            type="button"
            class="schat-scope-tab"
            :class="{ active: scopeFilter === 'private' }"
            @click="scopeFilter = 'private'"
          >私聊{{ privateCount }}</button>
          <button
            type="button"
            class="schat-scope-tab"
            :class="{ active: scopeFilter === 'group' }"
            @click="scopeFilter = 'group'"
          >群聊{{ groupCount }}</button>
        </div>

        <div v-if="chat.state.sessionDebugLoading && !chat.state.sessionDebugList.length" class="schat-empty">
          <span>加载中…</span>
        </div>
        <div v-else-if="chat.state.sessionDebugError" class="schat-empty error">
          <div class="schat-empty-title">无法加载会话列表</div>
          <div class="schat-empty-sub">{{ chat.state.sessionDebugError }}</div>
          <el-button :icon="Refresh" size="small" style="margin-top: 10px;" @click="chat.loadSessionDebugList()">重试</el-button>
        </div>
        <div v-else-if="filteredSessions.length === 0" class="schat-empty">
          <div class="schat-empty-title">没有匹配的会话</div>
          <div class="schat-empty-sub">当前 scope / 搜索条件下无结果</div>
        </div>
        <div v-else class="schat-list-items">
          <button
            v-for="s in filteredSessions"
            :key="s.scope + '-' + s.session_id"
            class="schat-item"
            :class="{ active: !!selectedSession && s.session_id === selectedSession.session_id && s.scope === selectedSession.scope }"
            @click="selectSession(s)"
          >
            <span class="schat-item-dot" :style="{ background: scopeColor(s.scope) }"></span>
            <div class="schat-item-main">
              <div class="schat-item-label">
                <span class="schat-item-name">{{ s.session_name || s.user_name || s.session_id }}</span>
                <span class="schat-scope-badge" :style="{ color: scopeColor(s.scope) }">{{ scopeBadge(s.scope) }}</span>
              </div>
              <div class="schat-item-sub">
                {{ s.user_name }}<template v-if="s.user_id && s.user_id !== s.user_name">（{{ s.user_id }}）</template>
              </div>
              <div class="schat-item-meta">
                <span v-if="s.role" class="schat-item-role">{{ s.role }}</span>
                <span class="schat-item-id">id={{ s.session_id }}</span>
              </div>
            </div>
          </button>
        </div>
      </aside>
      </template>

      <!-- 右：消息记录 -->
      <section class="schat-messages">
        <div v-if="!selectedSession" class="schat-msg-empty">
          <el-icon :size="26" color="var(--text-3)"><ChatLineRound /></el-icon>
          <div style="font-weight: 600; color: var(--text-1); margin-top: 8px;">未选中会话</div>
          <div style="font-size: 12px; color: var(--text-3); margin-top: 4px;">
            点击左侧任一会话，查看它存储的全部聊天记录。
          </div>
        </div>
        <template v-else>
          <div class="schat-msg-head">
            <span class="schat-msg-scope" :style="{ background: scopeColor(selectedSession.scope) }">{{ scopeBadge(selectedSession.scope) }}</span>
            <span class="schat-msg-name">{{ selectedSession.session_name || selectedSession.user_name }}</span>
            <span class="schat-msg-count">共 {{ messageTotal }} 条 · 显示最新 {{ messageCount }} 条</span>
            <span v-if="selectedMessagesError" class="schat-msg-error">{{ selectedMessagesError }}</span>
            <span class="schat-msg-count">{{ following ? '跟随最新' : '保留阅读位置' }}</span>
            <el-button size="small" @click="scrollToBottom">最新</el-button>
            <el-button v-if="!isAllLoaded" :icon="Refresh" size="small" :loading="chat.state.sessionDebugMessagesLoading" @click="loadAllMessages">加载全部</el-button>
          </div>

          <div ref="listRef" class="schat-msg-list reading-scroll" @scroll.passive="reading.onScroll">
            <div v-if="chat.state.sessionDebugMessagesLoading && !messages.length" class="schat-msg-hint">加载中…</div>
            <div v-else-if="messages.length === 0" class="schat-msg-hint">该会话没有消息</div>
            <div v-else-if="!isAllLoaded" class="schat-topbar">
              <span>已省略更早的 {{ messageTotal - messageCount }} 条消息</span>
            </div>
            <article
              v-for="m in messages"
              :key="m.message_id || m.timestamp + '-' + (m.content_text || '').slice(0, 8)"
              class="chat-record"
              :data-scroll-key="m.message_id || m.timestamp + '-' + (m.content_text || '').slice(0, 8)"
            >
              <div class="chat-record-head">
                <span class="chat-record-sender">{{ m.user_name || m.user_id || 'system' }}</span>
                <span v-if="m.user_id && m.user_id !== m.user_name" class="chat-record-uid">id={{ m.user_id }}</span>
                <span class="chat-record-ts">{{ fmtTs(m.timestamp) }}</span>
                <span v-if="m.user_id === 'system'" class="chat-record-type">system</span>
                <button
                  type="button"
                  class="chat-record-rawbtn"
                  @click="toggleRaw(m.message_id)"
                >{{ isRawOpen(m.message_id) ? '收起原始' : '原始 JSON' }}</button>
              </div>

              <div v-if="m.content_text" class="chat-record-text">{{ m.content_text }}</div>

              <div v-if="m.images && m.images.length" class="chat-record-images">
                <img
                  v-for="img in m.images"
                  :key="img"
                  :src="img"
                  alt="image"
                  loading="lazy"
                  @error="hideBrokenImg"
                  @load="following && scrollToBottom()"
                />
              </div>

              <pre v-if="isRawOpen(m.message_id)" class="chat-record-raw">{{ JSON.stringify(m, null, 2) }}</pre>
            </article>
          </div>
        </template>
      </section>
    </ResizableSplit>
  </el-dialog>
</template>

<style scoped>
:global(.session-chat-dialog .el-dialog__header) { padding: 14px 20px !important; }

.schat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 16px;
  padding-right: 8px;
  flex-wrap: wrap;
}
.schat-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.schat-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}
.schat-sub {
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 400;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.schat-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
}

.schat-body {
  height: min(76vh, calc(100dvh - 160px));
  border-top: 1px solid var(--border-1);
  border-bottom: 1px solid var(--border-1);
}

/* ---- 左列表 ---- */
.schat-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-1);
  min-width: 0;
  overflow-y: auto;
  padding: 12px;
  gap: 8px;
}
.schat-scope-tabs {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-2);
  border-radius: 999px;
  padding: 2px;
  flex-shrink: 0;
  width: fit-content;
}
.schat-scope-tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
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
.schat-scope-tab:hover { color: var(--text-1); }
.schat-scope-tab.active {
  background: var(--bg-surface);
  color: var(--accent);
  box-shadow: var(--shadow-1);
}

.schat-list-items {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.schat-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.schat-item:hover { background: var(--bg-subtle); }
.schat-item.active {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.schat-item-dot {
  width: 9px;
  height: 9px;
  min-width: 9px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}
.schat-item-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.schat-item-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 13px;
}
.schat-item-name {
  color: var(--text-1);
  font-weight: 500;
  flex-shrink: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.schat-scope-badge {
  font-size: 10px;
  font-weight: 600;
  flex-shrink: 0;
  white-space: nowrap;
}
.schat-item-sub {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.schat-item-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 10.5px;
  color: var(--text-3);
}
.schat-item-role {
  background: var(--bg-subtle);
  border-radius: 999px;
  padding: 0 6px;
  white-space: nowrap;
}
.schat-item-id {
  font-family: var(--font-mono);
  white-space: nowrap;
}

/* ---- 右消息 ---- */
.schat-messages {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  background: var(--bg-app);
  min-width: 0;
  min-height: 0;
}
.schat-msg-empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
}
.schat-msg-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border-1);
  flex-wrap: wrap;
}
.schat-msg-scope {
  font-size: 10.5px;
  font-weight: 600;
  border-radius: 999px;
  padding: 2px 9px;
  color: var(--text-on-accent);
}
.schat-msg-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
  word-break: break-all;
}
.schat-msg-count {
  font-size: 11.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.schat-msg-error {
  font-size: 11.5px;
  color: var(--danger);
}

.schat-msg-list {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
.schat-msg-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
  font-size: 12.5px;
  padding: 30px 12px;
}
.schat-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border: 1px dashed var(--border-2);
  border-radius: var(--radius-md);
  color: var(--text-2);
  font-size: 12px;
}
.schat-topbar span { flex: 1 1 auto; min-width: 0; }

.chat-record {
  flex-shrink: 0;   /* 禁止在滚动容器里被压缩成一条, 保证每张卡片按内容展开 */
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  overflow: hidden;
  min-height: 42px;
}
.chat-record-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: var(--bg-subtle);
  border-bottom: 1px solid var(--border-1);
  flex-wrap: wrap;
}
.chat-record-sender {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-1);
}
.chat-record-uid {
  font-size: 11px;
  color: var(--text-3);
  font-family: var(--font-mono);
}
.chat-record-ts {
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
}
.chat-record-type {
  font-size: 10px;
  font-weight: 600;
  border-radius: 999px;
  padding: 1px 7px;
  background: color-mix(in srgb, var(--tag-info) 15%, transparent);
  color: var(--tag-info);
}
.chat-record-rawbtn {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--accent);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
  white-space: nowrap;
}
.chat-record-rawbtn:hover { text-decoration: underline; }
.chat-record-text {
  padding: 8px 12px;
  font-size: 13px;
  color: var(--text-1);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
}
.chat-record-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0 12px 8px;
}
.chat-record-images img {
  max-width: min(180px, 100%);
  max-height: 180px;
  border-radius: 6px;
  border: 1px solid var(--border-2);
}
.chat-record-raw {
  margin: 0;
  padding: 10px 12px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-2);
  background: var(--bg-subtle);
  border-top: 1px solid var(--border-1);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.45;
}

/* ---- 空态 / 错误态 ---- */
.schat-empty {
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
.schat-empty.error { color: var(--danger); }
.schat-empty-title { font-size: 13px; font-weight: 600; color: var(--text-1); }
.schat-empty-sub { font-size: 12px; margin-top: 2px; }
</style>
