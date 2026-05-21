#!/usr/bin/env python3
"""Overlay the factory-floor layout on the background image for tuning.

The walkable polygon, points of interest (with their gravitation boxes) and
work-station slots all come from
``frontend/src/components/factory-layout.json`` — the same file FactoryFloor.vue
reads — so editing that JSON and re-running this script previews exactly what
the app will render.

Usage:
    pip install Pillow
    python scripts/preview_factory_layout.py            # writes factory-layout-preview.png
    python scripts/preview_factory_layout.py --robots 6 # also scatter sample robots
    python scripts/preview_factory_layout.py --scale 2 --out /tmp/floor.png
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

REPO = Path(__file__).resolve().parent.parent
LAYOUT = REPO / "frontend/src/components/factory-layout.json"
ASSETS = REPO / "frontend/src/assets"

# Agent palette, mirrored from AgentAvatar.vue.
PALETTE = [
    (255, 214, 68),
    (0, 187, 170),
    (255, 119, 0),
    (68, 170, 238),
    (238, 51, 34),
    (136, 221, 34),
    (238, 119, 34),
    (255, 204, 0),
]


def draw_robot(d: ImageDraw.ImageDraw, cx: float, cy: float, h: float, color):
    """Draw a simple robot with its feet anchored at (cx, cy)."""
    hw = h * 0.42
    top = cy - h
    d.rectangle([cx - hw * 0.5, cy - h * 0.32, cx - hw * 0.08, cy], fill=color)
    d.rectangle([cx + hw * 0.08, cy - h * 0.32, cx + hw * 0.5, cy], fill=color)
    d.rounded_rectangle(
        [cx - hw, cy - h * 0.62, cx + hw, cy - h * 0.30],
        5,
        fill=(20, 12, 4),
        outline=color,
        width=3,
    )
    d.rounded_rectangle(
        [cx - hw * 0.78, top, cx + hw * 0.78, cy - h * 0.62],
        4,
        fill=(20, 12, 4),
        outline=color,
        width=3,
    )
    d.rectangle(
        [cx - hw * 0.5, top + h * 0.10, cx - hw * 0.15, top + h * 0.21], fill=(255, 232, 192)
    )
    d.rectangle(
        [cx + hw * 0.15, top + h * 0.10, cx + hw * 0.5, top + h * 0.21], fill=(255, 232, 192)
    )


def draw_station(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    """Draw a work-station monitor centred on (cx, cy)."""
    d.rounded_rectangle(
        [cx - s, cy - s * 0.8, cx + s, cy + s * 0.1],
        4,
        fill=(13, 6, 0),
        outline=(150, 90, 30),
        width=3,
    )
    bars = [(0, 187, 170), (68, 170, 238), (136, 221, 34)]
    for k, c in enumerate(bars):
        y = cy - s * 0.58 + k * s * 0.25
        d.rectangle([cx - s * 0.74, y, cx + s * 0.74, y + s * 0.12], fill=c)


def label(d: ImageDraw.ImageDraw, x: float, y: float, text: str, color):
    """Draw text with a dark backing box for readability."""
    box = d.textbbox((x, y), text)
    d.rectangle([box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2], fill=(10, 6, 0, 220))
    d.text((x, y), text, fill=color)


def main() -> None:
    ap = argparse.ArgumentParser(description="Preview the factory-floor layout.")
    ap.add_argument(
        "--out",
        default="factory-layout-preview.png",
        help="output PNG path (default: factory-layout-preview.png)",
    )
    ap.add_argument(
        "--scale", type=float, default=1.0, help="upscale the output image (default: 1.0)"
    )
    ap.add_argument(
        "--robots", type=int, default=0, help="scatter N sample idle robots across the POIs"
    )
    args = ap.parse_args()

    layout = json.loads(LAYOUT.read_text())
    img_path = ASSETS / layout["image"]["src"]
    im = Image.open(img_path).convert("RGB")
    if args.scale != 1.0:
        im = im.resize((round(im.width * args.scale), round(im.height * args.scale)))
    W, H = im.size
    d = ImageDraw.Draw(im, "RGBA")

    floor = layout["walkableFloor"]
    pois = layout["pointsOfInterest"]
    slots = layout["stationSlots"]
    radius = layout["gravitateRadius"]

    # Walkable polygon — robots stay inside this.
    pts = [(fx * W, fy * H) for fx, fy in floor]
    d.polygon(pts, fill=(0, 255, 130, 45), outline=(0, 255, 130, 230))
    for i, (px, py) in enumerate(pts):
        d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=(0, 255, 130, 255))
        label(d, px + 9, py - 7, f"v{i} ({floor[i][0]:.2f},{floor[i][1]:.2f})", (0, 255, 130))

    # Work-station slots.
    for i, (fx, fy) in enumerate(slots):
        cx, cy = fx * W, fy * H
        draw_station(d, cx, cy, W * 0.05)
        label(d, cx + W * 0.055, cy, f"slot {i}", (255, 180, 70))

    # Points of interest + gravitation box (the jitter range robots land in).
    for poi in pois:
        cx, cy = poi["x"] * W, poi["y"] * H
        rx, ry = radius * W, radius * H
        d.rectangle([cx - rx, cy - ry, cx + rx, cy + ry], outline=(255, 70, 70, 220), width=2)
        d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=(255, 70, 70, 255))
        label(
            d, cx + 11, cy - 8, f"{poi['label']} ({poi['x']:.2f},{poi['y']:.2f})", (255, 220, 120)
        )

    # Optional sample robots, gravitating to a random POI like idle agents do.
    rng = random.Random(7)
    for n in range(args.robots):
        poi = pois[n % len(pois)]
        fx = poi["x"] + (rng.random() - 0.5) * 2 * radius
        fy = poi["y"] + (rng.random() - 0.5) * 2 * radius
        draw_robot(d, fx * W, fy * H, H * 0.07, PALETTE[n % len(PALETTE)])

    out = Path(args.out)
    im.save(out)

    print(
        f"image   : {img_path.relative_to(REPO)} ({layout['image']['width']}x"
        f"{layout['image']['height']})"
    )
    print(f"polygon : {len(floor)} vertices, gravitate radius {radius}")
    for poi in pois:
        print(f"  POI   {poi['id']:<9} ({poi['x']:.2f}, {poi['y']:.2f})  {poi['label']}")
    for i, (fx, fy) in enumerate(slots):
        print(f"  slot {i}          ({fx:.2f}, {fy:.2f})")
    print(f"wrote   : {out}")


if __name__ == "__main__":
    main()
