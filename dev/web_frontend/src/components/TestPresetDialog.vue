<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MagicStick, Loading, Delete, CopyDocument } from '@element-plus/icons-vue'
import type { PresetBlock, PresetBlocks, ServerMessage } from '../types'
import { channel } from '../api/channel'
import { chat } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

// 分块预设清单: { memory: [...], chat: [...], amy: [...] }
const blocks = ref<PresetBlocks>({ memory: [], chat: [], amy: [] })
const loaded = ref(false)
const loading = ref(false)
// 各分块当前选择（空字符串 = 不修改该块）
const sel = ref<Record<PresetBlock, string>>({ memory: '', chat: '', amy: '' })
const busy = ref<Record<PresetBlock, boolean>>({ memory: false, chat: false, amy: false })
const applying = ref(false)

// 分块显示名（memory = 记忆）
const BLOCK_LABEL: Record<PresetBlock, string> = {
  memory: '记忆 (memory)',
  chat: '消息记录 (chat_history)',
  amy: '情绪 (amy)',
}

// 弹窗打开时拉一次分块预设清单
watch(
  () => props.visible,
  (v) => {
    if (v) {
      loaded.value = false
      channel.send({ type: 'list_presets' })
    }
  },
)

function applyEnv() {
  if (applying.value) return
  applying.value = true
  const payload: { memory?: string; chat?: string; amy?: string } = {}
  for (const b of ['memory', 'chat', 'amy'] as PresetBlock[]) {
    if (sel.value[b]) payload[b] = sel.value[b]
  }
  channel.send({ type: 'apply_env', ...payload })
}

// 弹出命名框（默认当前时间），确认返回名字，取消返回 null
async function namePrompt(block: PresetBlock, title: string): Promise<string | null> {
  const defaultName = `预设_${timestampName()}`
  try {
    const res = await ElMessageBox.prompt(
      '请输入预设名（留空则使用当前时间）',
      title,
      {
        inputValue: defaultName,
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        inputPlaceholder: '预设名',
      },
    )
    return (res.value || '').trim() || defaultName
  } catch {
    return null
  }
}

async function captureMirror(block: PresetBlock) {
  if (busy.value[block]) return
  const name = await namePrompt(block, `抓取 ${BLOCK_LABEL[block]} 正式环境镜像`)
  if (name === null) return
  busy.value[block] = true
  channel.send({ type: 'capture_mirror', block, name })
}

async function copyState(block: PresetBlock) {
  if (busy.value[block]) return
  const name = await namePrompt(block, `复制当前 ${BLOCK_LABEL[block]} 状态`)
  if (name === null) return
  busy.value[block] = true
  channel.send({ type: 'copy_state', block, name })
}

async function deletePreset(block: PresetBlock, name: string) {
  try {
    await ElMessageBox.confirm(`确定删除 ${BLOCK_LABEL[block]} 的预设「${name}」？`, '删除预设', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  channel.send({ type: 'delete_preset', block, name })
}

function timestampName(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
    `_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  )
}

function onServerMessage(msg: ServerMessage) {
  if (msg.type === 'presets') {
    blocks.value = msg.blocks
    loaded.value = true
    loading.value = false
    return
  }
  if (msg.type === 'env_applied') {
    applying.value = false
    if (!msg.ok) {
      ElMessage.error('环境准备失败')
      return
    }
    const parts: string[] = []
    if (msg.memory) parts.push(`记忆=${msg.memory}`)
    if (msg.chat) parts.push(`消息=${msg.chat}`)
    if (msg.amy) parts.push(`amy=${msg.amy}`)
    ElMessage.success(`已准备环境: ${parts.join('，') || '未选择分块'}`)
    // 切换后清缓存重连 WS，拉取最新会话/记录
    chat.clearAll()
    channel.reconnect()
    return
  }
  if (msg.type === 'mirror_captured') {
    busy.value[msg.block] = false
    if (!msg.ok) {
      ElMessage.error(`镜像抓取失败: ${msg.name}`)
      return
    }
    ElMessage.success(`已抓取 ${BLOCK_LABEL[msg.block]} 镜像: ${msg.name}`)
    channel.send({ type: 'list_presets' })
    return
  }
  if (msg.type === 'state_copied') {
    busy.value[msg.block] = false
    if (!msg.ok) {
      ElMessage.error(`复制失败: ${msg.name}`)
      return
    }
    ElMessage.success(`已复制当前 ${BLOCK_LABEL[msg.block]} 状态: ${msg.name}`)
    channel.send({ type: 'list_presets' })
    return
  }
  if (msg.type === 'preset_deleted') {
    if (!msg.ok) {
      ElMessage.error(`删除失败: ${msg.name}`)
      return
    }
    ElMessage.success(`已删除预设: ${msg.name}`)
    if (sel.value[msg.block] === msg.name) sel.value[msg.block] = ''
    channel.send({ type: 'list_presets' })
  }
}

let unsub: (() => void) | null = null
watch(
  () => props.visible,
  (v) => {
    if (v) {
      unsub = channel.onMessage(onServerMessage)
    } else {
      unsub?.()
      unsub = null
    }
  },
)
</script>

<template>
  <AppDialog
    :model-value="visible"
    title="环境准备 (预设)"
    width="620px"
    @update:model-value="emit('update:visible', $event)"
  >
    <div class="preset-hint">
      仅进行环境准备：将 记忆 / 消息记录 / amy 还原为所选预设（不再执行进阶触发操作）。
      每个分块可独立选用预设并自由组合；未选中的分块保持不变。
      点击"抓取镜像"抓取正式环境、"复制当前状态"保存当前测试状态（均需命名，留空默认当前时间）；
      删除预设请点击预设下拉中某项右侧的删除按钮。
    </div>

    <div v-if="!loaded && !loading" class="preset-empty">
      <el-icon class="is-loading" :size="18"><Loading /></el-icon>
      <span>加载预设清单中…</span>
    </div>

    <div v-else class="preset-blocks">
      <div v-for="b in (['memory', 'chat', 'amy'] as PresetBlock[])" :key="b" class="preset-block">
        <div class="block-head">
          <span class="block-label">{{ BLOCK_LABEL[b] }}</span>
          <el-select v-model="sel[b]" size="small" class="block-select" placeholder="选择预设">
            <el-option label="（不修改）" value="" />
            <el-option
              v-for="name in blocks[b]"
              :key="name"
              :label="name"
              :value="name"
            >
              <div class="preset-option">
                <span class="preset-option-name">{{ name }}</span>
                <el-button
                  class="preset-option-del"
                  size="small"
                  :icon="Delete"
                  text
                  type="danger"
                  @click.stop="deletePreset(b, name)"
                />
              </div>
            </el-option>
          </el-select>
          <el-button
            size="small"
            :icon="MagicStick"
            :loading="busy[b]"
            @click="captureMirror(b)"
          >
            抓取镜像
          </el-button>
          <el-button size="small" :icon="CopyDocument" @click="copyState(b)">
            复制当前状态
          </el-button>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button size="small" :disabled="!loaded" @click="emit('update:visible', false)">
        关闭
      </el-button>
      <el-button
        size="small"
        type="primary"
        :loading="applying"
        :disabled="!loaded"
        @click="applyEnv"
      >
        应用环境
      </el-button>
    </template>
  </AppDialog>
</template>

<style scoped>
.preset-hint {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.6;
  background: var(--bg-subtle);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  margin-bottom: 12px;
}

.preset-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-3);
  font-size: 13px;
  padding: 20px 0;
  justify-content: center;
}

.preset-blocks {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.preset-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
}

.block-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.block-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  flex: 0 0 172px;
  line-height: 1;
  white-space: nowrap;
}

.block-select {
  flex: 1;
}

.preset-option {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.preset-option-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-option-del {
  flex-shrink: 0;
}
</style>
