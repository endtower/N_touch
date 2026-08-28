<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
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
  virtualizer.value.scrollToEnd({ behavior: 'smooth' })
  showJumpToBottom.value = false
}

// 切会话: 重置并贴底 (anchorTo:'end' 会自动处理, 这里兜底一次)
watch(
  () => props.identity.session_id,
  () => {
    showJumpToBottom.value = false
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

onMounted(() => {
  requestAnimationFrame(() => {
    virtualizer.value.scrollToEnd()
  })
})
</script>

<template>
  <div class="chat-window-wrap">
    <div
      ref="scroller"
      class="chat-window"
      @scroll.passive="onScroll"
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
          v-for="vItem in virtualizer.getVirtualItems()"
          :key="String(vItem.key)"
          :data-index="vItem.index"
          :ref="measureItem"
          class="virtual-item"
          :style="{
            position: 'absolute',
            top: 0,
            left: 0,
            width: '100%',
            transform: `translateY(${vItem.start}px)`,
          }"
        >
          <MessageItem
            :message="messages[vItem.index]"
            :active-user-id="activeUserId"
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
  </div>
</template>

<style scoped>
/* 虚拟列表项: 用 padding-bottom 提供消息间距 (measureElement 测量 border-box, 不含 margin) */
.virtual-item {
  padding-bottom: 20px;
}
.virtual-item .msg-row {
  margin-bottom: 0;
}
</style>