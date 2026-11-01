"""Finding the timed runs inside a session.

`window.driving_window` answers "when was the car moving", which is the right
question for trimming an export and the wrong one for comparing. A RaceChrono
session is whatever was recorded between two taps of a button, and that is not a
fixed unit: ten autocross sessions held exactly one run each, four circuit
sessions held a whole evening of laps, and six SCCA sessions held one or two runs
apiece with staging in between.

## The rule, and why it needs no tuned constant

Measured across all twenty sessions, movement segments -- speed above 3 m/s, gaps
under 8 s bridged -- came out like this:

    SCCA 121241     85s/1278m    4s/15m    107s/1348m
    SCCA 133056     20s/81m      4s/13m    104s/1349m
    SCCA 134654     100s/1341m
    Grip  (x10)     39-43s / 496-539m
    AMP   (x4)      919-1256s / 20548-26428m

Within a session the real runs are within a few percent of each other in
distance -- 1278 against 1348 m, 95% -- and everything that is not a run is far
smaller: 81 m is 6% of that session's longest segment, 15 m is 1%. So a run is
**a movement segment whose length is comparable to the longest one in the
session**, which is self-referential and therefore does not need a metres
threshold that a different course would invalidate. An absolute cutoff tuned on
these files is exactly the kind of thing that breaks on the next dataset.

The circuit sessions fall out of the same rule with one segment each, which is
the whole session's driving: a circuit session is one continuous run containing
many laps, and laps are found by `laps.laps` instead.

## What this deliberately does not do

It does not decide whether a segment is a *timed* run. Without a start/finish
trap there is nothing in the data that distinguishes a competition run from a fun
run, and guessing would be inventing a fact. `distance_m` is reported so a caller
can filter, and `tools/dataset_report.py` prints it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import geo

# Speed above which the car counts as moving. Well above GPS noise at a
# standstill and well below any cornering speed seen here (minimum apex speed
# across the AMP laps was 13.6 m/s).
MOVE_MS = 3.0

# Two movement bursts closer together than this are one run with a slow section
# in it, not two runs. The shortest real gap between runs in these sessions was
# 20 s of staging; the longest within-run dip below 3 m/s was under 4 s.
GAP_S = 8.0

# A segment shorter than this fraction of the session's longest is not a run.
# Measured margin: the smallest real second run was 95% of its session's longest,
# the largest piece of non-run movement was 6%.
MIN_FRACTION = 0.40

# Below this there is not enough of a path to compare or map, whatever it was.
MIN_DISTANCE_M = 100.0
MIN_DURATION_S = 10.0


@dataclass
class RunSpan:
    """One continuous piece of driving, as a half-open row range."""

    index: int                  # 1-based, in time order within the session
    i0: int
    i1: int                     # exclusive
    t0: float                   # unix
    t1: float
    distance_m: float
    peak_speed_ms: float
    fraction: float             # of the session's longest segment

    @property
    def duration(self):
        return self.t1 - self.t0


def _segments(run, move_ms, gap_s):
    """Contiguous stretches of movement, with short gaps bridged."""
    ts = run.values("timestamp")
    spd = run.values("speed_gps")
    moving = np.isfinite(spd) & (spd > float(move_ms))
    if not moving.any():
        return []
    edges = np.flatnonzero(np.diff(np.r_[0, moving.view(np.int8), 0]))
    out = []
    for a, b in edges.reshape(-1, 2):
        b = min(int(b), ts.size - 1)
        a = int(a)
        if out and ts[a] - ts[out[-1][1]] < float(gap_s):
            out[-1][1] = b
        else:
            out.append([a, b])
    return out


def detect(run, move_ms=MOVE_MS, gap_s=GAP_S, min_fraction=MIN_FRACTION,
           min_distance_m=MIN_DISTANCE_M, min_duration_s=MIN_DURATION_S,
           max_accuracy_m=10.0):
    """The runs in this session, in time order.

    Always returns at least one span when the car moved at all: a session with a
    single continuous stretch of driving is one run, which is what a circuit
    session and a single-run autocross session both are.
    """
    ts = run.values("timestamp")
    spd = run.values("speed_gps")
    lat = run.values("latitude")
    lon = run.values("longitude")
    good = run.good_mask(max_accuracy_m)
    origin = run.mean_latlon(max_accuracy_m)

    raw = []
    for a, b in _segments(run, move_ms, gap_s):
        m = np.zeros(ts.shape, bool)
        m[a:b + 1] = True
        m &= good & np.isfinite(lat) & np.isfinite(lon)
        if m.sum() < 10:
            continue
        x, y = geo.to_xy(lat[m], lon[m], *origin)
        dist = float(geo.cumulative_distance(x, y)[-1])
        peak = float(np.nanmax(spd[a:b + 1])) if b >= a else 0.0
        raw.append((a, b, dist, peak))
    if not raw:
        return []

    longest = max(d for _a, _b, d, _p in raw)
    out = []
    for a, b, dist, peak in raw:
        frac = dist / longest if longest > 0 else 0.0
        if frac < min_fraction:
            continue
        if dist < min_distance_m:
            continue
        if ts[b] - ts[a] < min_duration_s:
            continue
        out.append(RunSpan(index=len(out) + 1, i0=a, i1=b + 1,
                           t0=float(ts[a]), t1=float(ts[b]),
                           distance_m=round(dist, 1),
                           peak_speed_ms=round(peak, 2),
                           fraction=round(frac, 4)))
    for i, r in enumerate(out, start=1):
        r.index = i
    return out


def describe(run, spans=None):
    """One line per run, for a report."""
    spans = detect(run) if spans is None else spans
    return ["run %d: %.1f s, %.0f m, peak %.1f m/s (%.0f%% of the longest)"
            % (s.index, s.duration, s.distance_m, s.peak_speed_ms,
               100 * s.fraction)
            for s in spans]
