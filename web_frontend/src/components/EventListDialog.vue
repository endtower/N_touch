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
import type { LongEvent } from '../types'
import { chat } from '../stores/chat'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean] }>()

type Status = 'ongoing' | 'completed' | 'failed' | 'aborted' | 'planned' | 'all'
interface StatusOpt {
  value: Status
  label: string
  tag: 'primary' | 'success' | 'danger' | 'info' | 'warning'
  color: string   // cytoscape 风格 swatch
}
const STATUS_OPTIONS: StatusOpt[] = [
  { value: 'ongoing',   label: '正在发生 (ongoing)',  tag: 'primary', color: '#818cf8' },
  { value: 'completed', label: '已完成 (completed)',  tag: 'success', color: '#34d399' },
  { value: 'failed',    label: '失败 (failed)',       tag: 'danger',  color: '#f87171' },
  { value: 'aborted',   label: '中止 (aborted)',      tag: 'info',    color: '#a3a3a3' },
  { value: 'planned',   label: '计划中 (planned)',    tag: 'warning', color: '#fbbf24' },
  { value: 'all',       label: '全部',                tag: 'info',    color: '#94a3b8' },
]

// 给前端校验用: 排除 'all' 的 status 列表 (用数组 + .includes 比 Set 跟 Vue reactive 协同更稳)
const _STATUS_VALUES: readonly Status[] = STATUS_OPTIONS
  .filter(o => o.value !== 'all')
  .map(o => o.value)

const status = ref<Status>(chat.state.longEventsStatus ?? 'ongoing')
const expandedRows = ref<string[]>([])
const searchKeyword = ref('')
const refreshTimer = ref<number | null>(null)

// 每行独立一份编辑表单 (避免状态共享导致的脏写)
interface EditForm {
  event_id: string
  title: string
  summary: string
  event_type: Status
  next_occurrence: string
  start_time: string       // ISO datetime; 允许编辑 (例如回填历史事件)
  updated_at: string       // ISO datetime; 允许编辑 (用户手动覆写)
  end_time: string         // 只读展示, 不提交
}
const editForms = ref<Record<string, EditForm>>({})
const editSaving = ref<Record<string, boolean>>({})
// 时间字段的本地校验错误 (key = `${eventId}.${field}`)
const editTimeErrors = ref<Record<string, string | null>>({})

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

const events = computed<LongEvent[]>(() => chat.state.longEvents?.events ?? [])
const total = computed(() => chat.state.longEvents?.total ?? 0)
const loading = computed(() => chat.state.longEventsLoading)
const error = computed(() => chat.state.longEventsError)

const visibleEvents = computed<LongEvent[]>(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return events.value
  return events.value.filter(e =>
    (e.title || '').toLowerCase().includes(kw) ||
    (e.summary || '').toLowerCase().includes(kw) ||
    (e.event_type || '').toLowerCase().includes(kw),
  )
})

const statusCounts = computed(() => {
  // 顶部 chip 显示各 status 的数量, 但需要单独拉一下 "all" 才知道总数
  // 这里只在当前数据集上统计, 数字可能不全, 仅作参考
  const m: Record<string, number> = {}
  for (const e of events.value) {
    m[e.event_type] = (m[e.event_type] || 0) + 1
  }
  return m
})

watch(
  () => props.visible,
  async (v) => {
    if (v) {
      status.value = chat.state.longEventsStatus ?? 'ongoing'
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
  await chat.loadLongEvents(status.value, 500, 0)
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

async function changeStatus(ev: LongEvent, newStatus: string) {
  if (ev.event_type === newStatus) return
  try {
    await ElMessageBox.confirm(
      `把「${ev.title}」从 ${ev.event_type} 改为 ${newStatus} ?${newStatus === 'completed' ? ' (会自动设置 end_time)' : ''}`,
      '切换事件状态',
      { type: 'info', confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await fetch('/events/set_type', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_id: ev.event_id, event_type: newStatus }),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    const body = await res.json()
    const updated = body.event as LongEvent
    if (status.value !== 'all' && updated.event_type !== status.value) {
      if (chat.state.longEvents) {
        chat.state.longEvents.events = chat.state.longEvents.events.filter(e => e.event_id !== updated.event_id)
        chat.state.longEvents.total = Math.max(0, chat.state.longEvents.total - 1)
      }
      ElMessage.success(`已改为 ${newStatus}, 不再匹配当前筛选`)
    } else {
      chat.patchLongEvent(updated)
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

function toggleExpand(eventId: string) {
  if (expandedRows.value.includes(eventId)) {
    expandedRows.value = expandedRows.value.filter(id => id !== eventId)
  } else {
    expandedRows.value = [...expandedRows.value, eventId]
    // 第一次展开时, 拉一份快照作为可编辑表单
    if (!editForms.value[eventId]) {
      const ev = events.value.find(e => e.event_id === eventId)
      if (ev) {
        editForms.value[eventId] = {
          event_id: ev.event_id,
          title: ev.title ?? '',
          summary: ev.summary ?? '',
          event_type: (ev.event_type as Status) ?? 'ongoing',
          next_occurrence: ev.next_occurrence ?? '',
          start_time: ev.start_time ?? '',
          updated_at: ev.updated_at ?? '',
          end_time: ev.end_time ?? '',
        }
      }
    }
  }
}

function discardEdit(eventId: string) {
  delete editForms.value[eventId]
}

async function saveEdit(eventId: string) {
  const form = editForms.value[eventId]
  if (!form) return
  if (!form.title.trim()) {
    ElMessage.warning('title 必填')
    return
  }

  // —— 时间字段本地校验 ——
  const timeFields: Array<keyof EditForm> = ['next_occurrence', 'start_time', 'updated_at']
  for (const f of timeFields) {
    const err = validateIsoDatetime(form[f] as string)
    editTimeErrors.value[`${eventId}.${f}`] = err
    if (err) {
      ElMessage.error(`${f}: ${err}`)
      return
    }
  }

  editSaving.value[eventId] = true
  try {
    const body: Record<string, unknown> = {
      title: form.title.trim(),
      event_type: form.event_type,
      summary: form.summary,
    }
    // 时间字段: 空字符串传 null 让后端清空 (updated_at 仍允许清空, 但通常不应清空)
    body.next_occurrence = form.next_occurrence.trim() ? form.next_occurrence.trim() : null
    body.start_time = form.start_time.trim() ? form.start_time.trim() : null
    body.updated_at = form.updated_at.trim() ? form.updated_at.trim() : null

    const res = await fetch(`/events/${encodeURIComponent(eventId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    const r = await res.json()
    const updated = r.event as LongEvent
    chat.patchLongEvent(updated)
    // 如果新 status 不在当前筛选里, 把这一行移出当前 list (跟 set_type 一致)
    if (status.value !== 'all' && updated.event_type !== status.value) {
      if (chat.state.longEvents) {
        chat.state.longEvents.events = chat.state.longEvents.events.filter(e => e.event_id !== updated.event_id)
        chat.state.longEvents.total = Math.max(0, chat.state.longEvents.total - 1)
      }
      expandedRows.value = expandedRows.value.filter(id => id !== eventId)
      delete editForms.value[eventId]
    } else {
      // 同步本地 edit form
      editForms.value[eventId] = {
        event_id: updated.event_id,
        title: updated.title ?? '',
        summary: updated.summary ?? '',
        event_type: (updated.event_type as Status) ?? 'ongoing',
        next_occurrence: updated.next_occurrence ?? '',
        start_time: updated.start_time ?? '',
        updated_at: updated.updated_at ?? '',
        end_time: updated.end_time ?? '',
      }
    }
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(`保存失败: ${e?.message || e}`)
  } finally {
    editSaving.value[eventId] = false
  }
}

// ============================================================
// 创建 / 删除
// ============================================================

// 创建表单 (单实例, 不需要按 event_id 分)
interface CreateForm {
  title: string
  summary: string
  event_type: Status
  next_occurrence: string
  start_time: string          // 可空: 留空 = 用 now (后端默认); 填了 = 历史回填
}
const createDialogVisible = ref(false)
const creating = ref(false)
const createForm = reactive<CreateForm>({
  title: '',
  summary: '',
  event_type: 'ongoing',
  next_occurrence: '',
  start_time: '',
})
const createTimeErrors = reactive<Record<keyof CreateForm, string | null>>({
  title: null,
  summary: null,
  event_type: null,
  next_occurrence: null,
  start_time: null,
})

function resetCreateForm() {
  createForm.title = ''
  createForm.summary = ''
  createForm.event_type = 'ongoing'
  createForm.next_occurrence = ''
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
  createTimeErrors.summary = null  // summary 允许空 (后端也会接受空串)
  createTimeErrors.event_type = _STATUS_VALUES.includes(createForm.event_type) ? null : 'event_type 非法'
  createTimeErrors.next_occurrence = validateIsoDatetime(createForm.next_occurrence)
  createTimeErrors.start_time = validateIsoDatetime(createForm.start_time)
  if (createTimeErrors.title || createTimeErrors.next_occurrence || createTimeErrors.start_time || createTimeErrors.event_type) {
    ElMessage.warning('请修正表单中的错误')
    return
  }

  creating.value = true
  try {
    const body: Record<string, unknown> = {
      title: createForm.title.trim(),
      event_type: createForm.event_type,
      summary: createForm.summary,
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
    const created = r.event as LongEvent
    // 本地插入到 list 头部 (如果当前筛选匹配)
    if (status.value !== 'all' && created.event_type !== status.value) {
      ElMessage.success(`已创建「${created.title}」, 不在当前筛选中, 请切换 status 查看`)
    } else {
      chat.addLongEvent(created)
      ElMessage.success(`已创建「${created.title}」`)
    }
    createDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error(`创建失败: ${e?.message || e}`)
  } finally {
    creating.value = false
  }
}

async function deleteEvent(ev: LongEvent) {
  try {
    await ElMessageBox.confirm(
      `确认删除事件「${ev.title || ev.event_id}」? 此操作不可撤销 (节点和向量都会被清除)。`,
      '删除长期事件',
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
    const res = await fetch(`/events/${encodeURIComponent(ev.event_id)}`, {
      method: 'DELETE',
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
    }
    chat.removeLongEvent(ev.event_id)
    // 如果行正在展开, 把展开记录也清掉
    expandedRows.value = expandedRows.value.filter(id => id !== ev.event_id)
    delete editForms.value[ev.event_id]
    ElMessage.success(`已删除「${ev.title || ev.event_id}」`)
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
          <span>长期事件</span>
          <span v-if="chat.state.longEvents" class="evlist-counts">
            <strong>{{ total }}</strong> 条 · {{ STATUS_OPTIONS.find(o => o.value === status)?.label }}
          </span>
        </div>
        <div class="evlist-toolbar">
          <el-input
            v-model="searchKeyword"
            size="small"
            placeholder="搜索标题 / summary…"
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
            <div style="font-weight: 600; margin-bottom: 4px;">事件加载失败</div>
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
            {{ events.length === 0 ? '该状态没有事件' : '没有匹配的事件' }}
          </div>
          <div style="font-size: 12px;">
            {{ events.length === 0 ? '换一个 status 试试, 或选 "全部" 看看所有事件' : '清空搜索框看看' }}
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
            <template v-for="ev in visibleEvents" :key="ev.event_id">
              <tr
                :class="{ expanded: expandedRows.includes(ev.event_id) }"
                @click="toggleExpand(ev.event_id)"
              >
                <td class="caret-cell">
                  <el-icon :size="12" color="var(--text-3)">
                    <ArrowRight v-if="!expandedRows.includes(ev.event_id)" style="transform: rotate(0deg); transition: transform 0.15s ease;" />
                    <ArrowRight v-else style="transform: rotate(90deg); transition: transform 0.15s ease;" />
                  </el-icon>
                </td>
                <td>
                  <el-tag :type="statusTag(ev.event_type)" size="small">{{ ev.event_type }}</el-tag>
                </td>
                <td>
                  <div class="evlist-title-cell">
                    <span class="evlist-title-text">{{ ev.title || '—' }}</span>
                    <span v-if="ev.summary" class="evlist-summary-hint">
                      {{ ev.summary.slice(0, 80) }}{{ ev.summary.length > 80 ? '…' : '' }}
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
                    :model-value="ev.event_type"
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
                    title="删除此事件 (不可撤销)"
                    @click="deleteEvent(ev)"
                  />
                </td>
              </tr>
              <tr v-if="expandedRows.includes(ev.event_id)" class="evlist-detail-row">
                <td colspan="6">
                  <div class="evlist-detail" v-if="editForms[ev.event_id]" @click.stop>
                    <el-form label-position="top" size="default" class="evlist-edit-form">
                      <!-- 左: title + summary -->
                      <div class="evlist-edit-left">
                        <el-form-item label="title" required>
                          <el-input
                            v-model="editForms[ev.event_id]!.title"
                            placeholder="事件标题"
                          />
                        </el-form-item>
                        <el-form-item label="summary (LLM 读这个做决策; 改完会自动重生成向量)">
                          <el-input
                            v-model="editForms[ev.event_id]!.summary"
                            type="textarea"
                            :rows="8"
                            placeholder="事件概述"
                          />
                        </el-form-item>
                        <el-form-item label="event_id (只读)">
                          <el-input :model-value="ev.event_id" readonly disabled />
                        </el-form-item>
                      </div>

                      <!-- 右: 状态 + 时间小条目 -->
                      <div class="evlist-edit-right">
                        <el-form-item label="event_type">
                          <el-select
                            v-model="editForms[ev.event_id]!.event_type"
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
                          label="next_occurrence (下次发生; 可空)"
                          :error="editTimeErrors[`${ev.event_id}.next_occurrence`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.event_id]!.next_occurrence"
                            placeholder="2026-01-01T12:00:00"
                          />
                        </el-form-item>

                        <el-form-item
                          label="start_time (开始时间)"
                          :error="editTimeErrors[`${ev.event_id}.start_time`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.event_id]!.start_time"
                            placeholder="2026-01-01T12:00:00"
                          />
                        </el-form-item>

                        <el-form-item
                          label="updated_at (留空 = 自动刷为 now; 手动填 = 锁定为该值)"
                          :error="editTimeErrors[`${ev.event_id}.updated_at`] ?? undefined"
                        >
                          <el-input
                            v-model="editForms[ev.event_id]!.updated_at"
                            placeholder="留空由系统自动刷; 或手动指定 ISO datetime"
                          />
                        </el-form-item>

                        <el-form-item label="end_time (只读 · status=completed 自动设)">
                          <el-input :model-value="timeShort(editForms[ev.event_id]!.end_time) || '—'" readonly disabled />
                        </el-form-item>

                        <div class="evlist-readonly-extra">
                          <span><strong>is_vectorized</strong>{{ ev.is_vectorized ?? 0 }}</span>
                          <span><strong>end_time</strong>{{ timeShort(ev.end_time) }}</span>
                        </div>
                      </div>
                    </el-form>

                    <div class="evlist-edit-actions">
                      <span class="evlist-edit-hint">
                        💡 <code>title</code> 改名走原生 update; 改 <code>summary</code> 会重生成 <code>summary_vector</code>; 改 <code>event_type=completed</code> 自动设 <code>end_time</code>; 改 <code>start_time</code> / <code>updated_at</code> 会做 ISO 格式校验, 不合法不会提交。
                      </span>
                      <div style="display: flex; gap: 8px;">
                        <el-button @click="discardEdit(ev.event_id)">还原</el-button>
                        <el-button
                          type="primary"
                          :loading="editSaving[ev.event_id]"
                          @click="saveEdit(ev.event_id)"
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

    <!-- ============== 创建事件弹窗 ============== -->
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
            <span>新建长期事件</span>
            <span class="evlist-counts">手动创建节点, 会写入记忆库并生成 summary_vector</span>
          </div>
        </div>
      </template>

      <el-form label-position="top" size="default" class="evlist-create-form">
        <el-form-item label="title" required :error="createTimeErrors.title ?? undefined">
          <el-input v-model="createForm.title" placeholder="事件标题" maxlength="120" show-word-limit />
        </el-form-item>

        <el-form-item label="event_type" :error="createTimeErrors.event_type ?? undefined">
          <el-select v-model="createForm.event_type" style="width: 100%;">
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

        <el-form-item label="summary (LLM 读这个做决策)">
          <el-input
            v-model="createForm.summary"
            type="textarea"
            :rows="5"
            placeholder="事件概述 (可空)"
          />
        </el-form-item>

        <div class="evlist-create-grid">
          <el-form-item
            label="next_occurrence (下次发生; 可空)"
            :error="createTimeErrors.next_occurrence ?? undefined"
          >
            <el-input v-model="createForm.next_occurrence" placeholder="2026-01-01T12:00:00" />
          </el-form-item>

          <el-form-item
            label="start_time (留空 = now; 手动填 = 历史回填)"
            :error="createTimeErrors.start_time ?? undefined"
          >
            <el-input v-model="createForm.start_time" placeholder="2026-01-01T12:00:00" />
          </el-form-item>
        </div>

        <div class="evlist-edit-hint" style="margin-top: 4px;">
          💡 创建时会自动生成 <code>event_id</code> / <code>start_time</code> (now) / <code>updated_at</code> (now),
          并尝试生成 <code>summary_vector</code>; 若 summary 为空, 向量生成会被跳过。
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
