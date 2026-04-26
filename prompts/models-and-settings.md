# Models & Settings

Recommendations for generating Pioneer Square assets in Stable Diffusion (mage.space).

---

## Recommended Models

| Task | First choice | Backup |
|---|---|---|
| Robot sprites | PixelArt Diffusion | SDXL + pixel art LoRA |
| Background scene | DreamShaper XL | Juggernaut XL |
| UI tiles (pipes, gears, desk) | PixelArt Diffusion | SDXL + pixel art LoRA |

**PixelArt Diffusion** — search "pixel art" on mage.space and pick the most recently updated variant. Fine-tuned on pixel art; handles limited-palette constraint natively.

**DreamShaper XL** — best for the background scene. Takes Chrono Trigger / SNES environment prompts well. Generate at 1344×768 (SDXL's native 16:9 bucket).

**Pixel art LoRA** — if PixelArt Diffusion isn't available, stack a "pixel art" or "16-bit" LoRA on top of DreamShaper XL or base SDXL.

---

## Settings

### Sprites

| Setting | Value |
|---|---|
| Resolution | 512×512 |
| CFG scale | 7–9 |
| Steps | 25–35 |
| Sampler | DPM++ 2M Karras |

Generate at 512×512, then downscale to your target sprite size (e.g. 32×32) using **nearest-neighbor** resampling. Do not use bilinear or bicubic — they blur the pixels.

### Backgrounds

| Setting | Value |
|---|---|
| Resolution | 1344×768 |
| CFG scale | 6–8 |
| Steps | 30–40 |
| Sampler | DPM++ 2M Karras or Euler a |

---

## Universal Negative Prompt

Use this for everything:

```
blurry, anti-aliased, smooth edges, gradient, soft shading, photorealistic,
3D render, depth of field, bokeh, watermark, text, signature
```

---

## Transparent Backgrounds

SD cannot output PNG alpha natively. Add `white background` or `bright green background` to the prompt, then key it out in Photoshop, GIMP, or Aseprite.

---

## Workflow Tips

- **Sprites**: Generate a sprite sheet (4–6 poses in one image), then slice and export frames in **Aseprite**. Aseprite's palette swap feature lets you recolor the same robot sprite for all agent color variants without re-generating.
- **Backgrounds**: A high-res painterly background scaled down looks great behind CSS elements — it does not need to be strict pixel art.
- **Color matching**: The app palette is `#120900` (dark brown bg), `#e8aa00` (gold), `#00bbaa` (teal), `#cc5500` (copper). Mention these roles in prompts ("warm amber gold highlights", "teal glowing screens") to stay on-brand.
