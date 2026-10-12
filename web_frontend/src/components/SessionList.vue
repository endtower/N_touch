<script setup lang="ts">
import { ElMessageBox } from 'element-plus'
import { Plus, ChatDotRound, UserFilled, MoreFilled } from '@element-plus/icons-vue'
import { chat } from '../stores/chat'

const emit = defineEmits<{
  'new-session': []
}>()

async function onDelete(sid: string, name: string, ev: Event) {
  ev.stopPropagation()
  try {
    await ElMessageBox.confirm(
      `删除会话「${name}」？仅删除 WebChannel 注册表里的会话配置，聊天历史会保留。`,
      '确认',
      { type: 'warning' },
    )
    chat.deleteSession(sid)
  } catch {
    /* 用户取消 */
  }
}
</script>

<template>
  <aside class="session-list">
    <div class="session-list-header">
      <div class="brand">
        <span class="brand-title">会话</span>
        <span v-if="chat.state.sessions.length" class="brand-count">{{ chat.state.sessions.length }}</span>
      </div>
      <button class="header-icon-btn" title="新建会话（需要 Core 运行）" :disabled="!chat.state.connected" @click="emit('new-session')">
        <el-icon :size="15"><Plus /></el-icon>
      </button>
    </div>

    <div v-if="chat.state.sessions.length === 0" class="empty">
      <el-icon :size="32" color="var(--text-3)"><ChatDotRound /></el-icon>
      <div style="font-weight: 500; color: var(--text-2);">还没有会话</div>
      <div style="font-size: 11px;">点右上「新建」开始</div>
    </div>

    <ul class="session-items">
      <li
        v-for="s in chat.state.sessions"
        :key="s.session_id"
        :class="[
          'session-item',
          { active: s.session_id === chat.state.activeSessionId },
        ]"
        @click="chat.setActive(s.session_id)"
      >
        <div class="session-icon">
          <el-icon v-if="s.scope === 'group'" :size="16"><ChatDotRound /></el-icon>
          <el-icon v-else :size="16"><UserFilled /></el-icon>
        </div>
        <div class="session-info">
          <div class="session-name">{{ s.session_name || s.user_name }}</div>
          <div class="session-sub">
            <span>{{ s.user_name }}</span>
            <span v-if="s.role" class="role-badge">{{ s.role }}</span>
          </div>
        </div>
        <button
          class="session-delete"
          :disabled="!chat.state.connected"
          title="删除会话"
          @click="onDelete(s.session_id, s.session_name || s.user_name, $event)"
        >
          <el-icon :size="12"><MoreFilled /></el-icon>
        </button>
      </li>
    </ul>
  </aside>
</template>
