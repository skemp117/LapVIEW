"""Track map with a live position marker.

The polyline is static; only the marker moves. Coordinates come from the map
file's own stored transform -- never from the current run's extent -- so the
marker cannot drift when a run happens to go wider or shorter than the laps the
map was built from.
"""
from __future__ import annotations

import math

import cairo
import numpy as np

from .base import Gauge, anchor_box, filled_triangle, new_surface


class MapGauge(Gauge):
    name = "track_map"
    channels = ("latitude", "longitude", "bearing", "good")

    def place(self):
        cfg, lay = self.cfg, self.layout
        self.tm = self.context.get("trackmap")
        if self.tm is None:
            raise ValueError("track_map element enabled but no map was selected; "
                             "run tools/generate_map.py or set CONFIG['map_file']")

        self.h = lay.size(cfg.get("height", 0.25))
        self.w = self.h * self.tm.aspect
        x, y = anchor_box(cfg.anchor, lay.sx(cfg.x if cfg.x is not None else 0.03),
                          lay.sy(cfg.y), self.w, self.h)

        self.stroke = max(1.0, lay.size(cfg.get("stroke", 0.004)))
        self.outline = max(0.5, lay.size(cfg.get("outline", 0.0015)))
        self.colour = cfg.get("color", (1.0, 1.0, 1.0, 1.0))
        self.outline_colour = cfg.get("outline_color", (0.0, 0.0, 0.0, 1.0))
        marker = cfg.get("marker", {}) or {}
        self.marker_colour = marker.get("color", (0.9, 0.1, 0.1, 1.0))
        self.marker_size = lay.size(marker.get("size", 0.018))
        self.show_sf = bool(cfg.get("show_start_finish", False))
        self.sf_colour = cfg.get("start_finish_color", (0.95, 0.2, 0.2, 0.9))
        self.dim_alpha = float(self.context.get("dim_alpha", 0.4))
        self.clamp_marker = bool(cfg.get("clamp_marker", True))

        # Pad by half the total stroke plus the marker, so nothing is clipped.
        pad = self.stroke / 2.0 + self.outline + self.marker_size * 1.2
        self._pad = pad
        self._origin = (x, y)
        self._bbox = (x - pad, y - pad, self.w + 2 * pad, self.h + 2 * pad)

        self._last_xy = None
        self._last_angle = 0.0

    # -- projection --------------------------------------------------
    def _to_px(self, u, v):
        """Unit map coords -> frame pixels.

        `points_unit` lives in a square 0..1 box with the short axis centred;
        undo that centring so the drawn map is tight to its own bbox.
        """
        ox, oy = self._origin
        ar = self.tm.aspect
        if ar >= 1.0:
            sq_w = self.w
            sq_h = self.w
        else:
            sq_w = self.h
            sq_h = self.h
        return (ox + np.asarray(u) * sq_w - (sq_w - self.w) / 2.0,
                oy + np.asarray(v) * sq_h - (sq_h - self.h) / 2.0)

    # -- static ------------------------------------------------------
    def build_static(self):
        bx, by, bw, bh = self._bbox
        surf = new_surface(math.ceil(bw), math.ceil(bh))
        ctx = cairo.Context(surf)

        pts = self.tm.unit_points
        px, py = self._to_px(pts[:, 0], pts[:, 1])
        px = px - bx
        py = py - by

        # Black under white: stroke the outline wider first, then the path on
        # top. Round joins and caps -- this layout has tight radii and mitre
        # joins spike badly at the hairpin.
        for width, colour in ((self.stroke + 2 * self.outline, self.outline_colour),
                              (self.stroke, self.colour)):
            ctx.set_line_width(width)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_source_rgba(*colour)
            ctx.move_to(px[0], py[0])
            for a, b in zip(px[1:], py[1:]):
                ctx.line_to(a, b)
            if getattr(self.tm, "closed", True):
                # An open course must not be closed, or a line is drawn straight
                # from the finish back to the start.
                ctx.close_path()
            ctx.stroke()

        if self.show_sf and self.tm.start_finish.get("point"):
            u, v = self.tm.start_finish["point"]
            sx, sy = self._to_px(float(u), float(v))
            nx, ny = self.tm.start_finish.get("normal_unit", [0.0, 1.0])
            # Trap is drawn across the track, i.e. perpendicular to travel.
            tx, ty = -ny, nx
            ln = math.hypot(tx, ty) or 1.0
            half = self.marker_size * 0.9
            tx, ty = tx / ln * half, ty / ln * half
            ctx.set_line_width(self.stroke * 0.9)
            ctx.set_line_cap(cairo.LINE_CAP_BUTT)
            ctx.set_source_rgba(*self.sf_colour)
            ctx.move_to(float(sx) - bx - tx, float(sy) - by - ty)
            ctx.line_to(float(sx) - bx + tx, float(sy) - by + ty)
            ctx.stroke()

        surf.flush()
        return surf

    # -- dynamic -----------------------------------------------------
    def draw(self, ctx, row):
        self.blit_static(ctx)

        lat = row.get("latitude")
        lon = row.get("longitude")
        good = row.get("good", True)
        alpha_scale = 1.0

        if lat is not None and lat == lat and lon is not None and lon == lon:
            u, v = self.tm.project_point(lat, lon)
            x, y = self._to_px(u, v)
            if self.clamp_marker:
                # The map is the racing line, so the pit lane, the paddock and
                # any off-track excursion project outside its extent. Pinning the
                # marker to the nearest edge keeps a whole arrow on the map
                # instead of half of one sliding off it; during a lap the marker
                # is far inside the box and this never bites.
                ox, oy = self._origin
                x = min(max(float(x), ox), ox + self.w)
                y = min(max(float(y), oy), oy + self.h)
            bearing = row.get("bearing")
            if bearing is not None and bearing == bearing:
                self._last_angle = self.tm.bearing_point(float(bearing))
            self._last_xy = (float(x), float(y))
        if not good:
            # Hold the last known position and dim the marker rather than
            # letting it jump to a bad fix.
            alpha_scale = self.dim_alpha
        if self._last_xy is None:
            return

        c = self.marker_colour
        colour = (c[0], c[1], c[2], (c[3] if len(c) > 3 else 1.0) * alpha_scale)
        filled_triangle(ctx, self._last_xy[0], self._last_xy[1],
                        self.marker_size, self._last_angle, colour)
