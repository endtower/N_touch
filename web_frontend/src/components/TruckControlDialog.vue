<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Van, Setting, Refresh, VideoPlay, VideoPause } from '@element-plus/icons-vue'
import type { TruckState } from '../types'
import { chat } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

const localOverrides = ref<Record<string, boolean>>({})
const lastSynced = ref<number>(Date.now())

// 弹窗打开时拉一次最新状态
watch(
  () => props.visible,
  (v) => {
    if (v) {
      try { chat.refreshTrucks() } catch {}
    }
  },
)

// 同步后端状态到本地 overrides (后端是 source of truth)
watch(
  () => chat.state.trucks,
  (trucks) => {
    const m: Record<string, boolean> = {}
    for (const t of (trucks as TruckState[])) m[t.truck_id] = t.enabled
    localOverrides.value = m
    lastSynced.value = Date.now()
  },
  { immediate: true, deep: true },
)

// 类别内部 truck 顺序: private 优先 (私聊最常看)
const TRUCK_ORDER = ['private_truck', 'special_attention_truck']
const trucksByCategory = computed(() => {
  const out: Record<string, TruckState[]> = {}
  for (const t of chat.state.trucks) {
    if (!out[t.category]) out[t.category] = []
    out[t.category].push(t)
  }
  // 类别内部按 TRUCK_ORDER 排序
  for (const cat of Object.keys(out)) {
    out[cat] = out[cat].slice().sort((a, b) => {
      const ia = TRUCK_ORDER.indexOf(a.truck_id)
      const ib = TRUCK_ORDER.indexOf(b.truck_id)
      if (ia === -1 && ib === -1) return 0
      if (ia === -1) return 1
      if (ib === -1) return -1
      return ia - ib
    })
  }
  return out
})

// 类别显示顺序: 消息获取 (含私聊 / 群聊) 排第一
const CATEGORY_ORDER = ['消息获取', '监控']
const orderedCategories = computed(() => {
  const keys = Object.keys(trucksByCategory.value)
  return [
    ...CATEGORY_ORDER.filter(k => keys.includes(k)),
    ...keys.filter(k => !CATEGORY_ORDER.includes(k)),
  ]
})

const enabledCount = computed(
  () => chat.state.trucks.filter((t) => localOverrides.value[t.truck_id] ?? t.enabled).length,
)
const totalCount = computed(() => chat.state.trucks.length)

function toggle(t: TruckState) {
  const current = localOverrides.value[t.truck_id] ?? t.enabled
  const next = !current
  localOverrides.value[t.truck_id] = next
  chat.toggleTruck(t.truck_id, next)
}

function refresh() {
  chat.refreshTrucks()
}

function isOn(t: TruckState) {
  return localOverrides.value[t.truck_id] ?? t.enabled
}

function close() {
  emit('update:visible', false)
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    title="Truck 控制"
    width="560"
    :close-on-click-modal="false"
    align-center
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <template #header>
      <div class="truck-dialog-header">
        <div class="truck-dialog-title">
          <el-icon :size="16" color="var(--accent)"><Van /></el-icon>
          <span>Truck 控制</span>
        </div>
        <div class="truck-dialog-meta">
          运行中 <strong>{{ enabledCount }}</strong> / {{ totalCount }}
          <el-button :icon="Refresh" size="small" link @click="refresh">刷新</el-button>
        </div>
      </div>
    </template>

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
              <el-icon v-if="isOn(t)" :size="15" color="var(--accent-2)"><VideoPlay /></el-icon>
              <el-icon v-else :size="15" color="var(--text-3)"><VideoPause /></el-icon>
            </div>
            <div class="truck-item-text">
              <div class="truck-item-name">
                {{ t.label }}
                <span class="truck-item-id">({{ t.truck_id }})</span>
              </div>
              <div class="truck-item-desc">{{ t.description }}</div>
              <div v-if="t.cycle_time" class="truck-item-cycle">
                周期 {{ t.cycle_time }}s
              </div>
            </div>
          </div>
          <el-switch
            :model-value="isOn(t)"
            size="default"
            @update:model-value="toggle(t)"
          />
        </div>
      </div>
    </div>

    <div class="truck-tip">
      💡 开关会立即生效 (运行时切换), 状态会保存到
      <code>adapt/web/data/web_trucks.json</code>, 重启后自动恢复.
    </div>

    <template #footer>
      <el-button @click="close">关闭</el-button>
    </template>
  </el-dialog>
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

.truck-categories {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.truck-category {
  display: flex;
  flex-direction: column;
  gap: 8px;
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
  gap: 12px;
  padding: 12px 14px;
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
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  flex: 1;
}
.truck-item-icon {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 1px;
}
.truck-item.on .truck-item-icon {
  background: var(--accent-2);
  color: var(--text-inv);
}
.truck-item-text {
  flex: 1;
  min-width: 0;
}
.truck-item-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: -0.005em;
}
.truck-item-id {
  color: var(--text-3);
  font-size: 11px;
  font-weight: 400;
  margin-left: 4px;
}
.truck-item-desc {
  font-size: 12px;
  color: var(--text-2);
  margin-top: 2px;
  line-height: 1.45;
}
.truck-item-cycle {
  font-size: 11px;
  color: var(--text-3);
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
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
</style>