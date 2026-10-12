<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

// 表情包管理菜单：读取 LLM 自存表情包（memory/llm_save/stickers/）
// 点击缩略图 → 作为图片加入输入框待发送；编辑/删除/一键向量化走 /stickers HTTP 接口。
// 组件私有状态用本地 ref（弹窗/菜单类型状态，不进入全局 store）。

interface Sticker {
  filename: string
  sticker_title: string
  description: string
  mood: string
  saved_at: string
  url: string
  vectorized: boolean
}

const emit = defineEmits<{
  select: [url: string]
}>()

const popoverVisible = ref(false)
const stickers = ref<Sticker[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const editVisible = ref(false)
const editing = ref<Sticker | null>(null)
const form = ref({ sticker_title: '', description: '', mood: '' })
const saving = ref(false)

const vectorizing = ref(false)

const hasStickers = computed(() => stickers.value.length > 0)
const pendingVectorCount = computed(() => stickers.value.filter(s => !s.vectorized).length)

async function loadStickers() {
  loading.value = true
  error.value = null
  try {
    const res = await fetch('/stickers/list')
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as { stickers: Sticker[] }
    stickers.value = data.stickers ?? []
  } catch (e: any) {
    error.value = e?.message || String(e)
    stickers.value = []
  } finally {
    loading.value = false
  }
}

function onOpen() {
  popoverVisible.value = true
  loadStickers()
}

function onPick(s: Sticker) {
  emit('select', s.url)
  popoverVisible.value = false
}

function openEdit(s: Sticker) {
  editing.value = s
  form.value = {
    sticker_title: s.sticker_title || s.filename,
    description: s.description || '',
    mood: s.mood || '',
  }
  editVisible.value = true
}

async function saveEdit() {
  if (!editing.value) return
  const title = form.value.sticker_title.trim()
  if (!title) {
    ElMessage.warning('标题不能为空')
    return
  }
  saving.value = true
  const filename = encodeURIComponent(editing.value.filename)
  try {
    const res = await fetch(`/stickers/${filename}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sticker_title: form.value.sticker_title,
        description: form.value.description,
        mood: form.value.mood,
      }),
    })
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as { sticker: Sticker }
    const idx = stickers.value.findIndex(s => s.filename === editing.value!.filename)
    if (idx >= 0) stickers.value[idx] = data.sticker
    editVisible.value = false
    ElMessage.success('表情包已更新')
  } catch (e: any) {
    ElMessage.error(`更新失败：${e?.message || e}`)
  } finally {
    saving.value = false
  }
}

// 一键向量化：为全部表情包生成/更新语义向量（后端写入 _vectors.json），完成后刷新状态
async function onVectorize() {
  vectorizing.value = true
  try {
    const res = await fetch('/stickers/vectorize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: false }),
    })
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const data = (await res.json()) as { total: number; embedded: number; reused: number; skipped: number }
    await loadStickers()
    ElMessage.success(`向量化完成：新算 ${data.embedded} 个，复用 ${data.reused} 个`)
  } catch (e: any) {
    ElMessage.error(`向量化失败：${e?.message || e}`)
  } finally {
    vectorizing.value = false
  }
}

async function onDelete(s: Sticker) {
  try {
    await ElMessageBox.confirm(`确定删除表情包「${s.sticker_title || s.filename}」？`, '删除表情包', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  const filename = encodeURIComponent(s.filename)
  try {
    const res = await fetch(`/stickers/${filename}`, { method: 'DELETE' })
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${errBody || res.statusText}`)
    }
    const idx = stickers.value.findIndex(x => x.filename === s.filename)
    if (idx >= 0) stickers.value.splice(idx, 1)
    ElMessage.success('表情包已删除')
  } catch (e: any) {
    ElMessage.error(`删除失败：${e?.message || e}`)
  }
}
</script>

<template>
  <el-popover
    v-model:visible="popoverVisible"
    placement="top-start"
    trigger="click"
    :width="360"
    :show-arrow="false"
  >
    <template #reference>
      <el-button :icon="'Star'" size="small">表情</el-button>
    </template>

    <div class="sticker-menu">
      <div class="sticker-menu-head">
        <span>
          表情包库
          <span v-if="hasStickers" class="sticker-pending" :class="{ 'is-clear': pendingVectorCount === 0 }">
            {{ pendingVectorCount === 0 ? '向量已就绪' : `待向量化 ${pendingVectorCount}` }}
          </span>
        </span>
        <span class="sticker-menu-actions">
          <el-button
            link
            type="primary"
            size="small"
            :loading="vectorizing"
            :disabled="!hasStickers"
            @click="onVectorize()"
          >
            一键向量化
          </el-button>
          <el-button link type="primary" size="small" :loading="loading" @click="loadStickers()">
            刷新
          </el-button>
        </span>
      </div>

      <div v-if="loading" class="sticker-empty">加载中…</div>
      <div v-else-if="error" class="sticker-empty">
        <span class="sticker-error">{{ error }}</span>
      </div>
      <div v-else-if="!hasStickers" class="sticker-empty">暂无表情包</div>

      <div v-else class="sticker-grid">
        <div v-for="s in stickers" :key="s.filename" class="sticker-item" :title="s.sticker_title || s.filename">
          <img :src="s.url" alt="" />
          <div class="sticker-item-actions">
            <el-button link type="primary" size="small" @click.stop="onPick(s)">发送</el-button>
            <el-button link type="primary" size="small" @click.stop="openEdit(s)">编辑</el-button>
            <el-button link type="danger" size="small" @click.stop="onDelete(s)">删除</el-button>
          </div>
        </div>
      </div>
    </div>
  </el-popover>

  <el-dialog v-model="editVisible" title="编辑表情包" width="420px">
    <el-form label-width="72px">
      <el-form-item label="标题">
        <el-input v-model="form.sticker_title" placeholder="表情包标题" />
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="form.description" placeholder="表情包描述" />
      </el-form-item>
      <el-form-item label="情绪">
        <el-input v-model="form.mood" placeholder="情绪，如 开心 / 生气" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="editVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.sticker-menu {
  max-height: 320px;
  overflow-y: auto;
}
.sticker-menu-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 4px 8px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.sticker-menu-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.sticker-pending {
  margin-left: 6px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.sticker-pending.is-clear {
  color: var(--el-color-success);
}
.sticker-empty {
  padding: 24px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.sticker-error {
  color: var(--el-color-danger);
}
.sticker-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
}
.sticker-item {
  position: relative;
  border-radius: 6px;
  overflow: hidden;
  background: var(--el-fill-color);
  cursor: pointer;
}
.sticker-item img {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
}
.sticker-item-actions {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  background: var(--bg-overlay);
  opacity: 0;
  transition: opacity 0.15s ease;
}
.sticker-item:hover .sticker-item-actions {
  opacity: 1;
}
</style>
