<script setup>
defineProps({
  items: { type: Array, required: true },
})
</script>

<template>
  <section class="hardware-status-card">
    <div class="panel-title">
      <span>Hardware Status Center</span>
      <strong>Live</strong>
    </div>
    <div class="hardware-grid">
      <article v-for="item in items" :key="item.label" class="hardware-item">
        <span class="hardware-icon">{{ item.icon }}</span>
        <div>
          <span>{{ item.label }}</span>
          <strong :class="item.statusClass">{{ item.status }}</strong>
          <small v-if="item.detail">{{ item.detail }}</small>
          <div v-if="item.substates?.length" class="hardware-substates">
            <span v-for="substate in item.substates" :key="substate.label">
              {{ substate.label }} <b :class="substate.value === 'Connected' || substate.value === 'Ready' || substate.value === 'Streaming' ? 'status-good' : 'status-bad'">{{ substate.value }}</b>
            </span>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>
