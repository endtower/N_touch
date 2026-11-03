// 全局 chat 状态
import { reactive, computed } from 'vue'
import type { UiMessage, WebSession, FileMessage, SessionRuntime, Identity, GlobalStateSnapshot, TruckState, ActivityPayload, SocialMemorySnapshot, ExperienceList, TaskList, TaskInfo, SessionMessagesResponse, MemorySocialUpdateRequest, MemorySocialUpdateResponse, MemorySocialDeleteResponse, ExperienceUpdateRequest, ExperienceUpdateResponse, ExperienceDeleteResponse, SessionListResponse } from '../types'
import { channel } from '../api/channel'

interface State {
  connected: boolean
  // 系统模式: real=对话模式(真实库) / test=测试模式(测试隔离库)
  mode: 'real' | 'test'
  sessions: WebSession[]
  activeSessionId: string | null
  messagesBySession: Record<string, UiMessage[]>
  thinkingBySession: Record<string, boolean>
  loadingHistoryBySession: Record<string, boolean>
  runtimesBySession: Record<string, SessionRuntime>     // 新增: 身份运行时
  // LLM 全局状态: 当前正在处理的会话 + 聊天目的 + 事件链状态
  globalState: GlobalStateSnapshot
  // 单一活动状态 (后端 GlobalSessionState 维护单一值, 前端只渲染; null=空闲)
  activity: ActivityPayload | null
  // ---- 历史消息分页状态 (per session) ----
  // hasMoreBySession: 该会话是否还有更早的消息可拉 (后端 has_more)
  // loadingMoreBySession: 上滑加载更多的 in-flight, 防止重复请求
  // oldestMessageIdBySession: 该会话当前已加载的最早一条 message_id, 用于下次的 before_id
  hasMoreBySession: Record<string, boolean>
  loadingMoreBySession: Record<string, boolean>
  oldestMessageIdBySession: Record<string, string | null>
  // Truck 状态
  trucks: TruckState[]
  warnings: { code: string; message: string; session_id?: string; ts: number }[]  // 最近 N 条警告
  // 记忆社交实体 (Character / Group / FuzzyRole, 以 00000000 为中心)
  memorySocialSnapshot: SocialMemorySnapshot | null
  memorySocialLoading: boolean
  memorySocialError: string | null
  // 记忆经验/嘱托 (bequests 表)
  memoryExperiences: ExperienceList | null
  memoryExperiencesLoading: boolean
  memoryExperiencesError: string | null
  // 任务列表 (状态查看 + 状态快改 + 删除)
  tasks: TaskList | null
  tasksLoading: boolean
  tasksError: string | null
  tasksStatus: 'active' | 'expired' | 'all'
  // 会话聊天记录调试查看器 (HTTP, 独立于 WS 会话)
  sessionDebugList: WebSession[]
  sessionDebugLoading: boolean
  sessionDebugError: string | null
  sessionDebugMessages: FileMessage[]
  sessionDebugMessagesLoading: boolean
  sessionDebugMessagesError: string | null
  sessionDebugTotal: number
  _errorSink: (m: string) => void
  _warningSink: (w: { code: string; message: string; session_id?: string }) => void
}

const state = reactive<State>({
  connected: false,
  mode: 'real',
  sessions: [],
  activeSessionId: null,
  messagesBySession: {},
  thinkingBySession: {},
  loadingHistoryBySession: {},
  runtimesBySession: {},
  globalState: { session_id: null, chat_purpose: null, event_chain_active: null },
  activity: null,
  hasMoreBySession: {},
  loadingMoreBySession: {},
  oldestMessageIdBySession: {},
  trucks: [],
  warnings: [],
  memorySocialSnapshot: null,
  memorySocialLoading: false,
  memorySocialError: null,
  memoryExperiences: null,
  memoryExperiencesLoading: false,
  memoryExperiencesError: null,
  tasks: null,
  tasksLoading: false,
  tasksError: null,
  tasksStatus: 'active',
  sessionDebugList: [],
  sessionDebugLoading: false,
  sessionDebugError: null,
  sessionDebugMessages: [],
  sessionDebugMessagesLoading: false,
  sessionDebugMessagesError: null,
  sessionDebugTotal: 0,
  _errorSink: (m: string) => console.error('[chat]', m),
  _warningSink: (w) => console.warn('[chat warning]', w),
})

function ensureBucket(sid: string) {
  if (!state.messagesBySession[sid]) state.messagesBySession[sid] = []
  if (!(sid in state.thinkingBySession)) state.thinkingBySession[sid] = false
  if (!(sid in state.loadingHistoryBySession)) state.loadingHistoryBySession[sid] = false
  if (!(sid in state.runtimesBySession)) state.runtimesBySession[sid] = null as unknown as SessionRuntime
  if (!(sid in state.hasMoreBySession)) state.hasMoreBySession[sid] = false
  if (!(sid in state.loadingMoreBySession)) state.loadingMoreBySession[sid] = false
  if (!(sid in state.oldestMessageIdBySession)) state.oldestMessageIdBySession[sid] = null
}

// ============================================================
// 会话
// ============================================================

function setSessions(sessions: WebSession[]) {
  state.sessions = sessions
  if (state.activeSessionId === null && sessions.length > 0) {
    state.activeSessionId = sessions[0].session_id
  }
  if (state.activeSessionId && !sessions.find(s => s.session_id === state.activeSessionId)) {
    state.activeSessionId = sessions.length > 0 ? sessions[0].session_id : null
  }
  for (const s of sessions) ensureBucket(s.session_id)
}

function addSession(s: WebSession) {
  if (state.sessions.find(x => x.session_id === s.session_id)) return
  state.sessions.push(s)
  ensureBucket(s.session_id)
  state.activeSessionId = s.session_id
}

function removeSession(sid: string) {
  state.sessions = state.sessions.filter(s => s.session_id !== sid)
  // 聊天记录查看器与左侧列表共用同一个后端会话，删除后两侧同步移除
  state.sessionDebugList = (state.sessionDebugList ?? []).filter(s => s.session_id !== sid)
  delete state.messagesBySession[sid]
  delete state.thinkingBySession[sid]
  delete state.loadingHistoryBySession[sid]
  delete state.runtimesBySession[sid]
  delete state.hasMoreBySession[sid]
  delete state.loadingMoreBySession[sid]
  delete state.oldestMessageIdBySession[sid]
  if (state.activeSessionId === sid) {
    state.activeSessionId = state.sessions.length > 0 ? state.sessions[0].session_id : null
  }
}

/** 清空会话/消息/运行时/历史分页缓存（切换存储库后重新拉取前调用）。 */
function clearAll() {
  state.sessions = []
  state.activeSessionId = null
  state.messagesBySession = {}
  state.thinkingBySession = {}
  state.loadingHistoryBySession = {}
  state.runtimesBySession = {}
  state.hasMoreBySession = {}
  state.loadingMoreBySession = {}
  state.oldestMessageIdBySession = {}
  state.activity = null
  state.globalState = { session_id: null, chat_purpose: null, event_chain_active: null }
}

function setActive(sid: string) {
  if (state.sessions.find(s => s.session_id === sid)) {
    state.activeSessionId = sid
  }
}

// ============================================================
// 系统模式 (real 对话 / test 测试)
// ============================================================

function setMode(mode: 'real' | 'test') {
  state.mode = mode
}

// ============================================================
// 身份运行时
// ============================================================

function setRuntimes(rts: Record<string, SessionRuntime>) {
  for (const [sid, rt] of Object.entries(rts || {})) {
    state.runtimesBySession[sid] = rt
    ensureBucket(sid)
  }
}

function applyRuntime(rt: SessionRuntime) {
  state.runtimesBySession[rt.session_id] = rt
  ensureBucket(rt.session_id)
}

// ============================================================
// GlobalSessionState (LLM 当前处理的会话 + 聊天目的)
// ============================================================

function setGlobalState(snap: GlobalStateSnapshot) {
  state.globalState.session_id = snap.session_id ?? null
  state.globalState.chat_purpose = snap.chat_purpose ?? null
  const newActive = snap.event_chain_active ?? null
  state.globalState.event_chain_active = newActive
  // 事件链关闭 → 同步清空活动状态 (后端 clear_activity 也会广播 activity=null,
  // 这里兜底: 若只收到 global_state_update 而没收到 activity, 也清掉过期显示)
  if (newActive === false) {
    state.activity = null
  }
}

// ============================================================
// 单一活动状态 (后端 GlobalSessionState 维护单一值, 前端只渲染)
// ============================================================

function setActivity(activity: ActivityPayload | null) {
  // activity=null 表示空闲 (事件链结束 / proactive 退出), 前端清空显示
  state.activity = activity
}

function clearActivity() {
  state.activity = null
}

function pushWarning(code: string, message: string, session_id?: string) {
  const w = { code, message, session_id, ts: Date.now() }
  state.warnings.unshift(w)
  // 最多保留 20 条
  if (state.warnings.length > 20) state.warnings.pop()
  state._warningSink(w)
}

function clearWarning(idx: number) {
  state.warnings.splice(idx, 1)
}

// ============================================================
// Truck 状态 (来自 config/truck_settings.py)
// ============================================================

function setTrucks(trucks: TruckState[]) {
  state.trucks = Array.isArray(trucks) ? trucks : []
}

function toggleTruck(truck_id: string, enabled: boolean) {
  return saveTruckSettings({ enabled: { [truck_id]: enabled } })
}

async function refreshTrucks() {
  const res = await fetch('/console/trucks')
  if (!res.ok) throw new Error(`Truck 配置读取失败 (${res.status})`)
  const data = await res.json()
  setTrucks(data.trucks ?? [])
  return data as { startup_paused: boolean; core_ready: boolean }
}

async function saveTruckSettings(payload: { enabled?: Record<string, boolean>; startup_paused?: boolean }) {
  const res = await fetch('/console/trucks', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || 'Truck 配置保存失败')
  setTrucks(data.trucks ?? [])
  return data as { startup_paused: boolean; core_ready: boolean }
}

async function applyTruckSettings(action: 'apply' | 'stop') {
  const res = await fetch(`/console/trucks/${action}`, { method: 'POST' })
  const data = await res.json()
  if (!res.ok || !data.ok) throw new Error(data.error || 'Truck 状态切换失败')
  return refreshTrucks()
}

// ============================================================
// 历史 (分页: 首次取末尾 limit, 上滑用 before_id 取更早)
// ============================================================

// 首次进入会话只拉末尾 20 条, 让 ChatWindow 能可靠滚到底
// (大批次的"滚到一半"问题在切会话 + 大量历史消息时容易出现)
const HISTORY_PAGE_SIZE = 20
// 上滑每次额外拉 10 条 (更细颗粒, 避免一次插入过高导致视觉跳动)
const HISTORY_MORE_SIZE = 10

async function loadHistory(sid: string) {
  const sess = state.sessions.find(s => s.session_id === sid)
  if (!sess) return
  if (state.messagesBySession[sid] && state.messagesBySession[sid].length > 0) return
  state.loadingHistoryBySession[sid] = true
  if (!state.connected) {
    try {
      const qs = new URLSearchParams({ scope: sess.scope, id: sid, limit: String(HISTORY_PAGE_SIZE) })
      const res = await fetch(`/messages?${qs}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as SessionMessagesResponse
      applyHistory(sid, data.messages ?? [], { has_more: false })
    } catch (e) {
      state.loadingHistoryBySession[sid] = false
      console.warn('[chat] 历史记录读取失败', e)
    }
    return
  }
  // 首次只取末尾 HISTORY_PAGE_SIZE 条, 由后端按索引切片
  channel.send({ type: 'get_messages', session_id: sid, limit: HISTORY_PAGE_SIZE })
}

/** 上滑加载更早的历史 (page-up). 用当前已加载的最早 message_id 作为 before_id. */
function loadMore(sid: string) {
  const sess = state.sessions.find(s => s.session_id === sid)
  if (!sess) return
  if (state.loadingMoreBySession[sid]) return
  if (!state.hasMoreBySession[sid]) return
  const beforeId = state.oldestMessageIdBySession[sid]
  if (!beforeId) return
  state.loadingMoreBySession[sid] = true
  // page-up: 用更小的 MORE_SIZE, 避免 prepend 后视觉跳动太大
  channel.send({ type: 'get_messages', session_id: sid, limit: HISTORY_MORE_SIZE, before_id: beforeId })
}

/**
 * 把后端返回的 FileMessage[] 转成 UiMessage[] (与旧 applyHistory 相同的 role 判定)
 */
function _fileMsgsToUi(sid: string, fileMsgs: FileMessage[]): UiMessage[] {
  const sess = state.sessions.find(s => s.session_id === sid)
  if (!sess) return []
  return fileMsgs.map((m) => {
    // user_id === "system" 表示是 Abyss/LLM 自己发的消息 → assistant
    // user_id === 当前会话/活跃身份 → user (mine)
    // 其他 → other (群聊里其他人)
    let role: 'user' | 'assistant' | 'other'
    if (m.user_id === 'system') {
      role = 'assistant'
    } else if (m.user_id === sess.user_id) {
      role = 'user'
    } else if (sess.scope === 'private') {
      // 私聊里非自己非 system 的 (历史遗留) 当作 other
      role = 'other'
    } else {
      role = 'other'
    }
    return {
      id: `hist-${m.message_id || Math.random().toString(36).slice(2, 8)}`,
      session_id: sid,
      role,
      text: m.content_text || '',
      images: m.images || [],
      timestamp: m.timestamp || 0,
      user_name: m.user_name,
      user_id: m.user_id,
      _real_id: m.message_id,       // 后端真实 message_id (用于删除 / 广播匹配)
      _fromFile: true,
    }
  })
}

/**
 * 后端 messages 帧的统一入口, App.vue 仍叫 applyHistory, 内部按 has_more/oldest_message_id 区分.
 * - 首次 (旧 batch 行为): 整批 replace
 * - 分页 (before_id): 按时间正序 prepend 到头 (后端切片时已按时间顺序)
 *
 * 注意: 当后端只返回末尾 N 条, 这 N 条已是全局最后 N 条, 只需 replace.
 * 如果 total > returned && !before_id: 还有更早的没拉, hasMore=true.
 * 如果是带 before_id 的请求: returned 也按时间正序, prepend 到 list 头.
 */
function applyHistory(sid: string, fileMsgs: FileMessage[], meta?: { total?: number; returned?: number; has_more?: boolean; oldest_message_id?: string | null }) {
  ensureBucket(sid)
  state.loadingHistoryBySession[sid] = false
  state.loadingMoreBySession[sid] = false
  const out = _fileMsgsToUi(sid, fileMsgs)
  const old = state.messagesBySession[sid] || []
  // 不再用 timestamp 判断"首次替换 vs 向前拼接" —— 文件消息 timestamp 是整数秒,
  // 多条消息常同秒; 当上滑加载的最早一条与现有列表最早一条同秒时, 边界判断失效,
  // 会误走 replace 把已加载的整页消息吞掉.
  // 改用唯一 message_id (UiMessage 的 _real_id / id) 判断是否与已有重叠:
  //   - 同一 bucket 非空时, applyHistory 主要来自 loadMore 的 before_id 前翻
  //     (loadHistory 有"已有则跳过"守卫, 重连补拉也不会重复; 后端按 before_id 返回
  //      严格更老、无重叠的消息) → prepend.
  //   - 万一收到的是整批 replace (与已有重叠), 用 id 检测后仍回退 replace, 避免重复.
  if (old.length === 0) {
    // 首次: 直接 replace
    state.messagesBySession[sid] = out
  } else {
    const oldIds = new Set(old.map(m => m._real_id ?? m.id))
    const overlapped = out.some(m => oldIds.has(m._real_id ?? m.id))
    state.messagesBySession[sid] = overlapped ? out : [...out, ...old]
  }
  // 分页元数据更新
  state.hasMoreBySession[sid] = !!meta?.has_more
  const list = state.messagesBySession[sid]
  if (list.length > 0) {
    // 取 list 头的 _real_id 作为下次 page-up 的 before_id 锚点;
    // 后端给的 oldest_message_id 是本帧最早一条, 直接用它更可靠 (不受前端 id 命名影响).
    state.oldestMessageIdBySession[sid] =
      (meta?.oldest_message_id as string | null | undefined) ??
      list[0]._real_id ??
      list[0].id
  } else {
    state.oldestMessageIdBySession[sid] = null
  }
}

// ============================================================
// 消息
// ============================================================

function pushOptimisticUser(args: { session_id: string; text: string; images: string[] }): string {
  ensureBucket(args.session_id)
  const id = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  // 用当前激活身份作为显示名
  const rt = state.runtimesBySession[args.session_id]
  state.messagesBySession[args.session_id].push({
    id,
    session_id: args.session_id,
    role: 'user',
    text: args.text,
    images: [...args.images],
    timestamp: Date.now() / 1000,
    user_name: rt?.active_identity.user_name,
    user_id: rt?.active_identity.user_id,
  })
  return id
}

function applyUserEcho(args: {
  session_id: string
  message_id: string
  text: string
  images: string[]
  timestamp: number
  user_name: string
  user_id: string
}) {
  ensureBucket(args.session_id)
  const list = state.messagesBySession[args.session_id]
  // 匹配本地乐观推送: pushOptimisticUser 生成的 id 以 "local-" 开头
  // (旧逻辑靠 images.some(data:) 匹配, 纯文本消息 images=[] 会漏掉, 导致重复 push)
  const localIdx = list.findIndex(m =>
    m.role === 'user' && m.text === args.text && m.id.startsWith('local-')
  )
  if (localIdx >= 0) {
    list[localIdx] = {
      ...list[localIdx],
      images: args.images,
      id: args.message_id,
      user_name: args.user_name,
      user_id: args.user_id,
    }
  } else {
    // 去重: 已存在同 message_id (重连后重广播), 不重复 push.
    if (args.message_id && list.some(m => m.id === args.message_id)) return
    list.push({
      id: args.message_id,
      session_id: args.session_id,
      role: 'user',
      text: args.text,
      images: args.images,
      timestamp: args.timestamp,
      user_name: args.user_name,
      user_id: args.user_id,
    })
  }
  state.thinkingBySession[args.session_id] = true
}

function applyLlmMessage(args: {
  session_id: string
  message_id: string
  text: string
  images: string[]
  timestamp: number
}) {
  ensureBucket(args.session_id)
  state.thinkingBySession[args.session_id] = false
  const list = state.messagesBySession[args.session_id]
  // 去重: 后端若重广播同一 llm_message (重连/重放), 不重复 push.
  if (args.message_id && list.some(m => m.id === args.message_id)) return
  list.push({
    // 后端广播的 llm_message 现在带真实 message_id，用它作为 id，删除/广播匹配才不会落空。
    // 兼容旧帧缺 message_id 的情况，回退到本地伪 id。
    id: args.message_id || `llm-${args.timestamp}-${Math.random().toString(36).slice(2, 8)}`,
    session_id: args.session_id,
    role: 'assistant',
    text: args.text,
    images: args.images,
    timestamp: args.timestamp,
  })
}

// 删除一条消息: 同步给后端, 后端成功后会广播 message_deleted 给所有客户端 (含发起者),
// 这里本地也立即移除, 避免等待广播的延迟. 广播回来时消息已不在 store 中, 是幂等的.
function deleteMessage(args: { session_id: string; message_id: string }) {
  const list = state.messagesBySession[args.session_id]
  if (!list) return
  const idx = list.findIndex(m => m.id === args.message_id)
  if (idx < 0) return
  const m = list[idx]
  list.splice(idx, 1)
  // 历史消息有 _real_id (裸 message_id), 乐观推送/echo 的 m.id 本身就是真实 id.
  // 后端按 message_id 字段查找 JSON 文件, 必须发裸 id.
  const realId = m._real_id ?? m.id
  // 通知后端 (后端会真删文件 + 广播)
  channel.send({
    type: 'delete_message',
    session_id: args.session_id,
    message_id: realId,
  })
}

// 收到后端广播的 message_deleted: 用于多客户端同步 (本客户端可能不是发起者).
// 后端发的 message_id 是裸 id, 前端匹配规则: 历史消息 (m._real_id) 或 m.id 自身就是裸 id.
function applyMessageDeleted(args: { session_id: string; message_id: string }) {
  const list = state.messagesBySession[args.session_id]
  if (!list) return
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i]
    if (m._real_id === args.message_id || m.id === args.message_id) {
      list.splice(i, 1)
    }
  }
}

// 批量删除消息: 乐观移除 + 通知后端, 后端成功后广播 messages_deleted (幂等).
// message_ids 为前端展示 id (历史消息带 hist- 前缀), 内部换算成裸 id 后再发.
function deleteMessages(args: { session_id: string; message_ids: string[] }) {
  const list = state.messagesBySession[args.session_id]
  if (!list || !args.message_ids.length) return
  const idset = new Set(args.message_ids)
  const realIds: string[] = []
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i]
    if (idset.has(m.id)) {
      // 历史消息有 _real_id (裸 message_id), 乐观推送/echo 的 m.id 本身就是真实 id.
      realIds.push(m._real_id ?? m.id)
      list.splice(i, 1)
    }
  }
  if (!realIds.length) return
  channel.send({
    type: 'delete_messages',
    session_id: args.session_id,
    message_ids: realIds,
  })
}

// 收到后端广播的 messages_deleted: 用于多客户端同步 (本客户端可能不是发起者).
function applyMessagesDeleted(args: { session_id: string; message_ids: string[] }) {
  const list = state.messagesBySession[args.session_id]
  if (!list || !args.message_ids.length) return
  const idset = new Set(args.message_ids)
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i]
    if ((m._real_id && idset.has(m._real_id)) || idset.has(m.id)) {
      list.splice(i, 1)
    }
  }
}

// ============================================================
// 身份操作 (前端 -> 后端)
// ============================================================

function createIdentity(args: { session_id: string; user_id: string; user_name: string; switch_to?: boolean }) {
  channel.send({ type: 'create_identity', ...args })
}

function updateIdentity(args: {
  session_id: string
  old_user_id: string
  old_user_name: string
  new_user_id?: string
  new_user_name?: string
}) {
  channel.send({ type: 'update_identity', ...args })
}

function deleteIdentity(args: { session_id: string; user_id: string; user_name: string }) {
  channel.send({ type: 'delete_identity', ...args })
}

function switchIdentity(args: { session_id: string; user_id: string; user_name: string }) {
  channel.send({ type: 'switch_identity', ...args })
}

// ============================================================
// 记忆社交实体 (HTTP, 不走 WS)
// ============================================================

async function loadMemorySocial(limit = 500): Promise<void> {
  state.memorySocialLoading = true
  state.memorySocialError = null
  try {
    const qs = new URLSearchParams({ limit: String(limit) })
    const res = await fetch(`/memory/social?${qs.toString()}`)
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as SocialMemorySnapshot
    state.memorySocialSnapshot = data
    state.memorySocialError = null
  } catch (e: any) {
    state.memorySocialError = e?.message || String(e)
    state.memorySocialSnapshot = null
  } finally {
    state.memorySocialLoading = false
  }
}

function clearMemorySocial() {
  state.memorySocialSnapshot = null
  state.memorySocialError = null
}

/**
 * 编辑某个记忆实体的具体字段 (别名/印象/熟悉度/关系等)。
 * 成功后用后端返回的最新 node 原地替换 snapshot 里的对应节点，
 * 使详情面板和列表无需全量重拉也能即时反映。
 */
async function updateSocialMemory(
  entity_type: 'character' | 'group' | 'fuzzy_role',
  entity_id: string,
  payload: MemorySocialUpdateRequest,
): Promise<void> {
  const res = await fetch(`/memory/social/${entity_type}/${entity_id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const errBody = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
  }
  const data = (await res.json()) as MemorySocialUpdateResponse
  if (state.memorySocialSnapshot && data.node) {
    const list = state.memorySocialSnapshot.nodes
    const idx = list.findIndex(n => n.id === data.node.id)
    if (idx >= 0) list[idx] = data.node
  }
  if (entity_type !== 'fuzzy_role' && (payload.aliases !== undefined || payload.name !== undefined)) {
    // 首个别名决定会话名；编辑后同步侧栏和会话记录查看器。
    try {
      const sessionsRes = await fetch('/sessions')
      if (!sessionsRes.ok) throw new Error(`HTTP ${sessionsRes.status}`)
      const sessionsData = (await sessionsRes.json()) as SessionListResponse
      setSessions(sessionsData.sessions ?? [])
      state.sessionDebugList = sessionsData.sessions ?? []
    } catch (e) {
      console.warn('[chat] 会话名称刷新失败', e)
    }
  }
}

/**
 * 删除某个记忆实体 (角色 / 群组 / 模糊角色)。
 * 后端对角色 / 群组会级联删除锚定到它的模糊角色；成功后本地同步移除该实体、
 * 其依附的模糊角色以及相关关系边，并按类型重算计数。
 */
async function deleteSocialMemory(
  entity_type: 'character' | 'group' | 'fuzzy_role',
  entity_id: string,
): Promise<void> {
  const res = await fetch(`/memory/social/${entity_type}/${entity_id}`, { method: 'DELETE' })
  if (!res.ok) {
    const errBody = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
  }
  const data = (await res.json()) as MemorySocialDeleteResponse
  if (state.memorySocialSnapshot && data.deleted) {
    const snap = state.memorySocialSnapshot
    const removed = new Set<string>([data.entity_id])
    // 级联：角色 / 群组被删时，锚定到它的模糊角色也一并移除
    if (entity_type !== 'fuzzy_role') {
      for (const n of snap.nodes) {
        if (n.type === 'FuzzyRole' && n.source_entity_uuid === data.entity_id) removed.add(n.id)
      }
    }
    snap.nodes = snap.nodes.filter(n => !removed.has(n.id))
    snap.edges = snap.edges.filter(e => !removed.has(e.source) && !removed.has(e.target))
    snap.character_count = snap.nodes.filter(n => n.type === 'Character').length
    snap.group_count = snap.nodes.filter(n => n.type === 'Group').length
    snap.fuzzy_role_count = snap.nodes.filter(n => n.type === 'FuzzyRole').length
  }
}

// ------------------------------------------------------------
// 记忆经验/嘱托 (HTTP, 不走 WS)
// ------------------------------------------------------------

async function loadExperiences(limit = 200): Promise<void> {
  state.memoryExperiencesLoading = true
  state.memoryExperiencesError = null
  try {
    const qs = new URLSearchParams({ limit: String(limit) })
    const res = await fetch(`/memory/experiences?${qs.toString()}`)
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as ExperienceList
    state.memoryExperiences = data
    state.memoryExperiencesError = null
  } catch (e: any) {
    state.memoryExperiencesError = e?.message || String(e)
    state.memoryExperiences = null
  } finally {
    state.memoryExperiencesLoading = false
  }
}

function clearExperiences() {
  state.memoryExperiences = null
  state.memoryExperiencesError = null
}

/**
 * 编辑某条经验/嘱托的具体字段 (title / summary)。
 * 成功后用后端返回的最新 experience 原地替换列表里对应项，
 * 使详情面板无需全量重拉也能即时反映。
 */
async function updateExperience(
  bequest_id: string,
  payload: ExperienceUpdateRequest,
): Promise<void> {
  const res = await fetch(`/memory/experiences/${bequest_id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const errBody = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
  }
  const data = (await res.json()) as ExperienceUpdateResponse
  if (state.memoryExperiences && data.experience) {
    const list = state.memoryExperiences.experiences
    const idx = list.findIndex(e => e.bequest_id === data.experience.bequest_id)
    if (idx >= 0) list[idx] = data.experience
  }
}

/**
 * 删除某条经验/嘱托。成功后从列表里原地移除对应项（免全量重拉）。
 */
async function deleteExperience(bequest_id: string): Promise<void> {
  const res = await fetch(`/memory/experiences/${bequest_id}`, { method: 'DELETE' })
  if (!res.ok) {
    const errBody = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
  }
  const data = (await res.json()) as ExperienceDeleteResponse
  if (state.memoryExperiences && data.deleted) {
    state.memoryExperiences.experiences = state.memoryExperiences.experiences.filter(
      e => e.bequest_id !== data.bequest_id,
    )
    state.memoryExperiences.total = Math.max(0, state.memoryExperiences.total - 1)
  }
}

// ============================================================
// 任务列表 (状态查看 + 状态快改 + 删除)
// ============================================================

async function loadTasks(status: 'active' | 'expired' | 'all' = 'active', limit = 200, offset = 0): Promise<void> {
  state.tasksLoading = true
  state.tasksError = null
  state.tasksStatus = status
  try {
    const qs = new URLSearchParams({
      status,
      limit: String(limit),
      offset: String(offset),
    })
    const res = await fetch(`/events/list?${qs.toString()}`)
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as TaskList
    state.tasks = data
    state.tasksError = null
  } catch (e: any) {
    state.tasksError = e?.message || String(e)
    state.tasks = null
  } finally {
    state.tasksLoading = false
  }
}

function patchTask(updated: TaskInfo) {
  if (!state.tasks) return
  const list = state.tasks.tasks
  const idx = list.findIndex(e => e.task_id === updated.task_id)
  if (idx >= 0) {
    list[idx] = { ...list[idx], ...updated }
  } else {
    list.unshift(updated)
  }
}

/** 新建任务 — 插到 list 头部, total +1 */
function addTask(created: TaskInfo) {
  if (!state.tasks) return
  const list = state.tasks.tasks
  // 避免重复
  if (list.some(e => e.task_id === created.task_id)) return
  list.unshift(created)
  state.tasks.total = (state.tasks.total ?? 0) + 1
}

/** 删除任务 — 从 list 移除, total -1 */
function removeTask(taskId: string) {
  if (!state.tasks) return
  const list = state.tasks.tasks
  const idx = list.findIndex(e => e.task_id === taskId)
  if (idx >= 0) {
    list.splice(idx, 1)
    state.tasks.total = Math.max(0, (state.tasks.total ?? 0) - 1)
  }
}

// ============================================================
// 会话聊天记录调试查看器 (HTTP /sessions、/messages, 不走 WS)
// ============================================================

async function loadSessionDebugList(): Promise<void> {
  state.sessionDebugLoading = true
  state.sessionDebugError = null
  try {
    const res = await fetch(`/sessions`)
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as SessionListResponse
    state.sessionDebugList = data.sessions ?? []
    state.sessionDebugError = null
  } catch (e: any) {
    state.sessionDebugError = e?.message || String(e)
    state.sessionDebugList = []
  } finally {
    state.sessionDebugLoading = false
  }
}

function clearSessionDebugList() {
  state.sessionDebugList = []
  state.sessionDebugError = null
  clearSessionDebugMessages()
}

let sessionDebugRequest = 0
async function loadSessionDebugMessages(scope: string, id: string, limit = 50): Promise<void> {
  const request = ++sessionDebugRequest
  state.sessionDebugMessagesLoading = true
  state.sessionDebugMessagesError = null
  try {
    const qs = new URLSearchParams({ scope, id, limit: String(limit) })
    const res = await fetch(`/messages?${qs.toString()}`)
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as SessionMessagesResponse
    if (request !== sessionDebugRequest) return
    state.sessionDebugMessages = data.messages ?? []
    state.sessionDebugTotal = data.total ?? (state.sessionDebugMessages.length)
    state.sessionDebugMessagesError = null
  } catch (e: any) {
    if (request !== sessionDebugRequest) return
    state.sessionDebugMessagesError = e?.message || String(e)
  } finally {
    if (request === sessionDebugRequest) state.sessionDebugMessagesLoading = false
  }
}

function clearSessionDebugMessages() {
  ++sessionDebugRequest
  state.sessionDebugMessagesLoading = false
  state.sessionDebugMessages = []
  state.sessionDebugMessagesError = null
  state.sessionDebugTotal = 0
}

// ============================================================
// 会话操作
// ============================================================

function createSession(payload: {
  scope: 'private' | 'group'
  group_id?: string
  user_id: string
  user_name?: string
  session_name?: string
  role?: string
}) {
  channel.send({ type: 'create_session', ...payload })
}

function deleteSession(sid: string, scope?: string) {
  channel.send({ type: 'delete_session', session_id: sid, ...(scope ? { scope } : {}) })
}

function sendMessage(args: { session_id: string; text: string; images: string[] }) {
  // 选中会话即视为要在该会话发言：身份缺失由服务端按会话补齐，
  // 前端不再拦截（否则旧会话/平台前缀会话永远发不出去）。
  channel.send({ type: 'message', ...args })
}

function showError(msg: string) {
  if (typeof state._errorSink === 'function') state._errorSink(msg)
}

function setErrorSink(fn: (m: string) => void) {
  state._errorSink = fn
}

function setWarningSink(fn: (w: { code: string; message: string; session_id?: string }) => void) {
  state._warningSink = fn
}

// ============================================================
// 派生
// ============================================================

const activeSession = computed<WebSession | null>(() =>
  state.sessions.find(s => s.session_id === state.activeSessionId) || null,
)
const activeMessages = computed<UiMessage[]>(() =>
  state.activeSessionId ? state.messagesBySession[state.activeSessionId] || [] : [],
)
const activeThinking = computed<boolean>(() =>
  state.activeSessionId ? !!state.thinkingBySession[state.activeSessionId] : false,
)
const activeLoadingHistory = computed<boolean>(() =>
  state.activeSessionId ? !!state.loadingHistoryBySession[state.activeSessionId] : false,
)
const activeHasMore = computed<boolean>(() =>
  state.activeSessionId ? !!state.hasMoreBySession[state.activeSessionId] : false,
)
const activeLoadingMore = computed<boolean>(() =>
  state.activeSessionId ? !!state.loadingMoreBySession[state.activeSessionId] : false,
)
const activeRuntime = computed<SessionRuntime | null>(() =>
  state.activeSessionId ? state.runtimesBySession[state.activeSessionId] || null : null,
)
const canSend = computed<boolean>(() => {
  const rt = activeRuntime.value
  return !!rt && rt.identity_lib.length > 0
})

export const chat = {
  state,
  activeSession,
  activeMessages,
  activeThinking,
  activeLoadingHistory,
  activeHasMore,
  activeLoadingMore,
  activeRuntime,
  canSend,
  // 会话
  setSessions,
  addSession,
  removeSession,
  setActive,
  setMode,
  clearAll,
  loadHistory,
  loadMore,
  applyHistory,
  pushOptimisticUser,
  applyUserEcho,
  applyLlmMessage,
  deleteMessage,
  applyMessageDeleted,
  deleteMessages,
  applyMessagesDeleted,
  createSession,
  deleteSession,
  sendMessage,
  showError,
  setErrorSink,
  // 身份
  setRuntimes,
  applyRuntime,
  setGlobalState,
  setActivity,
  clearActivity,
  setTrucks,
  toggleTruck,
  refreshTrucks,
  saveTruckSettings,
  applyTruckSettings,
  pushWarning,
  clearWarning,
  createIdentity,
  updateIdentity,
  deleteIdentity,
  switchIdentity,
  setWarningSink,
  // 记忆社交实体
  loadMemorySocial,
  clearMemorySocial,
  updateSocialMemory,
  deleteSocialMemory,
  // 记忆经验/嘱托
  loadExperiences,
  clearExperiences,
  updateExperience,
  deleteExperience,
  // 任务列表
  loadTasks,
  patchTask,
  addTask,
  removeTask,
  // 会话聊天记录调试查看器
  loadSessionDebugList,
  clearSessionDebugList,
  loadSessionDebugMessages,
  clearSessionDebugMessages,
}
