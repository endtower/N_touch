<script setup lang="ts">
import AppDialog from './AppDialog.vue'
import { ref, watch } from 'vue'
import type { Identity } from '../types'

const props = defineProps<{
  visible: boolean
  editing?: Identity | null
}>()

const emit = defineEmits<{
  'update:visible': [v: boolean]
  'submit': [payload: { user_id: string; user_name: string; switch_to: boolean }]
}>()

const userId = ref('')
const userName = ref('')
const switchTo = ref(true)

watch(
  () => props.visible,
  (v) => {
    if (!v) return
    if (props.editing) {
      userId.value = props.editing.user_id
      userName.value = props.editing.user_name
      switchTo.value = false
    } else {
      userId.value = ''
      userName.value = ''
      switchTo.value = true
    }
  },
)

function close() {
  emit('update:visible', false)
}

function submit() {
  const uid = userId.value.trim()
  const uname = userName.value.trim()
  if (!uid || !uname) return
  emit('submit', { user_id: uid, user_name: uname, switch_to: switchTo.value })
}
</script>

<template>
  <AppDialog
    :model-value="props.visible"
    :title="props.editing ? '改名 / 改 id' : '新建身份'"
    width="440"
    :close-on-click-modal="false"
    align-center
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <div class="form-row">
      <label class="form-label">
        ID <span class="required">*</span>
      </label>
      <el-input
        v-model="userId"
        placeholder="例如 788599177"
        maxlength="32"
        clearable
      />
    </div>

    <div class="form-row">
      <label class="form-label">
        角色 / 显示名 <span class="required">*</span>
      </label>
      <el-input
        v-model="userName"
        placeholder="例如 大湿 / 老默"
        maxlength="32"
        clearable
      />
    </div>

    <div v-if="!props.editing" class="form-row switch-row">
      <label class="form-label"></label>
      <el-checkbox v-model="switchTo">
        <span class="switch-text">创建后立即切到此身份</span>
      </el-checkbox>
    </div>

    <div class="form-hint">
      <div class="hint-row">
        <span class="hint-dot">•</span>
        <span>ID + 角色 联合唯一. 同一 ID 不同名 = 不同条目, 可并存.</span>
      </div>
      <div class="hint-row">
        <span class="hint-dot">•</span>
        <span>想"改名" = 用管理角色的"改名"按钮, 不要新建.</span>
      </div>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button
        type="primary"
        :disabled="!userId.trim() || !userName.trim()"
        @click="submit"
      >
        {{ props.editing ? '保存修改' : '创建' }}
      </el-button>
    </template>
  </AppDialog>
</template>

<style scoped>
.form-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.form-row.switch-row {
  align-items: center;
}

.form-label {
  flex: 0 0 100px;
  text-align: right;
  font-size: 13px;
  color: var(--text-2);
  font-weight: 500;
}
.required {
  color: var(--danger);
  margin-left: 2px;
}
.form-row :deep(.el-input) {
  flex: 1;
}

.switch-text {
  color: var(--text-2);
  font-size: 13px;
}

.form-hint {
  margin-top: 6px;
  padding: 10px 12px;
  background: var(--bg-subtle);
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.6;
}
.hint-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.hint-dot {
  color: var(--accent);
  font-weight: bold;
}
</style>