<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlertTriangle, Camera, Mic2, Package, Volume2, Wifi } from 'lucide-vue-next'
import PerformancePanel from './PerformancePanel.vue'

const props = defineProps({
  switchStatus: { type: Object, default: () => ({}) },
})

const status = ref({
  running: false,
  ready: false,
  network_reachable: false,
  rtsp_port_ready: false,
  rtsp_connected: false,
  stream_ready: false,
  websocket_connected: false,
  data_initialized: false,
  has_detection_result: false,
  frame: 0,
  fps: 0,
  total_fps_display: 0,
  inventory: [],
  low_stock: [],
  alert: null,
  stream_url: '/api/v1/retail-single/mjpeg',
  preview_url: '/api/v1/retail-single/frame.jpg',
  source_url: null,
  last_error: null,
  webrtc_ready: false,
  webrtc_url: null,
})
const wsState = ref('Disconnected')
const voiceConnection = ref('Disconnected')
const voicePhase = ref('waiting')
const voiceOutputStatus = ref('Audio ready')
const voiceSpeaking = ref(false)
const voiceQuestion = ref('')
const inventoryAnswer = ref(null)
const voiceTurns = ref([])
const videoMode = ref('mjpeg')
const videoElement = ref(null)
const videoAspectRatio = ref('16 / 9')

let pollTimer = null
let ws = null
let reconnectTimer = null
let voiceWs = null
let voiceReconnectTimer = null
let rtcPeer = null
let rtcSignal = null
let rtcTimer = null

const hasCjk = (value) => /[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]/u.test(String(value || ''))
const englishVoiceText = (value) => String(value || '')
  .replace(/商品\s*(\d+)/gu, 'Product $1')
  .replace(/[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+/gu, 'product')
  .replace(/\s+/g, ' ')
  .trim()
const productName = (value, id) => {
  const name = String(value || '').trim()
  return name && !hasCjk(name) ? name : `Product ${id || ''}`.trim()
}
const stockStatus = (value) => {
  const normalized = String(value || '').trim().replaceAll('_', ' ').toUpperCase()
  if (normalized === 'IN STOCK') return 'In Stock'
  if (normalized === 'LOW STOCK') return 'Low Stock'
  if (normalized === 'OUT OF STOCK') return 'Out of Stock'
  return normalized && !hasCjk(normalized) ? normalized : 'Status Pending'
}
const inventoryRows = computed(() => (status.value.inventory || []).map((item) => ({
  ...item,
  displayName: productName(item.name || item.product, item.id),
  displayStatus: stockStatus(item.status),
})))
const alertRow = computed(() => status.value.alert || status.value.low_stock?.[0] || null)
const alertProduct = computed(() => productName(
  alertRow.value?.product || alertRow.value?.name,
  alertRow.value?.product_id || alertRow.value?.id,
))
const alertStatus = computed(() => stockStatus(alertRow.value?.status || 'OUT_OF_STOCK'))
const alertChange = computed(() => {
  const previous = alertRow.value?.previous_count
  const current = alertRow.value?.current_count
  return Number.isFinite(previous) && Number.isFinite(current) ? `${previous} -> ${current}` : null
})
const layerSummary = computed(() => [
  { label: 'Live AI View', value: status.value.stream_ready ? 'Detected stream live' : waitingMessage.value },
  { label: 'AI Result', value: status.value.data_initialized ? 'Product status live' : 'Waiting for product registration' },
  { label: 'Business Insight', value: alertRow.value ? 'Restock required' : status.value.data_initialized ? 'Shelf healthy' : 'Waiting for business data' },
  { label: 'Performance', value: `${(status.value.fps || 0).toFixed(1)} FPS` },
])
const metrics = computed(() => [
  { label: 'Network', value: status.value.network_reachable ? 'Connected' : 'Waiting' },
  { label: 'RTSP', value: status.value.rtsp_port_ready ? 'Ready' : 'Waiting' },
  { label: 'Video', value: videoMode.value === 'webrtc' ? 'WebRTC Live' : status.value.stream_ready ? 'MJPEG Fallback' : 'Waiting' },
  { label: 'Product Data', value: status.value.websocket_connected ? 'Connected' : 'Waiting' },
  { label: 'Frames', value: `${status.value.frame || 0}` },
  { label: 'FPS', value: `${(status.value.fps || 0).toFixed(1)}` },
])
const waitingMessage = computed(() => {
  if (!status.value.network_reachable) return 'Waiting for reCamera'
  if (!status.value.stream_ready) return 'Connecting to reCamera stream...'
  if (!status.value.data_initialized) return 'Waiting for product registration'
  return 'reCamera AI Live'
})
const statusLabel = computed(() => {
  if (status.value.stream_ready) return 'Streaming'
  if (!status.value.network_reachable) return 'Waiting for reCamera'
  return 'Connecting'
})
const streamUrl = computed(() => status.value.stream_url || '/api/v1/retail-single/mjpeg')
const voicePrompt = computed(() => {
  if (voiceConnection.value !== 'Connected') return 'Connecting to ReSpeaker voice service...'
  if (voicePhase.value === 'transcribing') return 'Transcribing your question and checking live inventory...'
  if (voicePhase.value === 'answered') return 'Answer ready — ask another shelf question when you are ready.'
  return 'Ready — ask a question in English, then pause for the answer.'
})

function closeWebRTC() {
  clearTimeout(rtcTimer)
  rtcTimer = null
  if (rtcSignal) {
    rtcSignal.onclose = null
    rtcSignal.close()
    rtcSignal = null
  }
  if (rtcPeer) {
    rtcPeer.ontrack = null
    rtcPeer.onicecandidate = null
    rtcPeer.close()
    rtcPeer = null
  }
  if (videoElement.value?.srcObject) videoElement.value.srcObject = null
  videoMode.value = 'mjpeg'
}

function updateVideoAspectRatio(event) {
  const media = event?.currentTarget
  const width = Number(media?.videoWidth || media?.naturalWidth || 0)
  const height = Number(media?.videoHeight || media?.naturalHeight || 0)
  if (width > 0 && height > 0) videoAspectRatio.value = `${width} / ${height}`
}

async function connectWebRTC() {
  if (videoMode.value === 'webrtc' || rtcSignal || !status.value.stream_ready
    || !status.value.webrtc_ready || !status.value.webrtc_url || typeof window === 'undefined'
    || typeof window.RTCPeerConnection !== 'function') return
  try {
    rtcPeer = new window.RTCPeerConnection({ iceServers: [] })
    rtcPeer.addTransceiver('video', { direction: 'recvonly' })
    rtcPeer.ontrack = (event) => {
      if (event.streams?.[0]) {
        videoMode.value = 'webrtc'
        nextTick(() => {
          if (videoElement.value) videoElement.value.srcObject = event.streams[0]
        })
        clearTimeout(rtcTimer)
      }
    }
    rtcPeer.onicecandidate = (event) => {
      if (!event.candidate || !rtcSignal || rtcSignal.readyState !== WebSocket.OPEN) return
      rtcSignal.send(JSON.stringify({
        type: 'webrtc/candidate', value: event.candidate.candidate,
        sdpMid: event.candidate.sdpMid, sdpMLineIndex: event.candidate.sdpMLineIndex,
      }))
    }
    const offer = await rtcPeer.createOffer()
    await rtcPeer.setLocalDescription(offer)
    rtcSignal = new WebSocket(status.value.webrtc_url)
    rtcSignal.onopen = () => rtcSignal.send(JSON.stringify({ type: 'webrtc/offer', value: offer.sdp }))
    rtcSignal.onmessage = async (event) => {
      try {
        const message = JSON.parse(event.data)
        if (message.type === 'webrtc/answer' && message.value) {
          await rtcPeer.setRemoteDescription({ type: 'answer', sdp: message.value })
        } else if (message.type === 'webrtc/candidate' && message.value) {
          await rtcPeer.addIceCandidate({ candidate: message.value, sdpMid: message.sdpMid ?? null, sdpMLineIndex: message.sdpMLineIndex ?? 0 })
        }
      } catch { closeWebRTC() }
    }
    rtcSignal.onerror = () => closeWebRTC()
    rtcTimer = window.setTimeout(() => { if (videoMode.value !== 'webrtc') closeWebRTC() }, 8000)
  } catch {
    closeWebRTC()
  }
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/v1/retail-single/status')
    const payload = await res.json()
    status.value = payload.status || status.value
    connectWebSocket()
    if (status.value.stream_ready && status.value.webrtc_ready) connectWebRTC()
    else if (videoMode.value === 'webrtc' || rtcSignal) closeWebRTC()
  } catch {
    // Keep the customer-facing waiting state while the backend reconnects.
  }
}

function wsUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/retail-single/events`
}

function connectWebSocket() {
  if (ws || wsState.value === 'Connected') return
  wsState.value = 'Reconnecting'
  try {
    ws = new WebSocket(wsUrl())
  } catch {
    wsState.value = 'Disconnected'
    return
  }
  ws.onopen = () => { wsState.value = 'Connected' }
  ws.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'retail.single.status' && payload.data) {
        status.value = payload.data
      }
      if (payload.type === 'retail.single.status' && payload.payload) {
        status.value = payload.payload
      }
      if (payload.type === 'retail.single.inventory' && payload.payload?.inventory) {
        status.value = { ...status.value, inventory: payload.payload.inventory }
      }
      if (payload.type === 'retail.single.alert' && payload.payload) {
        status.value = { ...status.value, alert: payload.payload }
      }
    } catch {
      // ignore malformed payloads
    }
  }
  ws.onclose = () => {
    wsState.value = 'Disconnected'
    ws = null
    clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(() => {
      connectWebSocket()
    }, 1800)
  }
  ws.onerror = () => { wsState.value = 'Disconnected' }
}

function disconnectWebSocket() {
  clearTimeout(reconnectTimer)
  reconnectTimer = null
  if (ws) {
    ws.close()
    ws = null
  }
  wsState.value = 'Disconnected'
}

const shouldConnectVoice = computed(() => (
  props.switchStatus?.active_demo === 'single' ||
  props.switchStatus?.target_demo === 'single'
) && props.switchStatus?.state !== 'IDLE')

function voiceWsUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/sales-voice/stream`
}

function connectVoiceWebSocket() {
  if (!shouldConnectVoice.value || voiceWs) return
  voiceConnection.value = 'Reconnecting'
  try {
    voiceWs = new WebSocket(voiceWsUrl())
  } catch {
    voiceConnection.value = 'Disconnected'
    return
  }
  voiceWs.onopen = () => {
    voiceConnection.value = 'Connected'
    voicePhase.value = 'ready'
  }
  voiceWs.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'connection') {
        voiceConnection.value = payload.status === 'connected' ? 'Connected' : 'Reconnecting'
      }
      if (payload.type === 'turn' && payload.text) {
        voicePhase.value = 'transcribing'
        voiceQuestion.value = payload.text
        const turn = {
          id: `${Date.now()}-${Math.random()}`,
          turn_idx: payload.idx,
          question: payload.text,
          answer: null,
          answer_type: null,
          value: null,
          unit: null,
          items: [],
          metrics: null,
          status: null,
          data: null,
        }
        voiceTurns.value = [turn, ...voiceTurns.value].slice(0, 12)
      }
      if (payload.type === 'inventory.answer') {
        voicePhase.value = 'answered'
        inventoryAnswer.value = payload
        const answerText = englishVoiceText(payload.answer)
        const answerItems = (payload.items || []).map(englishVoiceText)
        const matchingIndex = voiceTurns.value.findIndex((turn) => (
          payload.turn_idx != null && turn.turn_idx === payload.turn_idx
        ))
        if (matchingIndex >= 0) {
          voiceTurns.value = voiceTurns.value.map((turn, index) => (
            index === matchingIndex
              ? {
                ...turn,
                answer: answerText,
                status: payload.status,
                data: payload.data,
                answer_type: payload.answer_type,
                value: payload.value,
                unit: payload.unit,
                items: answerItems,
                metrics: payload.metrics || null,
              }
              : turn
          ))
        } else {
          const question = String(payload.question || voiceQuestion.value || '').trim()
          voiceTurns.value = [{
            id: `${Date.now()}-${Math.random()}`,
            turn_idx: payload.turn_idx,
            question,
            answer: answerText,
            status: payload.status,
            data: payload.data,
            answer_type: payload.answer_type,
            value: payload.value,
            unit: payload.unit,
            items: answerItems,
            metrics: payload.metrics || null,
          }, ...voiceTurns.value].slice(0, 12)
        }
        speakAnswer(answerText)
      }
    } catch {
      // Ignore malformed Voice frames.
    }
  }
  voiceWs.onclose = () => {
    voiceConnection.value = 'Disconnected'
    voicePhase.value = 'waiting'
    voiceWs = null
    clearTimeout(voiceReconnectTimer)
    if (shouldConnectVoice.value) {
      voiceReconnectTimer = setTimeout(connectVoiceWebSocket, 2000)
    }
  }
  voiceWs.onerror = () => { voiceConnection.value = 'Disconnected' }
}

async function speakAnswer(text) {
  const normalized = String(text || '').trim()
  if (!normalized || voiceSpeaking.value) return
  voiceSpeaking.value = true
  voiceOutputStatus.value = 'Speaking with Amy (Piper)...'
  try {
    const response = await fetch('/api/v1/inventory-voice/tts/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: normalized }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
    voiceOutputStatus.value = 'Audio ready · Amy (Piper)'
  } catch (error) {
    voiceOutputStatus.value = `Speech playback failed: ${error?.message || 'Backend unavailable'}`
  } finally {
    voiceSpeaking.value = false
  }
}

function replayAnswer(text) {
  speakAnswer(text || inventoryAnswer.value?.answer)
}

function disconnectVoiceWebSocket() {
  clearTimeout(voiceReconnectTimer)
  voiceReconnectTimer = null
  if (voiceWs) {
    voiceWs.onclose = null
    voiceWs.close()
    voiceWs = null
  }
  voiceConnection.value = 'Disconnected'
  voicePhase.value = 'waiting'
}

watch(
  () => props.switchStatus,
  (value) => {
    if (value?.target_demo === 'single' || value?.active_demo === 'single') {
      connectWebSocket()
      refreshStatus()
    }
    if (value?.target_demo === 'single' || value?.active_demo === 'single') {
      connectVoiceWebSocket()
    } else if (value?.state === 'IDLE') {
      disconnectVoiceWebSocket()
      closeWebRTC()
    }
  },
  { deep: true, immediate: true },
)

onMounted(async () => {
  await refreshStatus()
  connectVoiceWebSocket()
  connectWebRTC()
  pollTimer = setInterval(refreshStatus, 900)
})

onBeforeUnmount(() => {
  clearInterval(pollTimer)
  disconnectWebSocket()
  disconnectVoiceWebSocket()
  closeWebRTC()
})
</script>

<template>
  <section class="stage-grid single-stage">
    <div class="hero-video">
      <div class="video-head">
        <span><Camera :size="16" /> Live Camera</span>
        <strong>{{ statusLabel }}</strong>
      </div>
      <div class="single-camera-scene" :style="{ aspectRatio: videoAspectRatio }">
        <video v-if="status.stream_ready && videoMode === 'webrtc'" ref="videoElement" class="single-camera-stream" autoplay playsinline muted aria-label="Retail single live stream" @loadedmetadata="updateVideoAspectRatio"></video>
        <img v-else-if="status.stream_ready" class="single-camera-stream" :src="streamUrl" alt="Retail single live stream" @load="updateVideoAspectRatio" />
        <div class="stream-badge">reCamera Live AI</div>
        <div class="stream-corner">
          <span>reCamera AI</span>
          <strong>{{ status.stream_ready ? (videoMode === 'webrtc' ? 'WebRTC Live' : 'MJPEG Fallback') : (status.data_initialized ? 'Products Ready' : 'Initializing') }}</strong>
        </div>
        <div v-if="!status.stream_ready" class="stream-waiting">
          <Camera :size="24" />
          <strong>{{ waitingMessage }}</strong>
        </div>
      </div>
    </div>

    <div class="voice-query-card">
      <div class="voice-query-head">
        <div>
          <p class="section-kicker">Voice Inventory Assistant</p>
          <strong>Ask about the live shelf in English</strong>
        </div>
        <div class="voice-query-examples" aria-label="Example questions">
          <span>How many bottles?</span>
          <span>How many types?</span>
          <span>What is missing?</span>
        </div>
        <span class="voice-query-status"><Mic2 :size="15" /> {{ voiceConnection }}</span>
      </div>
      <small class="voice-output-status"><Volume2 :size="12" /> {{ voiceOutputStatus }}</small>
      <p class="voice-query-prompt" aria-live="polite">{{ voicePrompt }}</p>
      <div v-if="voiceTurns.length" class="voice-history" aria-live="polite">
        <article v-for="turn in voiceTurns" :key="turn.id" class="voice-history-item">
          <div class="voice-query-line">
            <span>Heard</span>
            <strong>{{ turn.question }}</strong>
          </div>
          <div v-if="turn.answer" class="voice-answer-line">
            <div class="voice-answer-heading">
              <span>Answer</span>
              <button class="voice-replay" type="button" title="Read answer aloud" :disabled="voiceSpeaking" @click="replayAnswer(turn.answer)">
                <Volume2 :size="14" /> {{ voiceSpeaking ? 'Speaking' : 'Replay' }}
              </button>
            </div>
            <div v-if="turn.answer_type === 'number' && typeof turn.value === 'number'" class="voice-answer-number">
              <strong>{{ turn.value }}</strong>
              <span>{{ turn.unit }}</span>
            </div>
            <div v-else-if="turn.answer_type === 'overview' && turn.metrics" class="voice-answer-metrics">
              <span><strong>{{ turn.metrics.total_count }}</strong> bottles</span>
              <span><strong>{{ turn.metrics.type_count }}</strong> types</span>
              <span><strong>{{ turn.metrics.missing_count }}</strong> missing</span>
            </div>
            <div v-else-if="turn.answer_type === 'list' && turn.items?.length" class="voice-answer-items">
              <span v-for="item in turn.items" :key="item">{{ item }}</span>
            </div>
            <strong>{{ turn.answer }}</strong>
            <small>
              {{ turn.status === 'OK' ? 'Grounded in ReCamera data' : 'ReCamera data unavailable' }}
              <template v-if="turn.data?.data_age_seconds != null">
                · {{ Number(turn.data.data_age_seconds).toFixed(1) }}s old
              </template>
            </small>
          </div>
          <div v-else class="voice-query-empty">Analyzing live inventory...</div>
        </article>
      </div>
    </div>

    <aside class="inventory-card">
      <p class="section-kicker">Live AI View</p>
      <div class="camera-status-row">
        <Wifi :size="18" />
        <span>reCamera Detected Stream</span>
        <strong>{{ status.stream_ready ? 'STREAMING' : 'WAITING' }}</strong>
      </div>
      <div class="inventory-stats">
        <article><span>Present</span><strong>{{ status.present_count || 0 }}</strong></article>
        <article><span>Registered</span><strong>{{ status.registered_count || 0 }}</strong></article>
        <article class="danger"><span>Missing</span><strong>{{ status.missing_count || 0 }}</strong></article>
      </div>
      <div class="inventory-list">
        <div v-for="item in inventoryRows" :key="item.id" class="inventory-row">
          <span>{{ item.displayName }}</span>
          <strong>{{ item.count }}</strong>
          <em :class="{ warning: item.displayStatus !== 'In Stock' }">{{ item.displayStatus }}</em>
        </div>
        <div v-if="!inventoryRows.length" class="empty-data">
          {{ status.websocket_connected ? 'Waiting for initial product registration' : 'Waiting for reCamera product data' }}
        </div>
      </div>
      <div class="business-card">
        <p class="section-kicker">AI Result</p>
        <strong>{{ status.data_initialized ? `${status.present_count || 0} of ${status.registered_count || 0} registered products present` : 'Waiting for reCamera product registration' }}</strong>
        <span>Product identity and presence come directly from the reCamera AI WebSocket.</span>
      </div>
      <div v-if="alertRow" class="alert-card alert-card-real">
        <AlertTriangle :size="22" />
        <div>
          <span>Restock Required</span>
          <strong>Product: {{ alertProduct }}</strong>
          <strong v-if="alertChange">Stock Change: {{ alertChange }}</strong>
          <strong>Current Stock: {{ alertRow.current_count }}</strong>
          <strong>Status: {{ alertStatus }}</strong>
          <strong>Action: Please replenish this shelf.</strong>
        </div>
      </div>
      <div v-else-if="status.data_initialized" class="alert-card alert-card-idle">
        <Package :size="22" />
        <div>
          <span>Stock Monitoring</span>
          <strong>No restock action required</strong>
        </div>
      </div>
      <div class="business-card">
        <p class="section-kicker">Business Insight</p>
        <strong>{{ alertRow ? 'Dispatch restock for the missing product now' : status.data_initialized ? 'No immediate action required' : 'Business status will appear after registration' }}</strong>
        <span>Stock changes and alerts are derived only from consecutive real reCamera snapshots.</span>
      </div>
      <PerformancePanel :metrics="metrics" />
      <div class="layer-stack">
        <div v-for="layer in layerSummary" :key="layer.label" class="event-row">
          <span>{{ layer.label }}</span>
          <strong>{{ layer.value }}</strong>
        </div>
      </div>
    </aside>
  </section>
</template>
