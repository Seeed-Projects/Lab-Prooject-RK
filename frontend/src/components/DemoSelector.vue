<script setup>
defineProps({
  modelValue: { type: String, required: true },
  options: { type: Array, required: true },
})

defineEmits(['update:modelValue'])
</script>

<template>
  <label class="demo-selector">
    <span>Demo</span>
    <select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="option in options" :key="option.id" :value="option.id">
        {{ option.label }}
      </option>
    </select>
  </label>
</template>
