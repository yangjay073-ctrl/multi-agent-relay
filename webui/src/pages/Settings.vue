<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const settings = ref(null)
const sel = ref(null)
const form = ref({})
const testing = ref(false)
const testResult = ref('')
const saved = ref('')

const BLANK = { name: '', type: 'api', base_url: '', api_key: '', model: '', launch_command: '', command: '', cwd: '' }

onMounted(async () => { settings.value = await api.settings() })

function pick(name) {
  sel.value = name
  form.value = { name, ...settings.value.profiles[name] }
  testResult.value = saved.value = ''
}

async function save() {
  const { name, ...prof } = form.value
  if (!name || name === '本地Mock(测试用)') { saved.value = '名称无效'; return }
  if (prof.type === 'api' && !(prof.base_url && prof.api_key && prof.model)) {
    saved.value = 'API 类需 base_url / api_key / model 全部填写'; return
  }
  if (prof.type === 'cli' && !prof.command) {
    saved.value = 'CLI 类需填写 command'; return
  }
  const s = JSON.parse(JSON.stringify(settings.value))
  if (sel.value && sel.value !== name) delete s.profiles[sel.value]
  s.profiles[name] = prof
  await api.saveSettings(s)
  settings.value = s
  sel.value = name
  saved.value = '已保存 ✓'
}

async function remove() {
  if (!sel.value) return
  delete settings.value.profiles[sel.value]
  for (const r of ['A', 'B']) if (settings.value.bind[r] === sel.value) settings.value.bind[r] = ''
  await api.saveSettings(settings.value)
  sel.value = null; form.value = {}
}

async function testConn() {
  testing.value = true; testResult.value = ''
  try {
    const d = await api.profileTest({ ...form.value })
    testResult.value = `可用 ✓ ${d.reply.slice(0, 80)}`
  } catch (e) { testResult.value = `不可用 ✗ ${e.message}` }
  testing.value = false
}
</script>

<template>
  <div class="page-title">设置</div>
  <p class="hint">Agent 配置池（多候选）：A / B 在任务布置页各自绑定，可按任务切换。App 不预设任何 Agent；key 只存本地 data/settings.json。</p>

  <div class="prof-grid" v-if="settings">
    <div class="prof-list">
      <div v-for="(p, n) in settings.profiles" :key="n" class="prof-item" :class="{ sel: sel === n }" @click="pick(n)">
        <div class="n">{{ n }}</div>
        <div class="t">{{ p.type }} · {{ p.type === 'api' ? (p.model || p.base_url) : (p.launch_command || '剪贴板') }}
          <span v-if="settings.bind.A === n" class="badge mono">A</span>
          <span v-if="settings.bind.B === n" class="badge mono">B</span>
        </div>
      </div>
      <button class="btn ghost" @click="sel = null; form = { ...BLANK }; testResult = saved = ''">＋ 新增配置</button>
    </div>

    <div class="form-col card" v-if="form.name !== undefined">
      <div class="row">
        <div style="flex:1"><label class="fld">名称</label><input v-model="form.name"></div>
        <div style="width:220px"><label class="fld">类型</label>
          <select v-model="form.type">
            <option value="api">api（OpenAI 兼容）</option>
            <option value="cli">cli（本地命令行 Agent）</option>
            <option value="clipboard">clipboard（剪贴板半自动）</option>
          </select>
        </div>
      </div>
      <template v-if="form.type === 'api'">
        <div style="margin-top:10px"><label class="fld">base_url</label><input v-model="form.base_url" placeholder="https://your-agent.example.com/v1"></div>
        <div class="row" style="margin-top:10px">
          <div style="flex:1"><label class="fld">api_key</label><input v-model="form.api_key" type="password"></div>
          <div style="flex:1"><label class="fld">model</label><input v-model="form.model"></div>
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn ghost" :disabled="testing" @click="testConn">{{ testing ? '测试中…' : '测试连接' }}</button>
          <span style="font-size:13px" :style="{ color: testResult.startsWith('可用') ? 'var(--ok)' : 'var(--bad)' }">{{ testResult }}</span>
        </div>
      </template>
      <template v-if="form.type === 'cli'">
        <div style="margin-top:10px"><label class="fld">command（任务包经 stdin 传入，stdout 即产出）</label>
          <input v-model="form.command" placeholder="例：codex exec --skip-git-repo-check -  或  claude -p"></div>
        <div style="margin-top:10px"><label class="fld">cwd（工作目录，执行型 Agent 在此目录干活，可空）</label>
          <input v-model="form.cwd" placeholder="D:\projects\demo"></div>
        <p class="hint" style="margin-top:8px; color:var(--warn)">⚠ 配置即授权该命令在本机执行任务（含文件读写），仅添加你信任的 Agent。测试连接会真实调用一次。</p>
        <div class="row" style="margin-top:12px">
          <button class="btn ghost" :disabled="testing" @click="testConn">{{ testing ? '测试中…' : '测试连接（真实调用）' }}</button>
          <span style="font-size:13px" :style="{ color: testResult.startsWith('可用') ? 'var(--ok)' : 'var(--bad)' }">{{ testResult }}</span>
        </div>
      </template>
      <template v-else-if="form.type === 'clipboard'">
        <div style="margin-top:10px"><label class="fld">目标程序（可执行文件名或路径，如 notepad）</label>
          <input v-model="form.launch_command" placeholder="notepad"></div>
        <p class="hint" style="margin-top:8px">发送时会优先把已运行的目标窗口置前（Windows），找不到才启动新实例。</p>
      </template>
      <div class="row" style="margin-top:16px">
        <button class="btn" @click="save">保存</button>
        <button class="btn danger" v-if="sel" @click="remove">删除</button>
        <span style="font-size:13px; color:var(--ok)">{{ saved }}</span>
      </div>
    </div>
    <div class="form-col card" v-else>
      <p class="hint">← 左侧选择一个配置，或点「新增配置」。<br><br>
      当前绑定：A = {{ settings.bind.A || '未绑定' }} · B = {{ settings.bind.B || '未绑定' }}（在任务布置页切换）</p>
    </div>
  </div>
</template>
