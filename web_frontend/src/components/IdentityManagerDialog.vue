<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { Identity, SessionRuntime, WebSession } from '../types'
import { chat } from '../stores/chat'
import CreateIdentityDialog from './CreateIdentityDialog.vue'

const props = defineProps<{
  visible: boolean
  session: WebSession
}>()

const emit = defineEmits<{
  'update:visible': [v: boolean]
}>()

const createDialogVisible = ref(false)
const editingIdentity = ref<Identity | null>(null)

const runtime = computed<SessionRuntime>(() => {
  const sid = props.session.session_id
  const rt = chat.state.runtimesBySession[sid]
  if (rt) return rt
  return {
    session_id: sid,
    scope: props.session.scope,
    active_identity: {
      user_id: props.session.user_id,
      user_name: props.session.user_name,
      is_default: true,
      created_at: 0,
    },
    identity_lib: [],
  }
})

const isPrivate = computed(() => props.session.scope === 'private')

function isActive(i: Identity): boolean {
  const a = runtime.value.active_identity
  return i.user_id === a.user_id && i.user_name === a.user_name
}

async function onSwitch(i: Identity) {
  if (isActive(i)) return
  if (isPrivate.value) {
    try {
      await ElMessageBox.confirm(
        '你在私聊中切换身份, 可能导致对话上下文与历史消息不匹配, 建议仅用于测试。\n\n确认要切换吗？',
        '私聊切身份',
        { confirmButtonText: '切换', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  chat.switchIdentity({
    session_id: props.session.session_id,
    user_id: i.user_id,
    user_name: i.user_name,
  })
}

function onCreate() {
  editingIdentity.value = null
  createDialogVisible.value = true
}

function onEdit(i: Identity) {
  editingIdentity.value = i
  createDialogVisible.value = true
}

async function onDelete(i: Identity) {
  if (isActive(i)) {
    ElMessage.warning('当前激活的身份不能直接删, 先切到别的再删')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定删除身份 "${i.user_name}" (id=${i.user_id}) 吗？`,
      '删除身份',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  chat.deleteIdentity({
    session_id: props.session.session_id,
    user_id: i.user_id,
    user_name: i.user_name,
  })
}

function onDialogSubmit(payload: { user_id: string; user_name: string; switch_to: boolean }) {
  if (editingIdentity.value) {
    chat.updateIdentity({
      session_id: props.session.session_id,
      old_user_id: editingIdentity.value.user_id,
      old_user_name: editingIdentity.value.user_name,
      new_user_id: payload.user_id,
      new_user_name: payload.user_name,
    })
  } else {
    chat.createIdentity({
      session_id: props.session.session_id,
      user_id: payload.user_id,
      user_name: payload.user_name,
      switch_to: payload.switch_to,
    })
  }
  createDialogVisible.value = false
}

function close() {
  emit('update:visible', false)
}
</script>

<template>
  <AppDialog
    :model-value="props.visible"
    title="角色管理"
    width="560"
    :close-on-click-modal="false"
    align-center
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <div class="mgr-scope">
      <span class="mgr-scope-label">会话</span>
      <strong class="mgr-scope-name">{{ props.session.user_name }}</strong>
      <span class="mgr-scope-meta">
        ({{ props.session.session_id }}, {{ isPrivate ? '私聊' : '群聊' }})
      </span>
      <span v-if="isPrivate" class="mgr-scope-warn">⚠ 私聊建议不要切换身份</span>
    </div>

    <div v-if="runtime.identity_lib.length === 0" class="mgr-empty">
      <div class="mgr-empty-icon">👤</div>
      <div>该会话还没有任何身份</div>
      <div class="mgr-empty-hint">点下方「+ 新建身份」开始</div>
    </div>

    <div v-else class="mgr-list">
      <div
        v-for="(i, idx) in runtime.identity_lib"
        :key="`${i.user_id}-${i.user_name}-${idx}`"
        :class="['mgr-item', isActive(i) ? 'active' : '']"
      >
        <div class="mgr-item-main">
          <div class="mgr-item-left">
            <span class="mgr-item-name">{{ i.user_name }}</span>
            <span class="mgr-item-id">({{ i.user_id }})</span>
            <span v-if="i.is_default" class="mgr-item-badge">默认</span>
          </div>
          <span v-if="isActive(i)" class="mgr-item-check">✓ 当前</span>
        </div>
        <div class="mgr-item-actions">
          <el-button
            v-if="!isActive(i)"
            size="small"
            type="primary"
            plain
            @click="onSwitch(i)"
          >
            切换
          </el-button>
          <el-button size="small" plain @click="onEdit(i)">改名</el-button>
          <el-button
            v-if="!isActive(i)"
            size="small"
            type="danger"
            plain
            @click="onDelete(i)"
          >
            删除
          </el-button>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
      <el-button type="primary" @click="onCreate">+ 新建身份</el-button>
    </template>
  </AppDialog>

  <CreateIdentityDialog
    v-model:visible="createDialogVisible"
    :editing="editingIdentity"
    @submit="onDialogSubmit"
  />
</template>

<style scoped>
/* ============================================================
   角色管理弹窗 — 完整黑夜/浅色双主题
   ============================================================ */

.mgr-scope {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
  padding-bottom: 14px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border-1);
  color: var(--text-2);
}
.mgr-scope-label {
  color: var(--text-3);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.mgr-scope-name {
  color: var(--text-1);
  font-size: 14px;
  font-weight: 600;
}
.mgr-scope-meta {
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.mgr-scope-warn {
  margin-left: auto;
  color: var(--warn);
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.mgr-empty {
  padding: 40px 24px;
  text-align: center;
  color: var(--text-3);
  background: var(--bg-subtle);
  border: 1px dashed var(--border-1);
  border-radius: var(--radius);
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.mgr-empty-icon {
  font-size: 28px;
  opacity: 0.5;
  margin-bottom: 4px;
}
.mgr-empty-hint {
  font-size: 11.5px;
  color: var(--text-3);
  margin-top: 2px;
}

.mgr-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 480px;
  overflow-y: auto;
  padding-right: 4px;
}

.mgr-item {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--radius);
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  transition: all 0.15s ease;
}
.mgr-item:hover {
  border-color: var(--border-2);
}
.mgr-item.active {
  background: var(--accent-soft);
  border-color: var(--accent);
}

.mgr-item-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}
.mgr-item-left {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  flex-wrap: wrap;
}
.mgr-item-name {
  font-weight: 600;
  color: var(--text-1);
  font-size: 14px;
  letter-spacing: -0.01em;
}
.mgr-item.active .mgr-item-name {
  color: var(--accent);
}
.mgr-item-id {
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.mgr-item-badge {
  font-size: 10px;
  padding: 1px 7px;
  background: var(--bg-subtle);
  color: var(--text-2);
  border-radius: 999px;
  font-weight: 500;
  letter-spacing: 0.02em;
}
.mgr-item-check {
  color: var(--accent-2);
  font-weight: 600;
  font-size: 12px;
  background: var(--accent-2-soft);
  padding: 2px 8px;
  border-radius: 999px;
  flex-shrink: 0;
}

.mgr-item-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
</style>