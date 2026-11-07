<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, computed } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { ElMessageBox } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import type { UiMessage, WebSession } from '../types'
import { chat } from '../stores/chat'
import MessageItem from './MessageItem.vue'

const props = defineProps<{
  messages: UiMessage[]
  thinking: boolean
  loadingHistory: boolean
  identity: WebSession
  activeUserId?: string | null
  hasMore?: boolean
  loadingMore?: boolean
}>()

const scroller = ref<HTMLElement | null>(null)

// =====================================================
// 滚动策略: TanStack Virtual 的 chat 模式
// =====================================================
// 对齐 QQ/微信/Telegram 这类 IM 的标准行为:
//   - 新消息出现在底部, 用户贴底时自动跟随 (followOnAppend)
//   - 用户上滑看历史时, 新消息不打扰 (不拽回底部)
//   - 加载更早消息 (prepend) 时, 视觉位置不跳动 (anchorTo:'end' + getItemKey)
//   - 图片异步加载 / LLM 流式增长时, 底部保持钉住 (measureElement + anchorTo:'end')
//
// 不再手写 scrollTop += delta / nudgeToBottom / 时间戳判断 append vs prepend.
// 关键: options 必须用 computed 包装.
// @tanstack/vue-virtual 内部用 `computed(() => ({ ...unref(options) }))` +
// `watch(() => unref(options))` 追踪 options. 若直接传普通对象, `count: props.messages.length`
// 只在 setup 时求值一次并变成固定数字, 之后 props.messages 增长不会更新 count
// → count 永远为 0 → getTotalSize()=0 → 容器高度 0px → 一条消息都不渲染.
const virtualizer = useVirtualizer(computed(() => ({
  count: props.messages.length,
  getScrollElement: () => scroller.value,
  estimateSize: (index) => {
    const m = props.messages[index]
    // 初始估计, measureElement 会精确测量 (有图片的消息通常更高)
    return m && m.images.length > 0 ? 200 : 72
  },
  getItemKey: (index) => props.messages[index]?.id ?? index,
  anchorTo: 'end',
  followOnAppend: true,
  scrollEndThreshold: 80,
  overscan: 6,
})))

const showJumpToBottom = ref(false)

// 虚拟项 + 消息合并, 并过滤掉越界项.
// 删除消息后, 虚拟化器内部状态可能滞后一帧, getVirtualItems() 短暂返回
// index >= messages.length 的越界项; 直接渲染 messages[vItem.index].id 会抛
// TypeError 导致页面崩溃 (删除时偶发崩溃的根因之一), 这里在数据层保证安全.
const virtualItems = computed(() =>
  virtualizer.value
    .getVirtualItems()
    .map(vItem => ({ vItem, msg: props.messages[vItem.index] }))
    .filter(item => !!item.msg),
)

// 虚拟列表项的测量回调 (Vue 的 ref 回调可能传入组件实例, 过滤掉)
function measureItem(el: unknown) {
  if (el instanceof Element) virtualizer.value.measureElement(el)
}

function onScroll() {
  showJumpToBottom.value = !virtualizer.value.isAtEnd()
  // 触顶加载更早消息
  const el = scroller.value
  if (
    el &&
    el.scrollTop <= 10 &&
    el.scrollHeight > el.clientHeight &&
    props.hasMore &&
    !props.loadingMore
  ) {
    const sid = chat.state.activeSessionId
    if (sid) chat.loadMore(sid)
  }
}

function jumpToBottom() {
  // Variable-height messages are measured while scrolling. Smooth scrolling can stop at an obsolete estimate.
  virtualizer.value.scrollToEnd()
  showJumpToBottom.value = false
}

// 切会话: 重置并贴底 (anchorTo:'end' 会自动处理, 这里兜底一次)
watch(
  () => props.identity.session_id,
  () => {
    showJumpToBottom.value = false
    exitSelectMode()
    requestAnimationFrame(() => {
      virtualizer.value.scrollToEnd()
    })
  },
)

// 首次载入 (0 → N) 时贴底. append/prepend 时前长度 >0, 由 followOnAppend / anchorTo:'end'
// 处理 (上滑看历史时新消息不打扰, 加载更早不跳动), 这里不介入.
watch(
  () => props.messages.length,
  (n, old) => {
    if (old === 0 && n > 0) {
      requestAnimationFrame(() => {
        virtualizer.value.scrollToEnd()
      })
    }
  },
)

// 自己发送消息 → 立即贴底.
// TanStack 的 followOnAppend 只在"接近底部"时才自动跟随 (设计如此, 避免上滑看历史时被拽回).
// 但"主动发消息"是明确意图: 无论当前是否上滑, 都应滚到底.
// 只对 role==='user' (自己的消息) 且 id 发生变化时触发; 他人消息(role==='other')、
// LLM 回复(role==='assistant')、prepend 加载更早(末尾 id 不变) 一律不介入.
let prevLastMsgId: string | null = null
watch(
  () => props.messages[props.messages.length - 1],
  (last) => {
    if (!last) {
      prevLastMsgId = null
      return
    }
    if (last.role === 'user' && last.id !== prevLastMsgId) {
      requestAnimationFrame(() => {
        virtualizer.value.scrollToEnd()
      })
    }
    prevLastMsgId = last.id
  },
  { flush: 'post' },
)

// =====================================================
// 多选模式 (框选 / 点选批量删除)
// =====================================================
const selectMode = ref(false)
const selectedIds = ref<Set<string>>(new Set())
// 橡皮筋选框 (client 坐标, fixed 覆盖层; 拖动期间不滚动, 视觉一致)
const marquee = ref<{ left: number; top: number; width: number; height: number } | null>(null)
let marqueeStart: { x: number; y: number } | null = null

function enterSelectMode(preselect?: string) {
  selectMode.value = true
  selectedIds.value = new Set(preselect ? [preselect] : [])
}

function exitSelectMode() {
  selectMode.value = false
  selectedIds.value = new Set()
  marquee.value = null
  marqueeStart = null
}

function toggleSelect(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}

function selectAllLoaded() {
  selectedIds.value = new Set(props.messages.map(m => m.id))
}

function deleteSelected() {
  const ids = [...selectedIds.value]
  if (!ids.length) return
  // 用 ElMessageBox 而非 window.confirm: 原生对话框在内嵌浏览器 (webview) 中
  // 会触发壳层原生弹窗实现, 有崩溃风险; 页内对话框无此问题.
  ElMessageBox.confirm(
    `确认删除选中的 ${ids.length} 条消息？此操作不可恢复。`,
    '批量删除',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  )
    .then(() => {
      chat.deleteMessages({ session_id: props.identity.session_id, message_ids: ids })
      exitSelectMode()
    })
    .catch(() => {
      // 用户取消, 无操作
    })
}

// ---- 橡皮筋框选: 多选模式下按下并拖动即可框选 ----
// .msg-row 占满整行宽度, 因此"气泡旁的空白"也在行内; 判定标准改为是否落在实际内容上:
// 只有按在气泡/头像/头部/勾选框上才交给点选, 其余空白 (含行内空白) 均可启动框选.
const MSG_CONTENT_SEL = '.msg-bubble, .msg-avatar, .msg-head, .msg-check'

function onScrollerPointerDown(ev: PointerEvent) {
  if (!selectMode.value || ev.button !== 0) return
  const target = ev.target as HTMLElement | null
  if (target && target.closest(MSG_CONTENT_SEL)) return
  ev.preventDefault()
  const el = scroller.value
  if (!el) return
  marqueeStart = { x: ev.clientX, y: ev.clientY }
  marquee.value = { left: ev.clientX, top: ev.clientY, width: 0, height: 0 }
  el.setPointerCapture(ev.pointerId)
}

function onScrollerPointerMove(ev: PointerEvent) {
  if (!marqueeStart) return
  const left = Math.min(marqueeStart.x, ev.clientX)
  const top = Math.min(marqueeStart.y, ev.clientY)
  marquee.value = {
    left,
    top,
    width: Math.abs(ev.clientX - marqueeStart.x),
    height: Math.abs(ev.clientY - marqueeStart.y),
  }
  // 与可见消息行求交, 实时更新选中集合.
  // 累加语义: 以已有选中为基础, 框选只增不减, 不会取消已选中的消息.
  const box = marquee.value
  const el = scroller.value
  if (!el) return
  const next = new Set(selectedIds.value)
  el.querySelectorAll<HTMLElement>('.msg-row[data-message-id]').forEach(row => {
    const r = row.getBoundingClientRect()
    if (
      r.left < box.left + box.width &&
      r.right > box.left &&
      r.top < box.top + box.height &&
      r.bottom > box.top
    ) {
      const id = row.dataset.messageId
      if (id) next.add(id)
    }
  })
  selectedIds.value = next
}

function onScrollerPointerUp() {
  if (!marqueeStart) return
  // 位移过小 = 点击空白: 不动已有选择 (避免误清; 全部取消用操作栏"取消"或 ESC)
  marqueeStart = null
  marquee.value = null
}

function onKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Escape' && selectMode.value) exitSelectMode()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  requestAnimationFrame(() => {
    virtualizer.value.scrollToEnd()
  })
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="chat-window-wrap">
    <div
      ref="scroller"
      :class="['chat-window', { 'select-mode': selectMode }]"
      @scroll.passive="onScroll"
      @pointerdown="onScrollerPointerDown"
      @pointermove="onScrollerPointerMove"
      @pointerup="onScrollerPointerUp"
    >
      <div v-if="loadingMore" class="loading-more-hint">加载更早的消息…</div>
      <div v-else-if="hasMore" class="load-more-hint">上滑加载更早的消息</div>
      <div v-if="loadingHistory && messages.length === 0" class="loading-hint">
        加载历史消息...
      </div>

      <!-- 虚拟列表: 只渲染可视区域的消息 -->
      <div
        :style="{
          height: virtualizer.getTotalSize() + 'px',
          position: 'relative',
          width: '100%',
        }"
      >
        <div
          v-for="item in virtualItems"
          :key="String(item.vItem.key)"
          :data-index="item.vItem.index"
          :ref="measureItem"
          class="virtual-item"
          :style="{
            position: 'absolute',
            top: 0,
            left: 0,
            width: '100%',
            transform: `translateY(${item.vItem.start}px)`,
          }"
        >
          <MessageItem
            :message="item.msg"
            :active-user-id="activeUserId"
            :select-mode="selectMode"
            :selected="selectedIds.has(item.msg.id)"
            @toggle-select="toggleSelect"
            @enter-select-mode="enterSelectMode"
          />
        </div>
      </div>

      <div
        v-if="messages.length === 0 && !thinking && !loadingHistory"
        class="empty-hint"
      >
        <div class="empty-hint-text">该会话暂无消息</div>
        <div class="empty-hint-sub">
          {{ identity.user_name
          }}<span v-if="identity.role"> · 角色 {{ identity.role }}</span>
        </div>
      </div>
    </div>
    <transition name="jump-fade">
      <button
        v-if="showJumpToBottom"
        class="jump-to-bottom-btn"
        type="button"
        title="跳到底部"
        @click="jumpToBottom"
      >
        <el-icon :size="16"><ArrowDown /></el-icon>
      </button>
    </transition>

    <!-- 橡皮筋选框 (fixed 覆盖层, 不随滚动) -->
    <Teleport to="body">
      <div
        v-if="marquee"
        class="marquee-overlay"
        :style="{
          left: marquee.left + 'px',
          top: marquee.top + 'px',
          width: marquee.width + 'px',
          height: marquee.height + 'px',
        }"
      />
    </Teleport>

    <!-- 多选操作栏 -->
    <Teleport to="body">
      <div v-if="selectMode" class="select-bar">
        <span class="select-bar-count">已选 {{ selectedIds.size }} 条</span>
        <button class="select-bar-btn" type="button" @click="selectAllLoaded">
          全选
        </button>
        <button
          class="select-bar-btn danger"
          type="button"
          :disabled="selectedIds.size === 0"
          @click="deleteSelected"
        >
          删除
        </button>
        <button class="select-bar-btn" type="button" @click="exitSelectMode">
          取消
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
/* 多选模式: 禁止文本选择, 避免拖动框选时误选气泡文字 */
.chat-window.select-mode {
  user-select: none;
}

/* 橡皮筋选框 (Teleport 到 body, scoped 属性仍会带上) */
.marquee-overlay {
  position: fixed;
  z-index: 3000;
  border: 1px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 15%, transparent);
  pointer-events: none;
}

/* 底部批量操作栏 */
.select-bar {
  position: fixed;
  left: 50%;
  bottom: 20px;
  transform: translateX(-50%);
  z-index: 3000;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md, 10px);
  box-shadow: var(--shadow-3);
}
.select-bar-count {
  font-size: 13px;
  color: var(--text-1);
  min-width: 60px;
}
.select-bar-btn {
  padding: 5px 14px;
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-1);
  font-size: 13px;
  cursor: pointer;
  font-family: inherit;
}
.select-bar-btn:hover {
  background: var(--bg-subtle);
}
.select-bar-btn.danger {
  color: var(--danger);
  border-color: var(--danger);
}
.select-bar-btn.danger:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 虚拟列表项: 用 padding-bottom 提供消息间距 (measureElement 测量 border-box, 不含 margin) */
.virtual-item {
  padding-bottom: 20px;
}
.virtual-item .msg-row {
  margin-bottom: 0;
}
</style>
