import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as ElIcons from '@element-plus/icons-vue'

import App from './App.vue'
// 设计令牌必须先于组件样式加载：style.css 的兼容映射引用它定义的变量
import './tokens.css'
import './style.css'

const app = createApp(App)
app.use(ElementPlus)

// 把所有 Element Plus 图标注册为全局组件（<ElIconCamera /> 这样用）
for (const [name, comp] of Object.entries(ElIcons)) {
  app.component(name, comp as any)
}

app.mount('#app')
