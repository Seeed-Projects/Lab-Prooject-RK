<script setup>
import { CircleStop, Mic2, Play, ScanLine } from 'lucide-vue-next'
import StatusBadge from './StatusBadge.vue'

defineProps({
  demos: { type: Array, required: true },
  selectedId: { type: String, default: '' },
  pending: { type: Object, required: true },
})

defineEmits(['select', 'start', 'stop'])

function iconFor(type) {
  return type.includes('audio') ? Mic2 : ScanLine
}
</script>

<template>
  <div class="table-wrap">
    <table class="demo-table">
      <thead>
        <tr>
          <th>Demo</th>
          <th>Type</th>
          <th>Status</th>
          <th>Run ID</th>
          <th class="action-column">Control</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="demo in demos"
          :key="demo.id"
          :class="{ selected: selectedId === demo.id }"
          @click="$emit('select', demo.id)"
        >
          <td>
            <div class="demo-identity">
              <component :is="iconFor(demo.type)" :size="19" aria-hidden="true" />
              <div>
                <strong>{{ demo.displayName }}</strong>
                <span>{{ demo.description }}</span>
              </div>
            </div>
          </td>
          <td><span class="type-label">{{ demo.typeLabel }}</span></td>
          <td><StatusBadge :status="demo.status" /></td>
          <td><code>{{ demo.run?.run_id ? demo.run.run_id.slice(0, 16) : '—' }}</code></td>
          <td class="actions" @click.stop>
            <button
              v-if="!demo.isActive"
              class="icon-action start-action"
              :disabled="pending[demo.id]"
              :title="`Start ${demo.displayName}`"
              :aria-label="`Start ${demo.displayName}`"
              @click="$emit('start', demo)"
            >
              <Play :size="18" fill="currentColor" />
            </button>
            <button
              v-else
              class="icon-action stop-action"
              :disabled="pending[demo.id] || demo.status === 'stopping'"
              :title="`Stop ${demo.displayName}`"
              :aria-label="`Stop ${demo.displayName}`"
              @click="$emit('stop', demo)"
            >
              <CircleStop :size="19" />
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
