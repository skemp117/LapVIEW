"""The session model: everything the browser needs about one run, as JSON.

This is deliberately separate from the server. The server does routing and
bytes; this decides *what a session is* -- which channels exist and what they
span, where the laps are, which map covers it, which clips cover which part of
the timeline, and how video time relates to data time. All of that is testable
without a socket.

## One clock

Every time in this model is **seconds since the run's first sample**, called `t`.
Not unix time (13 significant digits before the decimal point, which JSON floats
and a `<input type=range>` both handle badly), and not video time (there may be
several clips, each starting at a different point).

Converting to a clip's own playback position is the only place the offset
matters:

    video_time = t + run.t0 - clip.data_t0

`clip.data_t0` already carries the audio-correlation offset, so a page that
follows this one rule is synchronised for free.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from racechrono import csv_io, job as job_mod, laps as laps_mod, video, window

# Channels worth putting in front of someone first, in this order. Everything
# else the run happens to carry is still listed, just further down.
PREFERRED = (
    "speed_gps", "rpm", "throttle_position", "brake_pressure",
    "imu_lateral_acc", "imu_longitudinal_acc", "imu_combined_acc",
    "lateral_acc", "longitudinal_acc", "combined_acc",
    "imu_yaw_rate", "steering_angle", "gear",
    "coolant_temperature", "oil_temperature", "oil_pressure",
    "intake_air_temperature", "engine_load", "fuel_level",
    "lean_angle", "altitude", "bearing", "accuracy",
)

# Not interesting as a trace: bookkeeping, or a copy of the clock.
HIDE = {"timestamp", "utc_time", "lap_number", "fragment_id", "fix_type",
        "latitude", "longitude", "distance_traveled"}


def _finite(a):
    return a[np.isfinite(a)]


def _decimate(t, v, n):
    """Reduce a series to at most `n` points, keeping both extremes of each bucket.

    Plain subsampling drops the peaks, and a peak is usually the point -- the
    brake trace is mostly zero with brief spikes, and a 20000-sample run drawn
    into an 800 px trace loses them entirely. Min/max per bucket keeps the
    envelope, so what is drawn is never quieter than the data.
    """
    if t.size == 0:
        return [], []
    if t.size <= n:
        ok = np.isfinite(v)
        return (np.round(t[ok], 3).tolist(),
                np.round(v[ok], 4).tolist())
    # Two points per bucket -- a minimum and a maximum -- so the number of
    # buckets is half the point budget.
    edges = np.linspace(0, t.size, max(2, n // 2) + 1).astype(int)
    out_t, out_v = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        seg = v[a:b]
        ok = np.isfinite(seg)
        if not ok.any():
            continue
        st = t[a:b][ok]
        sv = seg[ok]
        lo, hi = int(np.argmin(sv)), int(np.argmax(sv))
        # Emit in time order so the polyline does not zigzag backwards.
        for j in sorted({lo, hi}):
            out_t.append(round(float(st[j]), 3))
            out_v.append(round(float(sv[j]), 4))
    return out_t, out_v


def channel_list(run):
    """Every plottable channel, preferred ones first, with its span.

    The span comes from the data rather than from a table of expected ranges:
    the throttle in this car reads 11.37 to 85.49, not 0 to 100, and a trace
    scaled to a nominal range would be a flat line two thirds of the way up.
    """
    out, seen = [], set()
    names = list(run.df.columns)
    order = [c for c in PREFERRED if c in names or run.has(c)]
    order += [c for c in names if c not in order]
    for name in order:
        try:
            col = run.col(name)
        except KeyError:
            continue
        if col in seen or col in HIDE:
            continue
        seen.add(col)
        v = _finite(run.values(col))
        if v.size == 0 or float(v.max() - v.min()) == 0.0:
            # A channel that never changes -- `altitude` is a constant 0 in this
            # dataset, `fix_type` a constant 1 -- has nothing to show.
            continue
        out.append({
            "name": col,
            "unit": run.units.get(col, ""),
            "min": round(float(v.min()), 4),
            "max": round(float(v.max()), 4),
            "p1": round(float(np.percentile(v, 1)), 4),
            "p99": round(float(np.percentile(v, 99)), 4),
            "coverage": round(float(v.size) / float(run.df.shape[0]), 4),
            "kind": ("circular" if col in csv_io.CIRCULAR
                     else "step" if col in csv_io.STEP else "continuous"),
            "preferred": col in PREFERRED,
        })
    return out


def trace(run, channel, n=1200, t0=None, t1=None):
    """One channel, decimated to `n` buckets, in run-relative seconds."""
    col = run.col(channel)
    t = run.values("timestamp") - run.t0
    v = run.values(col)
    if t0 is not None or t1 is not None:
        m = np.ones(t.shape, bool)
        if t0 is not None:
            m &= t >= float(t0)
        if t1 is not None:
            m &= t <= float(t1)
        t, v = t[m], v[m]
    tt, vv = _decimate(t, v, int(n))
    return {"channel": col, "unit": run.units.get(col, ""),
            "t": tt, "v": vv, "n": len(tt)}


def lap_table(run, cfg=None):
    """Laps with a best-lap flag, in run-relative seconds.

    An autocross run crosses the line once, so this is legitimately empty and
    the page has to cope rather than assume a lap list exists.
    """
    got = laps_mod.laps(run)
    if not got:
        return []
    best = min(range(len(got)), key=lambda i: got[i].duration)
    kept, dropped = laps_mod.filter_laps(got, exclude_edge=True)
    keep_keys = {(l.number, l.i0) for l in kept}
    why = {(l.number, l.i0): w for l, w in dropped}
    rows = []
    for i, l in enumerate(got):
        key = (l.number, l.i0)
        rows.append({
            "number": l.number,
            "t0": round(l.t0 - run.t0, 3),
            "t1": round(l.t1 - run.t0, 3),
            "duration": round(l.duration, 3),
            "label": window.format_lap_time(l.duration),
            "distance_m": round(float(l.distance_m), 1),
            "best": i == best,
            "representative": key in keep_keys,
            "excluded_because": why.get(key, ""),
        })
    return rows


def position_track(run, tm, max_accuracy_m=10.0, n=2000):
    """The driven line in the map's own unit coordinates.

    Reusing the map's stored transform rather than fitting one to this run is the
    whole point of the map file: a run that goes wide, or one that never leaves
    the pit lane, must not move the coordinate system under the marker.
    """
    good = run.good_mask(max_accuracy_m)
    lat = run.values("latitude")
    lon = run.values("longitude")
    t = run.values("timestamp") - run.t0
    m = good & np.isfinite(lat) & np.isfinite(lon)
    if not m.any():
        return {"t": [], "u": [], "v": []}
    idx = np.flatnonzero(m)
    if idx.size > n:
        idx = idx[np.linspace(0, idx.size - 1, n).astype(int)]
    u, v = tm.project(lat[idx], lon[idx])
    return {"t": np.round(t[idx], 3).tolist(),
            "u": np.round(np.asarray(u), 5).tolist(),
            "v": np.round(np.asarray(v), 5).tolist()}


def map_payload(tm):
    if tm is None:
        return None
    return {
        "name": tm.name,
        "closed": bool(tm.closed),
        "aspect": round(float(tm.aspect), 5),
        "length_m": round(float(tm.length_m), 1),
        "lap_count": int(tm.lap_count),
        "points": [[round(float(a), 5), round(float(b), 5)]
                   for a, b in tm.unit_points],
        "start_finish": (tm.start_finish or {}).get("point"),
        "sf_normal": (tm.start_finish or {}).get("normal_unit"),
        "start": (tm.start or {}).get("point"),
        "finish": (tm.finish or {}).get("point"),
        # Metres per unit of map space: the scale bar and any distance the page
        # reports come from this, so it has to travel with the geometry.
        "span_m": round(float(max(tm.extent_m["x"][1] - tm.extent_m["x"][0],
                                  tm.extent_m["y"][1] - tm.extent_m["y"][0])), 2),
    }


def clip_payload(clips, run):
    """Clips as timeline spans, with what is known about each one's sync.

    `sync_confidence` is reported rather than hidden. A clip that correlated at
    12 sd and one that fell back to container timestamps both play, but only one
    of them can be trusted to the frame, and someone reading a trace against the
    video needs to know which they have.
    """
    out = []
    for i, c in enumerate(clips):
        out.append({
            "index": i,
            "name": c.path.name,
            "t0": round(c.data_t0 - run.t0, 3),
            "t1": round(c.data_t1 - run.t0, 3),
            "duration": round(float(c.duration), 3),
            "fps": round(float(c.fps), 4),
            "size": list(c.display_size),
            "rotation": int(c.rotation),
            "has_audio": bool(c.has_audio),
            "sync_method": c.sync_method,
            "sync_confidence": (None if not math.isfinite(c.sync_confidence)
                                else round(float(c.sync_confidence), 2)),
            "offset": round(float(c.offset), 3),
        })
    return out


def imu_payload(run):
    cal = run.imu
    if cal is None:
        return None
    return {
        "trusted": bool(cal.trusted),
        "fit_r": round(float(cal.fit_r), 3),
        "g_magnitude": round(float(cal.g_magnitude), 4),
        "tilt_deg": round(float(cal.tilt_deg), 1),
        "heading_offset_deg": round(float(cal.heading_offset_deg), 1),
        "still_seconds": round(float(cal.still_seconds), 2),
        # +/-1, and it arrives as -0.9999999999999998 from the SVD determinant.
        "handed": round(float(cal.handed), 3),
        "notes": list(cal.notes) if cal.notes else [],
    }


def describe(run, cfg, tm=None, clips=(), trace_points=1200):
    """The whole session, as one JSON-safe dict."""
    if tm is None:
        tm, _dist = job_mod._select_map(cfg, run)
    target = window.session_target(run, cfg)
    laps = lap_table(run, cfg)
    channels = channel_list(run)
    default = next((c["name"] for c in channels if c["preferred"]),
                   channels[0]["name"] if channels else None)
    return {
        "stem": run.stem,
        "file": Path(run.path).name,
        "title": run.meta.get("Session") or run.meta.get("Note") or run.stem,
        "meta": {k: v for k, v in run.meta.items() if isinstance(v, str)},
        "t0_unix": round(float(run.t0), 3),
        "duration": round(float(run.duration), 3),
        "rows": int(run.df.shape[0]),
        "sample_hz": round(run.df.shape[0] / max(run.duration, 1e-6), 1),
        "driving": {"t0": round(target.t0 - run.t0, 3),
                    "t1": round(target.t1 - run.t0, 3)},
        "laps": laps,
        "channels": channels,
        "default_channel": default,
        "map": map_payload(tm),
        "track": position_track(run, tm, cfg.quality.max_accuracy_m) if tm else None,
        "clips": clip_payload(clips, run),
        "imu": imu_payload(run),
        "traces": ({default: trace(run, default, trace_points)}
                   if default else {}),
    }


def summarise(run, cfg, clips=()):
    """The one-line form for a session list: cheap, no map, no traces."""
    got = laps_mod.laps(run)
    return {
        "stem": run.stem,
        "file": Path(run.path).name,
        "title": run.meta.get("Session") or run.stem,
        "t0_unix": round(float(run.t0), 3),
        "duration": round(float(run.duration), 3),
        "laps": len(got),
        "best_lap": (round(min(l.duration for l in got), 3) if got else None),
        "clips": len(clips),
        "has_imu": run.imu is not None,
    }
