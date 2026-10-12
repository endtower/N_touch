<script setup lang="ts">
import { ref, watch, computed } from 'vue'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{
  'update:visible': [v: boolean]
  'create': [payload: {
    scope: 'private' | 'group'
    group_id?: string        // 群聊 id (私聊没有)
    user_id: string          // 我的 id
    user_name: string        // 我的名字
    session_name?: string    // 会话名 (私聊 = user_name, 群聊必填)
  }]
}>()

const scope = ref<'private' | 'group'>('private')

// 私聊字段
const privateId = ref('')
const privateName = ref('')

// 群聊字段
const groupId = ref('')
const groupName = ref('')           // 会话名 (必填)
const myIdInGroup = ref('10000') // 默认预填
const myNameInGroup = ref('主人')     // 默认预填

const isPrivate = computed(() => scope.value === 'private')

watch(() => props.visible, (v) => {
  if (v) {
    scope.value = 'private'
    privateId.value = ''
    privateName.value = ''
    groupId.value = ''
    groupName.value = ''
    myIdInGroup.value = '10000'
    myNameInGroup.value = '主人'
  }
})

function close() {
  emit('update:visible', false)
}

const canSubmit = computed(() => {
  if (isPrivate.value) {
    return !!privateId.value.trim() && !!privateName.value.trim()
  } else {
    return !!groupId.value.trim() && !!groupName.value.trim()
        && !!myIdInGroup.value.trim() && !!myNameInGroup.value.trim()
  }
})

function onConfirm() {
  if (!canSubmit.value) return
  if (isPrivate.value) {
    // 私聊: 会话名 = 我的名字, 没有 group_id
    emit('create', {
      scope: 'private',
      user_id: privateId.value.trim(),
      user_name: privateName.value.trim(),
      session_name: privateName.value.trim(),
    })
  } else {
    // 群聊: 有 group_id, session_name 必填, 不带 role
    emit('create', {
      scope: 'group',
      group_id: groupId.value.trim(),
      user_id: myIdInGroup.value.trim(),
      user_name: myNameInGroup.value.trim(),
      session_name: groupName.value.trim(),
    })
  }
  close()
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    @update:model-value="emit('update:visible', $event)"
    title="新建会话"
    width="480px"
    :close-on-click-modal="false"
  >
    <el-form label-width="92px" size="default">
      <el-form-item label="类型">
        <el-radio-group v-model="scope">
          <el-radio-button value="private">私聊</el-radio-button>
          <el-radio-button value="group">群聊</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <!-- ========== 私聊表单 ========== -->
      <template v-if="isPrivate">
        <el-form-item label="我的 ID">
          <el-input
            v-model="privateId"
            placeholder="比如 10000"
            clearable
            maxlength="32"
          />
          <div class="form-hint">私聊里只有你一个人, ID 同时作为会话 ID</div>
        </el-form-item>

        <el-form-item label="我的名字">
          <el-input
            v-model="privateName"
            placeholder="比如 主人"
            clearable
            maxlength="32"
          />
          <div class="form-hint">显示名, 同时作为会话显示名</div>
        </el-form-item>
      </template>

      <!-- ========== 群聊表单 ========== -->
      <template v-else>
        <el-form-item label="会话名">
          <el-input
            v-model="groupName"
            placeholder="比如 测试群 / 勇者的茶会"
            clearable
            maxlength="32"
          />
          <div class="form-hint">群聊的显示名 (必填)</div>
        </el-form-item>

        <el-form-item label="群聊 ID">
          <el-input
            v-model="groupId"
            placeholder="比如 788599177"
            clearable
            maxlength="32"
          />
          <div class="form-hint">QQ 群号, 作为该群聊的唯一 ID</div>
        </el-form-item>

        <el-form-item label="我的 ID">
          <el-input
            v-model="myIdInGroup"
            placeholder="比如 10000"
            clearable
            maxlength="32"
          />
          <div class="form-hint">在群聊里你的 ID (LLM 用来识别你)</div>
        </el-form-item>

        <el-form-item label="我的名字">
          <el-input
            v-model="myNameInGroup"
            placeholder="比如 主人"
            clearable
            maxlength="32"
          />
          <div class="form-hint">在群聊里你的显示名 (默认主人)</div>
        </el-form-item>

        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 4px;">
          <template #title>
            <span style="font-size: 12px;">
              想在群聊里扮演其他角色？创建后点上方「⚙ 管理角色」即可
            </span>
          </template>
        </el-alert>
      </template>
    </el-form>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button
        type="primary"
        :disabled="!canSubmit"
        @click="onConfirm"
      >
        创建
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.form-hint {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
  line-height: 1.4;
}
</style>