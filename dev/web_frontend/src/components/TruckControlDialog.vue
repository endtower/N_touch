<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { Van, Refresh, VideoPlay, VideoPause } from '@element-plus/icons-vue'
import type { TruckState } from '../types'
import { chat } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()
const startupPaused = ref(false)
const coreReady = ref(false)
const saving = ref(false)
const error = ref('')
let timer: number | null = null
let refreshing = false

async function refresh() {
  if (saving.value || refreshing) return
  refreshing = true
  try {
    const data = await chat.refreshTrucks()
    startupPaused.value = data.startup_paused
    coreReady.value = data.core_ready
    error.value = ''
  } catch (e: any) { error.value = e?.message || String(e) }
  finally { refreshing = false }
}

watch(() => props.visible, visible => {
  if (timer !== null) window.clearInterval(timer)
  timer = null
  if (visible) {
    void refresh()
    timer = window.setInterval(refresh, 3000)
  }
})
onBeforeUnmount(() => { if (timer !== null) window.clearInterval(timer) })

const trucksByCategory = computed(() => {
  const out: Record<string, TruckState[]> = {}
  for (const t of chat.state.trucks) (out[t.category] ??= []).push(t)
  return out
})
const orderedCategories = computed(() => Object.keys(trucksByCategory.value))
const enabledCount = computed(() => chat.state.trucks.filter(t => t.enabled).length)
const totalCount = computed(() => chat.state.trucks.length)
function isOn(t: TruckState) { return t.configured_enabled ?? false }
function runtimeLabel(t: TruckState) {
  const labels: Record<string, string> = { running: '运行中', paused: '暂停中', stopped: '已停止', unknown: '未同步' }
  return labels[t.runtime_state || 'unknown'] || t.runtime_state
}
async function save(payload: { enabled?: Record<string, boolean>; startup_paused?: boolean }) {
  saving.value = true
  try {
    const data = await chat.saveTruckSettings(payload)
    startupPaused.value = data.startup_paused
    coreReady.value = data.core_ready
    error.value = ''
    ElMessage.success('启动配置已保存')
  } catch (e: any) { ElMessage.error(e?.message || String(e)) }
  finally { saving.value = false }
}
function toggle(t: TruckState) { void save({ enabled: { [t.truck_id]: !isOn(t) } }) }
async function runtimeAction(action: 'apply' | 'stop') {
  saving.value = true
  try {
    await chat.applyTruckSettings(action)
    ElMessage.success(action === 'apply' ? '已应用到当前运行' : '已停止全部 Truck')
  } catch (e: any) { ElMessage.error(e?.message || String(e)) }
  finally { saving.value = false; void refresh() }
}
function close() { emit('update:visible', false) }
</script>

<template>
  <AppDialog size="panel"
    :model-value="props.visible"
    title="Truck 配置与运行"
    class="truck-dialog"
    :close-on-click-modal="false"
    align-center
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <template #header>
      <div class="truck-dialog-header">
        <div class="truck-dialog-title">
          <el-icon :size="16" color="var(--accent)"><Van /></el-icon>
          <span>Truck 配置与运行</span>
        </div>
        <div class="truck-dialog-meta">
          已启动 <strong>{{ enabledCount }}</strong> / {{ totalCount }}
          <el-button :icon="Refresh" size="small" link @click="refresh">刷新</el-button>
        </div>
      </div>
    </template>

    <div class="truck-body">
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <div class="truck-tip">
        <el-checkbox :model-value="startupPaused" :disabled="saving"
          @update:model-value="(v: any) => save({ startup_paused: !!v })">启动 Core 时暂停自动任务（开发）</el-checkbox>
        <div>{{ coreReady ? 'Core 运行中；配置开关不会直接切换正在运行的 Truck。' : 'Core 未运行；可以保存下次启动配置。' }}</div>
      </div>
      <div v-if="chat.state.trucks.length === 0" class="truck-empty">
        加载中… (或后端尚未注册 truck)
      </div>

      <div v-else class="truck-categories">
        <div v-for="cat in orderedCategories" :key="cat" class="truck-category">
          <div class="truck-category-label">{{ cat }}</div>
          <div
            v-for="t in trucksByCategory[cat]"
            :key="t.truck_id"
            :class="['truck-item', { on: isOn(t) }]"
          >
            <div class="truck-item-main">
              <div class="truck-item-icon">
                <el-icon v-if="isOn(t)" :size="14" color="var(--accent-2)"><VideoPlay /></el-icon>
                <el-icon v-else :size="14" color="var(--text-3)"><VideoPause /></el-icon>
              </div>
              <div class="truck-item-text">
                <div class="truck-item-head">
                  <span class="truck-item-name">{{ t.label }}</span>
                  <span class="truck-item-id">{{ t.truck_id }}</span>
                </div>
                <div class="truck-item-desc">{{ t.description }}</div>
              </div>
            </div>
            <div class="truck-item-action">
              <div class="truck-item-meta">
                <span :class="['truck-status', t.runtime_state || 'unknown']">
                  <i class="truck-status-dot" />{{ runtimeLabel(t) }}
                </span>
                <span v-if="t.cycle_time" class="truck-cycle">周期 {{ t.cycle_time }}s</span>
              </div>
              <div class="truck-switch-row">
                <span class="truck-switch-label">启动启用</span>
                <el-switch
                  :disabled="saving"
                  :model-value="isOn(t)"
                  size="default"
                  @update:model-value="toggle(t)"
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button :disabled="!coreReady || saving" @click="runtimeAction('stop')">停止全部</el-button>
      <el-button type="primary" :disabled="!coreReady || saving" @click="runtimeAction('apply')">应用到当前运行</el-button>
      <el-button @click="close">关闭</el-button>
    </template>
  </AppDialog>
</template>

<style scoped>
.truck-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-right: 8px;
}
.truck-dialog-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}
.truck-dialog-meta {
  font-size: 12px;
  color: var(--text-3);
  display: flex;
  align-items: center;
  gap: 6px;
}
.truck-dialog-meta strong {
  color: var(--accent-2);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.truck-empty {
  padding: 32px;
  text-align: center;
  color: var(--text-3);
  font-size: 13px;
}

.truck-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px;
}

.truck-categories {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.truck-category {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.truck-category-label {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 0 4px;
}

.truck-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 9px 13px;
  border-radius: var(--radius);
  background: var(--bg-surface);
  border: 1px solid var(--border-1);
  transition: all 0.15s ease;
}
.truck-item:hover {
  border-color: var(--border-2);
}
.truck-item.on {
  background: var(--accent-2-soft);
  border-color: var(--accent-2);
}

.truck-item-main {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
}
.truck-item-icon {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.truck-item.on .truck-item-icon {
  background: var(--accent-2);
  color: var(--text-inv);
}
.truck-item-text {
  flex: 1;
  min-width: 0;
}
.truck-item-head {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.truck-item-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: -0.005em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.truck-item-id {
  flex-shrink: 0;
  color: var(--text-3);
  font-size: 10.5px;
  font-weight: 400;
  font-family: var(--font-mono);
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-app);
}
.truck-item-desc {
  font-size: 11.5px;
  color: var(--text-2);
  margin-top: 2px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.truck-item-action {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex-shrink: 0;
}
.truck-item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}
.truck-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.truck-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.truck-status.running {
  color: var(--accent-2);
}
.truck-status.running .truck-status-dot {
  box-shadow: 0 0 0 3px var(--accent-2-soft);
}
.truck-status.paused {
  color: var(--warn);
}
.truck-cycle {
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.truck-cycle::before {
  content: '·';
  margin-right: 6px;
}
.truck-switch-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.truck-switch-label {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}

.truck-tip {
  margin-top: 14px;
  padding: 8px 12px;
  background: var(--bg-subtle);
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.5;
}
.truck-tip code {
  background: var(--bg-app);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10.5px;
  font-family: var(--font-mono);
  color: var(--accent);
}
/* Keep header controls clear of the shared close button. */
.truck-dialog-header, .truck-dialog-meta { flex-wrap: wrap; min-width: 0; gap: 8px; }
</style>
