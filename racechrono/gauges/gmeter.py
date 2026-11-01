"""G-meter: scatter dot on concentric rings, not a needle."""
from __future__ import annotations

import math
from collections import deque

import cairo

from .base import Gauge, TEXT, new_surface


class GMeter(Gauge):
    name = "gmeter"

    def place(self):
        cfg, lay = self.cfg, self.layout
        self.d = lay.size(cfg.get("diameter", 0.13))
        self.r = self.d / 2.0

        if cfg.x is None:
            ref = self.context.get("reference_center_x", lay.sx(0.5))
            ref_d = self.context.get("reference_diameter", self.d)
            self.cx = ref + float(cfg.get("offset_d", 0.0)) * ref_d
        else:
            self.cx = lay.sx(cfg.x)
        self.bottom = lay.sy(cfg.y)
        # The magnitude readout hangs below the rings, so the shared baseline is
        # the bottom of the *text*, not of the ring. Placing by the ring instead
        # pushes the readout off the bottom of the frame.
        self.text_offset = self.r * 1.22
        self.text_size = self.r * 0.58
        self.cy = self.bottom - (self.text_offset + self.text_size / 2.0)

        self.rings = [float(v) for v in cfg.get("rings", [1.0, 2.0])]
        self.g_max = max(self.rings)
        self.invert_lat = bool(cfg.get("invert_lateral", False))
        self.invert_lon = bool(cfg.get("invert_longitudinal", False))
        self.trail_s = float(cfg.get("trail_s", 0.0))
        self.backing_alpha = float(cfg.get("backing_alpha", 0.0))
        self.dot_colour = cfg.get("dot_color", (0.88, 0.09, 0.09, 1.0))
        self.text_colour = cfg.get("text_color", (1.0, 1.0, 1.0, 1.0))
        self.lat_channel = str(cfg.get("lateral_channel", "lateral_acc"))
        self.lon_channel = str(cfg.get("longitudinal_channel", "longitudinal_acc"))
        self.mag_channel = str(cfg.get("magnitude_channel", "combined_acc"))

        self._trail = deque()
        pad = self.r * 0.62      # room for the ring labels and the readout
        self._bbox = (self.cx - self.r - pad, self.cy - self.r - pad,
                      2 * (self.r + pad), 2 * (self.r + pad))

    @property
    def channels(self):
        return (self.lat_channel, self.lon_channel, self.mag_channel)

    # -- static ------------------------------------------------------
    def build_static(self):
        x, y, w, h = self._bbox
        surf = new_surface(math.ceil(w), math.ceil(h))
        ctx = cairo.Context(surf)
        cx, cy, r = self.cx - x, self.cy - y, self.r

        if self.backing_alpha > 0:
            ctx.set_source_rgba(0.05, 0.06, 0.08, self.backing_alpha)
            ctx.arc(cx, cy, r * 1.02, 0, 2 * math.pi)
            ctx.fill()

        # Dashed crosshair axes.
        ctx.save()
        ctx.set_line_width(max(1.0, r * 0.022))
        ctx.set_dash([r * 0.10, r * 0.085])
        ctx.set_source_rgba(1, 1, 1, 0.55)
        ctx.move_to(cx - r, cy)
        ctx.line_to(cx + r, cy)
        ctx.move_to(cx, cy - r)
        ctx.line_to(cx, cy + r)
        ctx.stroke()
        ctx.restore()

        # Concentric rings with labels up the vertical axis.
        for g in self.rings:
            rr = r * (g / self.g_max)
            ctx.set_line_width(max(1.0, r * 0.022))
            ctx.set_source_rgba(1, 1, 1, 0.75)
            ctx.arc(cx, cy, rr, 0, 2 * math.pi)
            ctx.stroke()
            TEXT.draw(ctx, "%gG" % g, cx - r * 0.10, cy - rr,
                      r * 0.30, self.text_colour, align="right", valign="middle")
        surf.flush()
        return surf

    # -- dynamic -----------------------------------------------------
    def draw(self, ctx, row):
        self.blit_static(ctx)
        lat = row.get(self.lat_channel)
        lon = row.get(self.lon_channel)
        mag = row.get(self.mag_channel)

        if lat is None or lat != lat or lon is None or lon != lon:
            TEXT.draw(ctx, "--", self.cx, self.cy + self.text_offset,
                      self.text_size, self.text_colour,
                      align="center", valign="middle", bold=True)
            return

        sx = -1.0 if self.invert_lat else 1.0
        sy = -1.0 if self.invert_lon else 1.0
        # +lateral puts the dot right, +longitudinal (acceleration) puts it up,
        # and screen Y is down, hence the negation on the vertical term.
        px = self.cx + sx * (float(lat) / self.g_max) * self.r
        py = self.cy - sy * (float(lon) / self.g_max) * self.r
        # Clamp to the outer ring so a spike cannot draw outside the bbox.
        dx, dy = px - self.cx, py - self.cy
        dist = math.hypot(dx, dy)
        if dist > self.r:
            px = self.cx + dx / dist * self.r
            py = self.cy + dy / dist * self.r

        if self.trail_s > 0:
            self._trail.append((px, py))
            keep = max(1, int(self.trail_s * self.context.get("fps", 60)))
            while len(self._trail) > keep:
                self._trail.popleft()
            n = len(self._trail)
            for i, (tx, ty) in enumerate(self._trail):
                a = (i + 1) / float(n) * 0.55
                ctx.set_source_rgba(self.dot_colour[0], self.dot_colour[1],
                                    self.dot_colour[2], a)
                ctx.arc(tx, ty, self.r * 0.085, 0, 2 * math.pi)
                ctx.fill()

        ctx.set_source_rgba(*self.dot_colour)
        ctx.arc(px, py, self.r * 0.115, 0, 2 * math.pi)
        ctx.fill()

        if mag is None or mag != mag:
            mag = math.hypot(float(lat), float(lon))
        TEXT.draw(ctx, "%.1f" % float(mag), self.cx, self.cy + self.text_offset,
                  self.text_size, self.text_colour,
                  align="center", valign="middle", bold=True)
