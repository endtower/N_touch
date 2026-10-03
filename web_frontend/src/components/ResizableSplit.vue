<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'

const props = withDefaults(defineProps<{
  storageKey: string
  label?: string
  initialWidth?: number
  minWidth?: number
  maxWidth?: number
  contentMinWidth?: number
}>(), { label: '列表', initialWidth: 280, minWidth: 200, maxWidth: 480, contentMinWidth: 360 })
const root = ref<HTMLElement | null>(null)
const available = ref(0)
const preferred = ref(props.initialWidth)
const hidden = ref(false)
const drawerOpen = ref(false)
const dragging = ref(false)
const HANDLE_WIDTH = 10
const narrow = computed(() => available.value > 0 && available.value < props.minWidth + props.contentMinWidth + HANDLE_WIDTH)
const collapsed = computed(() => narrow.value ? !drawerOpen.value : hidden.value)
const maximum = computed(() => Math.max(props.minWidth, Math.min(props.maxWidth, available.value - props.contentMinWidth - HANDLE_WIDTH)))
const width = computed(() => narrow.value
  ? Math.min(preferred.value, Math.max(120, available.value - 40))
  : Math.max(props.minWidth, Math.min(preferred.value, maximum.value)))
const columns = computed(() => collapsed.value || narrow.value ? '36px minmax(0, 1fr)' : `${width.value}px ${HANDLE_WIDTH}px minmax(0, 1fr)`)
let observer: ResizeObserver | undefined
let pointer: { id: number; x: number; width: number } | null = null

function save() {
  try { localStorage.setItem(props.storageKey, JSON.stringify({ width: preferred.value, hidden: hidden.value })) } catch { /* storage unavailable */ }
}
function toggle() {
  if (narrow.value) drawerOpen.value = !drawerOpen.value
  else { hidden.value = !hidden.value; save() }
}
function closeOnNarrow() { drawerOpen.value = false }
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape' && narrow.value && drawerOpen.value) { event.stopPropagation(); closeOnNarrow() }
}
function resize(value: number) { preferred.value = Math.max(props.minWidth, Math.min(value, maximum.value)) }
function start(event: PointerEvent) {
  if (event.button !== 0 || narrow.value) return
  event.preventDefault()
  pointer = { id: event.pointerId, x: event.clientX, width: width.value }
  dragging.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (pointer?.id === event.pointerId) resize(pointer.width + event.clientX - pointer.x)
}
function finish() { if (pointer) save(); pointer = null; dragging.value = false }
function keyboard(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  resize(event.key === 'Home' ? props.minWidth : event.key === 'End' ? maximum.value : width.value + (event.key === 'ArrowLeft' ? -16 : 16))
  save()
}
onMounted(() => {
  try {
    const saved = JSON.parse(localStorage.getItem(props.storageKey) || 'null')
    if (Number.isFinite(saved?.width)) preferred.value = Math.max(props.minWidth, Math.min(saved.width, props.maxWidth))
    hidden.value = saved?.hidden === true
  } catch { /* use defaults */ }
  observer = new ResizeObserver(([entry]) => { if (entry) available.value = entry.contentRect.width })
  if (root.value) observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
defineExpose({ toggle, closeOnNarrow })
</script>

<template>
  <div ref="root" class="reading-split" :class="{ 'is-dragging': dragging, 'is-narrow': narrow }" :style="{ gridTemplateColumns: columns }" @keydown="escape">
    <div v-if="collapsed || narrow" class="split-rail">
      <button type="button" class="split-toggle" :title="`展开${label}`" :aria-label="`展开${label}`" @click="toggle"><el-icon><ArrowRight /></el-icon></button>
    </div>
    <button v-if="narrow && drawerOpen" class="split-backdrop" type="button" :aria-label="`关闭${label}浮层`" @click="closeOnNarrow" />
    <aside v-show="!collapsed" class="split-sidebar" :aria-label="label" :class="{ 'is-drawer': narrow }" :style="narrow ? { width: width + 'px' } : undefined">
      <div class="split-sidebar-tools">
        <span>{{ label }} <small class="split-resize-hint">拖动右侧调整</small></span>
        <button class="split-toggle" type="button" :title="`收起${label}`" :aria-label="`收起${label}`" @click="toggle"><el-icon><ArrowLeft /></el-icon></button>
      </div>
      <div class="split-sidebar-content"><slot name="sidebar" /></div>
    </aside>
    <div v-if="!collapsed && !narrow" class="split-handle" role="separator" tabindex="0" aria-orientation="vertical"
      :aria-label="`调整${label}宽度`" :aria-valuemin="minWidth" :aria-valuemax="maximum" :aria-valuenow="Math.round(width)"
      title="拖动调整宽度；方向键也可调整，双击恢复默认"
      @pointerdown="start" @pointermove="move" @pointerup="finish" @pointercancel="finish" @lostpointercapture="finish"
      @keydown="keyboard" @dblclick="resize(initialWidth); save()" />
    <div class="split-content" :inert="narrow && drawerOpen"><slot /></div>
  </div>
</template>

<style scoped>
.reading-split { display: grid; position: relative; flex: 1 1 auto; width: 100%; min-width: 0; min-height: 0; overflow: hidden; }
.split-sidebar, .split-content, .split-sidebar-content { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.split-sidebar { background: var(--bg-surface); overflow: hidden; }
.split-sidebar-content { flex: 1; overflow: hidden; }
.split-sidebar-tools { display: flex; align-items: center; justify-content: space-between; flex: none; padding: 3px 8px 3px 12px; border-bottom: 1px solid var(--border-1); color: var(--text-3); font-size: 11px; }
.split-toggle { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: var(--radius-sm); color: var(--text-2); background: transparent; cursor: pointer; }
.split-toggle:hover, .split-toggle:focus-visible { color: var(--accent); background: var(--accent-soft); }
.split-rail { background: var(--bg-surface); border-right: 1px solid var(--border-1); padding: 6px 3px; }
.split-resize-hint { margin-left: 8px; font-size: 10px; color: var(--text-2); }
.split-handle { position: relative; background: var(--bg-subtle); border-inline: 1px solid var(--border-1); cursor: col-resize; touch-action: none; }
.split-handle::after { content: ''; position: absolute; top: 50%; left: 3px; width: 2px; height: 36px; border-radius: 2px; transform: translateY(-50%); background: var(--text-3); }
.split-handle:hover, .split-handle:focus-visible, .is-dragging .split-handle { background: var(--accent-soft); }
.split-handle:hover::after, .split-handle:focus-visible::after, .is-dragging .split-handle::after { background: var(--accent); }
.is-dragging { user-select: none; cursor: col-resize; }
.split-backdrop { position: absolute; inset: 0; z-index: 4; border: 0; background: rgba(0, 0, 0, .3); }
.split-sidebar.is-drawer { position: absolute; inset: 0 auto 0 0; z-index: 5; box-shadow: var(--shadow-3); }
</style>
