<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { memoryRequest } from '../api/memory'
const props = defineProps<{ kind: string; itemId: string; version?: string | null }>()
const item = ref<any>(null), error = ref(''), busy = ref(false)
let generation = 0
const labels: Record<string, string> = { vector_returned: '向量候选返回', returned: '查询返回', injected: '注入上下文', selected: '人格选用' }
function date(value?: number) { return value ? new Date(value * 1000).toLocaleString() : '尚无记录' }
async function load() {
  const current = ++generation
  item.value = null; error.value = ''
  try {
    const data = await memoryRequest(`/memory/maintenance/items?kind=${props.kind}&item_id=${encodeURIComponent(props.itemId)}`)
    if (current === generation) item.value = data.items[0]
  } catch (err: any) { if (current === generation) error.value = err.message }
}
async function review() {
  if (!item.value || busy.value) return
  busy.value = true
  try {
    await memoryRequest('/memory/maintenance/reviews', 'POST', { kind: props.kind, item_id: props.itemId, revision: item.value.revision })
    ElMessage.success('已记录人工检查'); await load()
  } catch (err: any) { ElMessage.error(err.message); await load() }
  finally { busy.value = false }
}
watch(() => [props.kind, props.itemId, props.version], load, { immediate: true })
</script>
<template>
  <section class="maintenance-status" aria-label="人工维护与使用情况">
    <p v-if="error" role="alert">{{ error }} <el-button size="small" @click="load">重试</el-button></p>
    <template v-else-if="item">
      <div class="review-row"><strong>{{ item.maintenance.human_confirmed ? '当前版本已人工确认' : item.maintenance.changed_since_review ? '人工确认后内容已变更' : '当前版本尚未人工确认' }}</strong>
        <el-button size="small" :loading="busy" @click="review">已检查，无需修改</el-button></div>
      <p>最近人工检查：{{ date(item.maintenance.last_review?.reviewed_at) }}</p>
      <p v-for="(stat, key) in (item.usage as Record<string, any>)" :key="key">{{ labels[key] || key }}：{{ stat.count }} 次 · 最近 {{ date(stat.last_used_at) }}</p>
      <p class="hint">统计从启用后开始，同一事件链同一版本只计一次；返回或注入不代表实际采用。</p>
    </template>
    <span v-else>读取维护情况…</span>
  </section>
</template>
<style scoped>
.maintenance-status { margin-top: 16px; padding: 12px; border: 1px solid var(--border-1); border-radius: 8px; color: var(--text-2); font-size: 12px; }
.review-row { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; justify-content: space-between; }
p { margin: 8px 0 0; overflow-wrap: anywhere; } .hint { color: var(--text-3); }
</style>
