<script setup>
defineProps({
  streams: { type: Array, required: true },
})
</script>

<template>
  <section class="video-grid">
    <article v-for="stream in streams" :key="stream.id" class="video-tile">
      <div class="video-surface">
        <div
          v-for="box in stream.boxes"
          :key="box.label"
          class="detect-box"
          :style="{ left: box.x + '%', top: box.y + '%', width: box.w + '%', height: box.h + '%' }"
        >
          <span>{{ box.label }}</span>
        </div>
        <div class="scan-line"></div>
      </div>
      <footer>
        <span>{{ stream.name }}</span>
        <strong>{{ stream.fps }} FPS</strong>
      </footer>
    </article>
  </section>
</template>
