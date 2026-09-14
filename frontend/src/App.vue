<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import DemoSelector from './components/DemoSelector.vue'
import DemoGuideCard from './components/DemoGuideCard.vue'
import HardwareStatusCard from './components/HardwareStatusCard.vue'
import MultiCameraDemo from './components/MultiCameraDemo.vue'
import SalesConversationDemo from './components/SalesConversationDemo.vue'
import SingleCameraDemo from './components/SingleCameraDemo.vue'
import seeedLogo from './assets/seeed-logo.png'

const demos = [
  { id: 'single', label: 'Smart Shelf Monitoring (Single camera)', component: SingleCameraDemo },
  { id: 'multi', label: 'Smart Shelf Monitoring (Multi-camera)', component: MultiCameraDemo },
  { id: 'voice', label: 'AI Sales Conversation Analysis', component: SalesConversationDemo },
]

const initialDemo = new URLSearchParams(window.location.search).get('demo')
const selectedDemo = ref(demos.some((demo) => demo.id === initialDemo) ? initialDemo : 'single')
const displayDemo = ref(selectedDemo.value)
const displayDemoMeta = computed(() => demos.find((demo) => demo.id === displayDemo.value) || demos[0])
const activeComponent = computed(() => displayDemoMeta.value.component)
const showcaseStatus = ref({
  active_demo: null,
  target_demo: null,
  state: 'IDLE',
  generation: 0,
  error: null,
  started_at: null,
})
const transitionStates = new Set(['IDLE', 'SWITCHING', 'STOPPING', 'STARTING', 'READY_CHECK'])
const displayState = computed(() => {
  const state = showcaseStatus.value.state
  if (state === 'RUNNING' && showcaseStatus.value.active_demo === displayDemo.value) return 'RUNNING'
  if (displayDemo.value === 'single' && state === 'ERROR') return 'WAITING FOR DEVICE'
  if (displayDemo.value === 'voice' && state === 'ERROR') return 'INITIALIZING'
  if (transitionStates.has(state)) return 'INITIALIZING'
  return state === 'ERROR' ? 'ERROR' : 'INITIALIZING'
})
const displayStateClass = computed(() => displayState.value.toLowerCase().replaceAll(' ', '-'))
const displayMessage = computed(() => {
  if (displayState.value === 'RUNNING') return 'Live AI experience ready'
  if (displayState.value === 'WAITING FOR DEVICE') return 'Waiting for Camera Connection'
  if (displayDemo.value === 'voice') return 'Voice Service Initializing'
  if (displayDemo.value === 'single') return 'Preparing camera experience'
  if (displayState.value === 'ERROR') return 'AI service temporarily unavailable'
  return 'Preparing live AI view'
})
const demoGuideById = {
  single: 'Pick up a bottle to trigger the AI detection. The system will identify the stock change, detect the shortage, and provide a restocking recommendation.',
  multi: 'Select this demo to automatically start multi-camera AI inference. The system analyzes multiple video streams simultaneously and detects shelf conditions in real time.',
  voice: '1. Automatically summarize the conversation content. 2. Have a casual conversation with your friend and experience real-time AI speech analysis.',
}
const hardwareItems = ref([
  { icon: '🎤', label: 'ReSpeaker', status: 'Checking', statusClass: 'status-pending', detail: 'USB audio device' },
  { icon: '📷', label: 'reCamera', status: 'Checking', statusClass: 'status-pending', detail: 'Network / RTSP / video stream' },
  { icon: '🧠', label: 'RK3588 AI Engine', status: 'Checking', statusClass: 'status-pending', detail: 'Local RKNN runtime' },
])
const guideText = computed(() => demoGuideById[displayDemo.value] || demoGuideById.single)
let pollTimer = null
let hardwareTimer = null
let requestToken = 0
let latestGeneration = 0
let pendingDemo = null
let showcaseRequestInFlight = false
let hardwareRequestInFlight = false
let syncingFromBackend = false

function applyShowcaseStatus(status) {
  const generation = Number(status?.generation || 0)
  const currentStartedAt = Date.parse(showcaseStatus.value?.started_at || '') || 0
  const incomingStartedAt = Date.parse(status?.started_at || '') || 0
  const startsNewBackendEpoch = incomingStartedAt > currentStartedAt
  if (generation < latestGeneration && !startsNewBackendEpoch) return
  latestGeneration = generation
  showcaseStatus.value = status

  const backendDemo = status.target_demo || status.active_demo
  if (pendingDemo) {
    if (backendDemo === pendingDemo) pendingDemo = null
    else return
  }
  if (backendDemo && demos.some((demo) => demo.id === backendDemo)) {
    syncingFromBackend = true
    displayDemo.value = backendDemo
    selectedDemo.value = backendDemo
    window.requestAnimationFrame(() => { syncingFromBackend = false })
  }
}

async function switchDemo(demoId) {
  const token = ++requestToken
  pendingDemo = demoId
  displayDemo.value = demoId
  showcaseStatus.value = {
    ...showcaseStatus.value,
    target_demo: demoId,
    state: 'SWITCHING',
    error: null,
  }
  try {
    const res = await fetch('/api/v1/showcase/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ demo: demoId, language: 'en' }),
    })
    const payload = await res.json()
    if (token !== requestToken) return
    if (!res.ok) throw new Error('Unable to switch demo')
    if (payload.showcase) applyShowcaseStatus(payload.showcase)
    pollShowcaseStatus()
  } catch (error) {
    if (token !== requestToken) return
    pendingDemo = null
    showcaseStatus.value = { ...showcaseStatus.value, state: 'ERROR', error: String(error) }
  }
}

async function pollShowcaseStatus() {
  if (showcaseRequestInFlight) return showcaseStatus.value
  showcaseRequestInFlight = true
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 2000)
  try {
    const res = await fetch('/api/v1/showcase/status', { signal: controller.signal })
    const payload = await res.json()
    if (payload.showcase) {
      applyShowcaseStatus(payload.showcase)
      if (!pendingDemo && !payload.showcase.active_demo && !payload.showcase.target_demo) {
        displayDemo.value = selectedDemo.value
      }
      return payload.showcase
    }
  } catch {
    // Keep the selected experience visible while the backend reconnects.
  } finally {
    window.clearTimeout(timeout)
    showcaseRequestInFlight = false
  }
  return showcaseStatus.value
}

async function refreshHardwareStatus() {
  if (hardwareRequestInFlight) return
  hardwareRequestInFlight = true
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 3000)
  try {
    const res = await fetch('/api/v1/hardware/status', { signal: controller.signal })
    const payload = await res.json()
    hardwareItems.value = (payload.status?.items || []).map((item) => ({
      icon: item.name === 'ReSpeaker' ? '🎤' : item.name === 'reCamera' ? '📷' : '🧠',
      label: item.name,
      status: item.state,
      statusClass: item.connected ? 'status-good' : item.state === 'Connecting' ? 'status-warn' : 'status-bad',
      detail: item.detected_by,
      substates: item.name === 'reCamera' ? [
        { label: 'Network', value: item.details?.network_connected ? 'Connected' : 'Disconnected' },
        { label: 'RTSP', value: item.details?.rtsp_port_open ? 'Ready' : 'Waiting' },
        { label: 'Video', value: item.details?.stream_ready ? 'Streaming' : 'Waiting' },
      ] : [],
    }))
  } catch {
    // Keep the last detector result; a request timeout is not a hardware state.
  } finally {
    window.clearTimeout(timeout)
    hardwareRequestInFlight = false
  }
}

watch(selectedDemo, (demoId, previous) => {
  if (syncingFromBackend) return
  if (!previous || demoId === previous) return
  switchDemo(demoId)
})

onMounted(async () => {
  const current = await pollShowcaseStatus()
  // A fresh backend starts with no showcase selected. Activate the first
  // (single-camera) experience so its live inventory and Voice Assistant are
  // available without requiring a manual dropdown change.
  if (current?.state === 'IDLE' && !current.active_demo && !current.target_demo) {
    switchDemo(selectedDemo.value)
  }
  refreshHardwareStatus()
  pollTimer = setInterval(pollShowcaseStatus, 800)
  hardwareTimer = setInterval(refreshHardwareStatus, 2500)
})

onBeforeUnmount(() => {
  clearInterval(pollTimer)
  clearInterval(hardwareTimer)
})
</script>

<template>
  <main class="terminal-shell">
    <header class="terminal-header">
      <img class="seeed-logo" :src="seeedLogo" alt="Seeed Studio" />
      <div class="headline">
        <p>AI Sensing Demo Center</p>
        <h1>How to add Cutomized AI to your bussiness with ease?</h1>
      </div>
      <DemoSelector v-model="selectedDemo" :options="demos" />
    </header>

    <HardwareStatusCard :items="hardwareItems" />
    <DemoGuideCard :text="guideText" />

    <section class="demo-title">
      <span>Live AI Experience</span>
      <strong>{{ displayDemoMeta.label }}</strong>
      <em class="exclusive-status" :class="displayStateClass">
        {{ displayState }} · {{ displayMessage }}
      </em>
    </section>

    <component
      :is="activeComponent"
      :key="`${displayDemo}-${showcaseStatus.generation}`"
      :switch-status="showcaseStatus"
    />
  </main>
</template>
