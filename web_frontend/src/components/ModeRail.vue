<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatDotRound, Monitor } from '@element-plus/icons-vue'
import { chat } from '../stores/chat'
import { channel } from '../api/channel'
import type { ServerMessage } from '../types'

// 测试模式切换（最左侧标签式按钮）
// 启动测试模式时:
//   1. 关闭前端 truck 菜单里所有"运行中"的 truck
//   2. 通知后端重定向两个存储单例的数据库 (chat_history / memory)
//   3. 后端确认后清空缓存 + 重连 WS 重新拉取会话/历史
type Mode = 'real' | 'test'

const mode = computed<Mode>(() => chat.state.mode)
const inTest = computed(() => mode.value === 'test')

const pendingSwitch = ref<Mode | null>(null)

function switchMode(target: Mode) {
  if (target === mode.value || pendingSwitch.value != null) return
  const enable = target === 'test'
  if (enable) {
    // 1. 关闭 truck 菜单里所有运行中的 truck
    const running = chat.state.trucks.filter(t => t.enabled)
    for (const t of running) {
      chat.toggleTruck(t.truck_id, false)
    }
    ElMessage.success(`正在进入测试模式，已关闭 ${running.length} 个运行中的 truck…`)
  } else {
    ElMessage.success('正在切回对话模式…')
  }
  pendingSwitch.value = target
  // 2. 通知后端重定向存储数据库
  channel.send({ type: 'switch_test_mode', enable })
}

function onServerMessage(msg: ServerMessage) {
  if (msg.type !== 'test_mode_switched') return
  if (pendingSwitch.value == null) return
  const target = pendingSwitch.value
  pendingSwitch.value = null
  if (!msg.ok) {
    ElMessage.error('切换存储库失败')
    return
  }
  chat.setMode(target)
  ElMessage.success(target === 'test' ? '已进入测试模式' : '已切回对话模式')
  // 3. 清空缓存并重连 WS，重新拉取会话列表与聊天记录
  chat.clearAll()
  channel.reconnect()
}

let unsub: (() => void) | null = null
onMounted(() => { unsub = channel.onMessage(onServerMessage) })
onBeforeUnmount(() => { unsub?.() })
</script>

<template>
  <aside class="mode-rail">
    <!-- 标签式按钮：点击切换系统模式 -->
    <button
      class="mode-btn"
      :class="{ active: !inTest }"
      title="对话模式"
      @click="switchMode('real')"
    >
      <el-icon :size="15"><ChatDotRound /></el-icon>
      <span class="mode-label">对话</span>
    </button>

    <button
      class="mode-btn"
      :class="{ active: inTest }"
      title="测试模式（切换存储库到测试隔离环境）"
      @click="switchMode('test')"
    >
      <el-icon :size="15"><Monitor /></el-icon>
      <span class="mode-label">测试</span>
    </button>
  </aside>
</template>

<style scoped>
.mode-rail {
  flex: 0 0 46px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 10px 0;
  min-height: 0;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-1);
}

/* 标签式按钮 */
.mode-btn {
  width: 38px;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 5px 0;
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-3);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.12s ease;
}

.mode-label {
  font-size: 10px;
  line-height: 1;
}

.mode-btn:hover {
  background: var(--bg-subtle);
  color: var(--text-1);
}

.mode-btn.active {
  background: var(--accent-soft);
  color: var(--accent);
}
</style>