<template>
  <div class="panel-frame" :class="{ 'panel-frame--row': direction === 'row' }">
    <slot />
  </div>
</template>

<script setup>
defineProps({
  direction: { type: String, default: 'column' },
  borderWidth: { type: Number, default: 36 },
})
</script>

<style scoped>
.panel-frame {
  border: v-bind('borderWidth + "px"') solid transparent;
  border-image-source: url('@/assets/panel-bg.png');
  border-image-slice: 9% fill;
  border-image-width: v-bind('borderWidth + "px"');
  border-image-outset: 0;
  border-image-repeat: round;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}

.panel-frame--row {
  flex-direction: row;
}
</style>
