# Robot Sprite Prompts

Use **PixelArt Diffusion** (or SDXL + pixel art LoRA). Generate at **512×512**, CFG 7–9, 25–35 steps, DPM++ 2M Karras. Downscale to final size with **nearest-neighbor** resampling.

Add `white background` to each prompt, then key out in Aseprite or GIMP.

---

## Worker Robot — Sprite Sheet (All States)

One image containing all five animation states. Slice into frames after generation.

```
pixel art robot character sprite sheet, SNES 16-bit style, 5 frames in a
single horizontal row on white background, small boxy friendly robot
32 pixels tall, square head with two glowing square eyes, rectangular
torso with LED indicator on chest, short arms and blocky legs,
warm copper and dark brown metal body with riveted panel lines,

frame 1 - idle: standing upright, arms at sides,
frame 2 - thinking: head tilted 15 degrees, hand raised to chin, thought bubble,
frame 3 - working: slight crouch, both arms forward, leaning in,
frame 4 - busy: arms raised and spread wide, energetic pose,
frame 5 - error: hunched over, X eyes, small lightning bolt above head,

clean pixel edges, limited warm color palette, Chrono Trigger character style,
white background, no text, no labels
```

---

## Worker Robot — Single Idle Pose

Use this when you just need one clean frame to recolor per agent.

```
pixel art robot character, SNES 16-bit style, 32x32 pixels, white background,
small boxy friendly worker robot, standing idle pose, square head with
two glowing square eyes, rectangular torso with round LED on chest,
short arms at sides, blocky legs, warm copper and dark brown metal body,
riveted panel lines, clean pixel edges, limited color palette,
transparent-ready white background, Chrono Trigger NPC style
```

### Color Variants

Run the single idle prompt above and swap in one color description per agent.
Replace the body color phrase for each:

| Agent color | Phrase to add |
|---|---|
| Gold | `bright gold and dark brass body, amber LED glow` |
| Teal | `teal and dark navy body, cyan LED glow` |
| Orange | `warm orange and burnt sienna body, orange LED glow` |
| Sky blue | `sky blue and slate body, blue LED glow` |
| Red | `crimson red and dark maroon body, red LED glow` |
| Lime | `lime green and dark olive body, green LED glow` |
| Copper | `copper and rust body, orange-amber LED glow` |
| Amber | `amber yellow and deep brown body, yellow LED glow` |

---

## Foreman Robot

Larger, more authoritative. Should read as a leader at a glance.

```
pixel art robot character, SNES 16-bit style, 48x64 pixels, white background,
stocky authoritative robot wearing a small gold crown on its head,
square head slightly larger than a worker robot, warm burnished gold
and dark copper body, glowing amber chest plate with ornate panel design,
confident standing pose with one hand on hip, the other holding a clipboard,
warm amber glowing square eyes, sturdy wide-set legs, riveted armor panels,
clean pixel edges, Chrono Trigger boss NPC style, no text
```

---

## Robot — Reaction Expressions

Small overlay sprites to place above robots for state indicators.

```
pixel art expression bubble sprite sheet, SNES 16-bit style, white background,
5 small icons in a horizontal row, each 16x16 pixels,

icon 1 - thinking: white speech bubble with three dots "...",
icon 2 - working: yellow star burst / sparkle,
icon 3 - busy: orange double exclamation mark "!!",
icon 4 - error: red lightning bolt,
icon 5 - idle: small grey "zzz" sleep indicator,

clean pixel edges, bold readable shapes, limited color palette, no text labels
```

---

## Notes

- If the sprite sheet prompt produces inconsistent robot designs across frames, generate each state separately using the single idle prompt as a base and describe only the pose change.
- Use Aseprite's **Edit > Replace Color** or palette swap to recolor variants — much faster than re-generating each color.
- Keep the robots at **32×32** to match the current CSS avatar footprint (28px wide, ~64px tall including label).
