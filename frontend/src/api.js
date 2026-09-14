const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })
  const contentType = response.headers.get('content-type') || ''
  const body = contentType.includes('application/json') ? await response.json() : null
  if (!response.ok) {
    throw new Error(body?.detail || `Request failed (${response.status})`)
  }
  return body
}

export const api = {
  health: () => request('/health'),
  demos: () => request('/demos'),
  start: (demoId, parameters = {}) => request(`/demos/${encodeURIComponent(demoId)}/runs`, {
    method: 'POST',
    body: JSON.stringify({ parameters }),
  }),
  stop: (runId) => request(`/runs/${encodeURIComponent(runId)}/stop`, {
    method: 'POST',
    body: '{}',
  }),
  switchDemo: (demoId, parameters = {}) => request(`/demos/${encodeURIComponent(demoId)}/switch`, {
    method: 'POST',
    body: JSON.stringify({ parameters }),
  }),
  artifacts: (runId) => request(`/runs/${encodeURIComponent(runId)}/artifacts`),
  artifactUrl: (runId, name) => `${API_BASE}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`,
}

export function websocketUrl() {
  const configured = import.meta.env.VITE_WS_URL
  if (configured) return configured
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/stream`
}
