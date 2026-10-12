<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { memoryRequest } from '../api/memory'
const emit = defineEmits<{ openItem: [kind: string, itemId: string] }>()
const data = ref<any>({ requests: [], reviews: [] }), selectedId = ref(''), error = ref(''), busy = ref(false)
const conclusion = ref(''), deferHours = ref(24)
const names: Record<string, string> = { pending: '待处理', in_progress: '维护中', completed: '维护完成', unchanged: '无需修改', deferred: '已暂缓', rejected: '不采纳' }
const selected = computed(() => data.value.requests.find((r: any) => r.request_id === selectedId.value))
const terminal = computed(() => ['completed', 'unchanged', 'rejected'].includes(selected.value?.status))
function date(value: number) { return new Date(value * 1000).toLocaleString() }
async function reload() {
  error.value = ''
  try { data.value = await memoryRequest('/memory/maintenance') }
  catch (err: any) { error.value = err.message }
}
async function handle(status: string) {
  if (!selected.value || busy.value) return
  busy.value = true
  try {
    const result = await memoryRequest(`/memory/maintenance/requests/${selected.value.request_id}`, 'PATCH', { status, conclusion: conclusion.value, defer_hours: deferHours.value })
    if (result.unconfirmed_items?.length) ElMessage.warning('申请中的部分内容已变更或删除，未标记人工确认，请打开条目重新检查')
    else ElMessage.success('已记录处理结果')
    await reload()
  } catch (err: any) { ElMessage.error(err.message) }
  finally { busy.value = false }
}
function select(id: string) { selectedId.value = id; conclusion.value = ''; deferHours.value = 24 }
onMounted(reload)
defineExpose({ reload })
</script>
<template>
  <div class="maintenance-workspace">
    <aside>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="!data.requests.length">暂无专题维护申请。你仍可直接编辑经验、人格和锦囊。</p>
      <button v-for="request in data.requests" :key="request.request_id" class="request-item" :class="{ active: request.request_id === selectedId }" @click="select(request.request_id)">
        <strong>{{ request.topic }}</strong><span>{{ names[request.status] }} · {{ date(request.created_at) }}</span>
      </button>
    </aside>
    <section>
      <template v-if="selected">
        <h3>{{ selected.topic }}</h3><p>{{ selected.reason }}</p>
        <p class="muted">{{ names[selected.status] }} · 提交于 {{ date(selected.created_at) }}</p>
        <p v-if="selected.status === 'deferred'">暂缓至 {{ date(selected.defer_until) }}。到期后仍保留原申请，不自动催促。</p>
        <div class="item-links"><el-button v-for="item in selected.items" :key="`${item.kind}:${item.item_id}`" size="small" @click="emit('openItem', item.kind, item.item_id)">{{ item.title }}</el-button></div>
        <p>申请只邀请你检查内容；点击开始维护不会自动修改，也不授予 LLM 修改权限。</p>
        <p v-if="selected.conclusion">处理结论：{{ selected.conclusion }}</p>
        <template v-if="!terminal">
          <el-input v-model="conclusion" type="textarea" :rows="3" aria-label="维护处理说明" placeholder="可选：处理结论或暂缓原因，供 LLM 下次查看" />
          <div class="actions">
            <el-button :disabled="busy" @click="handle('in_progress')">开始维护</el-button>
            <el-button :disabled="busy" @click="handle('completed')">维护完成</el-button>
            <el-button :disabled="busy" @click="handle('unchanged')">无需修改</el-button>
            <el-button :disabled="busy" @click="handle('rejected')">不采纳</el-button>
          </div>
          <div class="actions"><span>暂缓小时</span><el-input-number v-model="deferHours" :min="1" :max="8760" size="small" /><el-button :disabled="busy" @click="handle('deferred')">暂缓</el-button></div>
        </template>
      </template>
      <template v-else><h3>按专题共同维护</h3><p>选择一条申请，查看依据、打开相关内容，并记录处理结果。</p></template>
      <div class="recent"><strong>最近人工维护</strong><p v-if="!data.reviews.length">尚无人工维护记录。</p><p v-for="review in data.reviews.slice(0, 10)" :key="review.review_id">{{ review.title }} · {{ review.action === 'edit' ? '人工编辑' : '人工检查' }} · {{ date(review.reviewed_at) }}</p></div>
    </section>
  </div>
</template>
<style scoped>
.maintenance-workspace { display: flex; width: 100%; min-height: 0; color: var(--text-1); }
aside { width: 30%; min-width: 180px; padding: 12px; border-right: 1px solid var(--border-1); overflow: auto; }
section { flex: 1; min-width: 0; padding: 20px; overflow: auto; }
.request-item { display: flex; flex-direction: column; gap: 7px; width: 100%; padding: 12px; margin-bottom: 8px; border: 1px solid var(--border-1); border-radius: 8px; text-align: left; background: var(--bg-subtle); color: var(--text-1); cursor: pointer; }
.request-item.active { border-color: var(--accent); } span, .muted { color: var(--text-3); font-size: 12px; }
p { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; } .actions, .item-links { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 12px 0; } .recent { margin-top: 24px; border-top: 1px solid var(--border-1); padding-top: 14px; font-size: 12px; }
</style>
