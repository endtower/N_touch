<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FolderRemove, Refresh, RefreshLeft } from '@element-plus/icons-vue'
import { memoryRequest } from '../api/memory'

defineEmits<{ openItem: [kind: string, itemId: string] }>()

const data = ref<any>({ requests: [], failure_reasons: [] })
const selectedId = ref('')
const error = ref('')
const loading = ref(false)
const busy = ref(false)
const showArchived = ref(false)

const archivedCount = computed(() => data.value.requests.filter((r: any) => r.archived_at).length)
const visibleRequests = computed(() => data.value.requests.filter((r: any) => showArchived.value || !r.archived_at))
const selected = computed(() => data.value.requests.find((r: any) => r.action_id === selectedId.value))

function date(value?: number | null) { return value ? new Date(value * 1000).toLocaleString() : '—' }

function pickFirstVisible() {
  const current = visibleRequests.value.some((r: any) => r.action_id === selectedId.value)
  if (!current) selectedId.value = visibleRequests.value[0]?.action_id ?? ''
}

async function reload() {
  error.value = ''
  loading.value = true
  try {
    data.value = await memoryRequest('/memory/maintenance')
    pickFirstVisible()
  } catch (err: any) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function toggleArchive(item: any) {
  const archiving = !item.archived_at
  try {
    await ElMessageBox.confirm(
      archiving
        ? `归档「${item.title}」？归档后默认列表不再显示，可在「显示已归档」中找回。`
        : `取消归档「${item.title}」？它将重新回到默认列表。`,
      archiving ? '归档维护记录' : '取消归档',
      { confirmButtonText: archiving ? '归档' : '取消归档', cancelButtonText: '返回', type: archiving ? 'warning' : 'info' },
    )
  } catch { return }
  busy.value = true
  try {
    await memoryRequest('/memory/maintenance/archive', 'POST', { action_id: item.action_id, archived: archiving })
    item.archived_at = archiving ? Date.now() / 1000 : null
    if (archiving) pickFirstVisible()
    ElMessage.success(archiving ? '已归档' : '已取消归档')
  } catch (err: any) {
    ElMessage.error(err.message || String(err))
  } finally {
    busy.value = false
  }
}

watch(showArchived, pickFirstVisible)
onMounted(reload)
defineExpose({ reload })
</script>
<template>
  <div class="maintenance-workspace">
    <aside class="maint-list">
      <div class="maint-list-head">
        <span class="maint-list-title">维护申请</span>
        <span class="maint-count">{{ visibleRequests.length }}</span>
        <label class="maint-filter">
          <el-switch v-model="showArchived" size="small" />
          <span>显示已归档<span v-if="archivedCount">（{{ archivedCount }}）</span></span>
        </label>
      </div>
      <div class="maint-list-scroll">
        <div v-if="error" class="maint-empty error" role="alert">
          <div class="maint-empty-title">无法读取维护申请</div>
          <div class="maint-empty-sub">{{ error }}</div>
          <el-button :icon="Refresh" size="small" style="margin-top: 10px;" @click="reload">重试</el-button>
        </div>
        <div v-else-if="loading && !data.requests.length" class="maint-empty"><span>加载中…</span></div>
        <div v-else-if="!visibleRequests.length" class="maint-empty">
          <div class="maint-empty-title">{{ showArchived ? '还没有维护记录' : '暂无新维护申请' }}</div>
          <div class="maint-empty-sub">{{ showArchived ? '已归档与未归档的申请都会显示在这里' : '效能感触顶后会在此登记申请' }}</div>
        </div>
        <button
          v-for="request in visibleRequests"
          :key="request.action_id"
          type="button"
          class="maint-card"
          :class="{ active: request.action_id === selectedId, archived: !!request.archived_at }"
          @click="selectedId = request.action_id"
        >
          <div class="maint-card-title">
            <span class="maint-card-name">{{ request.title }}</span>
            <span v-if="request.archived_at" class="maint-tag muted">已归档</span>
          </div>
          <div class="maint-card-meta">
            <span>{{ date(request.created_at) }}</span>
            <span v-if="request.failure_reasons.length" class="maint-tag">{{ request.failure_reasons.length }} 条负面原因</span>
          </div>
        </button>
      </div>
    </aside>

    <section class="maint-detail">
      <template v-if="selected">
        <header class="maint-detail-head">
          <div class="maint-detail-title">
            <h3>{{ selected.title }}</h3>
            <span v-if="selected.archived_at" class="maint-tag muted">已归档</span>
            <span class="maint-tag">{{ selected.axis }}</span>
          </div>
          <div class="maint-detail-actions">
            <span class="maint-time">{{ date(selected.created_at) }}</span>
            <el-button
              size="small"
              plain
              :type="selected.archived_at ? 'default' : 'warning'"
              :icon="selected.archived_at ? RefreshLeft : FolderRemove"
              :loading="busy"
              @click="toggleArchive(selected)"
            >{{ selected.archived_at ? '取消归档' : '归档' }}</el-button>
          </div>
        </header>

        <div class="maint-detail-body">
          <section class="maint-block">
            <div class="maint-block-label">申请理由</div>
            <p class="maint-text">{{ selected.details.reason }}</p>
          </section>
          <section class="maint-block">
            <div class="maint-block-label">改进需求</div>
            <p class="maint-text">{{ selected.details.plan }}</p>
          </section>
          <div class="maint-grid">
            <section class="maint-block">
              <div class="maint-block-label">提醒与告知方式</div>
              <p class="maint-text">{{ selected.details.notify_master || '—' }}</p>
            </section>
            <section class="maint-block">
              <div class="maint-block-label">实施时间</div>
              <p class="maint-text">{{ selected.details.next_occurrence || '—' }}</p>
            </section>
          </div>

          <section class="maint-block">
            <div class="maint-block-label">归入的负面原因（{{ selected.failure_reasons.length }}）</div>
            <p v-if="!selected.failure_reasons.length" class="maint-hint">发布这次申请时没有收集到负面结束评价。</p>
            <article v-for="reason in selected.failure_reasons" :key="reason.reason_id" class="maint-reason">
              <div class="maint-reason-head">
                <strong>{{ reason.title }}</strong>
                <span class="maint-tag">{{ reason.act }}</span>
              </div>
              <div class="maint-reason-meta">{{ reason.source }} · {{ date(reason.created_at) }}</div>
              <p class="maint-text">{{ reason.failure_reason }}</p>
            </article>
          </section>

          <section class="maint-block pending">
            <div class="maint-block-label">下次维护的待收集缓存（{{ data.failure_reasons.length }}）</div>
            <p v-if="!data.failure_reasons.length" class="maint-hint">缓存为空。申请发布后的新原因会在这里积累。</p>
            <article v-for="reason in data.failure_reasons" :key="reason.reason_id" class="maint-reason">
              <div class="maint-reason-head">
                <strong>{{ reason.title }}</strong>
                <span class="maint-tag">{{ reason.act }}</span>
              </div>
              <div class="maint-reason-meta">{{ reason.source }} · {{ date(reason.created_at) }}</div>
              <p class="maint-text">{{ reason.failure_reason }}</p>
            </article>
          </section>

          <div class="maint-footnote">申请编号 <code>{{ selected.action_id }}</code></div>
        </div>
      </template>

      <div v-else class="maint-detail-empty">
        <div class="maint-empty-title">未选中维护申请</div>
        <div class="maint-empty-sub">选择左侧任一申请，查看改进需求与归入的负面原因。</div>
      </div>
    </section>
  </div>
</template>
<style scoped>
.maintenance-workspace {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  color: var(--text-primary);
}

/* ---- 左列表 ---- */
.maint-list {
  flex: 0 0 340px;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-subtle);
}
.maint-list-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-subtle);
}
.maint-list-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.maint-count {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius-pill);
  padding: 0 6px;
  font-variant-numeric: tabular-nums;
}
.maint-filter {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  cursor: pointer;
  white-space: nowrap;
}
.maint-list-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
}

.maint-card {
  display: flex;
  flex-direction: column;
  gap: 5px;
  width: 100%;
  text-align: left;
  padding: 9px 10px;
  margin-bottom: 4px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.maint-card:hover { background: var(--bg-elevated); }
.maint-card.active { background: var(--accent-soft); border-color: var(--accent); }
.maint-card.archived .maint-card-name { color: var(--text-tertiary); }
.maint-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.maint-card-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.maint-card-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ---- 标签 ---- */
.maint-tag {
  display: inline-flex;
  align-items: center;
  padding: 1px 7px;
  border-radius: var(--radius-pill);
  font-size: 10.5px;
  font-weight: 600;
  white-space: nowrap;
  background: var(--accent-soft);
  color: var(--accent);
}
.maint-tag.muted {
  background: var(--bg-elevated);
  color: var(--text-tertiary);
  font-weight: 500;
}

/* ---- 右详情 ---- */
.maint-detail {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-app);
}
.maint-detail-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border-subtle);
}
.maint-detail-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.maint-detail-title h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.maint-detail-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}
.maint-time { font-size: 11px; color: var(--text-tertiary); }

.maint-detail-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px 24px;
}
.maint-block { margin-bottom: 18px; }
.maint-block-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}
.maint-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.maint-hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
.maint-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0 20px;
}

.maint-reason {
  padding: 10px 12px;
  margin-bottom: 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}
.maint-reason-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 2px;
}
.maint-reason-head strong {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.maint-reason-meta {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-bottom: 4px;
}

.maint-block.pending {
  margin-top: 22px;
  padding-top: 14px;
  border-top: 1px dashed var(--border-default);
}
.maint-footnote {
  margin-top: 20px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.maint-footnote code {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
}

/* ---- 空态 ---- */
.maint-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 4px;
  padding: 30px 12px;
  font-size: 12.5px;
  color: var(--text-tertiary);
}
.maint-empty.error { color: var(--tag-danger); }
.maint-empty-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.maint-empty-sub { font-size: 12px; }
.maint-detail-empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 24px;
  text-align: center;
}

@media (max-width: 760px) {
  .maintenance-workspace { flex-direction: column; }
  .maint-list { flex: 0 0 34%; border-right: 0; border-bottom: 1px solid var(--border-subtle); }
  .maint-detail { min-height: 0; }
}
</style>
