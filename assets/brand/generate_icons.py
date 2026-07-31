#!/usr/bin/env python3
"""Generate the Teams app icons for the Nuru avatar/bot (issue #53).

"Nuru" means *light* (Swahili) — the mark is a luminous golden orb (MTN yellow)
emitting concentric light/sound rings on an indigo→violet field that matches the
manifest ``accentColor`` (#5B5FC7). The orb reads as both "light" and a
voice/sound source, fitting the real-time voice avatar.

Teams icon requirements (see Microsoft docs):
  - color.png   : 192x192, full-colour, full-bleed (Teams masks/rounds it).
  - outline.png : 32x32, transparent background, single-colour (white) line art
                  used monochrome in the app rail.

Pillow is NOT a project dependency (this repo is stdlib-only). Run it isolated:

    uv run --with pillow python assets/brand/generate_icons.py

Everything is drawn at 4x and downscaled for clean anti-aliasing.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4  # supersample factor


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    px = grad.load()
    for y in range(size):
        px[0, y] = _lerp(top, bottom, y / max(1, size - 1))
    return grad.resize((size, size))


def _radial_glow(size: int, center, inner: tuple[int, int, int], outer_alpha0: tuple[int, int, int],
                 r_inner: float, r_outer: float) -> Image.Image:
    """A soft radial glow: opaque ``inner`` at r<=r_inner fading to alpha 0 at r_outer."""
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = glow.load()
    cx, cy = center
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - cx, y - cy)
            if d <= r_inner:
                a = 255
            elif d >= r_outer:
                a = 0
            else:
                a = round(255 * (1 - (d - r_inner) / (r_outer - r_inner)))
            if a:
                px[x, y] = (inner[0], inner[1], inner[2], a)
    return glow


def build_color() -> Image.Image:
    n = 192 * SS
    img = _vertical_gradient(n, (88, 92, 214), (38, 35, 96)).convert("RGBA")  # #585CD6 -> #262360
    draw = ImageDraw.Draw(img, "RGBA")

    cx = cy = n / 2
    orb_r = n * 0.205

    # Outer light/sound rings emanating from the orb.
    for i, rr in enumerate((0.34, 0.43, 0.52)):
        r = n * rr
        width = max(1, int(n * (0.018 - i * 0.004)))
        alpha = 150 - i * 45
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 230, 150, alpha), width=width)

    # Soft golden halo behind the orb.
    halo = _radial_glow(n, (cx, cy), (255, 203, 5), (0, 0, 0), orb_r * 1.05, orb_r * 2.25)
    img.alpha_composite(halo)

    # The luminous orb: white-hot core -> MTN gold edge.
    core = _radial_glow(n, (cx, cy - orb_r * 0.12), (255, 249, 222), (0, 0, 0), orb_r * 0.18, orb_r)
    draw.ellipse([cx - orb_r, cy - orb_r, cx + orb_r, cy + orb_r], fill=(255, 203, 5, 255))
    img.alpha_composite(core)
    # Subtle top highlight for a glossy, lit feel.
    hl_r = orb_r * 0.42
    hlx, hly = cx - orb_r * 0.28, cy - orb_r * 0.34
    draw.ellipse([hlx - hl_r, hly - hl_r, hlx + hl_r, hly + hl_r], fill=(255, 255, 255, 90))

    return img.resize((192, 192), Image.LANCZOS)


def build_outline() -> Image.Image:
    n = 32 * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    cx = cy = n / 2
    white = (255, 255, 255, 255)

    # Filled centre dot (the light source).
    dot = n * 0.12
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=white)
    # Two concentric ring strokes (light/sound waves).
    for rr, w in ((0.27, 0.05), (0.40, 0.045)):
        r = n * rr
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=white, width=max(1, int(n * w)))

    return img.resize((32, 32), Image.LANCZOS)


def main() -> None:
    color = build_color()
    outline = build_outline()
    color.save(os.path.join(HERE, "color.png"))
    outline.save(os.path.join(HERE, "outline.png"))
    print(f"Wrote color.png {color.size} and outline.png {outline.size}")


if __name__ == "__main__":
    main()
