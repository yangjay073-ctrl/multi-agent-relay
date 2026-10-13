const BASE = ''

async function j(method, url, body) {
  const r = await fetch(BASE + url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`)
  return data
}

export const api = {
  poll: (cursor) => j('GET', `/api/poll?cursor=${cursor}`),
  startSession: (p) => j('POST', '/api/session', p),
  stop: () => j('POST', '/api/stop'),
  replan: () => j('POST', '/api/replan'),
  sessions: () => j('GET', '/api/sessions'),
  rounds: (sid) => j('GET', `/api/rounds?sid=${sid}`),
  settings: () => j('GET', '/api/settings'),
  saveSettings: (s) => j('POST', '/api/settings', s),
  profileTest: (p) => j('POST', '/api/profile_test', p),
  deliver: (token, text) => j('POST', '/api/deliver', { token, text }),
  exportUrl: (sid) => `${BASE}/api/export?sid=${sid}`,
}
