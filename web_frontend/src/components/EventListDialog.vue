<script setup lang="ts">
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

type Status = 'active' | 'completed' | 'all'
interface StatusOpt {
  value: Status
  label: string
  tag: 'primary' | 'success' | 'danger' | 'info' | 'warning'
  color: string   // cytoscape 风格 swatch
}
const STATUS_OPTIONS: StatusOpt[] = [
  { value: 'active',    label: '进行中 (active)',     tag: 'primary', color: '#818cf8' },
  { value: 'completed', label: '已完成 (completed)',  tag: 'success', color: '#34d399' },
  { value: 'all',       label: '全部',                tag: 'info',    color: '#94a3b8' },
]

// 给前端校验用: 排除 'all' 的 status 列表 (用数组 + .includes 比 Set 跟 Vue reactive 协同更稳)
const _STATUS_VALUES: readonly Status[] = STATUS_OPTIONS
  .filter(o => o.value !== 'all')
  .map(o => o.value)

const status = ref<Status>(chat.state.tasksStatus ?? 'active')
const expandedRows = ref<string[]>([])
const searchKeyword = ref('')
const refreshTimer = ref<number | null>(null)

// 每行独立一份编辑表单 (避免状态共享导致的脏写)
interface EditForm {
  task_id: string
  title: string
  brief: string            // 简要总结 (前因与目的)
  conclusion: string       // 已获取的、用于汇报的结论
  stage: string            // 当前阶段 (还需要做什么)
  notes: string            // 任务笔记
  status: Status
  next_occurrence: string
  deadline: string         // ISO datetime; 最终汇报时间
  start_time: string       // ISO datetime; 允许编辑 (例如回填历史任务)
  updated_at: string       // ISO datetime; 允许编辑 (用户手动覆写)
  end_time: string         // 只读展示, 不提交
}
const editForms = ref<Record<string, EditForm>>({})
const editSaving = ref<Record<string, boolean>>({})
// 时间字段的本地校验错误 (key = `${taskId}.${field}`)
const editTimeErrors = ref<Record<string, string | null>>({})

// 展开行的任务笔记 → 平铺行（随编辑表单实时解析）
const parsedNotesByTask = computed<Record<string, TaskNotes>>(() => {
  const out: Record<string, TaskNotes> = {}
  for (const [id, form] of Object.entries(editForms.value)) {
    out[id] = parseTaskNotes(form.notes)
  }
  return out
})

/** 校验单个 ISO datetime 字符串。空串视为清空 (合法)。返回错误信息或 null */
function validateIsoDatetime(value: string): string | null {
  const s = (value || '').trim()
  if (!s) return null
  // 用 Date.parse 兜底, 然后用 ISO 严格格式校验
  // 接受 "2026-01-01T12:00:00" / "2026-01-01 12:00:00" / 带毫秒 / 带时区
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) {
    return '时间格式不合法 (需要 ISO datetime, 例如 2026-01-01T12:00:00)'
  }
  // 二次校验: 必须是 YYYY-MM-DD 开头, 且能被 new Date 解析
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
    (e.brief || '').toLowerCase().includes(kw) ||
    (e.conclusion || '').toLowerCase().includes(kw) ||
    (e.stage || '').toLowerCase().includes(kw) ||
    (e.notes || '').toLowerCase().includes(kw) ||
    (e.status || '').toLowerCase().includes(kw),
  )
})

const statusCounts = computed(() => {
  // 顶部 chip 显示各 status 的数量, 但需要单独拉一下 "all" 才知道总数
  // 这里只在当前数据集上统计, 数字可能不全, 仅作参考
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
    } else {
      if (refreshTimer.value != null) {
        window.clearInterval(refreshTimer.value)
        refreshTimer.value = null
      }
    }
  },
)

watch(status, async (v) => {
  if (props.visible) await refresh()
})

async function refresh() {
  await chat.loadTasks(status.value, 500, 0)
}

function statusTag(s: string) {
  return STATUS_OPTIONS.find(o => o.value === s)?.tag ?? 'info'
}
function statusColor(s: string) {
  return STATUS_OPTIONS.find(o => o.value === s)?.color ?? '#94a3b8'
}
function statusLabel(s: string) {
  return STATUS_OPTIONS.find(o => o.value === s)?.label ?? s
}

async function changeStatus(ev: TaskInfo, newStatus: string) {
  if (ev.status === newStatus) return
  try {
    await ElMessageBox.confirm(
      `把「${ev.title}」从 ${ev.status} 改为 ${newStatus} ?${newStatus === 'completed' ? ' (会自动设置 end_time)' : ''}`,
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

// ===== 任务笔记 → 平铺行结构化解析 =====
// notes 列为低权限记录通道（brain/hc/database/task.py）：每行一条笔记，
// 以「- 」开头视为 bullet；其余按普通段落展示（不再含「当前阶段」首行约定，
// 阶段已独立为 stage 列）。
interface TaskNoteLine {
  indent: number    // 层级（2 空格 = 1 层）
  text: string
  isBullet: boolean // 是否为「- 」笔记行；非 bullet 行按普通段落展示
}
interface TaskNotes {
  lines: TaskNoteLine[]
}

function parseTaskNotes(notes: string): TaskNotes {
  const lines: TaskNoteLine[] = []
  for (const raw of (notes || '').split('\n')) {
    if (!raw.trim()) continue
    const leading = (raw.match(/^\s*/) || [''])[0].length
    const text = raw.trim()
    lines.push({
      indent: Math.floor(leading / 2),
      text: text.startsWith('- ') ? text.slice(2) : text,
      isBullet: text.startsWith('- '),
    })
  }
  return { lines }
}

function close() { emit('update:visible', false) }

function toggleExpand(taskId: string) {
  if (expandedRows.value.includes(taskId)) {
    expandedRows.value = expandedRows.value.filter(id => id !== taskId)
  } else {
    expandedRows.value = [...expandedRows.value, taskId]
    // 第一次展开时, 拉一份快照作为可编辑表单
    if (!editForms.value[taskId]) {
      const ev = events.value.find(e => e.task_id === taskId)
      if (ev) {
        editForms.value[taskId] = {
          task_id: ev.task_id,
          title: ev.title ?? '',
          brief: ev.brief ?? '',
          conclusion: ev.conclusion ?? '',
          stage: ev.stage ?? '',
          notes: ev.notes ?? '',
          status: (ev.status as Status) ?? 'active',
          next_occurrence: ev.next_occurrence ?? '',
          deadline: ev.deadline ?? '',
          start_time: ev.start_time ?? '',
          updated_at: ev.updated_at ?? '',
          end_time: ev.end_time ?? '',
        }
      }
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

  // —— 时间字段本地校验 ——
  const timeFields: Array<keyof EditForm> = ['next_occurrence', 'deadline', 'start_time', 'updated_at']
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
      status: form.status,
      brief: form.brief,
      conclusion: form.conclusion,
      stage: form.stage,
      notes: form.notes,
    }
    // 时间字段: 空字符串传 null 让后端清空 (updated_at 仍允许清空, 但通常不应清空)
    body.next_occurrence = form.next_occurrence.trim() ? form.next_occurrence.trim() : null
    body.deadline = form.deadline.trim() ? form.deadline.trim() : null
    body.start_time = form.start_time.trim() ? form.start_time.trim() : null
    body.updated_at = form.updated_at.trim() ? form.updated_at.trim() : null

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
    // 如果新 status 不在当前筛选里, 把这一行移出当前 list (跟 set_type 一致)
    if (status.value !== 'all' && updated.status !== status.value) {
      if (chat.state.tasks) {
        chat.state.tasks.tasks = chat.state.tasks.tasks.filter(e => e.task_id !== updated.task_id)
        chat.state.tasks.total = Math.max(0, chat.state.tasks.total - 1)
      }
      expandedRows.value = expandedRows.value.filter(id => id !== taskId)
      delete editForms.value[taskId]
    } else {
      // 同步本地 edit form
      editForms.value[taskId] = {
        task_id: updated.task_id,
        title: updated.title ?? '',
        brief: updated.brief ?? '',
        conclusion: updated.conclusion ?? '',
        stage: updated.stage ?? '',
        notes: updated.notes ?? '',
        status: (updated.status as Status) ?? 'active',
        next_occurrence: updated.next_occurrence ?? '',
        deadline: updated.deadline ?? '',
        start_time: updated.start_time ?? '',
        updated_at: updated.updated_at ?? '',
        end_time: updated.end_time ?? '',
      }
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

// 创建表单 (单实例, 不需要按 task_id 分)
interface CreateForm {
  title: string
  brief: string               // 简要总结 (前因与目的)
  conclusion: string          // 已获取的、用于汇报的结论
  stage: string               // 当前阶段 (还需要做什么)
  notes: string               // 任务笔记
  status: Status
  next_occurrence: string
  deadline: string            // 可空: 最终汇报时间
  start_time: string          // 可空: 留空 = 用 now (后端默认); 填了 = 历史回填
}
const createDialogVisible = ref(false)
const creating = ref(false)
const createForm = reactive<CreateForm>({
  title: '',
  brief: '',
  conclusion: '',
  stage: '',
  notes: '',
  status: 'active',
  next_occurrence: '',
  deadline: '',
  start_time: '',
})
const createTimeErrors = reactive<Record<keyof CreateForm, string | null>>({
  title: null,
  brief: null,
  conclusion: null,
  stage: null,
  notes: null,
  status: null,
  next_occurrence: null,
  deadline: null,
  start_time: null,
})

function resetCreateForm() {
  createForm.title = ''
  createForm.brief = ''
  createForm.conclusion = ''
  createForm.stage = ''
  createForm.notes = ''
  createForm.status = 'active'
  createForm.next_occurrence = ''
  createForm.deadline = ''
  createForm.start_time = ''
  Object.keys(createTimeErrors).forEach(k => { (createTimeErrors as any)[k] = null })
}

function openCreateDialog() {
  resetCreateForm()
  createDialogVisible.value = true
}

async function submitCreate() {
  // —— 校验 ——
  createTimeErrors.title = createForm.title.trim() ? null : 'title 必填'
  createTimeErrors.brief = null  // brief 允许空 (后端也会接受空串)
  createTimeErrors.status = _STATUS_VALUES.includes(createForm.status) ? null : 'status 非法'
  createTimeErrors.next_occurrence = validateIsoDatetime(createForm.next_occurrence)
  createTimeErrors.deadline = validateIsoDatetime(createForm.deadline)
  createTimeErrors.start_time = validateIsoDatetime(createForm.start_time)
  if (createTimeErrors.title || createTimeErrors.next_occurrence || createTimeErrors.deadline || createTimeErrors.start_time || createTimeErrors.status) {
    ElMessage.warning('请修正表单中的错误')
    return
  }

  creating.value = true
  try {
    const body: Record<string, unknown> = {
      title: createForm.title.trim(),
      status: createForm.status,
      brief: createForm.brief,
      conclusion: createForm.conclusion,
      stage: createForm.stage,
      notes: createForm.notes,
    }
    if (createForm.next_occurrence.trim()) body.next_occurrence = createForm.next_occurrence.trim()
    if (createForm.deadline.trim()) body.deadline = createForm.deadline.trim()
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
    // 本地插入到 list 头部 (如果当前筛选匹配)
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
    // 如果行正在展开, 把展开记录也清掉
    expandedRows.value = expandedRows.value.filter(id => id !== ev.task_id)
    delete editForms.value[ev.task_id]
    ElMessage.success(`已删除「${ev.title || ev.task_id}」`)
  } catch (e: any) {
    ElMessage.error(`删除失败: ${e?.message || e}`)
  }
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :width="'90vw'"
    :close-on-click-modal="false"
    align-center
    :show-close="false"
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
            placeholder="搜索标题 / 简要 / 结论 / 阶段 / 笔记…"
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
                    <span v-if="ev.brief" class="evlist-summary-hint">
                      {{ ev.brief.slice(0, 80) }}{{ ev.brief.length > 80 ? '…' : '' }}
                    </span>
                  </div>
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
                <td colspan="6">
                  <div class="evlist-detail" v-if="editForms[ev.task_id]" @click.stop>
                    <!-- 任务概览：简要 / 已获结论 / 当前阶段 / 任务笔记（结构化只读视图） -->
                    <div class="evlist-notes">
                      <div class="evlist-notes-head">
                        <span class="evlist-notes-title">📋 任务概览</span>
                        <span class="evlist-notes-sub">简要 · 已获结论 · 当前阶段 · 任务笔记</span>
                      </div>
                      <div class="evlist-notes-body">
                        <div class="evlist-notes-field">
                          <span class="evlist-notes-field-tag">简要</span>
                          <span class="evlist-notes-field-text">{{ editForms[ev.task_id]!.brief || '（无）' }}</span>
                        </div>
                        <div class="evlist-notes-field">
                          <span class="evlist-notes-field-tag">已获结论</span>
                          <span class="evlist-notes-field-text">{{ editForms[ev.task_id]!.conclusion || '（无）' }}</span>
                        </div>
                        <div v-if="editForms[ev.task_id]!.stage" class="evlist-notes-stage">
                          <span class="evlist-notes-stage-tag">当前阶段</span>
                          <span class="evlist-notes-stage-text">{{ editForms[ev.task_id]!.stage }}</span>
                        </div>
                        <template v-if="parsedNotesByTask[ev.task_id]!.lines.length">
                          <div
                            v-for="(n, i) in parsedNotesByTask[ev.task_id]!.lines"
                            :key="i"
                            class="evlist-note-line"
                            :class="{ plain: !n.isBullet }"
                            :style="{ paddingLeft: (n.indent * 18 + 12) + 'px' }"
                          >
                            <span v-if="n.isBullet" class="evlist-note-dot" />
                            <span class="evlist-note-text">{{ n.text }}</span>
                          </div>
                        </template>
                        <div v-else class="evlist-notes-empty">（任务笔记为空）</div>
                      </div>
                    </div>

                    <el-form label-position="top" size="default" class="evlist-edit-form">
                      <!-- 左: title + brief/conclusion/stage/notes -->
                      <div class="evlist-edit-left">
                        <el-form-item label="title / 标题" required>
                          <el-input
                            v-model="editForms[ev.task_id]!.title"
                            placeholder="任务标题"
                          />
                        </el-form-item>
                        <el-form-item label="brief / 简要总结 (≤50 字; 说明任务前因与目的)">
                          <el-input
                            v-model="editForms[ev.task_id]!.brief"
                            type="textarea"
                            :rows="2"
                            placeholder="一句话说明这条任务从哪来、要达成什么"
                          />
                        </el-form-item>
                        <el-form-item label="conclusion / 已获结论 (≤100 字; 可直接用于汇报)">
                          <el-input
                            v-model="editForms[ev.task_id]!.conclusion"
                            type="textarea"
                            :rows="3"
                            placeholder="当前已获取、可直接用于汇报的结论"
                          />
                        </el-form-item>
                        <el-form-item label="stage / 当前阶段 (还需要做什么)">
                          <el-input
                            v-model="editForms[ev.task_id]!.stage"
                            placeholder="一句话刚性目标"
                          />
                        </el-form-item>
                        <el-form-item label="notes / 任务笔记 (每行一条; 推进时由 quest 整列清理重填)">
                          <el-input
                            v-model="editForms[ev.task_id]!.notes"
                            type="textarea"
                            :rows="8"
                            placeholder="低权限记录通道; 每行一条笔记, 可用「- 」作 bullet"
                          />
                        </el-form-item>
                        <el-form-item label="task_id (只读)">
                          <el-input :model-value="ev.task_id" readonly disabled />
                        </el-form-item>
                      </div>

                      <!-- 右: 状态 + 时间小条目 -->
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
                          label="next_occurrence (下次处理时间; 可空)"
                          :error="editTimeErrors[`${ev.task_id}.next_occurrence`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.task_id]!.next_occurrence"
                            placeholder="2026-01-01T12:00:00"
                          />
                        </el-form-item>

                        <el-form-item
                          label="deadline (最终汇报时间; 可空)"
                          :error="editTimeErrors[`${ev.task_id}.deadline`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.task_id]!.deadline"
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

                        <el-form-item
                          label="updated_at (留空 = 自动刷为 now; 手动填 = 锁定为该值)"
                          :error="editTimeErrors[`${ev.task_id}.updated_at`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.task_id]!.updated_at"
                            placeholder="留空由系统自动刷; 或手动指定 ISO datetime"
                          />
                        </el-form-item>

                        <el-form-item label="end_time (只读 · status=completed 自动设)">
                          <el-input :model-value="timeShort(editForms[ev.task_id]!.end_time) || '—'" readonly disabled />
                        </el-form-item>

                        <div class="evlist-readonly-extra">
                          <span><strong>end_time</strong>{{ timeShort(ev.end_time) }}</span>
                        </div>
                      </div>
                    </el-form>

                    <div class="evlist-edit-actions">
                      <span class="evlist-edit-hint">
                        💡 <code>title</code> 改名走原生 update; <code>brief</code> / <code>conclusion</code> / <code>stage</code> / <code>notes</code> 直接覆盖对应列（任务不落向量, 无需重生成）; 改 <code>status=completed</code> 自动设 <code>end_time</code>; 改 <code>next_occurrence</code> / <code>deadline</code> / <code>start_time</code> / <code>updated_at</code> 会做 ISO 格式校验, 不合法不会提交。
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
    <el-dialog
      v-model="createDialogVisible"
      width="640px"
      :close-on-click-modal="false"
      align-center
      :show-close="false"
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
        <el-form-item label="title / 标题" required :error="createTimeErrors.title ?? undefined">
          <el-input v-model="createForm.title" placeholder="任务标题" maxlength="120" show-word-limit />
        </el-form-item>

        <el-form-item label="status" :error="createTimeErrors.status ?? undefined">
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

        <el-form-item label="brief / 简要总结 (≤50 字; 说明任务前因与目的)">
          <el-input
            v-model="createForm.brief"
            type="textarea"
            :rows="2"
            placeholder="一句话说明这条任务从哪来、要达成什么 (可空)"
          />
        </el-form-item>

        <el-form-item label="conclusion / 已获结论 (≤100 字; 可直接用于汇报)">
          <el-input
            v-model="createForm.conclusion"
            type="textarea"
            :rows="2"
            placeholder="当前已获取、可直接用于汇报的结论 (可空)"
          />
        </el-form-item>

        <el-form-item label="stage / 当前阶段 (还需要做什么)">
          <el-input v-model="createForm.stage" placeholder="一句话刚性目标 (可空)" />
        </el-form-item>

        <el-form-item label="notes / 任务笔记 (每行一条; 可空)">
          <el-input
            v-model="createForm.notes"
            type="textarea"
            :rows="4"
            placeholder="低权限记录通道; 每行一条笔记, 可用「- 」作 bullet (可空)"
          />
        </el-form-item>

        <div class="evlist-create-grid">
          <el-form-item
            label="next_occurrence (下次处理时间; 可空)"
            :error="createTimeErrors.next_occurrence ?? undefined"
          >
            <el-input v-model="createForm.next_occurrence" placeholder="2026-01-01T12:00:00" />
          </el-form-item>

          <el-form-item
            label="deadline (最终汇报时间; 可空)"
            :error="createTimeErrors.deadline ?? undefined"
          >
            <el-input v-model="createForm.deadline" placeholder="2026-01-01T12:00:00" />
          </el-form-item>

          <el-form-item
            label="start_time (留空 = now; 手动填 = 历史回填)"
            :error="createTimeErrors.start_time ?? undefined"
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
    </el-dialog>
  </el-dialog>
</template>

<style scoped>
.evlist-dialog :deep(.el-dialog__header) { padding: 14px 20px !important; }
.evlist-dialog :deep(.el-dialog__body) { padding: 0 !important; }

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

/* —— 主体 (跟角色图一致: 80vh 高度, 浅灰底, 卡片面板) —— */
.evlist-body {
  display: flex;
  flex-direction: column;
  height: 80vh;
  min-height: 600px;
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

/* —— 表格区 (与 body 同色, 不再单独卡一层) —— */
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
  background: rgba(239, 68, 68, 0.08);
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
.evlist-create-dialog :deep(.el-dialog__header) { padding: 14px 20px !important; }
.evlist-create-dialog :deep(.el-dialog__body) { padding: 0 20px 12px !important; }
.evlist-create-dialog :deep(.el-dialog__footer) { padding: 12px 20px 16px !important; border-top: 1px solid var(--border-1); }

.evlist-detail-row { background: var(--bg-subtle); }
.evlist-detail-row td { padding: 0; border-bottom: 1px solid var(--border-1); cursor: default; }
.evlist-detail {
  padding: 18px 20px 18px 56px;
  font-size: 12.5px;
  background: var(--bg-subtle);
}

/* —— 任务笔记（结构化只读视图） —— */
.evlist-notes {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  margin-bottom: 16px;
}
.evlist-notes-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}
.evlist-notes-title {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--text-1);
}
.evlist-notes-sub {
  font-size: 10.5px;
  color: var(--text-3);
}
.evlist-notes-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.evlist-notes-field {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 6px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-1);
  border-radius: 6px;
}
.evlist-notes-field-tag {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  color: var(--text-2);
  background: var(--bg-surface);
  padding: 1px 7px;
  border-radius: 999px;
  letter-spacing: 0.03em;
  margin-top: 1px;
}
.evlist-notes-field-text {
  font-size: 12.5px;
  color: var(--text-1);
  line-height: 1.5;
  word-break: break-word;
}
.evlist-notes-stage {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 6px;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
  border-radius: 6px;
}
.evlist-notes-stage-tag {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 1px 7px;
  border-radius: 999px;
  letter-spacing: 0.03em;
}
.evlist-notes-stage-text {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.5;
}
.evlist-note-line {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  line-height: 1.55;
  font-size: 12.5px;
}
.evlist-note-line.plain {
  color: var(--text-2);
}
.evlist-note-dot {
  flex-shrink: 0;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  margin-top: 6px;
}
.evlist-note-text {
  color: var(--text-1);
  word-break: break-word;
}
.evlist-notes-empty {
  font-size: 11.5px;
  color: var(--text-3);
  font-style: italic;
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
.evlist-edit-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}
/* 小屏时上下堆叠 */
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
.evlist-readonly-extra {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--text-3);
}
.evlist-readonly-extra strong {
  font-weight: 600;
  color: var(--text-2);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-right: 6px;
}
/* 表单错误提示 (el-form-item 自带) */
.evlist-edit-right :deep(.el-form-item__error) {
  font-size: 11px;
  padding-top: 2px;
}
</style>
