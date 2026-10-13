<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const props = defineProps(['logs', 'logClass', 'wait', 'running'])
const emit = defineEmits(['started'])

const directive = ref('')
const settings = ref(null)
const bindA = ref(''), bindB = ref('')
const maxRounds = ref(5), roundTimeout = ref(600)
const error = ref('')
const logRef = ref(null)

onMounted(async () => {
  settings.value = await api.settings()
  maxRounds.value = settings.value.max_rounds
  roundTimeout.value = settings.value.round_timeout
  bindA.value = settings.value.bind.A
  bindB.value = settings.value.bind.B
})

const names = () => ['本地Mock(测试用)', ...Object.keys(settings.value?.profiles || {}).sort()]

async function start() {
  error.value = ''
  try {
    await api.startSession({
      directive: directive.value, bindA: bindA.value, bindB: bindB.value,
      max_rounds: +maxRounds.value, round_timeout: +roundTimeout.value,
    })
    emit('started')
  } catch (e) { error.value = e.message }
}
async function stop() { await api.stop().catch(() => {}) }
async function replan() { await api.replan().catch(() => {}) }

/* 剪贴板等待回传 */
const replyText = ref('')
const deliverResult = ref('')
async function deliver() {
  const d = await api.deliver(props.wait.token, replyText.value)
  deliverResult.value = d.ok ? '已回传 ✓' : '回传被丢弃（等待已超时/结束）'
  if (d.ok) { replyText.value = ''; setTimeout(() => deliverResult.value = '', 3000) }
}
</script>

<template>
  <div class="page-title">任务布置</div>
  <p class="hint">用户指令 → Agent A 拆解为结构化任务包（缺字段自动打回）→ 脱敏 → Agent B 执行 → A 验收循环</p>

  <div class="card">
    <label class="fld">用户指令</label>
    <textarea v-model="directive" rows="4" placeholder="例：修复 scenarios/demo_project 的 bug：discount(-3) 应返回 0 而不是 -300…"></textarea>
  </div>

  <div class="card" v-if="settings">
    <div class="row">
      <div style="flex:1; min-width:170px">
        <label class="fld">Agent A（任务布置 + 终判验收）</label>
        <select v-model="bindA"><option v-for="n in names()" :key="n" :value="n">{{ n }}</option></select>
      </div>
      <div style="flex:1; min-width:170px">
        <label class="fld">Agent B（执行）</label>
        <select v-model="bindB"><option v-for="n in names()" :key="n" :value="n">{{ n }}</option></select>
      </div>
      <div>
        <label class="fld">最大轮数</label>
        <input class="spin" type="number" v-model="maxRounds" min="1" max="99">
      </div>
      <div>
        <label class="fld">单轮超时(秒)</label>
        <input class="spin" type="number" v-model="roundTimeout" min="30" step="30">
      </div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="btn" :disabled="running" @click="start">▶ 启动会话</button>
      <button class="btn ghost" :disabled="!running" @click="replan">↻ 终止本轮并重新布置</button>
      <button class="btn danger" :disabled="!running" @click="stop">■ 终止</button>
    </div>
    <p v-if="error" style="color:var(--bad); margin-top:8px; font-size:13px">{{ error }}</p>
  </div>

  <div class="waitcard" v-if="wait && wait.active">
    <div class="t">⏳ 剪贴板半自动 · 等待回传（token {{ wait.token }}）</div>
    <p style="font-size:13px; color:var(--muted)">任务包已写入系统剪贴板并聚焦目标程序：请到目标 Agent 窗口 Ctrl+V 发送，把对方回复粘贴到下面回传。</p>
    <textarea v-model="replyText" rows="4" placeholder="把目标 Agent 的回复粘贴到这里…" style="margin:8px 0"></textarea>
    <button class="btn" @click="deliver">回传给 App</button>
    <span style="margin-left:10px; color:var(--warn); font-size:13px">{{ deliverResult }}</span>
  </div>

  <div class="logbox" ref="logRef">
    <div v-for="(l, i) in logs" :key="i" :class="logClass(l)">{{ l }}</div>
  </div>
</template>
