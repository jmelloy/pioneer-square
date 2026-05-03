<template>
  <div class="workshop-panel">
    <div class="workshop-frame">
      <video class="workshop-img" autoplay loop muted playsinline poster="@/assets/workshop.jpg">
        <source src="@/assets/workshop.mp4" type="video/mp4" />
        <img src="@/assets/workshop.jpg" alt="Pioneer Square Workshop" />
      </video>
      <div class="workshop-label">PIONEER SQUARE WORKSHOP</div>
    </div>
    <div class="workshop-lore">
      A multi-agent coding workshop where AI engineers collaborate in real-time.
    </div>
  </div>
</template>

<script setup lang="ts"></script>

<style scoped>
.workshop-panel {
  flex: 0 0 280px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: sticky;
  top: 40px;
  align-self: flex-start;
}

.workshop-frame {
  border: 3px solid var(--color-brass-dark);
  box-shadow:
    0 0 0 1px #000,
    inset 0 0 0 1px #000,
    0 0 24px rgba(232, 170, 0, 0.15);
  position: relative;
  overflow: hidden;
  image-rendering: pixelated;
  background: #120900;
}

.workshop-frame:hover {
  border-color: var(--color-brass);
  box-shadow:
    0 0 0 1px #000,
    inset 0 0 0 1px #000,
    0 0 32px rgba(232, 170, 0, 0.3);
}

.workshop-img {
  display: block;
  width: 100%;
  height: auto;
  image-rendering: pixelated;
}

.workshop-label {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  background: linear-gradient(transparent, rgba(18, 9, 0, 0.92));
  font-family: var(--font-pixel);
  font-size: 6px;
  color: var(--color-brass-light);
  letter-spacing: 1px;
  text-align: center;
  padding: 20px 8px 8px;
  text-shadow: 0 0 8px rgba(255, 214, 68, 0.6);
}

.workshop-lore {
  font-size: 10px;
  color: var(--color-text-dim);
  line-height: 1.7;
  letter-spacing: 0.5px;
  border-left: 2px solid var(--color-brass-dark);
  padding-left: 10px;
}

@media (max-width: 900px) {
  .workshop-panel {
    flex: none;
    width: 100%;
    position: static;
    flex-direction: row;
    align-items: flex-start;
    gap: 16px;
  }

  .workshop-frame {
    flex: 0 0 180px;
    max-width: 180px;
  }

  .workshop-lore {
    flex: 1;
    align-self: center;
  }
}

@media (max-width: 600px) {
  .workshop-frame {
    flex: 0 0 110px;
    max-width: 110px;
  }

  .workshop-lore {
    font-size: 9px;
  }
}
</style>
