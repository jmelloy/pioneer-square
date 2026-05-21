#!/usr/bin/env python3
"""Overlay the factory-floor layout on the background image for tuning.

The walkable polygon, patrol loop, points of interest and work-station slots
all come from ``frontend/src/components/factory-layout.json`` — the same file
FactoryFloor.vue reads — so editing that JSON and re-running this script
previews exactly what the app will render.

Usage:
    pip install Pillow
    python scripts/preview_factory_layout.py            # writes factory-layout-preview.png
    python scripts/preview_factory_layout.py --robots 6 # also scatter sample robots
    python scripts/preview_factory_layout.py --scale 2 --out /tmp/floor.png
"""

from __future__ import annotations

import argparse
import json
import math
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
MAX_TILT = 9.0


def robot_shape(d: ImageDraw.ImageDraw, cx: float, cy: float, h: float, color):
    """Draw a chunky robot with its feet anchored at (cx, cy)."""
    hw = h * 0.42
    top = cy - h
    d.rectangle([cx - hw * 0.5, cy - h * 0.32, cx - hw * 0.08, cy], fill=color)
    d.rectangle([cx + hw * 0.08, cy - h * 0.32, cx + hw * 0.5, cy], fill=color)
    d.rectangle(
        [cx - hw, cy - h * 0.62, cx + hw, cy - h * 0.30], fill=(20, 12, 4), outline=color, width=3
    )
    d.rectangle(
        [cx - hw * 0.78, top, cx + hw * 0.78, cy - h * 0.62],
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


def draw_robot(im: Image.Image, cx: float, cy: float, h: float, color, tilt: float = 0.0):
    """Composite a robot, leaning `tilt` degrees into its direction of travel."""
    pad = int(h)
    layer = Image.new("RGBA", (pad * 2, pad + int(h) + 4), (0, 0, 0, 0))
    robot_shape(ImageDraw.Draw(layer), pad, pad + int(h), h, color)
    if tilt:
        layer = layer.rotate(-tilt, center=(pad, pad + int(h)), resample=Image.NEAREST)
    im.paste(layer, (int(cx - pad), int(cy - pad - h)), layer)


def draw_station(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    """Draw an active-task tag (label bar + state badge) centred on (cx, cy)."""
    d.rounded_rectangle(
        [cx - s * 0.9, cy - s * 0.5, cx + s * 0.9, cy + s * 0.5],
        3,
        fill=(12, 7, 0, 235),
        outline=(210, 150, 60),
        width=3,
    )
    d.rectangle([cx - s * 0.6, cy - s * 0.24, cx + s * 0.6, cy - s * 0.05], fill=(230, 200, 120))
    d.rectangle([cx - s * 0.45, cy + s * 0.08, cx + s * 0.45, cy + s * 0.27], fill=(255, 190, 70))


def draw_arrow(d: ImageDraw.ImageDraw, p0, p1, color, width=3):
    """Draw a line from p0 to p1 with a direction arrowhead at its midpoint."""
    d.line([p0, p1], fill=color, width=width)
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    for off in (2.6, -2.6):
        d.line(
            [(mx, my), (mx + 13 * math.cos(ang + off), my + 13 * math.sin(ang + off))],
            fill=color,
            width=width,
        )


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
        "--robots", type=int, default=0, help="scatter N sample robots around the patrol loop"
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
    patrol = layout["patrolPath"]
    radius = layout["gravitateRadius"]
    pjit = layout["patrolJitter"]

    # Walkable polygon — robots stay inside this.
    pts = [(fx * W, fy * H) for fx, fy in floor]
    d.polygon(pts, fill=(0, 255, 130, 45), outline=(0, 255, 130, 230))
    for i, (px, py) in enumerate(pts):
        d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=(0, 255, 130, 255))
        label(d, px + 9, py - 7, f"v{i} ({floor[i][0]:.2f},{floor[i][1]:.2f})", (0, 255, 130))

    # Patrol loop — idle robots step along this, bouncing at the ends.
    pp = [(fx * W, fy * H) for fx, fy in patrol]
    for a, b in zip(pp, pp[1:], strict=False):
        draw_arrow(d, a, b, (120, 190, 255, 235))
    for i, (px, py) in enumerate(pp):
        d.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(120, 190, 255, 255))
        label(d, px + 10, py - 7, f"p{i} ({patrol[i][0]:.2f},{patrol[i][1]:.2f})", (170, 215, 255))

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

    # Sample robots spread around the patrol loop, leaning into their heading.
    rng = random.Random(7)
    for n in range(args.robots):
        seg = n * (len(patrol) - 1) / max(1, args.robots)
        i0 = int(seg)
        i1 = min(i0 + 1, len(patrol) - 1)
        f = seg - i0
        fx = patrol[i0][0] * (1 - f) + patrol[i1][0] * f + (rng.random() - 0.5) * 2 * pjit
        fy = patrol[i0][1] * (1 - f) + patrol[i1][1] * f + (rng.random() - 0.5) * 2 * pjit
        dx = (patrol[i1][0] - patrol[i0][0]) * W
        dy = (patrol[i1][1] - patrol[i0][1]) * H
        dist = math.hypot(dx, dy) or 1.0
        tilt = max(-MAX_TILT, min(MAX_TILT, dx / dist * MAX_TILT))
        draw_robot(im, fx * W, fy * H, H * 0.07, PALETTE[n % len(PALETTE)], tilt)

    out = Path(args.out)
    im.save(out)

    print(
        f"image   : {img_path.relative_to(REPO)} ({layout['image']['width']}x"
        f"{layout['image']['height']})"
    )
    print(f"polygon : {len(floor)} vertices")
    print(f"patrol  : {len(patrol)} waypoints, jitter {pjit}")
    for poi in pois:
        print(f"  POI   {poi['id']:<9} ({poi['x']:.2f}, {poi['y']:.2f})  {poi['label']}")
    print(f"wrote   : {out}")


if __name__ == "__main__":
    main()
