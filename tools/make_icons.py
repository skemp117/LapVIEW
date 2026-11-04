"""Draw the home-screen icons into webui/static/icons/.

    python tools/make_icons.py

A speedo on the app's accent blue: a 270-degree dial, a redline, and a needle.
No text, so nothing depends on which fonts this machine has. The PNGs are
committed; run this again only to change the design.

Four files, because each consumer wants something different:

  icon-192.png, icon-512.png   the manifest's `any` icons: a rounded square
                               on a transparent ground, drawn as is.
  icon-maskable-512.png        the manifest's `maskable` icon: full bleed,
                               because Android crops it to its own shape.
                               Everything that matters sits inside the
                               central circle of 40% radius that every mask
                               keeps.
  apple-touch-icon.png         180x180, full bleed and opaque. iOS rounds the
                               corners itself and fills any transparency with
                               black, and it ignores an SVG touch icon.
"""
from __future__ import annotations

import math
from pathlib import Path

import cairo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "webui/static/icons"

# From webui/static/app.css.
ACCENT = "#4fa3ff"      # --accent
INK = "#101318"         # --bg: the dial and needle
REDLINE = "#e0564b"     # --bad
WHITE = "#ffffff"


def rgb(hex_):
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def rounded_square(cr, s, radius):
    r = radius
    cr.new_sub_path()
    cr.arc(s - r, r, r, -math.pi / 2, 0)
    cr.arc(s - r, s - r, r, 0, math.pi / 2)
    cr.arc(r, s - r, r, math.pi / 2, math.pi)
    cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw(size, full_bleed):
    """One icon, `size` pixels square."""
    fmt = cairo.FORMAT_RGB24 if full_bleed else cairo.FORMAT_ARGB32
    surf = cairo.ImageSurface(fmt, size, size)
    cr = cairo.Context(surf)
    cr.scale(size, size)            # draw in a unit square

    # Ground.
    cr.set_source_rgb(*rgb(ACCENT))
    if full_bleed:
        cr.paint()
    else:
        rounded_square(cr, 1.0, 0.22)
        cr.fill()

    # The dial: 270 degrees open at the bottom, centred a touch low so the
    # needle's hub and the gap below it balance the arc above. Cairo's angles
    # run clockwise from +x because y points down, so 135 -> 405 goes over the
    # top. Nothing is further than 0.36 from the middle of the icon: inside the
    # maskable safe circle (0.40).
    cx, cy, r, w = 0.5, 0.54, 0.27, 0.085
    start, end = math.radians(135), math.radians(405)
    red = math.radians(360)         # the last 45 degrees are the redline
    cr.set_line_width(w)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgb(*rgb(INK))
    cr.arc(cx, cy, r, start, end)
    cr.stroke()
    # Square-ended over the dial, then a round end of its own, so the join
    # between the two colours is a clean radial line.
    cr.set_line_cap(cairo.LINE_CAP_BUTT)
    cr.set_source_rgb(*rgb(REDLINE))
    cr.arc(cx, cy, r, red, end)
    cr.stroke()
    cr.arc(cx + r * math.cos(end), cy + r * math.sin(end), w / 2, 0,
           2 * math.pi)
    cr.fill()

    # Ticks inside the dial, at the quarter points.
    cr.set_line_width(0.034)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgb(*rgb(INK))
    for frac in (0.25, 0.5, 0.75):
        a = start + frac * (end - start)
        r0, r1 = r - w / 2 - 0.04, r - w / 2 - 0.085
        cr.move_to(cx + r0 * math.cos(a), cy + r0 * math.sin(a))
        cr.line_to(cx + r1 * math.cos(a), cy + r1 * math.sin(a))
        cr.stroke()

    # The needle, between the top and the last tick: going quickly.
    a = start + 0.63 * (end - start)
    cr.set_line_width(0.062)
    cr.set_source_rgb(*rgb(WHITE))
    cr.move_to(cx, cy)
    cr.line_to(cx + 0.19 * math.cos(a), cy + 0.19 * math.sin(a))
    cr.stroke()
    cr.set_source_rgb(*rgb(INK))
    cr.arc(cx, cy, 0.064, 0, 2 * math.pi)
    cr.fill()
    cr.set_source_rgb(*rgb(WHITE))
    cr.arc(cx, cy, 0.022, 0, 2 * math.pi)
    cr.fill()

    surf.flush()
    return surf


ICONS = {
    "icon-192.png": (192, False),
    "icon-512.png": (512, False),
    "icon-maskable-512.png": (512, True),
    "apple-touch-icon.png": (180, True),
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (size, bleed) in ICONS.items():
        draw(size, bleed).write_to_png(str(OUT / name))
        print("wrote %s  %dx%d%s" % (OUT / name, size, size,
                                     "  full bleed" if bleed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
