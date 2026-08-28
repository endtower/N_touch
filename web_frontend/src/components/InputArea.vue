<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import StickerMenu from './StickerMenu.vue'

// 该组件只负责「把要发的内容整理好，丢给父组件」，不直接发请求
const props = defineProps<{
  disabled?: boolean   // 父组件禁用 (例如没身份, 不能发)
}>()
const emit = defineEmits<{
  send: [payload: { text: string; images: string[] }]
}>()

const text = ref('')
const images = ref<string[]>([])   // data:image/xxx;base64,xxx
const sending = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const canSend = computed(
  () => !props.disabled && !sending.value && (text.value.trim().length > 0 || images.value.length > 0),
)

// 把 File 转成 data URI（base64）
function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

async function handleFiles(fileList: FileList | null) {
  if (!fileList) return
  for (const f of Array.from(fileList)) {
    if (!f.type.startsWith('image/')) {
      ElMessage.warning(`跳过非图片文件：${f.name}`)
      continue
    }
    // 超过 8MB 的图提示一下，避免 base64 太大把后端打爆
    if (f.size > 8 * 1024 * 1024) {
      ElMessage.warning(`图片过大（${(f.size / 1024 / 1024).toFixed(1)}MB），可能影响速度：${f.name}`)
    }
    images.value.push(await fileToDataUrl(f))
  }
}

function onPickFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  handleFiles(input.files)
  // 清空 input，允许重复选同一张图
  input.value = ''
}

function pickFiles() {
  fileInput.value?.click()
}

function removeImage(i: number) {
  images.value.splice(i, 1)
}

// 表情包：从 /llm-save 拉取图片 → 转 base64 data URL → 加入待发送图片列表
function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = reject
    reader.readAsDataURL(blob)
  })
}

async function onStickerPick(url: string) {
  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    images.value.push(await blobToDataUrl(blob))
  } catch (e: any) {
    ElMessage.error(`表情包加载失败：${e?.message || e}`)
  }
}

// 粘贴图片支持：很多截图工具直接 Ctrl+V
async function onPaste(ev: ClipboardEvent) {
  if (!ev.clipboardData) return
  const items = Array.from(ev.clipboardData.items).filter(
    it => it.kind === 'file' && it.type.startsWith('image/'),
  )
  if (items.length === 0) return
  ev.preventDefault()
  const files: File[] = []
  for (const it of items) {
    const f = it.getAsFile()
    if (f) files.push(f)
  }
  // 走 handleFiles 一样的逻辑
  for (const f of files) {
    if (f.size > 8 * 1024 * 1024) {
      ElMessage.warning(`图片过大（${(f.size / 1024 / 1024).toFixed(1)}MB），可能影响速度：${f.name}`)
    }
    images.value.push(await fileToDataUrl(f))
  }
}

async function doSend() {
  if (!canSend.value) return
  sending.value = true
  try {
    emit('send', {
      text: text.value.trim(),
      images: [...images.value],
    })
    // 清空输入
    text.value = ''
    images.value = []
  } finally {
    // 真正的 sending 状态由父组件控制（拿到 LLM 响应后才结束）
    setTimeout(() => { sending.value = false }, 50)
  }
}

function onKeydown(ev: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault()
    doSend()
  }
}
</script>

<template>
  <div class="input-area">
    <!-- 缩略图预览 -->
    <div v-if="images.length" class="thumb-list">
      <div v-for="(img, i) in images" :key="i" class="thumb">
        <img :src="img" />
        <button class="thumb-remove" type="button" @click="removeImage(i)">×</button>
      </div>
    </div>

    <!-- 输入框 -->
    <textarea
      v-model="text"
      rows="3"
      placeholder="输入消息（Enter 发送，Shift+Enter 换行，可直接 Ctrl+V 粘贴图片）"
      @keydown="onKeydown"
      @paste="onPaste"
    />

    <!-- 工具栏 -->
    <div class="input-toolbar">
      <div class="left-tools">
        <el-button :icon="'Picture'" size="small" @click="pickFiles">添加图片</el-button>
        <StickerMenu @select="onStickerPick" />
        <input
          ref="fileInput"
          type="file"
          accept="image/*"
          multiple
          style="display: none"
          @change="onPickFiles"
        />
      </div>

      <el-button
        type="primary"
        :icon="'Promotion'"
        :disabled="!canSend"
        @click="doSend"
      >
        发送
      </el-button>
    </div>
  </div>
</template>
