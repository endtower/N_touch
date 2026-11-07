<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { computed, reactive, ref, watch } from 'vue'
import {
  Document,
  Refresh,
  ArrowRight,
  Clock,
  Plus,
  Delete,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { TaskInfo } from '../types'
import { chat } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

type Status = 'active' | 'expired' | 'all'
interface StatusOpt {
  value: Status
  label: string
  tag: 'primary' | 'success' | 'danger' | 'info' | 'warning'
  color: string
}
const STATUS_OPTIONS: StatusOpt[] = [
  { value: 'active',  label: '进行中 (active)',   tag: 'primary', color: 'var(--tag-info)' },
  { value: 'expired', label: '已过期 (expired)',  tag: 'info',    color: 'var(--text-tertiary)' },
  { value: 'all',     label: '全部',              tag: 'info',    color: 'var(--text-tertiary)' },
]

// 给前端校验用: 排除 'all' 的 status 列表
const _STATUS_VALUES: readonly Status[] = STATUS_OPTIONS
  .filter(o => o.value !== 'all')
  .map(o => o.value)

const status = ref<Status>(chat.state.tasksStatus ?? 'active')
const expandedRows = ref<string[]>([])
const searchKeyword = ref('')

// 每行独立一份编辑表单 (避免状态共享导致的脏写)
interface EditForm {
  task_id: string
  title: string
  context: string          // 要传递给下次启动时的上下文
  status: Status
  next_occurrence: string  // ISO datetime; 下次唤醒时间
  start_time: string       // ISO datetime; 允许编辑 (回填历史任务)
  end_time: string         // 只读展示, 不提交
  updated_at: string       // 只读展示
}
const editForms = ref<Record<string, EditForm>>({})
const editSaving = ref<Record<string, boolean>>({})
// 时间字段的本地校验错误 (key = `${taskId}.${field}`)
const editTimeErrors = ref<Record<string, string | null>>({})

/** 校验单个 ISO datetime 字符串。空串视为清空 (合法)。返回错误信息或 null */
function validateIsoDatetime(value: string): string | null {
  const s = (value || '').trim()
  if (!s) return null
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) {
    return '时间格式不合法 (需要 ISO datetime, 例如 2026-01-01T12:00:00)'
  }
  if (!/^\d{4}-\d{2}-\d{2}/.test(s)) {
    return '时间格式不合法 (需以 YYYY-MM-DD 开头)'
  }
  return null
}

const events = computed<TaskInfo[]>(() => chat.state.tasks?.tasks ?? [])
const total = computed(() => chat.state.tasks?.total ?? 0)
const loading = computed(() => chat.state.tasksLoading)
const error = computed(() => chat.state.tasksError)

const visibleEvents = computed<TaskInfo[]>(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return events.value
  return events.value.filter(e =>
    (e.title || '').toLowerCase().includes(kw) ||
    (e.context || '').toLowerCase().includes(kw) ||
    (e.status || '').toLowerCase().includes(kw),
  )
})

const statusCounts = computed(() => {
  const m: Record<string, number> = {}
  for (const e of events.value) {
    m[e.status] = (m[e.status] || 0) + 1
  }
  return m
})

watch(
  () => props.visible,
  async (v) => {
    if (v) {
      status.value = chat.state.tasksStatus ?? 'active'
      await refresh()
    }
  },
)

watch(status, async () => {
  if (props.visible) await refresh()
})

async function refresh() {
  await chat.loadTasks(status.value, 500, 0)
}

function statusTag(s: string) {
  return STATUS_OPTIONS.find(o => o.value === s)?.tag ?? 'info'
}

async function changeStatus(ev: TaskInfo, newStatus: string) {
  if (ev.status === newStatus) return
  try {
    await ElMessageBox.confirm(
      `把「${ev.title}」从 ${ev.status} 改为 ${newStatus} ?`,
      '切换任务状态',
      { type: 'info', confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await fetch('/events/set_type', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: ev.task_id, status: newStatus }),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    const body = await res.json()
    const updated = body.task as TaskInfo
    if (status.value !== 'all' && updated.status !== status.value) {
      if (chat.state.tasks) {
        chat.state.tasks.tasks = chat.state.tasks.tasks.filter(e => e.task_id !== updated.task_id)
        chat.state.tasks.total = Math.max(0, chat.state.tasks.total - 1)
      }
      ElMessage.success(`已改为 ${newStatus}, 不再匹配当前筛选`)
    } else {
      chat.patchTask(updated)
      ElMessage.success(`已改为 ${newStatus}`)
    }
  } catch (e: any) {
    ElMessage.error(`更新失败: ${e?.message || e}`)
  }
}

function timeShort(s?: string | null) {
  if (!s) return '—'
  return s.length >= 16 ? s.slice(0, 16).replace('T', ' ') : s
}

function close() { emit('update:visible', false) }

function buildEditForm(ev: TaskInfo): EditForm {
  return {
    task_id: ev.task_id,
    title: ev.title ?? '',
    context: ev.context ?? '',
    status: (ev.status as Status) ?? 'active',
    next_occurrence: ev.next_occurrence ?? '',
    start_time: ev.start_time ?? '',
    end_time: ev.end_time ?? '',
    updated_at: ev.updated_at ?? '',
  }
}

function toggleExpand(taskId: string) {
  if (expandedRows.value.includes(taskId)) {
    expandedRows.value = expandedRows.value.filter(id => id !== taskId)
  } else {
    expandedRows.value = [...expandedRows.value, taskId]
    if (!editForms.value[taskId]) {
      const ev = events.value.find(e => e.task_id === taskId)
      if (ev) editForms.value[taskId] = buildEditForm(ev)
    }
  }
}

function discardEdit(taskId: string) {
  delete editForms.value[taskId]
}

async function saveEdit(taskId: string) {
  const form = editForms.value[taskId]
  if (!form) return
  if (!form.title.trim()) {
    ElMessage.warning('title 必填')
    return
  }

  const timeFields: Array<keyof EditForm> = ['next_occurrence', 'start_time']
  for (const f of timeFields) {
    const err = validateIsoDatetime(form[f] as string)
    editTimeErrors.value[`${taskId}.${f}`] = err
    if (err) {
      ElMessage.error(`${f}: ${err}`)
      return
    }
  }

  editSaving.value[taskId] = true
  try {
    const body: Record<string, unknown> = {
      title: form.title.trim(),
      context: form.context,
      status: form.status,
    }
    body.next_occurrence = form.next_occurrence.trim() ? form.next_occurrence.trim() : null
    body.start_time = form.start_time.trim() ? form.start_time.trim() : null

    const res = await fetch(`/events/${encodeURIComponent(taskId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    const r = await res.json()
    const updated = r.task as TaskInfo
    chat.patchTask(updated)
    if (status.value !== 'all' && updated.status !== status.value) {
      if (chat.state.tasks) {
        chat.state.tasks.tasks = chat.state.tasks.tasks.filter(e => e.task_id !== updated.task_id)
        chat.state.tasks.total = Math.max(0, chat.state.tasks.total - 1)
      }
      expandedRows.value = expandedRows.value.filter(id => id !== taskId)
      delete editForms.value[taskId]
    } else {
      editForms.value[taskId] = buildEditForm(updated)
    }
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(`保存失败: ${e?.message || e}`)
  } finally {
    editSaving.value[taskId] = false
  }
}

// ============================================================
// 创建 / 删除
// ============================================================

interface CreateForm {
  title: string
  context: string
  status: Status
  next_occurrence: string
  start_time: string
}
const createDialogVisible = ref(false)
const creating = ref(false)
const createForm = reactive<CreateForm>({
  title: '',
  context: '',
  status: 'active',
  next_occurrence: '',
  start_time: '',
})
const createErrors = reactive<Record<keyof CreateForm, string | null>>({
  title: null,
  context: null,
  status: null,
  next_occurrence: null,
  start_time: null,
})

function resetCreateForm() {
  createForm.title = ''
  createForm.context = ''
  createForm.status = 'active'
  createForm.next_occurrence = ''
  createForm.start_time = ''
  Object.keys(createErrors).forEach(k => { (createErrors as any)[k] = null })
}

function openCreateDialog() {
  resetCreateForm()
  createDialogVisible.value = true
}

async function submitCreate() {
  createErrors.title = createForm.title.trim() ? null : 'title 必填'
  createErrors.status = _STATUS_VALUES.includes(createForm.status) ? null : 'status 非法'
  createErrors.next_occurrence = validateIsoDatetime(createForm.next_occurrence)
  createErrors.start_time = validateIsoDatetime(createForm.start_time)
  if (createErrors.title || createErrors.next_occurrence || createErrors.start_time || createErrors.status) {
    ElMessage.warning('请修正表单中的错误')
    return
  }

  creating.value = true
  try {
    const body: Record<string, unknown> = {
      title: createForm.title.trim(),
      context: createForm.context,
      status: createForm.status,
    }
    if (createForm.next_occurrence.trim()) body.next_occurrence = createForm.next_occurrence.trim()
    if (createForm.start_time.trim()) body.start_time = createForm.start_time.trim()

    const res = await fetch('/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    const r = await res.json()
    const created = r.task as TaskInfo
    if (status.value !== 'all' && created.status !== status.value) {
      ElMessage.success(`已创建「${created.title}」, 不在当前筛选中, 请切换 status 查看`)
    } else {
      chat.addTask(created)
      ElMessage.success(`已创建「${created.title}」`)
    }
    createDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(`创建失败: ${e?.message || e}`)
  } finally {
    creating.value = false
  }
}

async function deleteEvent(ev: TaskInfo) {
  try {
    await ElMessageBox.confirm(
      `确认删除任务「${ev.title || ev.task_id}」? 此操作不可撤销 (任务记录将被清除)。`,
      '删除任务',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  try {
    const res = await fetch(`/events/${encodeURIComponent(ev.task_id)}`, {
      method: 'DELETE',
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    chat.removeTask(ev.task_id)
    expandedRows.value = expandedRows.value.filter(id => id !== ev.task_id)
    delete editForms.value[ev.task_id]
    ElMessage.success(`已删除「${ev.title || ev.task_id}」`)
  } catch (e: any) {
    ElMessage.error(`删除失败: ${e?.message || e}`)
  }
}
</script>

<template>
  <AppDialog size="panel"
    :model-value="props.visible"
    :close-on-click-modal="false"
    align-center
    title="任务管理"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
    class="evlist-dialog"
  >
    <template #header>
      <div class="evlist-header">
        <div class="evlist-title">
          <el-icon :size="16" color="var(--accent)"><Document /></el-icon>
          <span>任务管理</span>
          <span v-if="chat.state.tasks" class="evlist-counts">
            <strong>{{ total }}</strong> 条 · {{ STATUS_OPTIONS.find(o => o.value === status)?.label }}
          </span>
        </div>
        <div class="evlist-toolbar">
          <el-input
            v-model="searchKeyword"
            size="small"
            placeholder="搜索标题 / 上下文…"
            clearable
            style="width: 220px;"
          >
            <template #prefix><el-icon><Document /></el-icon></template>
          </el-input>
          <el-select v-model="status" size="small" style="width: 180px;">
            <el-option v-for="o in STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value">
              <span style="display: inline-flex; align-items: center; gap: 6px;">
                <span :style="{
                  display: 'inline-block',
                  width: '8px',
                  height: '8px',
                  borderRadius: '50%',
                  background: o.color,
                }" />
                {{ o.label }}
              </span>
            </el-option>
          </el-select>
          <el-button :icon="Plus" size="small" type="primary" @click="openCreateDialog">新建</el-button>
          <el-button :icon="Refresh" size="small" @click="refresh">刷新</el-button>
          <el-button size="small" @click="close">关闭</el-button>
        </div>
      </div>
    </template>

    <div class="evlist-body">
      <div class="evlist-chips">
        <span
          v-for="o in STATUS_OPTIONS.filter(x => x.value !== 'all')"
          :key="o.value"
          :class="['evlist-chip', { active: status === o.value }]"
          :style="{ '--chip-color': o.color }"
          @click="status = o.value"
        >
          <span class="evlist-chip-dot" />
          {{ o.value }}
          <span v-if="statusCounts[o.value] !== undefined" class="evlist-chip-count">
            {{ statusCounts[o.value] }}
          </span>
        </span>
        <span
          v-if="status !== 'all'"
          :class="['evlist-chip', 'all', { active: false }]"
          style="--chip-color: var(--text-3);"
          @click="status = 'all'"
        >
          看全部
        </span>
      </div>

      <div class="evlist-table-wrap">
        <div v-if="error" class="evlist-state error">
          <el-icon :size="20"><Document /></el-icon>
          <div>
            <div style="font-weight: 600; margin-bottom: 4px;">任务加载失败</div>
            <div style="font-size: 12px;">{{ error }}</div>
          </div>
          <el-button :icon="Refresh" size="small" @click="refresh">重试</el-button>
        </div>

        <div v-else-if="loading && events.length === 0" class="evlist-state">
          <el-icon :size="20"><Refresh /></el-icon>
          <span>加载中…</span>
        </div>

        <div v-else-if="visibleEvents.length === 0" class="evlist-state">
          <div style="font-size: 13px; font-weight: 600; color: var(--text-1); margin-bottom: 4px;">
            {{ events.length === 0 ? '该状态没有任务' : '没有匹配的任务' }}
          </div>
          <div style="font-size: 12px;">
            {{ events.length === 0 ? '换一个 status 试试, 或选 "全部" 看看所有任务' : '清空搜索框看看' }}
          </div>
        </div>

        <table v-else class="evlist-table">
          <thead>
            <tr>
              <th style="width: 28px;"></th>
              <th style="width: 130px;">状态</th>
              <th>标题</th>
              <th style="width: 200px;">下次唤醒</th>
              <th style="width: 170px;">最近更新</th>
              <th style="width: 220px; text-align: right;">切状态</th>
              <th style="width: 90px; text-align: right;">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="ev in visibleEvents" :key="ev.task_id">
              <tr
                :class="{ expanded: expandedRows.includes(ev.task_id) }"
                @click="toggleExpand(ev.task_id)"
              >
                <td class="caret-cell">
                  <el-icon :size="12" color="var(--text-3)">
                    <ArrowRight v-if="!expandedRows.includes(ev.task_id)" style="transform: rotate(0deg); transition: transform 0.15s ease;" />
                    <ArrowRight v-else style="transform: rotate(90deg); transition: transform 0.15s ease;" />
                  </el-icon>
                </td>
                <td>
                  <el-tag :type="statusTag(ev.status)" size="small">{{ ev.status }}</el-tag>
                </td>
                <td>
                  <div class="evlist-title-cell">
                    <span class="evlist-title-text">{{ ev.title || '—' }}</span>
                    <span v-if="ev.context" class="evlist-summary-hint">
                      {{ ev.context.slice(0, 80) }}{{ ev.context.length > 80 ? '…' : '' }}
                    </span>
                  </div>
                </td>
                <td>
                  <span class="evlist-time">
                    <el-icon :size="11" color="var(--text-3)"><Clock /></el-icon>
                    {{ timeShort(ev.next_occurrence) }}
                  </span>
                </td>
                <td>
                  <span class="evlist-time">
                    <el-icon :size="11" color="var(--text-3)"><Clock /></el-icon>
                    {{ timeShort(ev.updated_at || ev.start_time) }}
                  </span>
                </td>
                <td class="evlist-status-cell" @click.stop>
                  <el-select
                    :model-value="ev.status"
                    size="small"
                    style="width: 100%;"
                    @change="(v: string) => changeStatus(ev, v)"
                  >
                    <el-option
                      v-for="o in STATUS_OPTIONS.filter(x => x.value !== 'all')"
                      :key="o.value"
                      :label="o.label"
                      :value="o.value"
                    >
                      <span style="display: inline-flex; align-items: center; gap: 6px;">
                        <span :style="{
                          display: 'inline-block',
                          width: '8px',
                          height: '8px',
                          borderRadius: '50%',
                          background: o.color,
                        }" />
                        {{ o.label }}
                      </span>
                    </el-option>
                  </el-select>
                </td>
                <td class="evlist-action-cell" @click.stop>
                  <el-button
                    :icon="Delete"
                    size="small"
                    type="danger"
                    plain
                    title="删除此任务 (不可撤销)"
                    @click="deleteEvent(ev)"
                  />
                </td>
              </tr>
              <tr v-if="expandedRows.includes(ev.task_id)" class="evlist-detail-row">
                <td colspan="7">
                  <div class="evlist-detail" v-if="editForms[ev.task_id]" @click.stop>
                    <el-form label-position="top" size="default" class="evlist-edit-form">
                      <!-- 左: title + context -->
                      <div class="evlist-edit-left">
                        <el-form-item label="title / 标题" required>
                          <el-input
                            v-model="editForms[ev.task_id]!.title"
                            placeholder="任务标题"
                          />
                        </el-form-item>
                        <el-form-item label="context / 传递给下次启动时的上下文">
                          <el-input
                            v-model="editForms[ev.task_id]!.context"
                            type="textarea"
                            :rows="8"
                            placeholder="这条事项的目的、已确认进展、人物/会话/材料线索、下次要处理什么"
                          />
                        </el-form-item>
                        <el-form-item label="task_id (只读)">
                          <el-input :model-value="ev.task_id" readonly disabled />
                        </el-form-item>
                      </div>

                      <!-- 右: 状态 + 时间 -->
                      <div class="evlist-edit-right">
                        <el-form-item label="status">
                          <el-select
                            v-model="editForms[ev.task_id]!.status"
                            style="width: 100%;"
                          >
                            <el-option
                              v-for="o in STATUS_OPTIONS.filter(x => x.value !== 'all')"
                              :key="o.value"
                              :label="o.label"
                              :value="o.value"
                            >
                              <span style="display: inline-flex; align-items: center; gap: 6px;">
                                <span :style="{
                                  display: 'inline-block',
                                  width: '8px',
                                  height: '8px',
                                  borderRadius: '50%',
                                  background: o.color,
                                }" />
                                {{ o.label }}
                              </span>
                            </el-option>
                          </el-select>
                        </el-form-item>

                        <el-form-item
                          label="next_occurrence (下次唤醒时间; 可空)"
                          :error="editTimeErrors[`${ev.task_id}.next_occurrence`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.task_id]!.next_occurrence"
                            placeholder="2026-01-01T12:00:00"
                          />
                        </el-form-item>

                        <el-form-item
                          label="start_time (开始时间)"
                          :error="editTimeErrors[`${ev.task_id}.start_time`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.task_id]!.start_time"
                            placeholder="2026-01-01T12:00:00"
                          />
                        </el-form-item>

                        <el-form-item label="updated_at (只读 · 保存时自动刷新)">
                          <el-input :model-value="timeShort(editForms[ev.task_id]!.updated_at)" readonly disabled />
                        </el-form-item>

                        <el-form-item label="end_time (只读 · 过期时自动设)">
                          <el-input :model-value="timeShort(editForms[ev.task_id]!.end_time)" readonly disabled />
                        </el-form-item>
                      </div>
                    </el-form>

                    <div class="evlist-edit-actions">
                      <span class="evlist-edit-hint">
                        💡 任务一次性：到点由 truck 交付后自动置 <code>expired</code>，续办靠重新登记。这里手动改 <code>status</code> / <code>next_occurrence</code> / <code>start_time</code> 会做 ISO 格式校验，不合法不会提交。
                      </span>
                      <div style="display: flex; gap: 8px;">
                        <el-button @click="discardEdit(ev.task_id)">还原</el-button>
                        <el-button
                          type="primary"
                          :loading="editSaving[ev.task_id]"
                          @click="saveEdit(ev.task_id)"
                        >保存</el-button>
                      </div>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ============== 创建任务弹窗 ============== -->
    <AppDialog
      v-model="createDialogVisible"
      width="640px"
      :close-on-click-modal="false"
      align-center
      class="evlist-create-dialog"
      append-to-body
    >
      <template #header>
        <div class="evlist-header">
          <div class="evlist-title">
            <el-icon :size="16" color="var(--accent)"><Plus /></el-icon>
            <span>新建任务</span>
            <span class="evlist-counts">手动创建任务, 会写入记忆库</span>
          </div>
        </div>
      </template>

      <el-form label-position="top" size="default" class="evlist-create-form">
        <el-form-item label="title / 标题" required :error="createErrors.title ?? undefined">
          <el-input v-model="createForm.title" placeholder="任务标题" maxlength="120" show-word-limit />
        </el-form-item>

        <el-form-item label="context / 传递给下次启动时的上下文">
          <el-input
            v-model="createForm.context"
            type="textarea"
            :rows="5"
            placeholder="这条事项的目的、已确认进展、人物/会话/材料线索、下次要处理什么 (可空)"
          />
        </el-form-item>

        <el-form-item label="status" :error="createErrors.status ?? undefined">
          <el-select v-model="createForm.status" style="width: 100%;">
            <el-option
              v-for="o in STATUS_OPTIONS.filter(x => x.value !== 'all')"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            >
              <span style="display: inline-flex; align-items: center; gap: 6px;">
                <span :style="{
                  display: 'inline-block',
                  width: '8px',
                  height: '8px',
                  borderRadius: '50%',
                  background: o.color,
                }" />
                {{ o.label }}
              </span>
            </el-option>
          </el-select>
        </el-form-item>

        <div class="evlist-create-grid">
          <el-form-item
            label="next_occurrence (下次唤醒时间; 可空)"
            :error="createErrors.next_occurrence ?? undefined"
          >
            <el-input v-model="createForm.next_occurrence" placeholder="2026-01-01T12:00:00" />
          </el-form-item>

          <el-form-item
            label="start_time (留空 = now; 手动填 = 历史回填)"
            :error="createErrors.start_time ?? undefined"
          >
            <el-input v-model="createForm.start_time" placeholder="2026-01-01T12:00:00" />
          </el-form-item>
        </div>

        <div class="evlist-edit-hint" style="margin-top: 4px;">
          💡 创建时会自动生成 <code>task_id</code> / <code>start_time</code> (now) / <code>updated_at</code> (now)。
        </div>
      </el-form>

      <template #footer>
        <div style="display: flex; justify-content: flex-end; gap: 8px;">
          <el-button @click="createDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
        </div>
      </template>
    </AppDialog>
  </AppDialog>
</template>

<style scoped>

.evlist-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 16px;
  padding-right: 8px;
}
.evlist-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
}
.evlist-counts {
  font-size: 11.5px;
  color: var(--text-3);
  font-weight: 400;
  margin-left: 4px;
}
.evlist-counts strong {
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}
.evlist-toolbar { display: flex; align-items: center; gap: 8px; }

/* —— 主体 —— */
.evlist-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  border-top: 1px solid var(--border-1);
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-surface);
  padding: 14px 18px;
  gap: 12px;
  overflow: hidden;
}

/* —— 状态 chips —— */
.evlist-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  flex: 0 0 auto;
}
.evlist-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--border-1);
  border-radius: 999px;
  background: var(--bg-surface);
  color: var(--text-2);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.12s ease;
  user-select: none;
}
.evlist-chip:hover {
  border-color: var(--border-2);
  color: var(--text-1);
}
.evlist-chip.active {
  background: color-mix(in srgb, var(--chip-color) 12%, transparent);
  border-color: var(--chip-color);
  color: var(--text-1);
}
.evlist-chip-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--chip-color);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--chip-color) 20%, transparent);
}
.evlist-chip-count {
  font-size: 10.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  margin-left: 2px;
  background: var(--bg-subtle);
  padding: 0 5px;
  border-radius: 999px;
}
.evlist-chip.active .evlist-chip-count {
  color: var(--text-1);
  background: color-mix(in srgb, var(--chip-color) 20%, transparent);
}

/* —— 表格区 —— */
.evlist-table-wrap {
  flex: 1 1 auto;
  min-height: 0;
  background: var(--bg-surface);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  overflow: auto;
  box-shadow: none;
}

.evlist-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 60px 20px;
  text-align: center;
  color: var(--text-3);
  flex-direction: column;
}
.evlist-state.error {
  flex-direction: row;
  justify-content: flex-start;
  background: color-mix(in srgb, var(--tag-danger) 8%, transparent);
  color: var(--danger);
  border-radius: var(--radius);
  margin: 16px;
  padding: 14px 18px;
}

.evlist-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 13px;
  color: var(--text-1);
}
.evlist-table thead th {
  position: sticky;
  top: 0;
  background: var(--bg-subtle);
  text-align: left;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-1);
  white-space: nowrap;
  z-index: 1;
}
.evlist-table tbody td {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-1);
  vertical-align: middle;
}
.evlist-table tbody tr {
  cursor: pointer;
  transition: background 0.1s ease;
}
.evlist-table tbody tr:hover { background: var(--bg-subtle); }
.evlist-table tbody tr.expanded { background: var(--accent-soft); }
.evlist-table tbody tr.expanded:hover { background: var(--accent-soft); }

.caret-cell { width: 28px; padding-right: 4px; }
.caret-cell .el-icon { transition: transform 0.15s ease; }

.evlist-title-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  max-width: 540px;
}
.evlist-title-text {
  font-weight: 500;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.evlist-summary-hint {
  font-size: 11.5px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.4;
}

.evlist-time {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-variant-numeric: tabular-nums;
  color: var(--text-2);
  font-size: 12px;
}
.evlist-status-cell { text-align: right; }
.evlist-action-cell { text-align: right; padding-right: 14px !important; }
.evlist-action-cell .el-button { padding: 4px 8px; }

/* —— 创建弹窗 —— */
.evlist-create-form { margin: 0; }
.evlist-create-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}
@media (max-width: 700px) {
  .evlist-create-grid { grid-template-columns: 1fr; }
}
.evlist-create-dialog :deep(.el-dialog__footer) { padding: 12px 20px 16px !important; border-top: 1px solid var(--border-1); }

.evlist-detail-row { background: var(--bg-subtle); }
.evlist-detail-row td { padding: 0; border-bottom: 1px solid var(--border-1); cursor: default; }
.evlist-detail {
  padding: 18px 20px 18px 56px;
  font-size: 12.5px;
  background: var(--bg-subtle);
}

/* 编辑表单 (展开行内) — 左右两栏 */
.evlist-edit-form {
  margin: 0;
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 18px 22px;
  align-items: start;
}
.evlist-edit-left {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-width: 0;
}
.evlist-edit-right {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-width: 0;
}
@media (max-width: 900px) {
  .evlist-edit-form {
    grid-template-columns: 1fr;
  }
}
.evlist-edit-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed var(--border-1);
  flex-wrap: wrap;
}
.evlist-edit-hint {
  font-size: 11px;
  color: var(--text-3);
  line-height: 1.5;
  flex: 1 1 auto;
  min-width: 0;
}
.evlist-edit-hint code {
  font-family: var(--font-mono);
  background: var(--bg-app);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--accent);
}
/* 表单错误提示 (el-form-item 自带) */
.evlist-edit-right :deep(.el-form-item__error) {
  font-size: 11px;
  padding-top: 2px;
}
/* Keep header controls clear of the shared close button. */
.evlist-header, .evlist-title, .evlist-toolbar { flex-wrap: wrap; min-width: 0; gap: 8px; }
</style>
