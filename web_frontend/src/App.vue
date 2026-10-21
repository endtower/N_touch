<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Moon, Sunny, Connection, Document, Files, MagicStick, ChatDotRound, Aim, DataAnalysis, Monitor, Van } from '@element-plus/icons-vue'
import ChatWindow from './components/ChatWindow.vue'
import InputArea from './components/InputArea.vue'
import SessionList from './components/SessionList.vue'
import ResizableSplit from './components/ResizableSplit.vue'
import CreateSessionDialog from './components/CreateSessionDialog.vue'
import TruckControlDialog from './components/TruckControlDialog.vue'
import MemoryDialog from './components/MemoryDialog.vue'
import EventListDialog from './components/EventListDialog.vue'
import ContextViewerDialog from './components/ContextViewerDialog.vue'
import AmyStatusDialog from './components/AmyStatusDialog.vue'
import SessionChatDialog from './components/SessionChatDialog.vue'
import TokenStatsDialog from './components/TokenStatsDialog.vue'
import SystemPanelDialog from './components/SystemPanelDialog.vue'
import IdentityBar from './components/IdentityBar.vue'
import TestPresetDialog from './components/TestPresetDialog.vue'
import { channel } from './api/channel'
import { hostTheme } from './api/hostTheme'
import { chat } from './stores/chat'
import type { ServerMessage } from './types'

const dialogVisible = ref(false)
const truckDialogVisible = ref(false)
const memoryGraphDialogVisible = ref(false)
const eventListDialogVisible = ref(false)
const contextOpen = ref(false)
const amyOpen = ref(false)
const sessionChatDialogVisible = ref(false)
const testPresetDialogVisible = ref(false)
const tokenStatsOpen = ref(false)
const systemPanelOpen = ref(false)
const coreReady = ref(false)
const managerAvailable = ref(false)
const coreError = ref('')
let consolePoll: number | null = null
let consolePolling = false
let disposed = false

async function pollConsole() {
  if (consolePolling || disposed) return
  consolePolling = true
  try {
    const res = await fetch('/console/status')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    if (disposed) return
    managerAvailable.value = data.manager_available === true
    coreReady.value = data.core_ready === true
    coreError.value = data.core?.last_error || ''
    if (coreReady.value) {
      channel.connect()
    } else {
      channel.disconnect()
      chat.clearActivity()
      chat.setGlobalState({ session_id: null, chat_purpose: null, event_chain_active: null })
      for (const sid of Object.keys(chat.state.thinkingBySession)) chat.state.thinkingBySession[sid] = false
      if (chat.state.sessions.length === 0) {
        await chat.loadSessionDebugList()
        if (!disposed && !chat.state.sessionDebugError) chat.setSessions(chat.state.sessionDebugList)
      }
    }
  } catch {
    if (disposed) return
    coreReady.value = false
    managerAvailable.value = false
    channel.disconnect()
  } finally {
    consolePolling = false
  }
}

const theme = ref<'light' | 'dark'>(
  (localStorage.getItem('abyss-theme') as 'light' | 'dark') || 'dark',
)

const enabledTruckCount = computed(() => chat.state.trucks.filter((t: { enabled: boolean }) => t.enabled).length)

function openTruckDialog() {
  truckDialogVisible.value = true
  // 拉取最新 truck 状态 (可能 WS 还没好, 不影响弹窗打开)
}

function applyTheme() {
  // 自定义 CSS 变量
  document.documentElement.dataset.theme = theme.value
  // Element Plus 主题 (它用 .dark class 触发)
  document.documentElement.classList.toggle('dark', theme.value === 'dark')
}

function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  applyTheme()
  localStorage.setItem('abyss-theme', theme.value)
  // 内嵌在启动器里时把选择同步过去；独立浏览器中是空操作
  hostTheme.push(theme.value)
}

onMounted(() => {
  applyTheme()
  // 跟随启动器的主题。这里只应用、不回推，避免两边来回回声。
  // 独立浏览器里 hostTheme.listen 是空操作。
  hostTheme.listen((t) => {
    if (t === theme.value) return
    theme.value = t
    applyTheme()
    localStorage.setItem('abyss-theme', t)
  })
})

let unsubMsg: (() => void) | null = null
let unsubStatus: (() => void) | null = null

onMounted(() => {
  chat.setErrorSink((m: string) => ElMessage.error(m))
  chat.setWarningSink((w) => {
    ElMessage.warning({
      message: w.message,
      duration: 5000,
      showClose: true,
    })
  })
  unsubMsg = channel.onMessage(handleServerMessage)
  unsubStatus = channel.onStatus((c) => {
    chat.state.connected = c
    // WS 刚连上时, 若当前会话的历史还没加载 (watch immediate 在 connect 之前跑过,
    // 那次 channel.send 会因未连接抛异常而丢失 get_messages), 这里补拉一次.
    // loadHistory 内部有"已加载则跳过"的守卫, 重复调用安全.
    if (c && chat.state.activeSessionId) {
      chat.loadHistory(chat.state.activeSessionId)
    }
  })
  void pollConsole()
  void chat.refreshTrucks().catch(() => {})
  consolePoll = window.setInterval(pollConsole, 3000)
})

onBeforeUnmount(() => {
  disposed = true
  if (consolePoll !== null) window.clearInterval(consolePoll)
  channel.disconnect()
  unsubMsg?.()
  unsubStatus?.()
})

// 选中会话变化时拉历史
watch(
  () => chat.state.activeSessionId,
  (sid) => { if (sid) chat.loadHistory(sid) },
  { immediate: true },
)

// ----- 之前在这里有"长度变化就强制 scrollToBottom"的兜底 watcher -----
// 已删除: 它会在用户上滑加载更早 (prepend) 时把用户拽回底部.
//
// 现在的策略:
// - ChatWindow 自己负责所有滚动决策 (prepend/append 路径, 切会话, 图片异步加载).
// - 上滑 preload 的视觉位置补偿也在 ChatWindow 里完成 (prependAnchor 机制).
// - App.vue 不再做二次兜底, 避免和 ChatWindow 内部的 prepend 补偿互相抢焦点.
//
// 如果后续发现 "发送/接收消息不滚到底" 复发, 我会用更安全的方式处理:
//   在 ChatWindow 内**自己** watch messages 的 append 增量, 通过比对时间戳
//   判断 prepend vs append, 而不是依赖 store 的 loadingMore 标志.

function handleServerMessage(msg: ServerMessage) {
  switch (msg.type) {
    case 'hello':
      chat.setSessions(msg.sessions)
      if (msg.runtimes) chat.setRuntimes(msg.runtimes)
      if (msg.global_state) chat.setGlobalState(msg.global_state)
      break
    case 'global_state_update':
      chat.setGlobalState(msg.state)
      break
    case 'truck_state':
    case 'truck_state_update':
      chat.setTrucks(msg.trucks)
      break
    case 'session_created':
      chat.addSession(msg.session)
      break
    case 'session_deleted':
      chat.removeSession(msg.session_id)
      break
    case 'sessions':
      chat.setSessions(msg.sessions)
      break
    case 'messages':
      chat.applyHistory(msg.session_id, msg.messages, {
        total: msg.total,
        returned: msg.returned,
        has_more: msg.has_more,
        oldest_message_id: msg.oldest_message_id ?? null,
      })
      break
    case 'user_echo':
      chat.applyUserEcho(msg)
      break
    case 'llm_message':
      chat.applyLlmMessage({
        session_id: msg.session_id,
        message_id: msg.message_id,
        text: msg.text,
        images: msg.segments
          .filter(s => s.type === 'image' && s.file)
          .map(s => s.file!),
        timestamp: msg.timestamp,
      })
      break
    case 'activity':
      chat.setActivity(msg.activity)
      break
    case 'message_deleted':
      chat.applyMessageDeleted({ session_id: msg.session_id, message_id: msg.message_id })
      break
    case 'messages_deleted':
      chat.applyMessagesDeleted({ session_id: msg.session_id, message_ids: msg.message_ids })
      break
    case 'session_runtime':
      chat.applyRuntime(msg.runtime)
      break
    case 'warning':
      chat.pushWarning(msg.code, msg.message, msg.session_id)
      break
    case 'error':
      chat.showError(`[${msg.code || 'error'}] ${msg.message}`)
      break
    case 'pong':
      break
  }
}

function onCreateSession(payload: {
  scope: 'private' | 'group'
  group_id?: string
  user_id: string
  user_name: string
  session_name?: string
}) {
  chat.createSession({
    scope: payload.scope,
    group_id: payload.group_id,
    user_id: payload.user_id,
    user_name: payload.user_name,
    session_name: payload.session_name,
  })
}

async function onSend(payload: { text: string; images: string[] }) {
  if (!chat.state.connected) {
    ElMessage.warning('还没连上服务端，稍等…')
    return
  }
  if (!chat.activeSession.value) {
    ElMessage.warning('请先选择一个会话')
    return
  }
  if (!payload.text && payload.images.length === 0) return

  const sid = chat.activeSession.value.session_id
  const list = chat.state.messagesBySession[sid] || []
  const last = list[list.length - 1]
  if (!last || last.role !== 'user' || last.text !== payload.text || Date.now() - (last.timestamp * 1000) > 1000) {
    chat.pushOptimisticUser({ session_id: sid, text: payload.text, images: payload.images })
  }
  try {
    chat.sendMessage({ session_id: sid, text: payload.text, images: payload.images })
  } catch (e: any) {
    ElMessage.error(`发送失败：${e?.message || e}`)
  }
}

</script>

<template>
  <div class="app-shell">
    <ResizableSplit storage-key="abyss:chat-sidebar" label="会话列表">
      <template #sidebar><SessionList @new-session="dialogVisible = true" /></template>

    <div class="app-main">
      <header class="app-header">
        <div style="display: flex; align-items: center; gap: 12px; min-width: 0; flex: 1;">
          <h1 style="flex-shrink: 0;" :class="{ 'core-off': managerAvailable && !coreReady }">Abyss</h1>
          <span class="header-sub" style="white-space: nowrap; flex-shrink: 0;"
            :title="coreError || 'Core 停止时仍可查看上下文、记录和配置 Truck'">{{ coreReady ? '运行中' : managerAvailable ? '已停止' : '未连接' }}</span>
        </div>
        <div style="display: flex; align-items: center; gap: 8px; flex-shrink: 0;">
          <button
            class="truck-toggle-btn"
            title="Truck 控制"
            @click="openTruckDialog"
          >
            <el-icon :size="14"><Van /></el-icon>
            <span class="truck-toggle-label">Truck</span>
            <span v-if="enabledTruckCount > 0" class="truck-toggle-badge">{{ enabledTruckCount }}</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="记忆 (Character / Group / FuzzyRole)"
            @click="memoryGraphDialogVisible = true"
          >
            <el-icon :size="14"><Connection /></el-icon>
            <span class="truck-toggle-label">记忆</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="任务管理 (按状态查看 / 编辑 / 删除)"
            @click="eventListDialogVisible = true"
          >
            <el-icon :size="14"><Document /></el-icon>
            <span class="truck-toggle-label">任务</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="LLM 上下文查看 (memory/chat & memory/pfca)"
            @click="contextOpen = true"
          >
            <el-icon :size="14"><Files /></el-icon>
            <span class="truck-toggle-label">上下文</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="LLM 当前情绪状态 (Amy: NE/DA, 活跃 thought, ACT 表)"
            @click="amyOpen = true"
          >
            <el-icon :size="14"><MagicStick /></el-icon>
            <span class="truck-toggle-label">状态</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="会话记录 (调试: 直接查看 chat_history 存储的消息)"
            @click="sessionChatDialogVisible = true"
          >
            <el-icon :size="14"><ChatDotRound /></el-icon>
            <span class="truck-toggle-label">会话记录</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="Token 用量统计 (各 Agent / API 提供商 / 模型的消耗与缓存命中)"
            @click="tokenStatsOpen = true"
          >
            <el-icon :size="14"><DataAnalysis /></el-icon>
            <span class="truck-toggle-label">用量</span>
          </button>
          <button
            class="truck-toggle-btn"
            title="系统设置 (模型接入 / 进程管理 / 实时日志)"
            @click="systemPanelOpen = true"
          >
            <el-icon :size="14"><Monitor /></el-icon>
            <span class="truck-toggle-label">系统</span>
          </button>
          <button
            v-if="chat.state.mode === 'test'"
            class="truck-toggle-btn"
            title="运行预设测试 (环境准备: 清空测试库并填入预设数据)"
            @click="testPresetDialogVisible = true"
          >
            <el-icon :size="14"><Aim /></el-icon>
            <span class="truck-toggle-label">测试</span>
          </button>
          <div class="status-pill" :class="{ ok: chat.state.connected }">
            <span class="status-dot" />
            <span class="status-text">{{ chat.state.connected ? '已连接' : '未连接' }}</span>
          </div>
          <button class="theme-toggle" :title="theme === 'light' ? '切换到深色' : '切换到浅色'" @click="toggleTheme">
            <el-icon v-if="theme === 'light'" :size="14"><Moon /></el-icon>
            <el-icon v-else :size="14"><Sunny /></el-icon>
          </button>
        </div>
      </header>

      <main class="app-body">
        <template v-if="chat.activeSession.value">
          <ChatWindow
            :messages="chat.activeMessages.value"
            :thinking="chat.activeThinking.value"
            :loading-history="chat.activeLoadingHistory.value"
            :identity="chat.activeSession.value"
            :active-user-id="chat.activeRuntime.value?.active_identity.user_id"
            :has-more="chat.activeHasMore.value"
            :loading-more="chat.activeLoadingMore.value"
          />
          <!-- 一个大区域: 底色 + 上面身份信息 + 下面输入框 (输入框自带边框, 即"框") -->
          <div class="input-region">
            <IdentityBar v-if="chat.state.connected" :session="chat.activeSession.value" />
            <div v-else class="header-sub" style="padding: 8px 12px;">Core 未运行，当前显示已保存的记录。</div>
            <InputArea @send="onSend" :disabled="!chat.canSend.value" />
          </div>
        </template>
        <div v-else class="no-session">
          <div class="no-session-icon">💬</div>
          <div class="no-session-text">
            {{ coreReady ? '从左侧选一个会话，或点「新建」开始' : 'Core 未运行。可以查看上下文、会话记录、调整布局和配置 Truck。' }}
          </div>
        </div>
      </main>
    </div>

    </ResizableSplit>

    <CreateSessionDialog
      v-model:visible="dialogVisible"
      @create="onCreateSession"
    />

    <TruckControlDialog v-model:visible="truckDialogVisible" />

    <MemoryDialog v-model:visible="memoryGraphDialogVisible" />
    <EventListDialog v-model:visible="eventListDialogVisible" />
    <ContextViewerDialog v-model:visible="contextOpen" />
    <AmyStatusDialog v-model:visible="amyOpen" />
    <SessionChatDialog v-model:visible="sessionChatDialogVisible" />
    <TokenStatsDialog v-model:visible="tokenStatsOpen" />
    <SystemPanelDialog v-model:visible="systemPanelOpen" />
    <TestPresetDialog v-model:visible="testPresetDialogVisible" />
  </div>
</template>
