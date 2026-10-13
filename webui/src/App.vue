<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from './api.js'
import Setup from './pages/Setup.vue'
import Timeline from './pages/Timeline.vue'
import Settings from './pages/Settings.vue'

const page = ref('setup')
const pages = { setup: Setup, timeline: Timeline, settings: Settings }
const running = ref(false)
const cursor = ref(0)
const logs = ref([])
const wait = ref(null)
let timer = null

async function poll() {
  try {
    const d = await api.poll(cursor.value)
    running.value = d.running
    wait.value = d.wait
    if (d.logs.length) {
      logs.value.push(...d.logs)
      if (logs.value.length > 500) logs.value = logs.value.slice(-500)
    }
    cursor.value = d.next
  } catch { /* server 重启间隙忽略 */ }
}

onMounted(() => { poll(); timer = setInterval(poll, 1000) })
onUnmounted(() => clearInterval(timer))

function logClass(line) {
  if (line.includes('✓') || line.includes('通过')) return 'l-ok'
  if (line.includes('✗') || line.includes('失败') || line.includes('不通过')) return 'l-bad'
  if (line.includes('超时') || line.includes('打回') || line.includes('僵局') || line.includes('脱敏')) return 'l-warn'
  if (line.startsWith('——')) return 'l-muted'
  return ''
}
</script>

<template>
  <nav class="side">
    <div class="brand">◈ Agent 中转协作</div>
    <button v-for="(comp, key) in pages" :key="key" class="nav-btn"
            :class="{ active: page === key }" @click="page = key">
      <span class="nav-dot"></span>{{ { setup: '任务布置', timeline: '轮次时间线', settings: '设置' }[key] }}
    </button>
    <div class="run-state">
      引擎状态：<b :class="{ off: !running }">{{ running ? '运行中' : '空闲' }}</b>
    </div>
  </nav>

  <main class="main">
    <component :is="pages[page]" :logs="logs" :log-class="logClass" :wait="wait"
               :running="running" @started="cursor = 0; logs = []" @refresh="poll" />
  </main>
</template>
