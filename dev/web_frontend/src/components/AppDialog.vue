<script setup lang="ts">
import { ElDialog } from 'element-plus'

defineOptions({ inheritAttrs: false })
withDefaults(defineProps<{
  size?: 'panel' | 'form'
  width?: string | number
}>(), { size: 'form', width: '560px' })
</script>

<template>
  <el-dialog
    v-bind="$attrs"
    :class="['app-dialog', `app-dialog--${size}`]"
    :width="size === 'panel' ? 'min(1280px, calc(100vw - var(--app-dialog-gap)))' : width"
    align-center
    :show-close="true"
    :close-on-click-modal="false"
  >
    <template v-for="(_, name) in $slots" #[name]="slotProps">
      <slot :name="name" v-bind="slotProps || {}" />
    </template>
  </el-dialog>
</template>

<style>
/* All windows share a viewport limit and a permanently visible close button. */
.app-dialog.el-dialog {
  --app-dialog-gap: 32px;
  display: flex;
  flex-direction: column;
  max-width: calc(100vw - var(--app-dialog-gap));
  max-height: calc(100dvh - var(--app-dialog-gap));
  margin: auto;
  padding: 0;
  overflow: hidden;
}
.app-dialog--panel.el-dialog { height: min(840px, calc(100dvh - var(--app-dialog-gap))); }
.app-dialog.el-dialog > .el-dialog__header {
  position: relative;
  flex: none;
  min-height: 60px;
  margin: 0;
  padding: 16px 64px 16px 20px !important;
}
.app-dialog.el-dialog > .el-dialog__header .el-dialog__headerbtn {
  top: 12px;
  right: 12px;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
}
.app-dialog .el-dialog__headerbtn:hover,
.app-dialog .el-dialog__headerbtn:focus-visible {
  background: var(--bg-subtle);
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.app-dialog .el-dialog__headerbtn .el-dialog__close { color: var(--text-2); }
.app-dialog.el-dialog > .el-dialog__body {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
}
.app-dialog--panel.el-dialog > .el-dialog__body {
  display: flex;
  flex-direction: column;
  padding: 0 !important;
}
.app-dialog.el-dialog > .el-dialog__footer { flex: none; }
@media (max-width: 600px) {
  .app-dialog.el-dialog {
    --app-dialog-gap: 16px;
  }
  .app-dialog--panel.el-dialog { height: calc(100dvh - 16px); }
  .app-dialog.el-dialog > .el-dialog__header { padding-left: 12px !important; }
}
</style>
