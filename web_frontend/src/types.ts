// 跟后端 WebChannel 协议对齐 (adapt/web/protocol.py)

// ---- 会话 ----

export interface WebSession {
  session_id: string       // 裸 ID
  scope: 'private' | 'group'
  user_id: string
  user_name: string
  session_name: string
  role: string | null
  platform: string         // qq | wechat | web
  created_at: number
}

// ---- 历史消息 (从 chat_history 文件读出来) ----

export interface FileMessage {
  message_id?: string
  user_id?: string
  user_name?: string
  timestamp?: number
  content_text: string
  content: SegmentJson[]
  images: string[]         // 由 _file_msg_to_dict 提取出来
}

// ---- 会话聊天记录调试查看器 (HTTP /sessions、/messages) ----

export interface SessionListResponse {
  sessions: WebSession[]
}

export interface SessionMessagesResponse {
  session_id: string
  scope: 'private' | 'group'
  total?: number
  messages: FileMessage[]
}

// ---- UI 消息 (前端用) ----

export interface UiMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'other'   // user: 自己; assistant: bot/LLM; other: 群聊里其他人
  text: string
  images: string[]
  timestamp: number
  user_name?: string
  user_id?: string                        // 发送者 user_id (用于对齐: == 当前 active identity → 右边)
  // 内部用
  _fromFile?: boolean                      // 标记是从历史文件来的
  _real_id?: string                        // 真实 message_id (历史消息的 hist- 前缀之外的部分)
}

// ---- 段 (segment) ----

export interface SegmentJson {
  type: string
  text?: string
  file?: string
  url?: string
  summary?: string
  qq?: string
  name?: string
  data?: Record<string, unknown>
}

// ============================================================
// 身份 (Identity) 与会话运行时 (SessionRuntime)
// ============================================================

export interface Identity {
  user_id: string
  user_name: string
  is_default: boolean
  created_at: number
}

export interface SessionRuntime {
  session_id: string
  scope: 'private' | 'group'
  active_identity: Identity
  identity_lib: Identity[]
}

// GlobalSessionState 同步 (来自后端 brain/hc/global_session_state.py)
export interface GlobalStateSnapshot {
  session_id: string | null     // 当前 LLM 正在处理的会话 (None = 无)
  chat_purpose: string | null   // 当前聊天目的文本 (None = 无)
  event_chain_active: boolean | null  // 系统是否处于激活状态 (前端展示用, 代表 LLM 当前活动状态, 而非事件链真实情况; null = 尚未同步)
}

// 单一活动状态 (来自 GlobalSessionState.FrontendBroadcastMixin, 后端维护单一值, 前端只渲染)
// activity 为 null 表示空闲 (事件链结束 / proactive 退出), 前端清空显示.
export interface ActivityPayload {
  source: 'pfca' | 'proactive'
  status: 'tool_call' | 'active'   // tool_call=正在调工具; active=模块活跃但未调工具
  tool_name: string | null
  args_summary: string | null
  // parent_tool_name: 父级工具名 (层级链场景, 后端 frontend_broadcast 预留; 当前无广播方)
  parent_tool_name?: string | null
  timestamp: number
}

// Truck 状态 (来自 config/truck_settings.py + 运行时覆盖)
export interface TruckState {
  truck_id: string
  label: string
  description: string
  category: string
  enabled: boolean
  configured_enabled?: boolean
  runtime_state?: string
  cycle_time: number | null
}

// 预设环境 (分块环境准备, 来自 tests/preset_env.py)
// 分块: memory(记忆) / chat(消息记录) / amy；每块预设均为情景文件(含正式环境镜像)
export type PresetBlock = 'memory' | 'chat' | 'amy'
export type PresetBlocks = Record<PresetBlock, string[]>

// ---- 浏览器 -> 服务端 ----

export type ClientMessage =
  | { type: 'ping' }
  | {
      type: 'create_session'
      scope: 'private' | 'group'
      group_id?: string          // 仅群聊: 群号, 作为文件 ID
      user_id: string            // 我的 ID (私聊 = 会话 ID; 群聊 = 在群里我的 ID)
      user_name?: string         // 我的名字
      session_name?: string      // 会话显示名
      role?: string              // (已弃用, 角色由 IdentityBar 管理)
    }
  | { type: 'list_sessions' }
  | { type: 'delete_session'; session_id: string; scope?: string }
  | {
      type: 'get_messages'
      session_id: string
      // 拉历史消息分页参数 (后端 adapt/web/channel.py)
      // limit    : 最多返回多少条, 默认 50, 上限 200
      // before_id: 只取该 message_id 之前的消息 (不含自身), 用于"上滑加载更多"
      //           不传 = 取最后 limit 条
      limit?: number
      before_id?: string
    }
  | { type: 'delete_message'; session_id: string; message_id: string }
  | { type: 'delete_messages'; session_id: string; message_ids: string[] }
  | {
      type: 'message'
      session_id: string
      text: string
      images: string[]
    }
  // 身份管理
  | {
      type: 'create_identity'
      session_id: string
      user_id: string
      user_name: string
      switch_to?: boolean
    }
  | {
      type: 'update_identity'
      session_id: string
      old_user_id: string
      old_user_name: string
      new_user_id?: string
      new_user_name?: string
    }
  | {
      type: 'delete_identity'
      session_id: string
      user_id: string
      user_name: string
    }
  | {
      type: 'switch_identity'
      session_id: string
      user_id: string
      user_name: string
    }
  // Truck 控制
  | { type: 'get_trucks' }
  | {
      type: 'set_truck'
      truck_id: string
      enabled: boolean
    }
  // 测试模式切换 (进入/退出)
  | { type: 'switch_test_mode'; enable: boolean }
  // 预设环境 (分块环境准备: 查询 / 应用 / 抓取镜像 / 复制当前状态 / 删除预设)
  | { type: 'list_presets' }
  | { type: 'apply_env'; memory?: string; chat?: string; amy?: string }
  | { type: 'capture_mirror'; block: PresetBlock; name: string }
  | { type: 'copy_state'; block: PresetBlock; name: string }
  | { type: 'delete_preset'; block: PresetBlock; name: string }

// ---- 服务端 -> 浏览器 ----

export type ServerMessage =
  | {
      type: 'hello'
      channel: string
      platform: string
      sessions: WebSession[]
      runtimes?: Record<string, SessionRuntime>
      global_state?: GlobalStateSnapshot
    }
  | { type: 'pong'; ts: number }
  | {
      type: 'context_updated'
      kind: string                       // chat | pfca | proactive | social_chat
      event: 'append' | 'archive'
      chain_id?: string | null           // 归属任务链 (链外写入为 null)
      timestamp: number
    }
  | {
      type: 'session_created'
      session: WebSession
    }
  | {
      type: 'session_deleted'
      session_id: string
    }
  | {
      type: 'sessions'
      sessions: WebSession[]
    }
  | {
      type: 'messages'
      session_id: string
      scope: 'private' | 'group'
      messages: FileMessage[]
      // 分页元数据 (后端 adapt/web/channel.py)
      // total             : 该会话总条数
      // returned          : 本帧返回了多少条
      // has_more          : 还有更早的消息 (用户上滑可继续加载)
      // oldest_message_id : 本帧最早那条的 message_id, 用于 next before_id
      total?: number
      returned?: number
      has_more?: boolean
      oldest_message_id?: string | null
    }
  | {
      type: 'user_echo'
      session_id: string
      message_id: string
      text: string
      images: string[]
      timestamp: number
      user_id: string
      user_name: string
      role: string | null
    }
  | {
      type: 'llm_message'
      session_id: string
      message_id: string
      target: { scope: string; target_id: string }
      segments: SegmentJson[]
      text: string
      timestamp: number
    }
  // 单一活动状态广播 (后端维护单一值, 前端只渲染; activity=null 表示空闲)
  | {
      type: 'activity'
      activity: ActivityPayload | null
      timestamp: number
    }
  // 单条消息删除广播 (后端 -> 所有客户端)
  | {
      type: 'message_deleted'
      session_id: string
      message_id: string
    }
  // 批量消息删除广播 (后端 -> 所有客户端, 多选/框选删除)
  | {
      type: 'messages_deleted'
      session_id: string
      message_ids: string[]
    }
  // 身份运行时广播
  | {
      type: 'session_runtime'
      runtime: SessionRuntime
    }
  // GlobalSessionState 同步 (LLM 正在处理的会话 + 聊天目的)
  | {
      type: 'global_state_update'
      state: GlobalStateSnapshot
      change: 'session' | 'purpose' | 'activity'
    }
  // Truck 状态
  | {
      type: 'truck_state'
      trucks: TruckState[]
    }
  | {
      type: 'truck_state_update'
      trucks: TruckState[]
      changed?: TruckState
    }
  // 测试模式切换结果
  | { type: 'test_mode_switched'; enable: boolean; ok: boolean }
  // 预设环境
  | { type: 'presets'; blocks: PresetBlocks }
  | {
      type: 'env_applied'
      memory?: string
      chat?: string
      amy?: string
      ok: boolean
    }
  | { type: 'mirror_captured'; block: PresetBlock; name: string; ok: boolean }
  | { type: 'state_copied'; block: PresetBlock; name: string; ok: boolean }
  | { type: 'preset_deleted'; block: PresetBlock; name: string; ok: boolean }
  // 警告 / 错误
  | {
      type: 'warning'
      code: string
      message: string
      session_id?: string
    }
  | {
      type: 'error'
      code?: string
      message: string
    }

// ============================================================
// 记忆社交实体 (Memory 可视化: Character / Group / FuzzyRole)
// 以 00000000 为中心的星形图 (SQLite)
// ============================================================

// 详细信息子结构 (与后端 brain.hc.database.character/group/fuzzy_role 对齐)
export interface SocialAlias {
  name: string
  usage_count?: number
}

export interface SocialContent {
  content: string
}

export interface SocialContact {
  session_id: string
  usage_count?: number
}

export interface SocialRelationship {
  type: string
}

export interface SocialNode {
  id: string                 // uuid 或 00000000
  type: 'Root' | 'Character' | 'Group' | 'FuzzyRole'
  label: string
  uuid: string
  // 通用
  name?: string
  description?: string
  usage_count?: number
  last_used_at?: string | null
  created_at?: string | null
  updated_at?: string | null
  // Character / Group
  aliases?: SocialAlias[]
  impressions?: SocialContent[]
  familiarities?: SocialContent[]
  contacts?: SocialContact[]
  relationships_with_default?: SocialRelationship[]
  // Character / Group：允许 conversation_agent 对该实体的会话发起对话
  allow_conversation?: boolean
  // Character 特有
  affinity_events?: SocialContent[]
  // Group 特有
  member_impressions?: SocialContent[]
  // FuzzyRole 特有
  content?: string
  source_session_id?: string | null
  source_entity_type?: 'character' | 'group' | string | null
  source_entity_uuid?: string | null
  // FuzzyRole 依附的来源实体 (用于详情面板跳转)
  source_label?: string
}

export interface SocialEdge {
  source: string
  target: string
  type: string
  source_type?: string
  target_type?: string
}

export interface SocialMemorySnapshot {
  nodes: SocialNode[]
  edges: SocialEdge[]
  character_count: number
  group_count: number
  fuzzy_role_count: number
  root_id: string
}

// 记忆内容编辑 (PATCH /memory/social/{entity_type}/{entity_id})
// 只提交要改的字段; aliases 为字符串数组 (整组替换)
export interface MemorySocialUpdateRequest {
  name?: string
  aliases?: string[]
  impression?: string
  familiarity?: string
  relationship?: string
  member_impression?: string
  self_image?: string
  allow_conversation?: boolean
  content?: string
}

export interface MemorySocialUpdateResponse {
  node: SocialNode
  updated: boolean
}

// 删除社交实体响应 (DELETE /memory/social/{entity_type}/{entity_id})
export interface MemorySocialDeleteResponse {
  deleted: boolean
  entity_type: 'character' | 'group' | 'fuzzy_role'
  entity_id: string
  cascaded_fuzzy_roles: number
}

// ============================================================
// 记忆经验/嘱托 (bequests 表, 列表式查看)
// ============================================================

export interface ExperienceItem {
  bequest_id: string
  retrieval_count?: number
  last_maintained_at?: number | null
  manual_maintenance_count?: number
  title: string
  summary: string
  category: string            // 'speech'（话术经验）/ 'decision'（决策经验）
  start_time?: string | null
  updated_at?: string | null
}

export interface ExperienceList {
  experiences: ExperienceItem[]
  total: number
}

// 编辑经验的可提交字段（PATCH /memory/experiences/{bequest_id}）
export interface ExperienceUpdateRequest {
  title?: string
  summary?: string
}

export interface ExperienceUpdateResponse {
  experience: ExperienceItem
  updated: boolean
}

// 删除经验响应（DELETE /memory/experiences/{bequest_id}）
export interface ExperienceDeleteResponse {
  deleted: boolean
  bequest_id: string
}

// ============================================================
// 任务管理 (quest 长期任务, 列表式查看 / 编辑 / 删除; 后端 /events/*)
// ============================================================

export type TaskStatus = 'active' | 'expired'

export interface TaskInfo {
  id: string
  task_id: string
  title: string
  context: string         // 要传递给下次启动时的上下文 (目的/进展/线索/下次要做什么)
  status: TaskStatus
  next_occurrence?: string | null
  start_time?: string | null
  end_time?: string | null
  updated_at?: string | null
}

export interface TaskList {
  tasks: TaskInfo[]
  total: number
  status: string
  limit: number
  offset: number
}

// ============================================================
// Context Viewer
// ============================================================

// 事件链文件夹内的单个上下文文件 (固定名 {prefix}.json 或 legacy 时间戳名)
export interface ChainFileItem {
  kind: string | null                  // 从文件名解析 (chat|pfca|proactive|social_chat|quest|null)
  name: string
  size: number
  mtime: number
}

// 事件链文件夹 (一个任务链 = 一个文件夹, 内含各 agent 的上下文文件)
export interface ChainInfo {
  chain_id: string
  is_active: boolean                   // 进行中 (仍被写入)
  latest_mtime: number                 // 链内文件最新 mtime
  files: ChainFileItem[]
}

// GET /context/chains 响应
export interface ChainsResponse {
  chains: ChainInfo[]
  unlinked: { files: ChainFileItem[] } // 未关联链 (链外写入 + legacy 迁移)
  fetched_at: number
}

export interface ContextToolCall {
  id: string
  type?: string
  function: {
    name: string
    arguments: string  // arguments 在 OpenAI 协议里是 JSON 字符串
  }
}

export interface ContextEntry {
  role: string                              // user | assistant | system | tool
  content: string | null                    // assistant 调 tool 时为 null
  // tool 角色的回执字段
  name?: string                             // 工具函数名 (send_message ...)
  tool_call_id?: string                     // 对应 assistant.tool_calls[].id
  // assistant 角色的调用字段
  tool_calls?: ContextToolCall[]
  reasoning_content?: string | null         // deepseek 的思考链 (可选)
}

export interface ContextFileContent {
  kind: string | null
  name: string
  chain_id: string | null
  entries: ContextEntry[]
  raw_size: number
}

// ============================================================
// Amy 情绪状态
// ============================================================

export interface AmyThought {
  id: string
  act: string
  desc: string
  intensity: number
  create_time: number
  expire_time: number
  remaining_seconds: number
  category: 'external' | 'evaluation' | 'self_action' | 'pfca_regulation' | 'system' | 'final' | 'unknown'
}

export interface AmyMood {
  norepinephrine: number
  dopamine: number
}

export interface AmyActEntry {
  name: string
  ne_delta: number
  da_delta: number
  max_stack: number
  stack_coefficient: number
  duration: number
  system_id?: string | null
}

export interface AmyFinalEvaluationEntry {
  name: string
}

export interface AmyDescriptionBand {
  lo: number
  hi: number
  text: string
}

export interface AmyDescriptions {
  dopamine: AmyDescriptionBand[]
  norepinephrine: AmyDescriptionBand[]
  range: { min: number; max: number }
}

export interface AmyDebtEntry {
  title: string
  kind: string
  score: number
  create_time: number
}

export interface AmyPresence {
  value: number
  attribution_prompt: string
  debts: AmyDebtEntry[]
}

// 决策侧情感最近五条变动（两轴共用一份历史，按发生顺序）
export interface AmyFeedbackHistoryEntry {
  axis: 'competence' | 'recognition'
  label: string
  delta: number
  before: number
  after: number
  title: string
  reason: string
  time: number
}

// 决策侧双轴：value=归属感，competence_value=效能感（默认 50，范围 0-100）
export interface AmyDecisionFeedback {
  schema_version: number
  value: number              // 归属感
  competence_value: number   // 效能感
  competence_name: string
  recognition_name: string
  prompt: string
  history: AmyFeedbackHistoryEntry[]
}

export interface AmyState {
  initialized: boolean
  fetched_at?: number
  mood: AmyMood
  amy_description: string
  presence: AmyPresence
  decision_feedback?: AmyDecisionFeedback
  thoughts: {
    all_active_count: number
    all_active: AmyThought[]
    top: AmyThought[]
  }
  thought_cache_timestamp: number
  acts: {
    external: AmyActEntry[]
    evaluation: AmyActEntry[]
    self_action: AmyActEntry[]
    pfca_regulation: AmyActEntry[]
    system: AmyActEntry[]
    final: AmyFinalEvaluationEntry[]
  }
  amy_descriptions: AmyDescriptions
  error?: string
}

// ============================================================
// 记事本笔记 (notepad 状态查看, GET /notes)
// ============================================================

export interface NotepadNoteEntry {
  note_id: string
  text: string
  source: string
  session_id: string
  session_name: string
  usage: string
  record_time: string
}

export interface NotepadNoteDoc {
  title: string
  chain_id: string
  trigger: string
  begin_time: string
  finalize_time: string
  final_evaluation: string | null
  summary: string
  notes: NotepadNoteEntry[]
}

export interface NotesResponse {
  pfca_manual: string       // PFCA 当前可见的记事本手册 (render_manual(max_chars=1000))
  all: NotepadNoteDoc[]     // 全部未归档（活动池）笔记, 落库时间倒序
  unprocessed_chars: number // 未处理笔记字符数 (反思触发统计口径)
  fetched_at: number
}

// ============================================================
// Token 用量统计 (GET /token-stats)
// 数据源: utils/llm_client_manager 的 token_stats.json (v2 按天分桶)
// ============================================================

// 单维度分桶 (某 agent / 某 provider / 某 model 在某区间内的累计)
export interface TokenBucket {
  call_count: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
  cache_hit_tokens: number
  cache_miss_tokens: number
  reasoning_tokens: number
  cost_cache_miss: number
  cost_cache_hit: number
  cost_output: number
  cost_total: number
  models: string[]          // 该维度下出现过的模型
  providers: string[]       // 该维度下出现过的提供商
}

export interface TokenStatsDay {
  date: string              // YYYY-MM-DD
  totals: TokenBucket       // 单日合计 (仅按 by_agent 累加, 避免跨维度重复计数)
  by_agent: Record<string, TokenBucket>
  by_provider: Record<string, TokenBucket>
  by_model: Record<string, TokenBucket>
}

export interface TokenStatsResponse {
  range: { start: string; end: string; days: string[] }
  totals: TokenBucket
  per_day: TokenStatsDay[]
  by_agent: Record<string, TokenBucket>
  by_provider: Record<string, TokenBucket>
  by_model: Record<string, TokenBucket>
  updated_at: string | null
  fetched_at: number
}
