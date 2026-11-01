"""Track map file format, GPS -> map-space projection, SVG preview.

The runtime must reuse the map's stored transform verbatim. Recomputing an
extent from the current run's own bounds makes the marker drift whenever a run
happens to run wide or short, which is exactly the failure this format exists to
prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import geo

FORMAT_VERSION = 1


@dataclass
class TrackMap:
    name: str
    origin: dict
    rotation: list
    extent_m: dict
    points_unit: list
    start_finish: dict = field(default_factory=dict)
    # An autocross course is open: it starts and finishes in different places, so
    # the polyline must not be closed and there is no single start/finish trap.
    closed: bool = True
    start: dict = field(default_factory=dict)
    finish: dict = field(default_factory=dict)
    length_m: float = 0.0
    lap_count: int = 0
    source_runs: list = field(default_factory=list)
    created: str = ""
    orientation: str = "north"
    version: int = FORMAT_VERSION
    path: Path | None = None

    # ---- geometry ------------------------------------------------------
    @property
    def rot(self):
        return np.asarray(self.rotation, dtype=float)

    @property
    def aspect(self):
        """width / height in unit space (always 1.0 for a fit-to-box map, so
        derive it from the metric extent instead)."""
        ex = self.extent_m["x"]
        ey = self.extent_m["y"]
        w = ex[1] - ex[0]
        h = ey[1] - ey[0]
        return (w / h) if h else 1.0

    @property
    def unit_points(self):
        return np.asarray(self.points_unit, dtype=float)

    def project_point(self, lat, lon):
        """Scalar convenience wrapper around :meth:`project`.

        `project` is vectorised, so with scalar input it still hands back
        one-element arrays; the frame loop wants plain floats.
        """
        u, v = self.project(lat, lon)
        return float(np.ravel(u)[0]), float(np.ravel(v)[0])

    def bearing_point(self, bearing_deg):
        return float(np.ravel(self.project_bearing(bearing_deg))[0])

    def project(self, lat, lon):
        """lat/lon -> unit map coordinates (0..1, Y already flipped for screen).

        Points outside the map's extent project outside 0..1, which is correct:
        the pit lane genuinely is off the racing line.
        """
        x, y = geo.to_xy(lat, lon, self.origin["lat"], self.origin["lon"])
        x, y = geo.apply_rotation(self.rot, x, y)
        return _normalise(x, y, self.extent_m)

    def project_bearing(self, bearing_deg):
        """Compass bearing -> screen-space rotation in radians.

        The stored rotation matrix turns the map; the marker has to turn with it.
        Screen Y is down, so the sign flips once more.
        """
        b = np.radians(np.asarray(bearing_deg, dtype=float))
        # Unit heading vector in local ENU metres.
        hx, hy = np.sin(b), np.cos(b)
        rx, ry = geo.apply_rotation(self.rot, hx, hy)
        return np.arctan2(rx, ry)  # 0 = up the screen, clockwise positive

    # ---- io ------------------------------------------------------------
    def to_dict(self):
        return {
            "version": self.version,
            "name": self.name,
            "created": self.created,
            "orientation": self.orientation,
            "source_runs": list(self.source_runs),
            "lap_count": self.lap_count,
            "origin": self.origin,
            "rotation": [list(map(float, r)) for r in self.rotation],
            "extent_m": self.extent_m,
            "points_unit": [[round(float(a), 6), round(float(b), 6)]
                            for a, b in self.points_unit],
            "start_finish": self.start_finish,
            "closed": bool(self.closed),
            "start": self.start,
            "finish": self.finish,
            "length_m": round(float(self.length_m), 2),
        }

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        self.path = path
        return path


def load(path):
    path = Path(path)
    d = json.loads(path.read_text(encoding="utf-8"))
    return TrackMap(
        name=d.get("name", path.stem),
        origin=d["origin"],
        rotation=d["rotation"],
        extent_m=d["extent_m"],
        points_unit=d["points_unit"],
        start_finish=d.get("start_finish", {}),
        closed=bool(d.get("closed", True)),
        start=d.get("start", {}),
        finish=d.get("finish", {}),
        length_m=d.get("length_m", 0.0),
        lap_count=d.get("lap_count", 0),
        source_runs=d.get("source_runs", []),
        created=d.get("created", ""),
        orientation=d.get("orientation", "north"),
        version=d.get("version", 0),
        path=path,
    )


def _normalise(x, y, extent_m):
    """Metres -> 0..1 box, preserving aspect, Y flipped for screen space."""
    ex, ey = extent_m["x"], extent_m["y"]
    w = ex[1] - ex[0]
    h = ey[1] - ey[0]
    span = max(w, h) or 1.0
    # Scale both axes by the same factor so the shape is not stretched, then
    # centre the short axis in its own 0..1 range.
    u = (np.asarray(x, float) - ex[0]) / span + (span - w) / (2.0 * span)
    v = (np.asarray(y, float) - ey[0]) / span + (span - h) / (2.0 * span)
    return u, 1.0 - v


def extent_from(x, y, pad_frac=0.02):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    px = (x.max() - x.min()) * pad_frac
    py = (y.max() - y.min()) * pad_frac
    return {"x": [float(x.min() - px), float(x.max() + px)],
            "y": [float(y.min() - py), float(y.max() + py)]}


def auto_select(maps_dir, lat, lon, max_km=5.0):
    """Pick the map whose origin is nearest a run's mean position."""
    best, best_d = None, float("inf")
    for p in sorted(Path(maps_dir).glob("*.json")):
        try:
            m = load(p)
        except Exception:
            continue
        dx, dy = geo.to_xy([lat], [lon], m.origin["lat"], m.origin["lon"])
        d = float(np.hypot(dx[0], dy[0]))
        if d < best_d:
            best, best_d = m, d
    if best is not None and best_d <= max_km * 1000.0:
        return best, best_d
    return None, best_d


def write_svg(tm, path, size=520, stroke=6.0, outline=2.0):
    """Preview so the map can be eyeballed without running a render."""
    path = Path(path)
    pts = tm.unit_points
    ar = tm.aspect
    if ar >= 1.0:
        w, h = size, size / ar
    else:
        w, h = size * ar, size
    # points_unit is a square 0..1 box with the short axis centred; undo that
    # centring for the preview so the SVG is tight to the track.
    sx, sy = (size, size)
    ox = (sx - w) / 2.0
    oy = (sy - h) / 2.0
    px = pts[:, 0] * sx - ox
    py = pts[:, 1] * sy - oy
    d = "M " + " L ".join("%.2f,%.2f" % (a, b) for a, b in zip(px, py))
    if tm.closed:
        d += " Z"

    sf = tm.start_finish or {}
    sf_svg = ""
    if "point" in sf:
        cx = sf["point"][0] * sx - ox
        cy = sf["point"][1] * sy - oy
        nx, ny = sf.get("normal_unit", [0.0, 1.0])
        # Draw the trap across the track, i.e. perpendicular to travel.
        tx, ty = -ny, nx
        ln = (tx * tx + ty * ty) ** 0.5 or 1.0
        tx, ty = tx / ln * 16.0, ty / ln * 16.0
        sf_svg = ('  <line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" '
                  'stroke="#e01b1b" stroke-width="3"/>\n'
                  % (cx - tx, cy - ty, cx + tx, cy + ty))

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
        'viewBox="0 0 %.0f %.0f">\n' % (w, h, w, h)
        + '  <rect width="100%%" height="100%%" fill="#20242b"/>\n'
        + '  <path d="%s" fill="none" stroke="#000" stroke-width="%.2f" '
          'stroke-linejoin="round" stroke-linecap="round"/>\n'
          % (d, stroke + 2 * outline)
        + '  <path d="%s" fill="none" stroke="#fff" stroke-width="%.2f" '
          'stroke-linejoin="round" stroke-linecap="round"/>\n' % (d, stroke)
        + sf_svg
        + ("" if tm.closed else _ends_svg(px[0], py[0], px[-1], py[-1]))
        + '  <text x="8" y="18" fill="#9aa3af" font-family="sans-serif" '
          'font-size="13">%s  %.0f m  %d laps  (%s)</text>\n'
          % (_esc(tm.name), tm.length_m, tm.lap_count,
             tm.orientation + ("" if tm.closed else ", open course"))
        + "</svg>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return path


def _ends_svg(sx, sy, fx, fy):
    """Green start, red finish -- an open course has two distinct ends."""
    return ('  <circle cx="%.2f" cy="%.2f" r="5" fill="#3ddc84"/>\n'
            '  <circle cx="%.2f" cy="%.2f" r="5" fill="#e01b1b"/>\n'
            % (sx, sy, fx, fy))


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
