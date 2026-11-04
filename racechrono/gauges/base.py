"""Gauge interface, layout maths and the caches that make the frame loop cheap.

Every gauge splits its drawing in two: a static layer built once (dial face,
ticks, numerals, the map polyline) and a per-frame pass that touches only what
moves (needle, dot, marker, value text). The static layer is blitted with
OPERATOR_SOURCE, which both clears the previous frame's dynamic pixels and
restores the face in a single operation over the gauge's own bounding box -- no
full-frame clear.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod

import cairo

FONT_PREFERRED = "Segoe UI"
FONT_FALLBACK = "DejaVu Sans"


def _font_installed(family):
    """Whether fontconfig knows this family. False where there is no fc-list."""
    fc = shutil.which("fc-list")
    if not fc:
        return False
    try:
        out = subprocess.run([fc, ":family=%s" % family, "family"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return bool(out.strip())


def pick_font(environ=None, platform=None, installed=_font_installed):
    """The gauge face: LAPVIEW_FONT, else Segoe UI where it exists, else DejaVu.

    Cairo's toy font API never fails on a missing family -- fontconfig quietly
    substitutes whatever it likes -- so asking for Segoe UI in a Linux container
    gives a font nobody chose, and numerals that change width between machines.
    Choosing the fallback by name makes the substitute a decision.
    """
    env = os.environ if environ is None else environ
    want = str(env.get("LAPVIEW_FONT") or "").strip()
    if want:
        return want
    if (platform or sys.platform) == "win32":
        return FONT_PREFERRED
    return FONT_PREFERRED if installed(FONT_PREFERRED) else FONT_FALLBACK


FONT = pick_font()


# ------------------------------------------------------------------ layout

class Layout:
    """Resolves fractional config geometry into pixels.

    Sizes are fractions of frame *height* so a gauge keeps its proportions when
    the frame size changes; horizontal anchors are fractions of frame *width*.
    """

    def __init__(self, width, height):
        self.w = int(width)
        self.h = int(height)

    def sx(self, frac):
        return float(frac) * self.w

    def sy(self, frac):
        return float(frac) * self.h

    def size(self, frac):
        return float(frac) * self.h


def _snap(box):
    """Grow a box out to whole pixels.

    Fractional bounds are the difference between a gauge's region being properly
    cleared and *almost* cleared. A fill or a clip on a fractional edge is
    antialiased, so the boundary row is only partly covered -- the old frame
    bleeds through it, and over a render those leftovers accumulate. Snapping
    outwards makes the clear, the clip and the static blit all land on the same
    whole pixels.
    """
    x, y, w, h = box
    x0, y0 = math.floor(x), math.floor(y)
    x1, y1 = math.ceil(x + w), math.ceil(y + h)
    return (float(x0), float(y0), float(x1 - x0), float(y1 - y0))


def anchor_box(anchor, x, y, w, h):
    """Top-left corner of a w*h box placed at (x, y) per `anchor`."""
    ax = x
    ay = y
    if anchor.endswith("-center") or anchor == "center":
        ax = x - w / 2.0
    elif anchor.endswith("-right"):
        ax = x - w
    if anchor.startswith("bottom"):
        ay = y - h
    elif anchor.startswith("center"):
        ay = y - h / 2.0
    return ax, ay


# ------------------------------------------------------------------ caches

# Grayscale antialiasing, not the platform default.
#
# cairo's default resolved to ANTIALIAS_SUBPIXEL here, which rasterises glyphs
# against an assumed RGB stripe: coverage is computed per colour channel, so the
# result is not a premultiplied ARGB pixel at all. Measured on one readout, 538
# of 2118 ink pixels had a colour channel above their own alpha, by up to 85.
#
# Two things go wrong with that. The export pipes BGRA to ffmpeg through
# `unpremultiply=inplace=1`, which divides by alpha and saturates wherever a
# channel exceeds it, so glyph edges come out too bright. And subpixel AA is only
# correct over a known background -- an overlay sits on arbitrary video, where the
# per-channel weighting shows up as coloured fringing on text.
#
# Grayscale AA produces one coverage value per pixel, which premultiplies
# correctly and composites correctly over anything. Measured cost: nothing, and
# 20 fewer ink pixels on the same glyph.
FONT_OPTIONS = cairo.FontOptions()
FONT_OPTIONS.set_antialias(cairo.ANTIALIAS_GRAY)


class TextCache:
    """Rendered text kept as small surfaces, keyed by content and style.

    Re-shaping text every frame is the usual hidden cost in a loop like this.
    The value readouts only ever take a few hundred distinct strings ("0".."120"
    for the speedo, four digits for the tacho), so caching the rasterised
    result removes the shaping work entirely.
    """

    def __init__(self, limit=4096):
        self._cache = {}
        self._limit = limit

    def render(self, text, size, colour, bold=False):
        key = (text, round(size, 2), colour, bold)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        weight = cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL
        # Measure on a scratch context.
        scratch = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
        scratch.set_font_options(FONT_OPTIONS)
        scratch.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, weight)
        scratch.set_font_size(size)
        ext = scratch.text_extents(text)
        pad = max(2.0, size * 0.2)
        sw = max(1, int(math.ceil(ext.width + 2 * pad)))
        sh = max(1, int(math.ceil(size * 1.6 + 2 * pad)))
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
        ctx = cairo.Context(surf)
        ctx.set_font_options(FONT_OPTIONS)
        ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, weight)
        ctx.set_font_size(size)
        ox = pad - ext.x_bearing
        oy = pad - ext.y_bearing
        ctx.set_source_rgba(*colour)
        ctx.move_to(ox, oy)
        ctx.show_text(text)
        surf.flush()
        # Offsets from the text's own origin to the surface's top-left, so the
        # caller can place it by baseline/centre without re-measuring.
        item = (surf, ext.width, ext.height, ox, oy)
        if len(self._cache) >= self._limit:
            self._cache.clear()
        self._cache[key] = item
        return item

    def draw(self, ctx, text, x, y, size, colour, align="center",
             valign="baseline", bold=False):
        surf, tw, th, ox, oy = self.render(text, size, colour, bold)
        dx = {"left": 0.0, "center": -tw / 2.0, "right": -tw}[align]
        dy = {"baseline": 0.0, "middle": th / 2.0, "top": th}[valign]
        ctx.save()
        ctx.set_source_surface(surf, x + dx - ox, y + dy - oy)
        ctx.paint()
        ctx.restore()


TEXT = TextCache()


# ------------------------------------------------------------------ gauge

class Gauge(ABC):
    """One overlay element.

    Registered by name so CONFIG stays data: enabling, disabling, moving or
    resizing an element never touches render code.
    """

    name = "gauge"
    #: channels the render loop must supply in each frame's row
    channels = ()

    def __init__(self, cfg, layout, context=None):
        self.cfg = cfg
        self.layout = layout
        self.context = context or {}
        self._static = None
        self.place()
        if getattr(self, "_bbox", None) is None:
            raise ValueError("%s.place() did not set _bbox" % type(self).__name__)
        self._bbox = _snap(self._bbox)

    # -- geometry ----------------------------------------------------
    @abstractmethod
    def place(self):
        """Compute pixel geometry and set self._bbox = (x, y, w, h)."""

    @property
    def draw_box(self):
        """The gauge's own drawing box. May extend past the frame: the padding
        exists so strokes and markers are not clipped inside the static layer."""
        return self._bbox

    @property
    def bbox(self):
        """The visible part of the drawing box -- the dirty rectangle to
        composite. Clamped to the frame so a generously padded gauge does not
        ask the compositor to touch pixels that do not exist. Whole pixels,
        since `_bbox` is snapped and the frame bounds are integers."""
        x, y, w, h = self._bbox
        x0 = max(0.0, x)
        y0 = max(0.0, y)
        x1 = min(float(self.layout.w), x + w)
        y1 = min(float(self.layout.h), y + h)
        return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))

    # -- drawing -----------------------------------------------------
    @abstractmethod
    def build_static(self):
        """Return an ImageSurface the size of bbox, drawn once."""

    @abstractmethod
    def draw(self, ctx, row):
        """Draw the per-frame parts onto the full-frame context."""

    def static(self):
        if self._static is None:
            self._static = self.build_static()
        return self._static

    def blit_static(self, ctx):
        """Restore this gauge's face, clearing last frame's dynamic pixels.

        The static surface is placed at the unclamped origin so it stays
        registered with the geometry, but only the on-frame rectangle is filled.
        """
        ox, oy, _, _ = self._bbox
        vis = self.bbox
        if vis[2] <= 0 or vis[3] <= 0:
            return
        ctx.save()
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_surface(self.static(), ox, oy)
        ctx.rectangle(*vis)
        ctx.fill()
        ctx.restore()


# ------------------------------------------------------------------ helpers

def new_surface(w, h):
    return cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w)), max(1, int(h)))


def dial_angles(sweep_deg):
    """(start_angle, sweep_radians) for a dial with its gap centred at the
    bottom. Cairo angles run clockwise on screen from +X, so the bottom is
    pi/2 and the sweep proceeds through the left, top and right."""
    gap = 360.0 - float(sweep_deg)
    start = math.pi / 2.0 + math.radians(gap / 2.0)
    return start, math.radians(float(sweep_deg))


def value_angle(value, vmin, vmax, start, sweep):
    frac = (float(value) - vmin) / (vmax - vmin) if vmax > vmin else 0.0
    frac = min(1.0, max(0.0, frac))
    return start + frac * sweep


def stroke_polyline(ctx, pts, width, colour, closed=False):
    if len(pts) < 2:
        return
    ctx.set_line_width(width)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_source_rgba(*colour)
    ctx.move_to(*pts[0])
    for p in pts[1:]:
        ctx.line_to(*p)
    if closed:
        ctx.close_path()
    ctx.stroke()


def filled_triangle(ctx, cx, cy, size, angle, colour):
    """Arrow marker: apex forward along `angle` (0 = up the screen)."""
    ctx.save()
    ctx.translate(cx, cy)
    ctx.rotate(angle)
    ctx.move_to(0.0, -size)
    ctx.line_to(size * 0.62, size * 0.72)
    ctx.line_to(0.0, size * 0.36)
    ctx.line_to(-size * 0.62, size * 0.72)
    ctx.close_path()
    ctx.set_source_rgba(*colour)
    ctx.fill()
    ctx.restore()
