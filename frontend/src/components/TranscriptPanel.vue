<script setup>
defineProps({
  turns: { type: Array, required: true },
})
</script>

<template>
  <section class="transcript-panel">
    <div class="section-label">Live Conversation</div>
    <div class="transcript-list">
      <article v-for="turn in turns" :key="turn.id" class="turn" :class="`speaker-${turn.speaker}`">
        <span>Speaker {{ turn.speaker }}</span>
        <p>{{ turn.text }}</p>
      </article>
    </div>
  </section>
</template>
