<template>
  <div class="layout-grid"><slot /></div>
</template>

<style scoped>
/* 3 columns on desktop, stacked on narrow screens. Children that need the
   full width can use `grid-column: 1 / -1`. */
.layout-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  grid-auto-rows: minmax(0, 1fr);
  gap: 12px;
  padding: 12px;
  min-height: 0;
}
@media (max-width: 1100px) {
  .layout-grid {
    grid-template-columns: minmax(0, 1fr);
    grid-auto-rows: minmax(240px, auto);
  }
}
</style>
