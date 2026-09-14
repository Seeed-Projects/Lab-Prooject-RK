<script setup>
defineProps({
  title: { type: String, default: 'Performance' },
  metrics: { type: Array, required: true },
})
</script>

<template>
  <aside class="performance-panel">
    <div class="panel-title">
      <span>{{ title }}</span>
      <strong>Live</strong>
    </div>
    <div class="metric-list">
      <div v-for="metric in metrics" :key="metric.label" class="metric-row">
        <span>{{ metric.label }}</span>
        <strong>{{ metric.value }}</strong>
      </div>
    </div>
  </aside>
</template>
