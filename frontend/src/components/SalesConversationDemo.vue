<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Mic2, Sparkles } from 'lucide-vue-next'
import PerformancePanel from './PerformancePanel.vue'
import { defaultVoicePresentation, normalizeVoiceAnalysis } from '../voicePresentation.js'

const props = defineProps({
  switchStatus: { type: Object, default: () => ({}) },
})

const connection = ref('Disconnected')
const runtimeStatus = ref(null)
const turns = ref([])
const summarizing = ref(false)
const summaryStatus = ref('Waiting')
const shouldConnect = computed(() => (
  props.switchStatus?.active_demo === 'voice' ||
  props.switchStatus?.target_demo === 'voice'
) && props.switchStatus?.state !== 'IDLE')
const statusText = computed(() => {
  if (runtimeStatus.value?.runtime?.phase === 'DEGRADED_USB'
    || runtimeStatus.value?.runtime?.state === 'DEGRADED_USB') return 'Voice device temporarily unavailable'
  if (!runtimeStatus.value?.device_ready) return 'Connect ReSpeaker via USB'
  if (!runtimeStatus.value?.running || !runtimeStatus.value?.service_ready) return 'Preparing Voice Assistant...'
  if (turns.value.length) return 'Conversation Active'
  return 'Ready - Start talking'
})
const micStatus = computed(() => runtimeStatus.value?.device_ready ? 'Connected' : 'Connect ReSpeaker via USB')
const asrStatus = computed(() => runtimeStatus.value?.model_health?.stt_ready ? 'Qwen3-ASR Loaded' : 'Loading Qwen3-ASR...')
const llmStatus = computed(() => runtimeStatus.value?.model_health?.llm_ready ? 'RKLLM Loaded' : 'Loading RKLLM...')
const speakerStatus = computed(() => runtimeStatus.value?.ai_ready ? 'Voice detected' : 'Waiting for speech')
const conversationStatus = computed(() => turns.value.length ? 'Conversation active' : runtimeStatus.value?.service_ready ? 'Ready - Start talking' : 'Preparing')
const serviceStatus = computed(() => isDegradedUsb.value
  ? 'Recovering audio interface...'
  : runtimeStatus.value?.running
  ? runtimeStatus.value?.service_ready ? 'Ready' : 'Initializing'
  : 'Starting')
const isDegradedUsb = computed(() => runtimeStatus.value?.runtime?.phase === 'DEGRADED_USB'
  || runtimeStatus.value?.runtime?.state === 'DEGRADED_USB')
const presentation = ref({ ...defaultVoicePresentation })
let ws = null
let reconnectTimer = null
let statusTimer = null
const metrics = computed(() => [
  { label: 'ASR Service', value: runtimeStatus.value?.service_ready ? 'Ready' : 'Initializing' },
  { label: 'STT Model', value: asrStatus.value },
  { label: 'LLM Model', value: llmStatus.value },
  { label: 'Live Stream', value: connection.value },
  { label: 'Conversation Turns', value: `${turns.value.length}` },
  { label: 'ReSpeaker', value: runtimeStatus.value?.device_ready ? 'Detected' : 'Checking' },
])

function wsUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/sales-voice/stream`
}

function connect() {
  if (!shouldConnect.value) return
  if (ws) ws.close()
  connection.value = 'Reconnecting'
  ws = new WebSocket(wsUrl())
  ws.onopen = () => { connection.value = 'Connected' }
  ws.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'connection' && payload.status) {
        connection.value = payload.status === 'connected'
          ? 'Connected'
          : payload.status === 'reconnecting' ? 'Reconnecting' : 'Disconnected'
        return
      }
      if (payload.type === 'turn') {
        turns.value = [{
          id: `${Date.now()}-${Math.random()}`,
          speaker: payload.speaker ?? '—',
          text: payload.text || '',
          timestamp: payload.timestamp || payload.start || new Date().toLocaleTimeString(),
        }, ...turns.value].slice(0, 10)
      }
      if (payload.type === 'summary') {
        summaryStatus.value = payload.status || 'Received'
        presentation.value = normalizeVoiceAnalysis(
          payload.analysis ?? payload.summary ?? payload.text ?? payload,
        )
      }
    } catch {
      // Ignore non-JSON frames from upstream.
    }
  }
  ws.onclose = () => {
    connection.value = 'Disconnected'
    if (shouldConnect.value) reconnectTimer = setTimeout(connect, 2000)
  }
  ws.onerror = () => { connection.value = 'Disconnected' }
}

function disconnect() {
  clearTimeout(reconnectTimer)
  reconnectTimer = null
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
  connection.value = 'Disconnected'
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/v1/sales-voice/status')
    const payload = await res.json()
    runtimeStatus.value = { ...(payload.status || {}), runtime: payload.runtime || null }
  } catch {
    runtimeStatus.value = null
  }
}

async function generateSummary() {
  if (!turns.value.length) return
  summarizing.value = true
  summaryStatus.value = 'Generating'
  try {
    const res = await fetch('/api/v1/sales-voice/summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ turns: turns.value.slice(0, 10).reverse() }),
    })
    const payload = await res.json()
    summaryStatus.value = payload.status === 'ok' ? 'RKLLM Ready' : 'Fallback'
    presentation.value = normalizeVoiceAnalysis(
      payload.analysis ?? payload.summary ?? payload.text ?? payload,
    )
  } catch (error) {
    summaryStatus.value = 'Fallback'
    presentation.value = {
      summary: 'The conversation was captured, but detailed AI analysis is temporarily unavailable.',
      intent: 'Pending AI analysis',
      recommendation: 'Review the latest transcript and follow up with the customer.',
    }
  } finally {
    summarizing.value = false
  }
}

onMounted(() => {
  refreshStatus()
  statusTimer = setInterval(refreshStatus, 1700)
})

watch(shouldConnect, (enabled) => {
  if (enabled) {
    connect()
    refreshStatus()
  } else {
    disconnect()
  }
}, { immediate: true })

onBeforeUnmount(() => {
  disconnect()
  clearInterval(statusTimer)
})
</script>

<template>
  <section class="stage-grid voice-stage">
    <div class="conversation-card">
      <div class="voice-head">
        <div>
          <p class="section-kicker">Live AI View</p>
          <h2>Sales Voice Assistant</h2>
        </div>
        <div class="voice-controls">
          <span class="connection-pill" :class="statusText.toLowerCase().replace(/\s+/g, '-')">{{ statusText }}</span>
          <div class="mic-pulse"><Mic2 :size="30" /><span></span></div>
        </div>
      </div>
      <div class="status-strip">
        <div class="event-row"><span>Microphone Status</span><strong>{{ micStatus }}</strong></div>
        <div class="event-row"><span>ASR Status</span><strong>{{ asrStatus }}</strong></div>
        <div class="event-row"><span>LLM Status</span><strong>{{ llmStatus }}</strong></div>
        <div class="event-row"><span>Speaker Detection Status</span><strong>{{ speakerStatus }}</strong></div>
        <div class="event-row"><span>Conversation Status</span><strong>{{ conversationStatus }}</strong></div>
      </div>
      <div class="turn-list">
        <article v-if="!turns.length" class="speech-turn empty-turn">
          <span>{{ serviceStatus }}</span>
          <p>{{ statusText }}</p>
        </article>
        <article v-for="turn in turns" :key="turn.id" class="speech-turn" :class="`speaker-${turn.speaker}`">
          <span>Speaker {{ turn.speaker }}</span>
          <p>{{ turn.text }}</p>
        </article>
      </div>
    </div>
    <aside class="summary-stack">
      <button class="language-button primary-action" :disabled="!turns.length || summarizing" @click="generateSummary">
        <Sparkles :size="16" /> {{ summarizing ? 'Generating AI Summary...' : 'Generate AI Summary' }}
      </button>
      <div class="summary-card">
        <p class="section-kicker">AI Sales Assistant</p>
        <article><span>Conversation Summary</span><strong>{{ presentation.summary }}</strong></article>
        <article><span>Customer Intent</span><strong>{{ presentation.intent }}</strong></article>
        <article><span>Recommendation</span><strong>{{ presentation.recommendation }}</strong></article>
      </div>
      <PerformancePanel :metrics="metrics" />
    </aside>
  </section>
</template>
