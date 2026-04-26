# UI Element Prompts

Use **PixelArt Diffusion** (or SDXL + pixel art LoRA). Generate at **512×512**, CFG 7–9, 25–35 steps. Add `white background` to each; key out after.

---

## Gears

### Gear Set — Sprite Sheet

Four sizes on one sheet, designed to be rotated in CSS or canvas.

```
pixel art mechanical gear sprite sheet, SNES 16-bit style, white background,
four gears in a horizontal row, sizes 16px / 24px / 32px / 48px,
warm gold and copper metal, clearly defined teeth, center bolt hole,
subtle specular highlight on the metal surface, riveted details on larger gears,
clean pixel edges, suitable for rotation animation, no motion blur,
Chrono Trigger steampunk aesthetic, no text
```

### Individual Gears — Color Variants

Run once per gear color used in the app. Swap the color phrase:

```
pixel art mechanical gear, SNES 16-bit style, 48x48 pixels, white background,
single large gear, [COLOR PHRASE], clearly defined teeth, center bolt hole,
subtle highlight, clean pixel edges, suitable for CSS rotation animation
```

| Gear position | Color phrase |
|---|---|
| Large (g1) | `bright gold with dark brass shading` |
| Medium (g2) | `teal with dark cyan shading` |
| Small (g3) | `warm orange with burnt copper shading` |
| Medium (g4) | `sky blue with slate shading` |
| Small (g5) | `amber yellow with dark ochre shading` |

---

## Pipes

### Pipe Tile Set

Seamlessly connectable tiles for building the ceiling pipe layout.

```
pixel art pipe tile set, SNES 16-bit style, white background, 6 tiles in a
2x3 grid, each tile 32x32 pixels,

tile 1 - horizontal straight pipe,
tile 2 - vertical straight pipe,
tile 3 - corner bend top-left,
tile 4 - corner bend top-right,
tile 5 - T-junction,
tile 6 - end cap with valve wheel,

warm copper and brass metal, riveted joints, subtle specular highlight on
the top surface, slightly darker underside for depth, clean pixel edges,
Chrono Trigger industrial style, no text
```

### Pipe Joint / Flange

The bolted circular joint where pipes meet.

```
pixel art pipe flange / bolted joint, SNES 16-bit style, 18x18 pixels,
white background, circular brass flange with 4 bolts at cardinal points,
warm gold center with darker outer ring, subtle specular highlight,
clean pixel edges, overhead view, steampunk aesthetic
```

---

## Workstation

### Full Workstation — Desk + Monitor

One sprite covering the monitor and desk together, per station.

```
pixel art computer workstation, SNES 16-bit style, 64x64 pixels, white background,
slight 3/4 front-facing perspective, dark walnut wood desk with copper metal
trim and riveted corners, old CRT monitor on the desk with a glowing teal
scan-line screen, copper-colored keyboard in front, warm amber desk lamp
on the left side, clean pixel edges, Lucca's workshop aesthetic, no characters,
no text on screen
```

### Monitor Screen States

Small 48×36 pixel screen inserts to swap into the monitor frame.

```
pixel art CRT monitor screen sprite sheet, SNES 16-bit style, white background,
5 screen states in a horizontal row, each 48x36 pixels,

screen 1 - active: teal green scan lines scrolling, bright,
screen 2 - thinking: pulsing blue cursor on dark background,
screen 3 - idle: dim grey dashes on dark screen, low brightness,
screen 4 - error: red flickering static with warning symbol,
screen 5 - off: pure dark screen with slight glass reflection,

clean pixel edges, no text, no characters
```

---

## Conveyor Belt

### Belt Section — Tileable

```
pixel art conveyor belt section, SNES 16-bit style, 256x32 pixels, white background,
horizontal conveyor belt viewed slightly from above, seamlessly tileable width,
dark brown rubber belt surface with repeating grip ridges, copper metal side
rails with rivets, warm metal sheen on the rails, subtle shadow under the belt,
Chrono Trigger factory aesthetic, no items on belt, no characters
```

### Belt Items — Sprite Sheet

Items that ride the conveyor. Six items on one sheet.

```
pixel art conveyor item sprite sheet, SNES 16-bit style, white background,
6 small items in a horizontal row, each 16x16 pixels,

item 1 - hex bolt (copper colored),
item 2 - small gear (gold),
item 3 - gem / crystal (teal glowing),
item 4 - gold coin (warm amber),
item 5 - star badge (bright yellow),
item 6 - wrench (warm grey metal),

clean pixel edges, simple readable shapes, warm color palette, no text
```

---

## Furnace

```
pixel art industrial furnace unit, SNES 16-bit style, 64x96 pixels, white background,
front-facing view, dark iron body with copper trim and rivets, large round
furnace door in the center with orange-red fire glow visible through the window,
small circular pressure gauge on the right with a red needle,
chimney pipe extending from the top, warm orange ambient glow around the door,
Chrono Trigger industrial style, no characters, no text
```

---

## Ticker Tape Banner

```
pixel art ticker tape display strip, SNES 16-bit style, 512x28 pixels,
white background, horizontal LED dot-matrix display panel, dark housing
with copper trim, warm amber glowing dot-matrix characters in the center
(show placeholder dots, no real text), slight scanline effect on the display,
clean pixel edges, factory aesthetic
```
