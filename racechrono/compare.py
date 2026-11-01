"""Comparing laps: a shared distance coordinate, time deltas, sector times.

Everything here rests on one idea. Two laps cannot be compared against elapsed
time, because at four seconds into the lap the two cars are in different places
and the difference between their speeds means nothing. They have to be compared
against **distance along the track**, so that every comparison is between two
moments at the same point on the circuit.

The distance comes from projecting each lap onto the map's centreline --
`geo.project_to_polyline`, the same primitive the map builder uses to average
traversals. Not from `distance_traveled`, which is the distance *that car*
travelled: a lap that ran wide accumulates more of it, so two laps would drift
out of step by exactly the amount of the line difference. On the AMP laps that is
83.7 m over 2929 m, 2.86%.

## What the delta trace actually says

`delta(s)` is how much time this lap has lost to the reference by the point `s`.
Its *slope* is what matters and is what people misread: a delta that is flat
through a corner means the corner was equal, however far below zero the line
sits. The rising and falling bits are where time changes hands. Both are
reported -- `delta` and `rate` -- because a plot of the delta alone invites
"I lost it all at turn 4" when turn 4 was where an earlier mistake finished
being paid for.

## Where the data is not there

A comparison interpolates between samples, and at AMP that quietly invents a
sizeable piece of every lap. Fourteen of twenty-seven laps have a single gap of
250 to 300 m -- and every one of them is in the same place, roughly 360 m to
690 m along the line, where `accuracy` blows out from its 10.0 m floor to as much
as 49 m and the quality gate drops the samples. It is a GPS shadow at that part
of the circuit, not a random dropout: it recurs in all four sessions, lasting 7
to 19 s each time. The ten autocross runs at the other venue have no gap over
2 m.

Straight-line interpolation across 250 m of a 2929 m lap is 9% of a delta trace
that looks exactly as authoritative as the rest of it. So every traversal
reports its `gaps`, the plots draw those stretches dimmed, and `losses` says what
fraction of each stretch was interpolated. A comparison that cannot see through a
tree is fine; one that does not say so is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import geo, laps as laps_mod, runs as runs_mod, window


@dataclass
class Traversal:
    """One timed pass over the reference line: a circuit lap, or a whole run.

    An autocross course is crossed once, so a "lap" there is the run itself.
    Keeping both in one type means the comparison code does not care which it
    has, and a course session gets run-to-run comparison for free.
    """

    key: str                    # "<run stem>#<n>", unique across a comparison
    run_stem: str
    label: str
    number: int | None
    t0: float                   # unix, first sample used
    t1: float
    duration: float             # the timed duration, from the lap boundaries
    s: np.ndarray = field(repr=False, default=None)      # metres along the line
    t: np.ndarray = field(repr=False, default=None)      # seconds from its start
    perp: np.ndarray = field(repr=False, default=None)   # metres off the line
    channels: dict = field(repr=False, default_factory=dict)
    coverage: tuple = (0.0, 0.0)    # the span of the line this actually covers

    @property
    def label_full(self):
        return "%s %s" % (self.run_stem, self.label)


def centreline(tm):
    """The map's polyline in unit space, with the metres-per-unit scale.

    Working in unit space rather than converting back to metres keeps this using
    the map's own stored transform verbatim -- the same one the marker and the
    overlay use. Arc lengths in unit space are metres once multiplied by `span`,
    because `_normalise` scales both axes by the same factor.
    """
    pts = tm.unit_points
    span = float(max(tm.extent_m["x"][1] - tm.extent_m["x"][0],
                     tm.extent_m["y"][1] - tm.extent_m["y"][0]))
    if tm.closed and not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[:1]])
    return pts, span


def _line_length(tm):
    pts, span = centreline(tm)
    return float(geo.cumulative_distance(pts[:, 0], pts[:, 1])[-1]) * span


def _monotonic(s, t, *arrays):
    """Force `s` to increase, and drop the samples that do not advance.

    Projection is not monotonic in the small: standing still puts a dozen samples
    within a metre of each other in whatever order the noise decides, and
    `np.interp` needs an increasing x. Taking a running maximum and then keeping
    only strict advances is the least clever fix and the one that cannot invent
    positions the car was never at.
    """
    s = np.maximum.accumulate(s)
    keep = np.r_[True, np.diff(s) > 1e-6]
    out = [s[keep], t[keep]]
    out += [np.asarray(a)[keep] for a in arrays]
    return out



def gaps_of(tv, gap_m=20.0):
    """Stretches of the line where this traversal has no samples.

    `gap_m` is deliberately well above the normal spacing: at 40 m/s and about
    30 Hz of usable fixes the median step is under a metre, so 20 m means a
    second of missing data rather than ordinary jitter.
    """
    d = np.diff(tv.s)
    out = []
    for i in np.flatnonzero(d > float(gap_m)):
        out.append((float(tv.s[i]), float(tv.s[i + 1]),
                    float(tv.t[i + 1] - tv.t[i])))
    return out


def _gap_mask(grid, gaps):
    """True where the grid falls inside a gap, so it can be drawn as unmeasured."""
    m = np.zeros(grid.shape, bool)
    for a, b, _dt in gaps:
        m |= (grid >= a) & (grid <= b)
    return m

def traversal(run, tm, i0, i1, key, label, number=None, channels=(),
              max_accuracy_m=10.0):
    """Build a `Traversal` from a row span of a run.

    Returns None when the span has too little usable position data to place on
    the line -- a lap recorded through a GPS dropout cannot be compared, and
    guessing would put a delta trace in front of someone that is really an
    artefact of a bad fix.
    """
    pts, span = centreline(tm)
    ts = run.values("timestamp")[i0:i1]
    lat = run.values("latitude")[i0:i1]
    lon = run.values("longitude")[i0:i1]
    good = run.good_mask(max_accuracy_m)[i0:i1]
    ok = good & np.isfinite(lat) & np.isfinite(lon)
    if ok.sum() < 32:
        return None

    u, v = tm.project(lat[ok], lon[ok])
    length = float(geo.cumulative_distance(pts[:, 0], pts[:, 1])[-1]) * span
    # Sequential forward-only matching, in unit space, with the window scaled
    # into unit distance. Independent per-sample projection plus a running
    # maximum collapsed entire SCCA runs into the last ten metres of the line --
    # see `geo.match_polyline`.
    s_unit, perp_unit = geo.match_polyline(
        pts[:, 0], pts[:, 1], u, v, closed=bool(tm.closed),
        slack_m=8.0 / span, min_window_m=25.0 / span,
        window_factor=4.0)
    s = s_unit * span
    perp = perp_unit * span

    t = ts[ok] - float(ts[ok][0])
    extra = [run.values(c)[i0:i1][ok] for c in channels]
    s, t, perp, *extra = _monotonic(s, t, perp, *extra)
    if s.size < 32:
        return None

    return Traversal(
        key=key, run_stem=run.stem, label=label, number=number,
        t0=float(ts[ok][0]), t1=float(ts[ok][-1]),
        # The span's own elapsed time. A lap overwrites this with the lap
        # boundary duration, which is the authoritative lap time.
        duration=float(t[-1]),
        s=s, t=t, perp=perp,
        channels={c: np.asarray(a) for c, a in zip(channels, extra)},
        coverage=(float(s[0]), float(s[-1])),
    )


def traversals(runs, tm, channels=(), max_accuracy_m=10.0, exclude_edge=False,
               keys=None):
    """Every comparable pass in these runs.

    A circuit run contributes one per complete lap. A course run contributes
    itself, trimmed to the driving: an autocross run has no lap boundaries, and
    including the approach and the roll-out would make the two ends of the delta
    trace nonsense.

    `keys` restricts the work rather than the result. Projecting one lap onto the
    centreline is a nearest-segment search over every sample; building all 27 AMP
    laps in order to compare three of them threw most of that away, so the filter
    runs before the projection instead of after.
    """
    want = set(keys) if keys else None
    out = []
    for run in runs:
        lp = laps_mod.laps(run, max_accuracy_m)
        # Laps only when the map corroborates them. `lap_number` reports exactly
        # one lap on SCCA session 121241 -- an autocross course with no
        # start/finish trap -- and branching on `if laps:` treated that session as
        # a circuit and produced one lap-shaped traversal of nothing. The rule
        # lives in `shapes`; this is the same rule.
        use_laps = bool(tm.closed) and len(lp) >= 1
        if use_laps:
            if exclude_edge:
                lp, _dropped = laps_mod.filter_laps(lp, exclude_edge=True)
            for lap in lp:
                key = "%s#%d" % (run.stem, lap.number)
                if want is not None and key not in want:
                    continue
                tv = traversal(run, tm, lap.i0, lap.i1, key=key,
                               label="lap %d" % lap.number, number=lap.number,
                               channels=channels, max_accuracy_m=max_accuracy_m)
                if tv is not None:
                    tv.duration = float(lap.duration)
                    out.append(tv)
            continue

        # An open course: one traversal per detected run. A session is not a run
        # -- six SCCA sessions held one or two each -- so the runs are found
        # rather than assumed, and a single-run session still yields one.
        spans = runs_mod.detect(run, max_accuracy_m=max_accuracy_m)
        multi = len(spans) > 1
        for span in spans:
            key = "%s#run%d" % (run.stem, span.index) if multi \
                else "%s#run" % run.stem
            if want is not None and key not in want:
                continue
            tv = traversal(run, tm, span.i0, span.i1, key=key,
                           label=("run %d" % span.index) if multi else "run",
                           number=span.index if multi else None,
                           channels=channels, max_accuracy_m=max_accuracy_m)
            if tv is not None:
                out.append(tv)
    return out


def common_span(tvs, tm=None):
    """The stretch of line every traversal actually covers.

    A comparison outside it is arithmetic on extrapolation. On a circuit this is
    almost the whole lap; on an open course the runs start and stop in different
    places and the overlap can be 30 m shorter at each end.
    """
    lo = max(float(t.coverage[0]) for t in tvs)
    hi = min(float(t.coverage[1]) for t in tvs)
    return lo, hi


def resample(tvs, points=1000, span=None):
    """Put every traversal on one distance grid.

    Returns ``(grid, table)`` where `table[key]` holds `t`, each requested
    channel, and `perp`, all as functions of distance. This is the object every
    comparison below is computed from, so the interpolation happens once.
    """
    if not tvs:
        return np.zeros(0), {}
    lo, hi = span if span is not None else common_span(tvs)
    if not (hi > lo):
        return np.zeros(0), {}
    grid = np.linspace(lo, hi, int(points))
    table = {}
    for tv in tvs:
        row = {"t": np.interp(grid, tv.s, tv.t),
               "perp": np.interp(grid, tv.s, tv.perp)}
        for name, arr in tv.channels.items():
            row[name] = np.interp(grid, tv.s, arr)
        # Elapsed time is only meaningful relative to where the grid starts.
        row["t"] = row["t"] - row["t"][0]
        table[tv.key] = row
    return grid, table


def delta(table, ref_key, key):
    """Time this traversal has lost to the reference, by distance.

    Positive means slower. The derivative is returned alongside it in
    `compare()`, because the slope is the part that localises a mistake and the
    offset is the part that gets misread as one.
    """
    return table[key]["t"] - table[ref_key]["t"]


def sector_edges(length, n=8, lo=0.0):
    """`n` equal-length sectors over the compared span.

    Equal distance, not equal time: time sectors would move between laps, so two
    laps could not be compared sector by sector. RaceChrono lets you place
    sectors by hand and that is worth adding, but a fixed division of the
    centreline is stable across every session at the venue, which is what makes
    a session-to-session table mean anything.
    """
    return np.linspace(lo, lo + length, int(n) + 1)


def sectors(grid, table, edges, ref_key=None):
    """Sector times per traversal, with the best in each and the ideal lap.

    The "ideal" is the sum of the best sector times, which is a real target: it
    is a lap that the driver has already demonstrated in pieces. It is reported
    with the count of distinct traversals it came from, because an ideal built
    from six different laps is a much weaker claim than one built from two.
    """
    if grid.size == 0 or not table:
        return {"edges": [], "rows": [], "best": [], "ideal": None}
    idx = np.clip(np.searchsorted(grid, edges), 0, grid.size - 1)
    keys = list(table)
    rows = []
    for key in keys:
        t = table[key]["t"]
        times = [float(t[b] - t[a]) for a, b in zip(idx[:-1], idx[1:])]
        rows.append({"key": key, "times": [round(x, 3) for x in times],
                     "total": round(float(sum(times)), 3)})
    n_sec = len(idx) - 1
    best, owners = [], []
    for j in range(n_sec):
        vals = [(r["times"][j], r["key"]) for r in rows]
        v, who = min(vals)
        best.append(round(v, 3))
        owners.append(who)
    ideal = {
        "total": round(float(sum(best)), 3),
        "sectors": best,
        "owners": owners,
        "from_traversals": len(set(owners)),
    }
    for r in rows:
        r["gaps"] = [round(r["times"][j] - best[j], 3) for j in range(n_sec)]
        r["best_sectors"] = [j for j in range(n_sec) if owners[j] == r["key"]]
    if ref_key is not None:
        ref = next((r for r in rows if r["key"] == ref_key), None)
        if ref:
            for r in rows:
                r["vs_ref"] = [round(r["times"][j] - ref["times"][j], 3)
                               for j in range(n_sec)]
    return {"edges": [round(float(e), 2) for e in edges],
            "rows": rows, "best": best, "ideal": ideal}


def compare(runs, tm, keys=None, ref=None, channels=("speed_gps",),
            points=1000, n_sectors=8, max_accuracy_m=10.0,
            exclude_edge=False):
    """The whole comparison, as one JSON-safe dict.

    `keys` selects which traversals to include (all of them by default); `ref` is
    the one everything is measured against, defaulting to the fastest.

    `exclude_edge` drops the first and last complete lap of each run. Off by
    default, because an out-lap is sometimes exactly what someone wants to look
    at -- but worth knowing what leaving them in does. Across nine AMP laps under
    110 s, speed at the very end of the lap spread by 10 m/s, from 15.6 to
    48.6 m/s. That is not a bug in the alignment: a lap that precedes an in-lap
    has the driver braking before the line, and the lap still ends at the line.
    It does make the ideal lap and any spread statistic misleading, so the UI
    preselects representative laps and offers the rest.
    """
    tvs = traversals(runs, tm, channels=channels, max_accuracy_m=max_accuracy_m,
                     exclude_edge=exclude_edge, keys=keys)
    return assemble(tvs, tm, ref=ref, channels=channels, points=points,
                    n_sectors=n_sectors)


def assemble(tvs, tm, ref=None, channels=("speed_gps",), points=1000,
             n_sectors=8):
    """The comparison itself, from traversals someone has already built.

    Split out from `compare` because the library hands over laps directly: each
    stored lap *is* a traversal, and re-deriving them by slicing a session would
    be looking for boundaries that were already found at import.
    """
    if not tvs:
        return {"traversals": [], "grid": [], "note": "nothing comparable here"}

    if ref is None or not any(t.key == ref for t in tvs):
        ref = min(tvs, key=lambda t: t.duration).key

    lo, hi = common_span(tvs)
    grid, table = resample(tvs, points, (lo, hi))
    if grid.size == 0:
        return {"traversals": [t.key for t in tvs], "grid": [],
                "note": "these traversals do not overlap on the centreline"}

    edges = sector_edges(hi - lo, n_sectors, lo)
    sec = sectors(grid, table, edges, ref_key=ref)

    # Report distance from the start of the compared span, not absolute
    # centreline distance. Lap slicing puts the first sample of a lap on the
    # sample *before* the crossing, so absolute distance starts a few metres
    # negative -- true, and unreadable as "-4 m into the lap". `offset_m` keeps
    # the absolute coordinate recoverable, which matters because it is the one
    # that is stable across sessions at the same venue.
    offset = float(lo)
    grid = grid - offset
    edges = np.asarray(edges, float) - offset
    sec["edges"] = [round(float(e), 2) for e in edges]

    step = float(grid[1] - grid[0]) if grid.size > 1 else 1.0
    out_tv = []
    for tv in tvs:
        d = delta(table, ref, tv.key)
        # Time gained or lost per 100 m: the slope, in units someone can read.
        rate = np.gradient(d, step) * 100.0
        row = {
            "key": tv.key,
            "run": tv.run_stem,
            "label": tv.label,
            "number": tv.number,
            "duration": round(float(tv.duration), 3),
            "time_label": window.format_lap_time(tv.duration),
            "t0_unix": round(tv.t0, 3),
            "is_ref": tv.key == ref,
            "covered": [round(tv.coverage[0] - offset, 1),
                        round(tv.coverage[1] - offset, 1)],
            "delta": np.round(d, 3).tolist(),
            "rate": np.round(rate, 3).tolist(),
            "final_delta": round(float(d[-1]), 3),
            "worst_delta": round(float(d.max()), 3),
            "t": np.round(table[tv.key]["t"], 3).tolist(),
            "off_line_p90": round(float(np.percentile(table[tv.key]["perp"], 90)), 2),
        }
        # Stretches with no samples, shifted into the same 0-based distance the
        # grid uses. The delta across one of these is interpolation, not
        # measurement, and the plots dim it for that reason.
        holes = [(a - offset, b - offset, dt) for a, b, dt in gaps_of(tv)]
        row["gaps"] = [[round(a, 1), round(b, 1), round(dt, 2)]
                       for a, b, dt in holes]
        row["gap_m"] = round(sum(b - a for a, b, _dt in holes), 1)
        row["measured_frac"] = round(
            1.0 - (float(_gap_mask(grid, holes).mean()) if holes else 0.0), 4)
        for name in channels:
            if name in table[tv.key]:
                row[name] = np.round(table[tv.key][name], 3).tolist()
        out_tv.append(row)
    out_tv.sort(key=lambda r: r["duration"])

    return {
        "map": tm.name,
        "closed": bool(tm.closed),
        "ref": ref,
        "span_m": [round(lo, 1), round(hi, 1)],
        "offset_m": round(offset, 2),
        "length_m": round(hi - lo, 1),
        # What the comparison could not reach: the traversals only overlap on
        # part of the line. On a circuit that is sample granularity at the lap
        # boundary, a few metres; on an open course the runs start and stop in
        # different places and it can be tens. Either way a final delta measured
        # over the overlap is not quite the lap-time difference -- measured at up
        # to 0.05 s over 8.4 m of a 2929 m lap -- and saying so is cheaper than
        # having someone find the discrepancy themselves.
        "uncompared_m": round(float(_line_length(tm)) - (hi - lo), 1),
        "grid": np.round(grid, 2).tolist(),
        "channels": list(channels),
        "traversals": out_tv,
        "sectors": sec,
    }


# ---------------------------------------------------------------- reporting

def format_table(c, top=12, width=None):
    """The comparison as a terminal table.

    A sector grid is the one view that answers "where am I actually losing it"
    without a plot, so it is worth having before any interface exists.
    """
    if not c.get("traversals"):
        return c.get("note", "nothing to compare")
    rows = c["traversals"][:int(top)]
    sec = c["sectors"]
    n = len(sec["best"])
    keys = {r["key"] for r in rows}
    by_key = {r["key"]: r for r in sec["rows"]}

    name_w = max(len(r["key"]) for r in rows)
    out = []
    out.append("%s  %s  %.0f m of line, %d sector(s)%s"
               % (c["map"], "circuit" if c["closed"] else "open course",
                  c["length_m"], n,
                  "" if c["uncompared_m"] < 1.0
                  else ", %.1f m not covered by every traversal"
                       % c["uncompared_m"]))
    holed = [r for r in c["traversals"] if r["gap_m"] > 20]
    if holed:
        out.append("%d of %d traversals have a GPS gap; worst is %.0f m "
                   "(%.0f%% of the line measured)"
                   % (len(holed), len(c["traversals"]),
                      max(r["gap_m"] for r in holed),
                      100 * min(r["measured_frac"] for r in holed)))
    edges = sec["edges"]
    head = " " * (name_w + 11) + " ".join(
        "%7s" % ("%d-%d" % (edges[j], edges[j + 1])) for j in range(n))
    out.append(head)
    out.append("-" * len(head))
    for r in rows:
        s = by_key[r["key"]]
        cells = []
        for j in range(n):
            mark = "*" if j in s["best_sectors"] else " "
            cells.append("%6.2f%s" % (s["times"][j], mark))
        out.append("%-*s %8s  %s" % (name_w, r["key"], r["time_label"],
                                     " ".join(cells)))
    out.append("-" * len(head))
    out.append("%-*s %8s  %s"
               % (name_w, "best sector", window.format_lap_time(sec["ideal"]["total"]),
                  " ".join("%6.2f " % v for v in sec["best"])))
    out.append("ideal %s, from %d traversal(s) -- %.2f s under the fastest real one"
               % (window.format_lap_time(sec["ideal"]["total"]),
                  sec["ideal"]["from_traversals"],
                  min(r["duration"] for r in c["traversals"])
                  - sec["ideal"]["total"]))
    out.append("")
    out.append("%-*s %8s  %8s  %8s  %s"
               % (name_w, "traversal", "time", "delta", "worst", "off line p90"))
    for r in rows:
        out.append("%-*s %8s  %+8.3f  %+8.3f  %8.2f m%s"
                   % (name_w, r["key"], r["time_label"], r["final_delta"],
                      r["worst_delta"], r["off_line_p90"],
                      "  <- reference" if r["is_ref"] else ""))
    return "\n".join(out)


def losses(c, key, n=5, window_m=100.0):
    """Where a traversal lost the most time to the reference.

    Reported as the worst stretches of `window_m`, because a delta trace read
    point by point says "you were 1.2 s down at 1400 m" -- true and useless. What
    is actionable is which hundred metres the time went in.
    """
    row = next((r for r in c["traversals"] if r["key"] == key), None)
    if row is None:
        return []
    import numpy as _np
    grid = _np.asarray(c["grid"])
    d = _np.asarray(row["delta"])
    if grid.size < 4:
        return []
    step = float(grid[1] - grid[0])
    span = max(2, int(round(window_m / step)))
    gained = d[span:] - d[:-span]
    order = _np.argsort(gained)[::-1]
    picked, out = [], []
    for j in order:
        if any(abs(int(j) - p) < span for p in picked):
            continue                    # do not report the same stretch twice
        picked.append(int(j))
        a, b = float(grid[j]), float(grid[j + span])
        # How much of this stretch is interpolation. A "loss" that is mostly gap
        # is a statement about the GPS, not about the driving.
        held = sum(max(0.0, min(b, g[1]) - max(a, g[0]))
                   for g in (row.get("gaps") or []))
        out.append({"from_m": round(a, 1),
                    "to_m": round(b, 1),
                    "lost_s": round(float(gained[j]), 3),
                    "interpolated": round(held / max(b - a, 1e-9), 3)})
        if len(out) >= int(n):
            break
    return out


def main(argv=None):
    import argparse
    import glob as _glob
    import json as _json
    from pathlib import Path as _Path

    from . import csv_io, trackmap

    ap = argparse.ArgumentParser(
        description="Compare laps on a shared distance coordinate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python -m racechrono.compare --runs "Raw Data/*amp*.csv"
  python -m racechrono.compare --runs "Raw Data/*grip*.csv" --sectors 6
  python -m racechrono.compare --runs "tests/data/*.csv" --exclude-edge --json
""")
    ap.add_argument("--runs", required=True, help="glob of session CSVs")
    ap.add_argument("--maps-dir", default="Maps")
    ap.add_argument("--map", help="a specific map file instead of auto-select")
    ap.add_argument("--sectors", type=int, default=8)
    ap.add_argument("--points", type=int, default=1000)
    ap.add_argument("--top", type=int, default=12,
                    help="how many traversals to print, fastest first")
    ap.add_argument("--ref", help="key of the reference traversal")
    ap.add_argument("--keys", nargs="*", help="only these traversal keys")
    ap.add_argument("--exclude-edge", action="store_true",
                    help="drop the first and last complete lap of each run")
    ap.add_argument("--losses", metavar="KEY",
                    help="also list where this traversal lost the most time")
    ap.add_argument("--json", action="store_true", help="emit the payload instead")
    args = ap.parse_args(argv)

    paths = sorted(_Path(p) for p in _glob.glob(args.runs))
    if not paths:
        raise SystemExit("no CSVs match %r" % args.runs)
    runs = []
    for p in paths:
        try:
            runs.append(csv_io.read_run(p, derive=False))
        except Exception as exc:
            print("  ! skipping %s: %s" % (p.name, exc))
    if not runs:
        raise SystemExit("no loadable runs")

    if args.map:
        tm = trackmap.load(args.map)
    else:
        lat, lon = runs[0].mean_latlon()
        tm, dist = trackmap.auto_select(args.maps_dir, lat, lon)
        if tm is None:
            raise SystemExit("no map within reach of %s (nearest %.0f km)"
                             % (runs[0].stem, dist / 1000.0))
    # Only the runs this map actually covers: a folder can hold two venues, and
    # projecting an autocross run onto a circuit 180 km away produces numbers.
    keep = []
    for run in runs:
        near, _d = trackmap.auto_select(args.maps_dir, *run.mean_latlon())
        if near is not None and near.name == tm.name:
            keep.append(run)
        else:
            print("  - %s is not at %s; leaving it out" % (run.stem, tm.name))
    if not keep:
        raise SystemExit("no run in that glob is at %s" % tm.name)

    c = compare(keep, tm, keys=args.keys, ref=args.ref, points=args.points,
                n_sectors=args.sectors, exclude_edge=args.exclude_edge)
    if args.json:
        print(_json.dumps(c, indent=1))
        return 0
    print(format_table(c, top=args.top))
    if args.losses:
        print()
        print("worst 100 m stretches for %s:" % args.losses)
        for L in losses(c, args.losses):
            note = ""
            if L["interpolated"] > 0.5:
                note = "   (%.0f%% interpolated -- no GPS there)" % (
                    100 * L["interpolated"])
            elif L["interpolated"] > 0.05:
                note = "   (%.0f%% interpolated)" % (100 * L["interpolated"])
            print("   %7.0f - %7.0f m   %+.3f s%s" % (L["from_m"], L["to_m"],
                                                      L["lost_s"], note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
