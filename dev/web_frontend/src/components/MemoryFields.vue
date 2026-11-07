<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { EditPen } from '@element-plus/icons-vue'

interface Field { key: string; label: string; value: string | string[] | boolean; multiline?: boolean; tags?: boolean; boolean?: boolean; options?: { value: string; label: string }[] }
const props = defineProps<{ recordId: string; rows: Field[]; save: (key: string, value: string | string[] | boolean) => Promise<void>; autofocusKey?: string }>()
const editing = ref(''), text = ref(''), tags = ref<string[]>([]), saving = ref(false), error = ref('')
let inputControl: { focus: () => void } | null = null
function setInputControl(element: any) { inputControl = element }
watch(() => props.recordId, () => {
  editing.value = ''; error.value = ''
  const first = props.rows.find(row => row.key === props.autofocusKey)
  if (first) begin(first)
}, { immediate: true })
function begin(row: Field) {
  if (saving.value || row.boolean) return
  editing.value = row.key; error.value = ''
  text.value = typeof row.value === 'string' ? row.value : ''
  tags.value = Array.isArray(row.value) ? [...row.value] : []
  nextTick(() => inputControl?.focus())
}
function cancel() { if (!saving.value) { editing.value = ''; error.value = '' } }
function isEmpty(value: string | string[] | boolean) {
  return Array.isArray(value) ? value.length === 0 : !value
}
async function commit(row: Field) {
  if (saving.value) return
  const recordId = props.recordId
  saving.value = true; error.value = ''
  try {
    await props.save(row.key, row.tags ? tags.value.map(value => value.trim()).filter(Boolean) : text.value.trim())
    if (recordId === props.recordId) editing.value = ''
  } catch (err: any) { if (recordId === props.recordId) error.value = err.message || String(err) }
  finally { saving.value = false }
}
// 勾选类字段（如「允许 conversation」）不走内联编辑态，点击即刻保存。
async function toggleBoolean(row: Field) {
  if (saving.value) return
  const recordId = props.recordId
  saving.value = true; error.value = ''
  try { await props.save(row.key, !row.value) }
  catch (err: any) { if (recordId === props.recordId) error.value = err.message || String(err) }
  finally { saving.value = false }
}
</script>

<template>
  <template v-for="row in rows" :key="row.key">
    <dt>{{ row.label }}</dt>
    <dd :class="{ 'kv-multiline': row.multiline }">
      <template v-if="row.boolean">
        <div class="kv-boolean">
          <el-switch :model-value="!!row.value" :loading="saving" :aria-label="row.label" :disabled="saving" @change="toggleBoolean(row)" />
          <span class="kv-boolean-state">{{ row.value ? '已允许' : '未允许' }}</span>
        </div>
        <div v-if="error" class="kv-edit-error" role="alert">{{ error }}</div>
      </template>
      <template v-else-if="editing === row.key">
        <el-select v-if="row.options" :ref="setInputControl" v-model="text" size="small" :aria-label="row.label" :disabled="saving"><el-option v-for="option in row.options" :key="option.value" :value="option.value" :label="option.label" /></el-select>
        <el-select v-else-if="row.tags" :ref="setInputControl" v-model="tags" multiple filterable allow-create default-first-option :reserve-keyword="false" size="small" :aria-label="row.label" :disabled="saving" placeholder="输入别名后回车" style="width: 100%;" />
        <el-input v-else :ref="setInputControl" v-model="text" size="small" :aria-label="row.label" :type="row.multiline ? 'textarea' : 'text'" :autosize="row.multiline ? { minRows: 2, maxRows: 8 } : false" :disabled="saving" @keyup.enter="!row.multiline && commit(row)" @keyup.esc="cancel" />
        <div class="kv-edit-actions">
          <el-button size="small" type="primary" :loading="saving" @click="commit(row)">保存</el-button>
          <el-button size="small" :disabled="saving" @click="cancel">取消</el-button>
        </div>
        <div v-if="error" class="kv-edit-error" role="alert">{{ error }}</div>
      </template>
      <button v-else type="button" class="kv-value" :class="{ empty: isEmpty(row.value) }" :aria-label="`修改${row.label}`" title="点击修改" @click="begin(row)">
        {{ (Array.isArray(row.value) ? row.value.join('、') : row.value) || '—' }}
        <el-icon class="kv-edit-icon"><EditPen /></el-icon>
      </button>
    </dd>
  </template>
</template>

<style scoped>
dt { font-size: 11.5px; color: var(--text-3); font-weight: 500; align-self: start; }
dd { margin: 0; font-size: 12.5px; color: var(--text-1); overflow-wrap: anywhere; font-variant-numeric: tabular-nums; min-width: 0; }
.kv-multiline { white-space: pre-wrap; line-height: 1.6; }
.kv-boolean { display: flex; align-items: center; gap: 8px; }
.kv-boolean-state { font-size: 12px; color: var(--text-3); }
.kv-value { display: inline-flex; align-items: baseline; gap: 5px; cursor: pointer; border: 0; border-radius: 5px; padding: 1px 5px; margin-left: -5px; min-height: 20px; background: transparent; color: inherit; font: inherit; text-align: left; white-space: inherit; overflow-wrap: anywhere; max-width: 100%; }
.kv-value:hover { background: var(--bg-subtle); }
.kv-value.empty { color: var(--text-3); font-style: italic; }
.kv-edit-icon { opacity: 0; color: var(--text-3); flex-shrink: 0; }
.kv-value:hover .kv-edit-icon, .kv-value:focus-visible .kv-edit-icon { opacity: 1; }
.kv-edit-actions { display: flex; gap: 8px; margin-top: 6px; }
.kv-edit-error { background: color-mix(in srgb, var(--tag-danger) 10%, transparent); border: 1px solid color-mix(in srgb, var(--tag-danger) 30%, transparent); color: var(--tag-danger); font-size: 11.5px; padding: 5px 9px; border-radius: 6px; margin-top: 6px; overflow-wrap: anywhere; }
</style>
