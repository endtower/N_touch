<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { memoryRequest } from '../api/memory'
const props = defineProps<{ kind: string; itemId: string; version?: string | null }>()
const item = ref<any>(null), error = ref(''), busy = ref(false)
let generation = 0
function date(value?: number) { return value ? new Date(value * 1000).toLocaleString() : '尚无记录' }
async function load() {
  const current = ++generation
  item.value = null; error.value = ''
  try {
    const data = await memoryRequest(`/memory/maintenance/items?kind=experience&item_id=${encodeURIComponent(props.itemId)}`)
    if (current === generation) item.value = data.items[0]
  } catch (err: any) { if (current === generation) error.value = err.message }
}
async function review() {
  if (busy.value) return
  busy.value = true
  try {
    await memoryRequest('/memory/maintenance/reviews', 'POST', { item_id: props.itemId })
    ElMessage.success('已记录本次人工维护'); await load()
  } catch (err: any) { ElMessage.error(err.message) }
  finally { busy.value = false }
}
watch(() => [props.itemId, props.version], load, { immediate: true })
</script>
<template>
  <section class="maintenance-status" aria-label="经验检索与人工维护统计">
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-else-if="item">
      <p>被检索 {{ item.retrieval_count }} 次</p>
      <p>上次人工维护：{{ date(item.last_maintained_at) }}</p>
      <p>人工维护 {{ item.manual_maintenance_count }} 次</p>
      <el-button size="small" :loading="busy" @click="review">记录一次人工维护</el-button>
    </template>
  </section>
</template>
<style scoped>
.maintenance-status { margin-top: 16px; padding: 12px; border: 1px solid var(--border-1); border-radius: 8px; font-size: 12px; }
p { margin: 8px 0; }
</style>
