"""Lap slicing and start/finish recovery from `lap_number` alone.

The CSV carries no trap geometry, so the start/finish line is reconstructed from
where `lap_number` increments. In this dataset `lap_number` is NaN until the
first crossing, so the NaN -> 1 transition is itself a crossing and must be
counted; laps are then the spans between consecutive crossings, which already
excludes the out-lap and the (unterminated) in-lap.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import geo


@dataclass
class Lap:
    """One complete lap: a half-open index span [i0, i1) into the source rows."""

    run_stem: str
    number: int
    i0: int
    i1: int
    t0: float
    t1: float
    distance_m: float
    fragment: float = 0.0

    @property
    def duration(self):
        return self.t1 - self.t0


def crossing_indices(lap_number):
    """Indices `i` such that the start/finish line was crossed between row `i`
    and row `i + 1`.

    Handles the NaN prefix: the first row with a lap number is a crossing.
    """
    lap = np.asarray(lap_number, dtype=float)
    have = np.isfinite(lap)
    if not have.any():
        return np.array([], dtype=int)

    out = []
    first = int(np.argmax(have))
    if first > 0:
        out.append(first - 1)
    # Increments within the numbered region.
    filled = lap.copy()
    filled[:first] = lap[first]
    inc = np.flatnonzero(np.diff(np.nan_to_num(_ffill(filled))) > 0)
    out.extend(int(i) for i in inc)
    return np.unique(np.asarray(out, dtype=int))


def _ffill(a):
    a = np.asarray(a, dtype=float)
    idx = np.where(np.isfinite(a), np.arange(a.size), 0)
    np.maximum.accumulate(idx, out=idx)
    return a[idx]


def start_finish(runs, max_accuracy_m=10.0, origin=None, iterations=2):
    """Estimate the start/finish line from every crossing in every run.

    Returns ``(point_xy, normal_xy, origin_latlon, crossing_count)`` with
    ``point_xy`` in metres relative to ``origin``.

    A first pass takes the midpoint of each straddling sample pair. That is
    quantised by the sample rate (~2 m at 40 m/s and 20 Hz), so a second pass
    intersects each straddling segment with the line implied by the first pass
    and re-centres on the exact intersections.
    """
    if origin is None:
        origin = runs[0].mean_latlon(max_accuracy_m)
    olat, olon = origin

    segs, headings = [], []
    for run in runs:
        lap = run.values("lap_number")
        idx = crossing_indices(lap)
        if idx.size == 0:
            continue
        x, y = geo.to_xy(run.values("latitude"), run.values("longitude"), olat, olon)
        brg = run.values("bearing")
        good = run.good_mask(max_accuracy_m)
        for i in idx:
            if i + 1 >= len(x) or not (good[i] and good[i + 1]):
                continue
            segs.append((x[i], y[i], x[i + 1], y[i + 1]))
            headings.append(brg[i])
    if not segs:
        raise ValueError("no usable start/finish crossings found")

    segs = np.asarray(segs, dtype=float)
    hdg = geo.circular_mean_deg(headings)
    # Direction of travel through the line; the line's normal points along it.
    normal = np.array([np.sin(np.radians(hdg)), np.cos(np.radians(hdg))])
    point = np.c_[(segs[:, 0] + segs[:, 2]) / 2.0,
                  (segs[:, 1] + segs[:, 3]) / 2.0].mean(axis=0)

    for _ in range(max(0, iterations - 1)):
        hits = []
        for x0, y0, x1, y1 in segs:
            a = np.array([x0, y0])
            d = np.array([x1 - x0, y1 - y0])
            denom = float(normal @ d)
            if abs(denom) < 1e-9:
                continue
            s = float(normal @ (point - a)) / denom
            if -0.5 <= s <= 1.5:  # allow slight overshoot, reject nonsense
                hits.append(a + s * d)
        if len(hits) >= 2:
            point = np.mean(hits, axis=0)

    return point, normal, (olat, olon), len(segs)


def laps(run, max_accuracy_m=10.0):
    """Complete laps in a run, in order.

    Spans between consecutive crossings only, so the out-lap (before the first
    crossing) and the in-lap (after the last) are excluded by construction.
    """
    idx = crossing_indices(run.values("lap_number"))
    if idx.size < 2:
        return []
    t = run.values("timestamp")
    lap = _ffill(run.values("lap_number"))
    dist = run.values("distance_traveled")
    frag = (run.values("fragment_id") if run.has("fragment_id")
            else np.zeros(len(t)))
    out = []
    for a, b in zip(idx[:-1], idx[1:]):
        i0, i1 = int(a) + 1, int(b) + 1
        if i1 - i0 < 10:
            continue
        # A lap that straddles a session pause is not a lap.
        if np.nanmax(frag[i0:i1]) != np.nanmin(frag[i0:i1]):
            continue
        n = lap[i0]
        out.append(Lap(run_stem=run.stem,
                       number=int(n) if np.isfinite(n) else -1,
                       i0=i0, i1=i1,
                       t0=float(t[i0]), t1=float(t[i1 - 1]),
                       distance_m=float(dist[i1 - 1] - dist[i0]),
                       fragment=float(frag[i0])))
    return out


def filter_laps(all_laps, exclude_edge=True, max_time_factor=1.25,
                min_time_factor=0.75, verbose=True):
    """Drop out/in-adjacent laps and outliers by time.

    `exclude_edge` drops the first and last complete lap of each run (still
    warm-up or already cooling). The time filter then rejects laps deviating
    from the median by more than the given factors -- this is what catches a
    cool-down or off-track lap.
    """
    kept, dropped = [], []
    if exclude_edge:
        by_run = {}
        for lp in all_laps:
            by_run.setdefault((lp.run_stem, lp.fragment), []).append(lp)
        staged = []
        for key, group in by_run.items():
            group.sort(key=lambda l: l.t0)
            if len(group) <= 2:
                dropped.extend((l, "edge lap (run has %d)" % len(group)) for l in group)
                continue
            dropped.append((group[0], "first complete lap"))
            dropped.append((group[-1], "last complete lap"))
            staged.extend(group[1:-1])
        all_laps = staged

    if not all_laps:
        return [], dropped

    times = np.array([l.duration for l in all_laps])
    med = float(np.median(times))
    for lp in all_laps:
        if lp.duration > med * max_time_factor:
            dropped.append((lp, "%.1fs > %.2fx median %.1fs"
                            % (lp.duration, max_time_factor, med)))
        elif lp.duration < med * min_time_factor:
            dropped.append((lp, "%.1fs < %.2fx median %.1fs"
                            % (lp.duration, min_time_factor, med)))
        else:
            kept.append(lp)
    return kept, dropped
