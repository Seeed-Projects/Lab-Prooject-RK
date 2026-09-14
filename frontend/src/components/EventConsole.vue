<script setup>
import { computed } from 'vue'
import { Radio, Trash2 } from 'lucide-vue-next'

const props = defineProps({
  events: { type: Array, required: true },
  selectedId: { type: String, default: '' },
  connection: { type: String, required: true },
})

defineEmits(['clear'])

const visibleEvents = computed(() => props.events.filter((event) => !props.selectedId || event.demo_id === props.selectedId))

function time(value) {
  if (!value) return '--:--:--'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value))
}

function summary(event) {
  const payload = event.payload || {}
  if (event.type === 'asr.turn') return `Speaker ${payload.speaker ?? '—'}  ${payload.text || ''}`
  if (event.type === 'metrics.fps') return `FPS ${payload.fps ?? '—'}`
  if (event.type === 'video.progress') return `Processed frame ${payload.frame ?? '—'}`
  if (event.type === 'artifact.ready') return `${payload.name || 'Artifact'} · ${payload.size ?? 0} bytes`
  if (event.channel === 'log') return payload.line || ''
  if (event.type?.startsWith('hub.run.')) return `Run state changed to ${event.type.split('.').at(-1)}`
  return JSON.stringify(payload)
}
</script>

<template>
  <section class="event-panel" aria-label="Live events">
    <header class="panel-header">
      <div>
        <h2>Live Events</h2>
        <span>{{ selectedId || 'All Demos' }}</span>
      </div>
      <div class="panel-tools">
        <span class="stream-state" :class="connection">
          <Radio :size="15" />{{ connection === 'connected' ? 'Live' : connection === 'connecting' ? 'Connecting' : 'Disconnected' }}
        </span>
        <button class="icon-button" title="Clear events" aria-label="Clear events" @click="$emit('clear')">
          <Trash2 :size="17" />
        </button>
      </div>
    </header>

    <div class="event-list" aria-live="polite">
      <div v-if="!visibleEvents.length" class="event-empty">
        <Radio :size="24" />
        <span>Waiting for events</span>
      </div>
      <article v-for="event in visibleEvents" :key="event.event_id || `${event.run_id}-${event.seq}`" class="event-row">
        <time>{{ time(event.timestamp) }}</time>
        <span class="channel-tag" :class="`channel-${event.channel}`">{{ event.channel }}</span>
        <div class="event-content">
          <strong>{{ event.type }}</strong>
          <span>{{ summary(event) }}</span>
        </div>
        <span class="event-seq">#{{ event.seq }}</span>
      </article>
    </div>
  </section>
</template>
