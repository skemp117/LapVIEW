"""Build one averaged track map from several runs.

    python tools/generate_map.py --runs "Raw Data/*.csv" --name "AMP Full" \
        --out Maps/amp_full.json --points 720

Averaging is by median across laps at each normalised-distance index, not mean:
the median throws away the one lap where someone ran wide, a mean smears it into
the racing line.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from racechrono import csv_io, geo, laps as laps_mod, runs as runs_mod, trackmap  # noqa: E402


def savgol(y, window=9, order=2, closed=True):
    """Mild Savitzky-Golay smoothing. Over-smoothing eats the hairpin, so keep
    it mild.

    `closed` decides how the ends are padded, and it matters more than it looks.
    A lap is a loop, so wrapping the ends keeps the join from developing a kink.
    An open course is not a loop, and wrapping splices the finish onto the start:
    smoothing then mixes finish coordinates into the first points and start
    coordinates into the last, which put a 14 m spike on each end of an averaged
    line whose points are 1.06 m apart. Open courses get edge padding instead.
    """
    try:
        from scipy.signal import savgol_filter
    except ImportError:
        return y
    if window < 3 or len(y) < window * 2:
        return y
    if window % 2 == 0:
        window += 1
    pad = window
    if closed:
        ext = np.concatenate([y[-pad:], y, y[:pad]])
    else:
        ext = np.concatenate([np.full(pad, y[0]), y, np.full(pad, y[-1])])
    sm = savgol_filter(ext, window, order, mode="interp")
    return sm[pad:-pad]


def resample_by_distance(x, y, n):
    """N points spaced evenly along the lap by arc length, not by time.

    Time spacing would over-weight the slow corners and thin out the straights,
    and the median across laps would then be comparing different bits of track.
    """
    s = geo.cumulative_distance(x, y)
    total = s[-1]
    if total <= 0:
        return None, 0.0
    u = np.linspace(0.0, total, n, endpoint=False)
    return np.c_[np.interp(u, s, x), np.interp(u, s, y)], float(total)


def build(run_paths, name, points=720, keep_edge_laps=False, max_accuracy_m=10.0,
          max_time_factor=1.25, min_time_factor=0.75, orientation="north",
          smooth_window=9, align="projection", align_iterations=3, verbose=True):
    runs = []
    for p in run_paths:
        try:
            runs.append(csv_io.read_run(p))
        except Exception as exc:
            print("  ! skipping %s: %s" % (Path(p).name, exc))
    if not runs:
        raise SystemExit("no loadable runs")

    # A single shared origin across every run, stored in the map file. Each run
    # computing its own origin would offset the laps against each other.
    origin = runs[0].mean_latlon(max_accuracy_m)
    if verbose:
        print("origin: %.7f, %.7f  (from %s)" % (origin[0], origin[1], runs[0].stem))

    all_laps = []
    for run in runs:
        got = laps_mod.laps(run, max_accuracy_m)
        all_laps.extend(got)
        if verbose:
            print("  %s: %d complete lap(s) %s" % (
                run.stem, len(got),
                ", ".join("%.1fs" % l.duration for l in got)))

    kept, dropped = laps_mod.filter_laps(
        all_laps, exclude_edge=not keep_edge_laps,
        max_time_factor=max_time_factor, min_time_factor=min_time_factor)
    if verbose:
        for lp, why in sorted(dropped, key=lambda d: (d[0].run_stem, d[0].t0)):
            print("  - drop %s lap %d (%.1fs): %s"
                  % (lp.run_stem, lp.number, lp.duration, why))
        print("using %d lap(s) of %d" % (len(kept), len(all_laps)))
    if not kept:
        raise SystemExit("no laps survived filtering")

    by_run = {r.stem: r for r in runs}
    paths, lengths = [], []
    for lp in kept:
        run = by_run[lp.run_stem]
        good = run.good_mask(max_accuracy_m)[lp.i0:lp.i1]
        lat = run.values("latitude")[lp.i0:lp.i1][good]
        lon = run.values("longitude")[lp.i0:lp.i1][good]
        if lat.size < points // 4:
            continue
        x, y = geo.to_xy(lat, lon, *origin)
        total = float(geo.cumulative_distance(x, y)[-1])
        if total <= 0:
            continue
        paths.append((x, y))
        lengths.append(total)
    if not paths:
        raise SystemExit("no laps had enough good position samples")

    if align == "projection":
        # Nineteen AMP laps differ in path length by 2.86%, and parameterising
        # each by a fraction of its own length spreads that difference over the
        # lap: deviation from the median was 3.28 m at p50 and 14.22 m at p95,
        # peaking at 24.5 m a quarter of the way round and collapsing back to
        # 3.4 m at the line -- the two places both methods anchor. Projecting
        # onto a shared reference instead holds it at 1.03 m p50, 4.32 m p95.
        # Phase alignment is not needed after this: the reference lap starts at
        # its own start-line crossing, so the arc-length coordinate is shared.
        med, stack, seed = _align_by_projection(paths, points, align_iterations)
        if verbose:
            print("aligned by projection onto %d-point reference (%d passes)"
                  % (points, max(1, align_iterations)))
    else:
        tracks = []
        for x, y in paths:
            pts, _total = resample_by_distance(x, y, points)
            if pts is not None:
                tracks.append(pts)
        stack = _align_phase(np.stack(tracks))
        med = np.median(stack, axis=0)

    if smooth_window:
        med[:, 0] = savgol(med[:, 0], smooth_window)
        med[:, 1] = savgol(med[:, 1], smooth_window)

    # Start/finish, from every crossing in every selected run.
    sf_xy, sf_normal, _, n_cross = laps_mod.start_finish(
        runs, max_accuracy_m, origin=origin)

    if orientation == "pca":
        rot = geo.pca_rotation(med[:, 0], med[:, 1])
    else:
        rot = geo.identity_rotation()

    rx, ry = geo.apply_rotation(rot, med[:, 0], med[:, 1])
    extent = trackmap.extent_from(rx, ry)
    ux, uy = trackmap._normalise(rx, ry, extent)

    sfx, sfy = geo.apply_rotation(rot, np.array([sf_xy[0]]), np.array([sf_xy[1]]))
    sfu, sfv = trackmap._normalise(sfx, sfy, extent)
    nx, ny = geo.apply_rotation(rot, np.array([sf_normal[0]]), np.array([sf_normal[1]]))

    closed_u = np.c_[np.append(ux, ux[0]), np.append(uy, uy[0])]
    length_m = float(np.median(lengths))

    tm = trackmap.TrackMap(
        name=name,
        created=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        orientation=orientation,
        source_runs=[Path(r.path).name for r in runs],
        lap_count=len(paths),
        origin={"lat": origin[0], "lon": origin[1]},
        rotation=[[float(rot[0][0]), float(rot[0][1])],
                  [float(rot[1][0]), float(rot[1][1])]],
        extent_m=extent,
        points_unit=closed_u,
        start_finish={
            "point": [float(sfu[0]), float(sfv[0])],
            "point_m": [float(sf_xy[0]), float(sf_xy[1])],
            "normal_m": [float(sf_normal[0]), float(sf_normal[1])],
            # Y flipped to match points_unit's screen space.
            "normal_unit": [float(nx[0]), float(-ny[0])],
            "crossings": int(n_cross),
        },
        length_m=length_m,
    )
    if verbose:
        spread = np.linalg.norm(stack - med, axis=2)
        print("lap-to-lap spread from median: p50 %.2f m  p95 %.2f m  max %.2f m"
              % (np.percentile(spread, 50), np.percentile(spread, 95), spread.max()))
        print("length: median %.1f m (%.3f mi), lap spread %.1f m"
              % (length_m, length_m / 1609.34, np.ptp(lengths)))
        print("start/finish from %d crossing(s)" % n_cross)
    return tm


def _traversals_of(run, max_accuracy_m):
    """Every driven traversal in one session, as (lat, lon) pairs.

    A session is not a run. Ten autocross sessions held exactly one run each, but
    six SCCA sessions held one or two apiece with staging between them, and
    treating a two-run session as a single path averages the drive back to the
    start line into the course. `runs.detect` splits it; a single-run session
    comes back as one span, which is what it always was.

    The head of each span starts at the start-line crossing where there is one.
    `lap_number` goes NaN to 1 exactly once per run, which marks it precisely --
    and the approach to the line is not part of the course. Including it dragged
    the first few points of the averaged line 13.6 m off, against 1.07 m
    spacing, because approach samples project onto the reference's first point
    and pile up there.
    """
    good = run.good_mask(max_accuracy_m)
    lat_all = run.values("latitude")
    lon_all = run.values("longitude")
    crossings = laps_mod.crossing_indices(run.values("lap_number"))
    out = []
    for span in runs_mod.detect(run, max_accuracy_m=max_accuracy_m):
        first, last = span.i0, span.i1 - 1
        for c in crossings:
            # The crossing sits between sample c and c+1.
            start = int(c) + 1
            if first < start < last:
                first = start
                break
        sl = slice(first, last + 1)
        keep = good[sl]
        lat = lat_all[sl][keep]
        lon = lon_all[sl][keep]
        if lat.size < 32:
            continue
        out.append((lat, lon, span))
    return out


def _traversal(run, max_accuracy_m):
    """The first traversal only, kept for callers that want one path per run."""
    got = _traversals_of(run, max_accuracy_m)
    if not got:
        return None
    lat, lon, _span = got[0]
    return lat, lon


def _align_by_projection(paths, points, iterations=3):
    """Put every traversal on a shared arc-length coordinate, then median them.

    Each path is projected onto a reference polyline, which gives its samples a
    distance-along-the-reference coordinate; resampling on that coordinate and
    taking the median produces the next reference. Two or three passes converge.

    The obvious alternative -- parameterise each run by a fraction of its own
    length -- drifts, because two runs of the same course differ in path length
    by about 8%. Measured on ten autocross runs, that drift showed up as a
    deviation from the median line growing smoothly from 2.5 m at the start to
    17.6 m at the end. Projection holds it at 0.8 m median, 3.1 m p95, and the
    remaining spike in the last 2% is the real roll-out past the finish.
    """
    lengths = [geo.cumulative_distance(x, y)[-1] for x, y in paths]
    seed = int(np.argsort(lengths)[len(lengths) // 2])
    ref = np.c_[paths[seed][0], paths[seed][1]]

    stack = None
    for _ in range(max(1, iterations)):
        ref_len = geo.cumulative_distance(ref[:, 0], ref[:, 1])[-1]
        grid = np.linspace(0.0, ref_len, points)
        rows = []
        for x, y in paths:
            s, _perp = geo.project_to_polyline(ref[:, 0], ref[:, 1], x, y)
            order = np.argsort(s)
            s2, x2, y2 = s[order], x[order], y[order]
            uniq = np.r_[True, np.diff(s2) > 1e-9]
            rows.append(np.c_[np.interp(grid, s2[uniq], x2[uniq]),
                              np.interp(grid, s2[uniq], y2[uniq])])
        stack = np.stack(rows)
        ref = np.median(stack, axis=0)
    return ref, stack, seed


def _trim_to_agreement(median, stack, max_spread_m):
    """Keep the span of the course every traversal agrees on.

    The run does not end at the finish line -- the car rolls out and stops
    wherever. Those tails diverge (33.9 m of scatter in the end points against
    0.6 m at the start), and they are not part of the course. Cutting where the
    spread crosses a threshold finds the boundary from the data instead of
    guessing a fraction.
    """
    dev = np.linalg.norm(stack - median, axis=2)
    p95 = np.percentile(dev, 95, axis=0)
    ok = p95 <= max_spread_m
    if not ok.any():
        return 0, len(median), p95
    keep = np.flatnonzero(ok)
    lo, hi = int(keep[0]), int(keep[-1]) + 1

    # The grid is uniform in arc length, so consecutive points should be evenly
    # spaced. Where they are not, the averaged position has been dragged by
    # samples piling up against the end of the reference -- the roll-out past the
    # finish, and the approach when there is no start line to cut at. Peel those
    # points off from each end.
    step = np.linalg.norm(np.diff(median, axis=0), axis=1)
    if step.size:
        typical = float(np.median(step))
        limit = max(3.0 * typical, 1e-6)
        while hi - lo > 16 and step[lo] > limit:
            lo += 1
        while hi - lo > 16 and step[hi - 2] > limit:
            hi -= 1
    return lo, hi, p95


def build_course(run_paths, name, points=480, max_accuracy_m=10.0,
                 max_spread_m=5.0, iterations=3, orientation="north",
                 smooth_window=9, max_length_factor=1.25,
                 min_length_factor=0.75, max_time_factor=2.5, verbose=True):
    """An averaged map of an open course, from several traversals of it.

    Same idea as averaging laps, minus the closed loop: an autocross course
    starts and finishes in different places, and each run crosses the start line
    once, so `lap_number` never increments and there are no laps to slice.
    """
    runs = []
    for p in run_paths:
        try:
            runs.append(csv_io.read_run(p))
        except Exception as exc:
            print("  ! skipping %s: %s" % (Path(p).name, exc))
    if not runs:
        raise SystemExit("no loadable runs")

    origin = runs[0].mean_latlon(max_accuracy_m)
    if verbose:
        print("origin: %.7f, %.7f  (from %s)" % (origin[0], origin[1], runs[0].stem))

    # Every traversal in every session, not one per file: a session can hold
    # several runs with staging between them, and averaging the drive back to the
    # start line into the course is how a map ends up with a spur on it.
    paths, used, labels, spans_used = [], [], [], []
    for run in runs:
        got = _traversals_of(run, max_accuracy_m)
        if not got:
            if verbose:
                print("  - drop %s: too few good moving samples" % run.stem)
            continue
        for lat, lon, span in got:
            x, y = geo.to_xy(lat, lon, *origin)
            paths.append((x, y))
            used.append(run)
            spans_used.append(span)
            labels.append("%s run %d" % (run.stem, span.index)
                          if len(got) > 1 else run.stem)
    if len(paths) < 2:
        raise SystemExit("need at least two traversals to average a course")

    lengths = np.array([geo.cumulative_distance(x, y)[-1] for x, y in paths])
    med_len = float(np.median(lengths))
    # Length alone cannot reject a walked course: walking the course covers
    # exactly the right distance. Time can, and by a wide margin -- a 1341 m
    # course walked at 1.4 m/s takes 958 s against a 103 s median run, 9.3x.
    # `max_time_factor` is deliberately loose at 2.5x so that a merely slow run
    # is never dropped: the slowest real run measured here was 1.10x its
    # session's median, so there is more than a factor of two of headroom on one
    # side and nearly four on the other.
    #
    # In practice `runs.detect` already refuses to see a walked course at all --
    # its movement gate is 3.0 m/s and walking is about 1.4 -- so this is the
    # second line rather than the first.
    durations = np.array([float(sp.duration) for sp in spans_used])
    med_dur = float(np.median(durations))
    keep = []
    for i, r in enumerate(used):
        if not (min_length_factor * med_len <= lengths[i] <= max_length_factor * med_len):
            continue
        if durations[i] > max_time_factor * med_dur:
            continue
        keep.append((i, r))
    if verbose:
        for i, r in enumerate(used):
            why = ""
            if not any(i == j for j, _ in keep):
                if durations[i] > max_time_factor * med_dur:
                    why = "   DROPPED (%.0f s against a %.0f s median -- too "                           "slow to be a run)" % (durations[i], med_dur)
                else:
                    why = "   DROPPED (length)"
            print("  %s: %.0f m, %.0f s%s" % (labels[i], lengths[i],
                                              durations[i], why))
    if len(keep) < 2:
        raise SystemExit("length filter left fewer than two traversals")
    paths = [paths[i] for i, _ in keep]
    used = [r for _, r in keep]

    ref, stack, seed = _align_by_projection(paths, points, iterations)
    lo, hi, p95 = _trim_to_agreement(ref, stack, max_spread_m)
    if verbose:
        dev = np.linalg.norm(stack - ref, axis=2)
        print("alignment: deviation p50 %.2f m, p95 %.2f m (seed %s)"
              % (np.percentile(dev, 50), np.percentile(dev, 95), used[seed].stem))
        print("trimmed to the span all runs agree within %.1f m: points %d..%d "
              "of %d (%.1f%% .. %.1f%%)"
              % (max_spread_m, lo, hi, points, 100.0 * lo / points,
                 100.0 * hi / points))
    med = ref[lo:hi]
    if med.shape[0] < 16:
        raise SystemExit("agreement span too short to make a course map")

    if smooth_window:
        med = med.copy()
        med[:, 0] = savgol(med[:, 0], smooth_window, closed=False)
        med[:, 1] = savgol(med[:, 1], smooth_window, closed=False)

    rot = (geo.pca_rotation(med[:, 0], med[:, 1]) if orientation == "pca"
           else geo.identity_rotation())
    rx, ry = geo.apply_rotation(rot, med[:, 0], med[:, 1])
    extent = trackmap.extent_from(rx, ry)
    ux, uy = trackmap._normalise(rx, ry, extent)
    length_m = float(geo.cumulative_distance(med[:, 0], med[:, 1])[-1])

    tm = trackmap.TrackMap(
        name=name,
        created=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        orientation=orientation,
        closed=False,
        source_runs=[Path(r.path).name for r in used],
        lap_count=len(used),
        origin={"lat": origin[0], "lon": origin[1]},
        rotation=[[float(rot[0][0]), float(rot[0][1])],
                  [float(rot[1][0]), float(rot[1][1])]],
        extent_m=extent,
        points_unit=np.c_[ux, uy],
        start={"point": [float(ux[0]), float(uy[0])],
               "point_m": [float(med[0, 0]), float(med[0, 1])]},
        finish={"point": [float(ux[-1]), float(uy[-1])],
                "point_m": [float(med[-1, 0]), float(med[-1, 1])]},
        length_m=length_m,
    )
    if verbose:
        print("course: %.1f m (%.3f mi) from %d traversal(s); start and finish "
              "%.1f m apart"
              % (length_m, length_m / 1609.34, len(used),
                 float(np.hypot(*(med[-1] - med[0])))))
    return tm


def _align_phase(stack):
    """Rotate each lap's point array so index 0 lines up across laps.

    Every lap already starts at the start/finish crossing, but the crossing is
    detected up to one sample late, which at 40 m/s is a couple of metres of
    phase error. Cross-correlating each lap against the first removes it, so the
    median is taken over points that are actually the same bit of track.
    """
    if stack.shape[0] < 2:
        return stack
    ref = stack[0]
    n = ref.shape[0]
    out = [ref]
    for lap in stack[1:]:
        best, best_err = 0, float("inf")
        # Only small shifts are plausible; a big one means a mis-sliced lap.
        for shift in range(-8, 9):
            err = np.linalg.norm(np.roll(lap, shift, axis=0) - ref, axis=1).mean()
            if err < best_err:
                best, best_err = shift, err
        out.append(np.roll(lap, best, axis=0))
    return np.stack(out)


def interactive_pick(candidates):
    print("CSVs in Raw Data:")
    for i, p in enumerate(candidates, 1):
        print("  [%2d] %s" % (i, p.name))
    raw = input("include which? (numbers/ranges, blank = all): ").strip()
    if not raw:
        return candidates
    picked = set()
    for tok in raw.replace(",", " ").split():
        if "-" in tok:
            a, b = tok.split("-", 1)
            picked.update(range(int(a), int(b) + 1))
        else:
            picked.add(int(tok))
    return [candidates[i - 1] for i in sorted(picked)
            if 1 <= i <= len(candidates)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", help="glob of CSVs; omit for interactive pick")
    ap.add_argument("--data-dir", default="Raw Data")
    ap.add_argument("--name", default=None, help="map name (default: session title)")
    ap.add_argument("--out", default=None, help="output .json path")
    ap.add_argument("--points", type=int, default=720)
    ap.add_argument("--keep-edge-laps", action="store_true",
                    help="keep the first and last complete lap of each run")
    ap.add_argument("--exclude-out-laps", action="store_true",
                    help="default behaviour; accepted for symmetry")
    ap.add_argument("--max-accuracy-m", type=float, default=10.0)
    ap.add_argument("--max-time-factor", type=float, default=1.25)
    ap.add_argument("--min-time-factor", type=float, default=0.75)
    ap.add_argument("--course", dest="course", action="store_true", default=None,
                    help="open course (autocross): average whole runs instead of "
                         "laps, and do not close the polyline")
    ap.add_argument("--circuit", dest="course", action="store_false",
                    help="closed circuit; the default when the runs have laps")
    ap.add_argument("--max-spread-m", type=float, default=5.0,
                    help="course mode: trim the ends to the span every run "
                         "agrees within")
    ap.add_argument("--align-iterations", type=int, default=3)
    ap.add_argument("--align", choices=("projection", "fraction"),
                    default="projection",
                    help="how laps or traversals are put on a common coordinate; "
                         "'fraction' is the older parameterise-by-own-length "
                         "method, kept for comparison")
    ap.add_argument("--orient", choices=("north", "pca"), default="north",
                    help="north = true GPS orientation (matches RaceChrono); "
                         "pca = long axis horizontal")
    ap.add_argument("--smooth-window", type=int, default=9,
                    help="Savitzky-Golay window; 0 disables")
    ap.add_argument("--both-orientations", action="store_true",
                    help="also write a <name>_pca variant for comparison")
    args = ap.parse_args(argv)

    if args.runs:
        paths = [Path(p) for p in sorted(glob.glob(args.runs))]
    else:
        paths = interactive_pick(sorted(Path(args.data_dir).glob("*.csv")))
    if not paths:
        raise SystemExit("no CSVs selected")
    print("runs: %s" % ", ".join(p.name for p in paths))

    name = args.name
    if not name:
        first = csv_io.read_run(paths[0])
        name = first.meta.get("Track name", "").strip('"') or first.stem
    out = Path(args.out) if args.out else Path("Maps") / (
        name.lower().replace(" ", "_") + ".json")

    # Auto-detect: a session whose runs never complete a lap is an open course.
    course = args.course
    if course is None:
        laps_seen = 0
        for q in paths:
            try:
                laps_seen = max(laps_seen, len(laps_mod.laps(csv_io.read_run(q))))
            except Exception:
                pass
        course = laps_seen < 1
        print("no complete laps found in any run; treating this as an open "
              "course. Pass --circuit to override." if course else
              "laps found (up to %d per run); building a closed circuit map. "
              "Pass --course to override." % laps_seen)

    variants = [(args.orient, out)]
    if args.both_orientations:
        other = "pca" if args.orient == "north" else "north"
        variants.append((other, out.with_name(out.stem + "_" + other + out.suffix)))

    for orient, dest in variants:
        print("\n--- orientation: %s%s ---"
              % (orient, ", open course" if course else ""))
        label = name if len(variants) == 1 else "%s (%s)" % (name, orient)
        if course:
            tm = build_course(paths, label, points=args.points,
                              max_accuracy_m=args.max_accuracy_m,
                              max_spread_m=args.max_spread_m,
                              iterations=args.align_iterations,
                              orientation=orient,
                              smooth_window=args.smooth_window,
                              max_length_factor=args.max_time_factor,
                              min_length_factor=args.min_time_factor)
        else:
            tm = build(paths, label,
                       points=args.points, keep_edge_laps=args.keep_edge_laps,
                       max_accuracy_m=args.max_accuracy_m,
                       max_time_factor=args.max_time_factor,
                       min_time_factor=args.min_time_factor,
                       orientation=orient, smooth_window=args.smooth_window,
                       align=args.align,
                       align_iterations=args.align_iterations)
        tm.save(dest)
        svg = trackmap.write_svg(tm, dest.with_suffix(".svg"))
        print("wrote %s\n      %s  (aspect %.3f w/h)" % (dest, svg, tm.aspect))


if __name__ == "__main__":
    main()
