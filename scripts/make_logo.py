"""Generate the project icon.

    pip install pillow && python scripts/make_logo.py

Pillow is only needed for this script, so it is deliberately absent from
requirements.txt; the bot itself has no use for it.

The mark is original. It borrows the three projects' colour identities -- Plex
amber, Seerr indigo, Telegram blue -- and none of their logos, which are
trademarks and not ours to redraw: an iris (the "seer") around a shape that
reads as both a play triangle and a send arrow, with an approval tick.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parents[1] / "assets"
SIZES = (512, 256, 128, 64)
SS = 4  # supersampling factor; edges are smoothed by downscaling at the end

PLEX_AMBER = (229, 160, 13)
PLEX_AMBER_LIGHT = (255, 199, 84)
SEERR_INDIGO = (99, 91, 255)
TELEGRAM_BLUE = (41, 169, 234)
BACKDROP_TOP = (26, 29, 58)
BACKDROP_BOTTOM = (13, 15, 33)


def linear_gradient(size: int, start: tuple, end: tuple, angle: float) -> Image.Image:
    """A smooth gradient, built small and scaled up so it stays cheap."""
    small = 64
    grad = Image.new("RGB", (small, small))
    pixels = grad.load()
    radians = math.radians(angle)
    dx, dy = math.cos(radians), math.sin(radians)

    # Normalise against the axis's actual span, or the ramp would cover only
    # part of the range and the start colour would never appear.
    low = min(dx, 0.0) + min(dy, 0.0)
    span = (max(dx, 0.0) + max(dy, 0.0)) - low or 1.0

    for y in range(small):
        for x in range(small):
            t = (((x / small) * dx + (y / small) * dy) - low) / span
            t = min(1.0, max(0.0, t))
            pixels[x, y] = tuple(
                round(start[i] + (end[i] - start[i]) * t) for i in range(3)
            )
    return grad.resize((size, size), Image.LANCZOS)


def rounded_mask(size: int, radius_ratio: float = 0.235) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * radius_ratio), fill=255
    )
    return mask


def build(size: int) -> Image.Image:
    s = size * SS
    c = s / 2

    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Backdrop: a dark rounded square so the three accent colours stay legible.
    backdrop = linear_gradient(s, BACKDROP_TOP, BACKDROP_BOTTOM, 90).convert("RGBA")
    icon.paste(backdrop, (0, 0), rounded_mask(s))

    # A soft indigo bloom behind the mark, for depth.
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (c - s * 0.34, c - s * 0.34, c + s * 0.34, c + s * 0.34),
        fill=(*SEERR_INDIGO, 90),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.06))
    icon.alpha_composite(Image.composite(glow, Image.new("RGBA", (s, s)), rounded_mask(s)))

    # The iris: an open ring, indigo into Telegram blue, broken where the tick
    # sits so the two shapes never touch.
    ring = Image.new("L", (s, s), 0)
    r = s * 0.30
    ImageDraw.Draw(ring).arc(
        (c - r, c - r, c + r, c + r), start=72, end=12, width=int(s * 0.05), fill=255
    )
    icon.paste(
        linear_gradient(s, SEERR_INDIGO, TELEGRAM_BLUE, 55).convert("RGBA"), (0, 0), ring
    )

    # The pupil: a triangle that reads as play and as send.
    blade = Image.new("L", (s, s), 0)
    bx = c - s * 0.018  # nudged left so the arrow looks centred, not tip-heavy
    ImageDraw.Draw(blade).polygon(
        [
            (bx - s * 0.092, c - s * 0.145),
            (bx + s * 0.160, c),
            (bx - s * 0.092, c + s * 0.145),
            (bx - s * 0.048, c),
        ],
        fill=255,
    )
    icon.paste(
        linear_gradient(s, PLEX_AMBER_LIGHT, PLEX_AMBER, 60).convert("RGBA"), (0, 0), blade
    )

    # The approval tick, centred in the ring's gap on the 45-degree diagonal.
    tx = ty = c + r * math.cos(math.radians(45))
    tick = Image.new("L", (s, s), 0)
    ImageDraw.Draw(tick).line(
        [
            (tx - s * 0.072, ty),
            (tx - s * 0.020, ty + s * 0.052),
            (tx + s * 0.080, ty - s * 0.062),
        ],
        fill=255,
        width=int(s * 0.052),
        joint="curve",
    )
    icon.paste(
        linear_gradient(s, PLEX_AMBER_LIGHT, PLEX_AMBER, 45).convert("RGBA"), (0, 0), tick
    )

    return icon.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for size in SIZES:
        icon = build(size)
        name = "logo.png" if size == max(SIZES) else f"icon-{size}.png"
        icon.save(OUT_DIR / name)
        print(f"wrote assets/{name} ({size}x{size})")


if __name__ == "__main__":
    main()
