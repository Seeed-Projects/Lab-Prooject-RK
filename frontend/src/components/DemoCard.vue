<script setup>
import { ArrowRight, Cpu, Mic2, ScanLine, Video } from 'lucide-vue-next'

const props = defineProps({
  demo: { type: Object, required: true },
})

defineEmits(['launch'])

const icons = { sound: Mic2, camera: ScanLine, multi: Video }
const Icon = icons[props.demo.kind] || Cpu
</script>

<template>
  <article class="showcase-card">
    <div class="showcase-card__media">
      <div class="glow-ring">
        <component :is="Icon" :size="38" />
      </div>
      <span>{{ demo.device }}</span>
    </div>
    <div class="showcase-card__body">
      <p class="eyebrow">{{ demo.category }}</p>
      <h2>{{ demo.title }}</h2>
      <h3>{{ demo.subtitle }}</h3>
      <ul>
        <li v-for="feature in demo.features" :key="feature">{{ feature }}</li>
      </ul>
      <button class="launch-button" @click="$emit('launch', demo.id)">
        Launch Demo
        <ArrowRight :size="18" />
      </button>
    </div>
  </article>
</template>
