<script setup lang="ts">
import { computed, ref } from 'vue'
import { User, ChatDotRound, UserFilled, Picture, Document, Delete, Check } from '@element-plus/icons-vue'
import type { UiMessage, WebSession } from '../types'
import { chat } from '../stores/chat'

const props = defineProps<{
  message: UiMessage
  activeUserId?: string | null   // 当前 active identity 的 user_id (用于判断"我扮演的角色")
  selectMode?: boolean           // 多选模式: 点击消息=切换选中, 显示勾选框
  selected?: boolean             // 多选模式下本条是否被选中
}>()

const emit = defineEmits<{
  'request-delete': [message_id: string]
  'toggle-select': [message_id: string]
  'enter-select-mode': [message_id: string]
}>()

// 是否"我扮演的角色": 消息的 user_id 与当前 active identity 一致 → 右边
const isMine = computed(() => {
  if (!props.message.user_id || !props.activeUserId) return false
  return props.message.user_id === props.activeUserId
})

// ---- 右键菜单 ----
const contextMenu = ref<{ x: number; y: number } | null>(null)

function onContextMenu(ev: MouseEvent) {
  ev.preventDefault()
  // 屏幕坐标即可, 不需要相对 row; clamp 到视口内, 避免菜单在右/下边缘被裁切
  const MENU_W = 160
  const MENU_H = 120
  const x = Math.max(8, Math.min(ev.clientX, window.innerWidth - MENU_W - 8))
  const y = Math.max(8, Math.min(ev.clientY, window.innerHeight - MENU_H - 8))
  contextMenu.value = { x, y }
}

function closeContextMenu() {
  contextMenu.value = null
}

function onDelete() {
  const id = props.message.id
  closeContextMenu()
  // 本地立即移除, 同时通知后端真删
  chat.deleteMessage({ session_id: props.message.session_id, message_id: id })
}

// 右键菜单: 进入多选模式, 预选中本条
function onSelectMode() {
  closeContextMenu()
  emit('enter-select-mode', props.message.id)
}

// 多选模式下点击消息 = 切换选中.
// 只响应实际内容区域 (气泡/头像/头部/勾选框) 的点击, 行内空白交给父级框选;
// 按下/抬起距离超阈值视为拖拽 (可能以框选结束), 不触发点选.
const MSG_CONTENT_SEL = '.msg-bubble, .msg-avatar, .msg-head, .msg-check'
let rowDownPos: { x: number; y: number } | null = null

function onRowPointerDown(ev: PointerEvent) {
  if (!props.selectMode) return
  rowDownPos = { x: ev.clientX, y: ev.clientY }
}

function onRowClick(ev: MouseEvent) {
  if (!props.selectMode) return
  const target = ev.target as HTMLElement | null
  if (!target || !target.closest(MSG_CONTENT_SEL)) return
  if (
    rowDownPos &&
    Math.hypot(ev.clientX - rowDownPos.x, ev.clientY - rowDownPos.y) > 6
  ) {
    rowDownPos = null
    return
  }
  rowDownPos = null
  emit('toggle-select', props.message.id)
}

// 全局点击/滚动时关闭菜单
if (typeof window !== 'undefined') {
  window.addEventListener('click', closeContextMenu)
  window.addEventListener('scroll', closeContextMenu, true)
}

// ---- 头部 ----
const roleLabel = computed(() => {
  if (props.message.role === 'user') return props.message.user_name || '我'
  if (props.message.role === 'assistant') return 'LLM'
  return props.message.user_name || '其他'
})

const showHeader = computed(() => {
  return props.message.role !== 'user'
})

const timeStr = computed(() => {
  const t = props.message.timestamp
  if (!t) return ''
  const d = new Date(t * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  if (props.message._fromFile) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})

const avatar = computed(() => {
  if (props.message.role === 'user') return User
  if (props.message.role === 'assistant') return ChatDotRound
  return UserFilled
})

// ---- 图片 URL 处理 ----
// 后端（新代码）会返回带 /media/images/2026-06-22/xxx 的完整 URL
// 但如果后端是旧版, 只会返回 basename (例如 "4B83...jpg")
// 我们前端再 fallback 一次: 拼成 /media/<basename>
function normalizeImgUrl(u: string): string {
  if (!u) return ''
  if (u.startsWith('data:')) return u
  if (u.startsWith('http://') || u.startsWith('https://')) return u
  if (u.startsWith('/')) return u
  // basename, 拼成 /media/<basename>
  return `/media/${u}`
}

const imageUrls = computed(() =>
  (props.message.images || [])
    .map(normalizeImgUrl)
    .filter(u => u && !u.includes('unresolved=1')),  // 过滤掉后端没找到的
)

// 把 basename 提取出来, 用于和 inline [图片:xxx] 配对
function baseName(p: string): string {
  return p.replace(/\\/g, '/').split('?')[0].split('/').pop() || p
}

// ---- Inline 文本解析 ----
// content_text 里包含 [图片:xxx] / [视频:xxx] / [文件:xxx] / [语音] / [富文本]
// 策略:
//   - [图片:xxx] 且能匹配到已渲染图片 -> 不显示 (避免重复)
//   - [图片:xxx] 但没匹配到图片 -> 显示一个轻量占位符
//   - [视频:xxx] / [文件:xxx] / [语音] / [富文本] -> 显示对应标签
interface InlinePart {
  kind: 'text' | 'image' | 'video' | 'file' | 'audio' | 'json' | 'other'
  text: string
  filename?: string
  matched: boolean   // 配到已渲染的图片就不重复显示
}

function parseInline(s: string): InlinePart[] {
  if (!s) return []
  const parts: InlinePart[] = []
  const re = /\[(图片|视频|文件|语音|富文本)(?::([^\]]+))?\]/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) {
      parts.push({ kind: 'text', text: s.slice(last, m.index), matched: true })
    }
    const tag = m[1]
    const filename = m[2]
    const kind: InlinePart['kind'] = (
      { 图片: 'image', 视频: 'video', 文件: 'file', 语音: 'audio', 富文本: 'json' } as const
    )[tag] || 'other'

    // 图片: 尝试匹配已渲染的 image URL
    let matched = false
    if (kind === 'image' && filename) {
      const want = baseName(filename)
      const idx = imageUrls.value.findIndex(u => baseName(u) === want)
      if (idx >= 0) {
        matched = true
      }
    }

    parts.push({ kind, text: m[0], filename, matched })
    last = re.lastIndex
  }
  if (last < s.length) {
    parts.push({ kind: 'text', text: s.slice(last), matched: true })
  }
  return parts
}

const inlineParts = computed(() => parseInline(props.message.text || ''))
</script>

<template>
  <div
    :class="['msg-row', message.role, { mine: isMine, selectable: selectMode, selected }]"
    :data-message-id="message.id"
    @click="onRowClick"
    @pointerdown="onRowPointerDown"
    @contextmenu="onContextMenu"
  >
    <!-- 多选勾选框 -->
    <div v-if="selectMode" :class="['msg-check', { checked: selected }]">
      <el-icon v-if="selected" :size="12"><Check /></el-icon>
    </div>

    <div :class="['msg-avatar', message.role]">
      <el-icon :size="15"><component :is="avatar" /></el-icon>
    </div>

    <div :class="['msg-body', message.role]">
      <div v-if="showHeader" class="msg-head">
        <span class="msg-name">{{ roleLabel }}</span>
        <span class="msg-time">{{ timeStr }}</span>
      </div>

      <div class="msg-bubble">
        <!-- inline [图片:xxx] 已经在文本里被引用过的图片, 走 inline-tag (不重复显示缩略图) -->
        <!-- 但实际上 matched=true 的图片就不显示 tag 了, 只剩文本. 所以这块模板主要是 unmatched -->

        <!-- 文本 + 未匹配到已渲染图片的内联占位符 -->
        <div v-if="inlineParts.length" class="msg-text">
          <template v-for="(p, i) in inlineParts" :key="i">
            <span v-if="p.kind === 'text'">{{ p.text }}</span>
            <span
              v-else-if="p.kind === 'image' && !p.matched"
              :class="['inline-tag', 'tag-image', 'tag-missing']"
              :title="p.filename"
            >
              🖼 {{ p.filename }}
            </span>
            <span
              v-else-if="p.kind === 'video'"
              :class="['inline-tag', 'tag-video']"
              :title="p.filename"
            >
              🎬 {{ p.filename || '视频' }}
            </span>
            <span
              v-else-if="p.kind === 'file'"
              :class="['inline-tag', 'tag-file']"
              :title="p.filename"
            >
              📎 {{ p.filename || '文件' }}
            </span>
            <span v-else-if="p.kind === 'audio'" :class="['inline-tag', 'tag-audio']">
              🎤 语音
            </span>
            <span v-else-if="p.kind === 'json'" :class="['inline-tag', 'tag-json']">
              📰 富文本
            </span>
            <span v-else :class="['inline-tag']">{{ p.text }}</span>
          </template>
        </div>

        <!-- 图片缩略图（已渲染的 + inline 没提到的） -->
        <div v-if="imageUrls.length" class="msg-images">
          <a
            v-for="(img, i) in imageUrls"
            :key="`${i}-${img}`"
            :href="img"
            target="_blank"
          >
            <img :src="img" :alt="`图${i + 1}`" loading="lazy" />
          </a>
        </div>
      </div>
    </div>
  </div>

  <!-- 右键菜单 -->
  <Teleport to="body">
    <div
      v-if="contextMenu"
      class="msg-context-menu"
      :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
      @click.stop
    >
      <button class="msg-context-menu-item danger" @click="onDelete">
        <el-icon :size="13"><Delete /></el-icon>
        <span>删除消息</span>
      </button>
      <button class="msg-context-menu-item" @click="onSelectMode">
        <el-icon :size="13"><Check /></el-icon>
        <span>多选（批量删除）</span>
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
/* ---- 多选模式 ---- */
.msg-row.selectable {
  cursor: pointer;
  user-select: none;
}
.msg-check {
  flex: none;
  align-self: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1.5px solid var(--border-1);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  transition: background 0.12s, border-color 0.12s;
}
.msg-check.checked {
  background: var(--accent);
  border-color: var(--accent);
}
.msg-row.selected .msg-bubble {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.msg-context-menu {
  position: fixed;
  z-index: 9999;
  min-width: 140px;
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-3);
  padding: 4px;
  font-family: inherit;
}
.msg-context-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  border: none;
  background: transparent;
  color: var(--text-1);
  font-size: 12.5px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  text-align: left;
  font-family: inherit;
}
.msg-context-menu-item:hover {
  background: var(--bg-subtle);
}
.msg-context-menu-item.danger:hover {
  background: rgba(220, 50, 50, 0.1);
  color: var(--danger, #e53935);
}
</style>