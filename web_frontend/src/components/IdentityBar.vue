<script setup lang="ts">
import { computed, ref } from 'vue'
import { User, Setting, Plus, Lock, Warning, ArrowDown, Check, CircleClose, Loading } from '@element-plus/icons-vue'
import type { Identity, SessionRuntime, WebSession } from '../types'
import { chat } from '../stores/chat'
import IdentityManagerDialog from './IdentityManagerDialog.vue'
import CreateIdentityDialog from './CreateIdentityDialog.vue'

const props = defineProps<{ session: WebSession }>()

const popoverVisible = ref(false)
const managerVisible = ref(false)
const createDialogVisible = ref(false)
const editingIdentity = ref<Identity | null>(null)

// runtime 可能为空 → 用 session 自身做 fallback, 让按钮能点
const runtime = computed<SessionRuntime>(() => {
  const sid = props.session.session_id
  const rt = chat.state.runtimesBySession[sid]
  if (rt) return rt
  const placeholder: Identity = {
    user_id: props.session.user_id,
    user_name: props.session.user_name,
    is_default: true,
    created_at: 0,
  }
  return {
    session_id: sid,
    scope: props.session.scope,
    active_identity: placeholder,
    identity_lib: [placeholder],
  }
})

const active = computed(() => runtime.value.active_identity)
const isPrivate = computed(() => props.session.scope === 'private')

// 系统当前状态展示 (后端维护单一 activity 状态, 前端只渲染):
//   - activity 存在且 status='tool_call':
//       source='proactive' → "proactive 调用 xxx"
//       其他 (pfca) → "LLM xxx"
//   - activity 存在且 status='active' (模块活跃但未调工具):
//       source='proactive' → "proactive 处理中"
//       其他 → "系统处理中…"
//   - activity 为 null (空闲):
//       event_chain_active=false → "系统未激活"
//       event_chain_active=true → "系统处理中…"
//       null → "同步中…"
const eventChainActive = computed<boolean | null>(() => {
  const v = chat.state.globalState.event_chain_active
  if (v === null || v === undefined) return null
  return v
})
const activity = computed(() => chat.state.activity)

function openManager() {
  popoverVisible.value = false
  managerVisible.value = true
}

function openCreate() {
  popoverVisible.value = false
  editingIdentity.value = null
  createDialogVisible.value = true
}

function switchTo(id: Identity) {
  if (id.user_id === active.value.user_id) {
    popoverVisible.value = false
    return
  }
  chat.switchIdentity({
    session_id: props.session.session_id,
    user_id: id.user_id,
    user_name: id.user_name,
  })
  popoverVisible.value = false
}

function onCreateSubmit(payload: { user_id: string; user_name: string; switch_to: boolean }) {
  chat.createIdentity({
    session_id: props.session.session_id,
    user_id: payload.user_id,
    user_name: payload.user_name,
    switch_to: payload.switch_to,
  })
  createDialogVisible.value = false
}
</script>

<template>
  <div class="identity-row">
    <div class="identity-row-left">
      <!-- 活动状态: 后端维护单一 activity, 前端只渲染 -->
      <!-- 1. proactive 正在调工具 → "proactive 调用 xxx" -->
      <span
        v-if="activity && activity.status === 'tool_call' && activity.source === 'proactive' && activity.args_summary"
        class="identity-hint-activity"
        :title="`proactive 正在调用: ${activity.args_summary}`"
      >
        <el-icon :size="13"><Loading /></el-icon>
        <span class="activity-text">proactive 调用 {{ activity.args_summary }}</span>
      </span>
      <!-- 2. LLM (pfca) 正在调用具体工具 -->
      <span
        v-else-if="activity && activity.status === 'tool_call' && activity.args_summary"
        class="identity-hint-activity"
        :title="`LLM 正在: ${activity.args_summary}`"
      >
        <el-icon :size="13"><Loading /></el-icon>
        <span class="activity-text">LLM {{ activity.args_summary }}</span>
      </span>
      <!-- 3. 模块活跃但未调工具 (proactive 启动后思考中) → "proactive 处理中" -->
      <span
        v-else-if="activity && activity.status === 'active' && activity.source === 'proactive'"
        class="identity-hint-proactive"
        title="proactive 会话激活, LLM 思考中"
      >
        <el-icon :size="13"><Loading /></el-icon>
        proactive 处理中
      </span>
      <!-- 4. 系统未激活 -->
      <span
        v-else-if="eventChainActive === false"
        class="identity-hint-inactive"
        title="系统尚未激活"
      >
        <el-icon :size="13"><CircleClose /></el-icon>
        系统未激活
      </span>
      <!-- 5. 系统激活 + LLM 思考中 -->
      <span
        v-else-if="eventChainActive === true"
        class="identity-hint-active-idle"
        title="系统已激活"
      >
        <el-icon :size="13"><Loading /></el-icon>
        系统处理中…
      </span>
      <!-- 6. 尚未同步系统状态 (初次握手前) -->
      <span
        v-else
        class="identity-hint-empty"
        title="等待系统状态同步"
      >
        <el-icon :size="13"><Warning /></el-icon>
        同步中…
      </span>
    </div>

    <div class="identity-row-right">
      <el-popover
        v-model:visible="popoverVisible"
        placement="top-end"
        :width="280"
        :show-arrow="false"
        trigger="click"
        popper-class="identity-popover-root"
      >
        <template #reference>
          <button
            type="button"
            class="identity-trigger"
            :title="`点击切换身份或管理角色`"
          >
            <el-icon :size="13" color="var(--text-2)"><User /></el-icon>
            <span class="identity-trigger-name">{{ active.user_name }}</span>
            <span class="identity-trigger-id">({{ active.user_id }})</span>
            <el-icon
              :size="11"
              color="var(--text-3)"
              class="trigger-arrow"
            >
              <ArrowDown />
            </el-icon>
          </button>
        </template>

        <div class="identity-menu">
          <!-- 切换身份列表 (一级选项卡) -->
          <div class="identity-menu-section">
            <div class="identity-menu-header">切换身份</div>
            <ul class="identity-menu-list">
              <li
                v-for="id in runtime.identity_lib"
                :key="id.user_id"
                :class="['identity-menu-item', { active: id.user_id === active.user_id }]"
                @click="switchTo(id)"
              >
                <div class="identity-menu-name">
                  <el-icon
                    v-if="id.user_id === active.user_id"
                    :size="13"
                    color="var(--accent)"
                  ><Check /></el-icon>
                  <span v-else class="identity-menu-check-spacer"></span>
                  <span>{{ id.user_name }}</span>
                </div>
                <span class="identity-menu-id">{{ id.user_id }}</span>
              </li>
            </ul>
          </div>

          <!-- 二级选项卡: 私聊提示 (从上方 hint 区移入, 仅私聊显示) -->
          <div v-if="isPrivate" class="identity-menu-subtab">
            <div class="identity-menu-subtab-icon">
              <el-icon :size="12" color="var(--warn)"><Lock /></el-icon>
            </div>
            <div class="identity-menu-subtab-text">
              <div class="identity-menu-subtab-title">私聊注意事项</div>
              <div class="identity-menu-subtab-desc">
                切换身份可能导致对话上下文与历史消息不匹配，建议仅用于测试。
              </div>
            </div>
          </div>

          <!-- 操作按钮 -->
          <div class="identity-menu-actions">
            <button class="identity-menu-btn" @click="openManager">
              <el-icon :size="13"><Setting /></el-icon>
              <span>管理角色</span>
            </button>
            <button class="identity-menu-btn" @click="openCreate">
              <el-icon :size="13"><Plus /></el-icon>
              <span>新建</span>
            </button>
          </div>
        </div>
      </el-popover>
    </div>

    <IdentityManagerDialog
      v-model:visible="managerVisible"
      :session="props.session"
    />
    <CreateIdentityDialog
      v-model:visible="createDialogVisible"
      :editing="editingIdentity"
      @submit="onCreateSubmit"
    />
  </div>
</template>

<style scoped>
.identity-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 4px;
  font-size: 13px;
  color: var(--text-1);
}

.identity-row-left {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.identity-hint-private {
  color: var(--warn);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.identity-hint-inactive {
  color: var(--text-3);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-style: italic;
}
.identity-hint-active-idle {
  color: var(--text-2);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.identity-hint-proactive {
  color: var(--accent);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 500;
}
.identity-hint-empty {
  color: var(--danger);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.identity-row-right {
  flex: 0 0 auto;
}

/* === 触发按钮: 一个整体, 带边框, 像 input 的右侧 === */
.identity-trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  font-size: 13px;
  color: var(--text-1);
  cursor: pointer;
  transition: all 0.12s ease;
  font-family: inherit;
  line-height: 1.4;
}
.identity-trigger:hover {
  border-color: var(--border-2);
  background: var(--bg-subtle);
}
.identity-trigger:active {
  transform: translateY(0.5px);
}
.identity-trigger-name {
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: -0.01em;
}
.identity-trigger-id {
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.trigger-arrow {
  margin-left: 2px;
  transition: transform 0.15s ease;
}
</style>

<style>
/* Popover 全局样式 (Element Plus popover 需要全局, scoped 不会生效到 teleport) */
.identity-popover-root {
  padding: 0 !important;
  border-radius: var(--radius) !important;
  border: 1px solid var(--border-1) !important;
  box-shadow: var(--shadow-3) !important;
  background: var(--bg-surface) !important;
}

.identity-popover-root .identity-menu {
  display: flex;
  flex-direction: column;
  padding: 6px;
  min-width: 240px;
}

.identity-popover-root .identity-menu-section {
  display: flex;
  flex-direction: column;
}

.identity-popover-root .identity-menu-header {
  padding: 6px 10px 4px;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.identity-popover-root .identity-menu-list {
  list-style: none;
  margin: 0;
  padding: 0 0 4px;
  max-height: 240px;
  overflow-y: auto;
}

.identity-popover-root .identity-menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background 0.1s ease;
  font-size: 13px;
  color: var(--text-1);
}
.identity-popover-root .identity-menu-item:hover {
  background: var(--bg-subtle);
}
.identity-popover-root .identity-menu-item.active {
  background: var(--accent-soft);
  color: var(--accent);
}
.identity-popover-root .identity-menu-item.active .identity-menu-id {
  color: var(--accent);
  opacity: 0.7;
}

.identity-popover-root .identity-menu-name {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.identity-popover-root .identity-menu-check-spacer {
  display: inline-block;
  width: 13px;
}
.identity-popover-root .identity-menu-name span:not(.identity-menu-check-spacer) {
  font-weight: 500;
}
.identity-popover-root .identity-menu-id {
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.identity-popover-root .identity-menu-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  padding: 6px 0 2px;
  border-top: 1px solid var(--border-1);
  margin-top: 4px;
}

/* === 二级选项卡: 私聊注意事项 === */
.identity-popover-root .identity-menu-subtab {
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  margin: 6px 0 2px;
  border-radius: var(--radius-sm);
  background: rgba(230, 162, 60, 0.08);
  border: 1px solid rgba(230, 162, 60, 0.18);
  font-size: 11.5px;
  line-height: 1.45;
}
.identity-popover-root .identity-menu-subtab-icon {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: flex-start;
  padding-top: 1px;
}
.identity-popover-root .identity-menu-subtab-text {
  flex: 1 1 auto;
  min-width: 0;
}
.identity-popover-root .identity-menu-subtab-title {
  font-weight: 600;
  color: var(--warn);
  margin-bottom: 2px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.identity-popover-root .identity-menu-subtab-desc {
  color: var(--text-2);
  font-size: 11.5px;
}

.identity-popover-root .identity-menu-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.1s ease;
}
.identity-popover-root .identity-menu-btn:hover {
  background: var(--bg-subtle);
  color: var(--text-1);
}
</style>