/** 入口：挂载 App（Vite 构建后的产物由 FastAPI 在 /static 下托管，见 vite.config.js 的 base）。 */
import { createApp } from 'vue'

import App from './App.vue'
import './styles.css'

createApp(App).mount('#app')
