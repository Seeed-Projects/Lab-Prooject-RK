<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlertTriangle, Clock3 } from 'lucide-vue-next'
import PerformancePanel from './PerformancePanel.vue'

const props = defineProps({
  switchStatus: { type: Object, default: () => ({}) },
})

const totalFps = ref(0)
const liveReady = ref(false)
const runState = ref('Waiting')
const runId = ref('')
const streamStats = ref([])
const stockAlerts = ref([])
const eventTimeline = ref([])
const streamNonce = ref(Date.now())
const imageEls = ref([])
const timelinePanel = ref(null)
const connection = ref('Disconnected')
let ws = null
let pollTimer = null
let reconnectTimer = null

const streams = computed(() => Array.from({ length: 4 }, (_, index) => ({
  id: index + 1,
  name: `Camera ${String(index + 1).padStart(2, '0')}`,
  fps: streamStats.value[index]?.fps ? Number(streamStats.value[index].fps).toFixed(1) : '—',
  captureFps: streamStats.value[index]?.capture_fps ? Number(streamStats.value[index].capture_fps).toFixed(1) : '—',
  inferenceFps: streamStats.value[index]?.inference_fps ? Number(streamStats.value[index].inference_fps).toFixed(1) : '—',
  displayFps: streamStats.value[index]?.display_fps ? Number(streamStats.value[index].display_fps).toFixed(1) : '—',
  ready: Boolean(streamStats.value[index]?.ready),
  status: streamStats.value[index]?.ready ? 'LIVE' : 'STARTING',
})))

function averageMetric(key) {
  const values = streamStats.value
    .map((stream) => Number(stream?.[key] || 0))
    .filter((value) => value > 0)
  if (!values.length) return 'Starting'
  return `${(values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(1)} FPS`
}

const metrics = computed(() => [
  { label: 'Live Streams', value: `${streams.value.filter((stream) => stream.ready).length}/4` },
  { label: 'Inference', value: averageMetric('inference_fps') },
  { label: 'Display', value: averageMetric('display_fps') },
  { label: 'NPU Workers', value: '4 RKNN' },
])
const alertFocus = computed(() => stockAlerts.value[0] || null)
const alertTrail = computed(() => {
  if (eventTimeline.value.length) return eventTimeline.value
  return stockAlerts.value.map((item) => ({
    time: item.time,
    camera: `Camera ${item.camera}`,
    title: `${item.product} · ${item.status}`,
  }))
})

function normalizeAlert(raw) {
  const source = raw.payload || raw
  const camera = Number(source.camera)
  const product = String(source.product || '').trim()
  const status = String(source.status || '').trim()
  if (!Number.isInteger(camera) || camera < 1 || !product || !status) return null
  return {
    camera: String(camera).padStart(2, '0'),
    product: product.toUpperCase(),
    status: status.replaceAll('_', ' '),
    action: String(source.display_action || source.action || 'RESTOCK').replaceAll('_', ' ').toUpperCase(),
    previousCount: Number.isFinite(Number(source.previous_count)) ? Number(source.previous_count) : null,
    currentCount: Number.isFinite(Number(source.current_count)) ? Number(source.current_count) : null,
    time: source.time || new Date().toLocaleTimeString(),
    timestamp: Number(source.timestamp || Date.now() / 1000),
    source: source.source,
  }
}

function normalizeAlerts(items) {
  return items.map(normalizeAlert).filter(Boolean)
}

function normalizeTimeline(items) {
  const seen = new Set()
  return items
    .map((entry, index) => ({
      time: entry.time || new Date().toLocaleTimeString(),
      timestamp: Number(entry.timestamp || 0),
      camera: `Camera ${String(entry.camera || 1).padStart(2, '0')}`,
      title: entry.title || `${entry.product || 'Product'} shortage detected`,
      order: index,
    }))
    .sort((left, right) => (right.timestamp - left.timestamp) || (left.order - right.order))
    .filter((entry) => {
      const signature = `${entry.timestamp || entry.time}|${entry.camera}|${entry.title}`
      if (seen.has(signature)) return false
      seen.add(signature)
      return true
    })
    .slice(0, 12)
}

function focusLatestTimelineEvent() {
  nextTick(() => {
    if (timelinePanel.value) timelinePanel.value.scrollTop = 0
  })
}

function streamUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/shelf/live/events`
}

function connectEvents() {
  if (ws) return
  ws = new WebSocket(streamUrl())
  ws.onopen = () => {
    connection.value = 'Connected'
  }
  ws.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'connection' && payload.status) {
        connection.value = payload.status === 'connected'
          ? 'Connected'
          : payload.status === 'reconnecting'
            ? 'Reconnecting'
            : 'Disconnected'
      }
      if (payload.type === 'retail.multi.status' && payload.data) {
        liveReady.value = Boolean(payload.data.ready)
        runState.value = payload.data.running ? (payload.data.ready ? 'RUNNING' : 'STARTING') : 'STOPPED'
        runId.value = payload.data.pid ? `pid-${payload.data.pid}` : runId.value
        totalFps.value = Number(payload.data.inference_fps || payload.data.total_fps || payload.data.fps || totalFps.value)
        streamStats.value = payload.data.streams || []
        if (Array.isArray(payload.data.alerts)) {
          stockAlerts.value = normalizeAlerts(payload.data.alerts)
        }
        if (Array.isArray(payload.data.timeline)) {
          eventTimeline.value = normalizeTimeline(payload.data.timeline)
        }
      }
      if (payload.type === 'retail.multi.streams' && payload.payload?.streams) {
        streamStats.value = payload.payload.streams
      }
      if (payload.type === 'stock_alert') {
        const alert = normalizeAlert(payload.payload || payload)
        if (!alert) return
        stockAlerts.value = [alert, ...stockAlerts.value].slice(0, 6)
        eventTimeline.value = normalizeTimeline([{
          time: alert.time,
          timestamp: alert.timestamp,
          camera: alert.camera,
          title: `RESTOCK REQUIRED: ${alert.product}${alert.previousCount !== null && alert.currentCount !== null ? ` (${alert.previousCount} -> ${alert.currentCount})` : ''}`,
        }, ...eventTimeline.value.map((entry) => ({
          ...entry,
          camera: entry.camera.replace('Camera ', ''),
        }))])
        focusLatestTimelineEvent()
      }
    } catch {
      // ignore malformed frames
    }
  }
  ws.onclose = () => {
    connection.value = 'Disconnected'
    ws = null
    clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(() => {
      if (props.switchStatus?.active_demo === 'multi' || props.switchStatus?.target_demo === 'multi') {
        connectEvents()
      }
    }, 1800)
  }
  ws.onerror = () => {
    connection.value = 'Disconnected'
  }
}

function setImageRef(el) {
  if (el && !imageEls.value.includes(el)) imageEls.value.push(el)
}

function releaseStreams() {
  for (const image of imageEls.value) {
    image.removeAttribute('src')
    image.src = ''
    image.load?.()
  }
  imageEls.value = []
}

async function refresh() {
  try {
    const res = await fetch('/api/v1/shelf/live/status')
    const payload = await res.json()
    liveReady.value = Boolean(payload.status?.ready)
    if (payload.status?.running) runState.value = payload.status.ready ? 'RUNNING' : 'STARTING'
    if (payload.status?.pid) runId.value = `pid-${payload.status.pid}`
    if (payload.status?.inference_fps) totalFps.value = Number(payload.status.inference_fps)
    else if (payload.status?.total_fps) totalFps.value = Number(payload.status.total_fps)
    if (Array.isArray(payload.status?.streams)) streamStats.value = payload.status.streams
    if (Array.isArray(payload.status?.alerts)) stockAlerts.value = normalizeAlerts(payload.status.alerts)
    if (Array.isArray(payload.status?.timeline)) {
      eventTimeline.value = normalizeTimeline(payload.status.timeline)
    }
  } catch {
    liveReady.value = false
  }
  try {
    const res = await fetch('/api/v1/showcase/status')
    const payload = await res.json()
    if (payload.showcase) {
      if (payload.showcase.active_demo !== 'multi' && payload.showcase.target_demo !== 'multi') {
        runState.value = 'Waiting'
      } else if (payload.showcase.state) {
        runState.value = payload.showcase.state
      }
    }
  } catch {
    // ignore
  }
}

watch(
  () => props.switchStatus,
  (value) => {
    if (value?.active_demo === 'multi' && value?.state === 'RUNNING') {
      connectEvents()
      refresh()
    } else if (value?.active_demo !== 'multi' && value?.target_demo !== 'multi') {
      liveReady.value = false
      runState.value = 'Waiting'
      releaseStreams()
      if (ws) {
        ws.close()
        ws = null
      }
    }
  },
  { deep: true, immediate: true },
)

onMounted(async () => {
  await refresh()
  pollTimer = setInterval(refresh, 2000)
})

onBeforeUnmount(() => {
  liveReady.value = false
  releaseStreams()
  clearInterval(pollTimer)
  clearTimeout(reconnectTimer)
  if (ws) {
    ws.onopen = null
    ws.onmessage = null
    ws.onerror = null
    ws.onclose = null
    ws.close()
    ws = null
  }
})
</script>

<template>
  <section class="stage-grid multi-stage">
    <div class="camera-wall">
      <article v-for="stream in streams" :key="stream.id" class="camera-tile video-tile" :class="{ 'camera-alert-active': alertFocus?.camera === String(stream.id).padStart(2, '0') }">
        <div class="tile-scene real-video-scene">
          <img
            v-if="liveReady"
            :ref="setImageRef"
            :src="`/api/v1/shelf/live/${stream.id}.mjpeg?t=${streamNonce}`"
            alt="Live RKNN shelf detection stream"
          />
          <div v-else class="tile-grid">
            <span>{{ stream.name }}</span>
          </div>
          <div v-if="alertFocus?.camera === String(stream.id).padStart(2, '0')" class="tile-restock-badge">RESTOCK REQUIRED</div>
        </div>
        <footer>
          <span>{{ stream.name }}</span>
          <strong><i :class="{ live: stream.ready }"></i>{{ stream.status }} · {{ stream.ready ? `${stream.displayFps} FPS` : 'Waiting' }}</strong>
        </footer>
      </article>
    </div>
    <aside class="multi-side">
        <PerformancePanel title="RK3588 NPU Showcase" :metrics="metrics" />
        <div class="event-card">
          <p class="section-kicker">AI Event Center</p>
          <div v-if="alertFocus" class="event-row highlight-alert">
            <AlertTriangle :size="16" />
            <span>Stock Alert</span>
            <strong>Camera {{ alertFocus.camera }} · {{ alertFocus.product }}</strong>
          </div>
          <div v-if="alertFocus" class="alert-details">
            <div><span>Product</span><strong>{{ alertFocus.product }}</strong></div>
            <div v-if="alertFocus.previousCount !== null && alertFocus.currentCount !== null"><span>Stock Change</span><strong>{{ alertFocus.previousCount }} -> {{ alertFocus.currentCount }}</strong></div>
            <div><span>Status</span><strong>{{ alertFocus.status }}</strong></div>
            <div><span>Action</span><strong>{{ alertFocus.action }}</strong></div>
          </div>
          <div v-else class="event-empty">
            <strong>Monitoring all camera zones</strong>
            <span>No stock alert received</span>
          </div>
        </div>
        <div ref="timelinePanel" class="event-card shortage-card">
          <p class="section-kicker"><Clock3 :size="16" /> Event Timeline</p>
          <div v-for="entry in alertTrail" :key="`${entry.timestamp || entry.time}-${entry.camera}-${entry.title}`" class="event-row">
            <span>{{ entry.time }}</span>
            <strong>{{ entry.camera }}</strong>
            <em>{{ entry.title }}</em>
          </div>
          <div v-if="!alertTrail.length" class="event-row">
            <span>Waiting</span>
            <strong>AI Event Center</strong>
            <em>No stock alert received yet</em>
          </div>
        </div>
    </aside>
  </section>
</template>
