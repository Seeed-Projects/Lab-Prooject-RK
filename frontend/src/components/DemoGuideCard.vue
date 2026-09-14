<script setup>
defineProps({
  text: { type: String, required: true },
})
</script>

<template>
  <section class="demo-guide-card">
    <div class="panel-title">
      <span>Demo Guide</span>
      <strong>Guide</strong>
    </div>
    <p>{{ text }}</p>
  </section>
</template>
