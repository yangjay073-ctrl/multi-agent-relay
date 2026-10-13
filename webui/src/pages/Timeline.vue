<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const sessions = ref([])
const sid = ref(null)
const rounds = ref([])
const selRound = ref(null)
const detail = ref('')

onMounted(load)

async function load() {
  sessions.value = (await api.sessions()).sessions
  if (sid.value && !sessions.value.some(s => s.id === sid.value)) { sid.value = null; rounds.value = [] }
}

async function pick(s) {
  sid.value = s.id
  selRound.value = null
  detail.value = ''
  rounds.value = (await api.rounds(s.id)).rounds
}

function pickRound(r) {
  selRound.value = r.round_no
  detail.value = [
    `=== 轮 ${r.round_no}  [${r.status}@${r.stage}]  耗时 ${r.duration_ms}ms ===`,
    `\n【任务包 · A 原文（未脱敏）】\n${r.task_pack || '（未产生）'}`,
    `\n【发给 B · 脱敏后】\n${r.sent_to_b || '（未产生）'}`,
    `\n【B 产出】\n${r.b_output || '（未产生）'}`,
    `\n【A 验收意见】\n${r.verdict || '（未产生）'}`,
  ].join('\n')
}
</script>

<template>
  <div class="page-title">轮次时间线</div>
  <p class="hint">任意会话 → 任意轮次的完整留痕（任务包原文 / 脱敏后发出 / B 产出 / 验收意见）；导出的 JSON 已全部重新脱敏。</p>

  <div class="tl-wrap">
    <div class="sess-col">
      <div class="row" style="margin-bottom:10px">
        <button class="btn ghost" @click="load">刷新</button>
      </div>
      <div class="sess-list">
        <div v-for="s in sessions" :key="s.id" class="sess-item" :class="{ sel: s.id === sid }" @click="pick(s)">
          <div class="d">#{{ s.id }} {{ s.directive }}</div>
          <div class="m">
            <span class="badge" :class="s.status || 'max_rounds'">{{ s.status || '运行中' }}</span>
            {{ s.created_at }}
          </div>
        </div>
        <p v-if="!sessions.length" class="hint">暂无会话记录</p>
      </div>
    </div>

    <div class="round-col" v-if="sid">
      <div class="row">
        <a class="btn ghost" style="text-decoration:none" :href="api.exportUrl(sid)" :download="`session_${sid}.json`">⬇ 导出会话 JSON（脱敏后）</a>
      </div>
      <div v-for="r in rounds" :key="r.round_no" class="round-item" :class="{ sel: selRound === r.round_no }" @click="pickRound(r)">
        <span class="badge mono">轮 {{ r.round_no }}</span>
        <span class="badge" :class="r.status">{{ r.status }}@{{ r.stage }}</span>
        <span style="color:var(--muted); font-family:var(--mono); font-size:12px"> {{ r.duration_ms }}ms</span>
        <div v-if="r.verdict" style="font-size:12px; color:var(--muted); margin-top:6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">{{ r.verdict }}</div>
      </div>
      <pre v-if="detail" class="detail">{{ detail }}</pre>
    </div>
  </div>
</template>
