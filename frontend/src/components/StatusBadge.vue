<script setup>
import { computed } from 'vue'

const props = defineProps({ status: { type: String, default: 'stopped' } })

const label = computed(() => ({
  running: 'Running',
  starting: 'Starting',
  stopping: 'Stopping',
  stopped: 'Stopped',
  completed: 'Completed',
  failed: 'Unavailable',
  unknown: 'Unknown',
})[props.status] || props.status)
</script>

<template>
  <span class="status-badge" :class="`status-${status}`">
    <span class="status-dot" aria-hidden="true"></span>
    {{ label }}
  </span>
</template>
