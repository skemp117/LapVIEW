"""Checks that do not need a render: config handling, element registry,
channel-set robustness, and the map transform's stability.

    python tools/selftest.py                  # everything, against the fixtures
    python tools/selftest.py library ingest   # only those areas
    python tools/selftest.py --list           # what the areas are
    python tools/selftest.py --times          # what each area costs
    python tools/selftest.py --source "D:/day" --maps Maps

Run the areas you touched. The whole suite is for before handing work over, not
for every edit -- it is the areas that make that affordable, so a change to the
library does not pay for re-checking the IMU calibration.

The suite reads `tests/data/` and `tests/maps/`, not the working directories.
It used to read `Raw Data/`, and when those sessions were swapped from circuit to
autocross the lap slicing, lap export, start/finish recovery, camera-split join
and closed-circuit map checks all quietly stopped running -- ten of them reported
`skip` and the suite still said it passed. Coverage cannot depend on which files
happen to be sitting in the input folder. Regenerate the fixtures with
`python tools/make_test_data.py`.
"""
from __future__ import annotations

import copy
import csv
import json
import io
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod                                     # noqa: E402
from main import CONFIG                                          # noqa: E402
from racechrono import (csv_io, gauges, job as job_mod,  # noqa: E402
                        laps as laps_mod, overlay as overlay_mod,
                        render, trackmap, video, window)
from racechrono.gauges.base import Layout                        # noqa: E402

# A folder of sessions to test against, and the maps that cover it. The pinned
# fixtures by default; `--source` points the same checks at real data, which is
# worth doing after a day at a new venue -- the fixture set is deliberately small
# and will not have every oddity a new phone produces.
#
# There is no `input_data` in the config any more. Nothing in the tool reads a
# fixed folder: an import points at one and the library holds what came of it.
SOURCE = ROOT / "tests/data"
if "--source" in sys.argv:
    SOURCE = Path(sys.argv[sys.argv.index("--source") + 1])
if "--maps" in sys.argv:
    CONFIG["maps_dir"] = sys.argv[sys.argv.index("--maps") + 1]
else:
    CONFIG["maps_dir"] = "tests/maps" if SOURCE == ROOT / "tests/data" else "Maps"

# --- parse the fixtures once -----------------------------------------------
#
# Reading a CSV is about a second and probing a clip about a tenth, and the
# ingest checks each planned an import from scratch: the same five files parsed
# and the same two clips probed a dozen times over. Caching both inside the test
# process took the suite from four minutes to well under one. It is safe for the
# same reason `_all_runs()` was already shared -- a check that mutated a parsed
# run would have been poisoning its neighbours long before this.
def _memo(module, name):
    real = getattr(module, name)
    cache = {}

    def wrapper(*a, **kw):
        key = (tuple(str(x) for x in a),
               tuple(sorted((k, str(v)) for k, v in kw.items())))
        if key not in cache:
            cache[key] = real(*a, **kw)
        return cache[key]

    wrapper.uncached = real
    setattr(module, name, wrapper)


_memo(csv_io, "read_run")
_memo(video, "probe")


def _memo_provisional_map():
    """Build each provisional track map once per process.

    73% of the suite was here: two areas repeatedly planning an import from the
    same folder, and a plan projects every sample of every run onto a candidate
    centreline to build the map. The runs are already shared objects, so their
    identity is a sound cache key. Real imports still pay this -- see the note in
    ingest._provisional_map.
    """
    from racechrono import ingest as ingest_mod
    real = ingest_mod._provisional_map
    cache = {}

    def wrapper(runs, closed, name, points=480):
        key = (tuple(id(r) for r in runs), bool(closed), name, points)
        if key not in cache:
            cache[key] = real(runs, closed, name, points)
        return cache[key]

    ingest_mod._provisional_map = wrapper


_memo_provisional_map()

PASS, FAIL, SKIP = "  ok  ", " FAIL ", " skip "
_failures = []
_skipped = []

# --- areas -----------------------------------------------------------------
#
# Every check belongs to an area, and an area can be asked for by name. The
# suite is four minutes end to end; a change to the library has no business
# paying for the IMU calibration checks, and being able to run a fifth of it in
# ten seconds is what makes it worth running at all.
_WANTED = [a.lower() for a in sys.argv[1:] if not a.startswith("-")]
_AREAS = []
_TIMES = {}
_area = ""
_muted = False
_TIMING = "--times" in sys.argv


def area(name):
    """Start an area, and decide whether its checks run at all."""
    global _area, _muted
    _area = name
    _AREAS.append(name)
    _muted = bool(_WANTED) and not any(w in name.lower() for w in _WANTED)
    if not _muted:
        print(name)


class _closing:
    """Close a library before its temporary directory is removed.

    Windows will not delete an open sqlite file, so a test that merely let the
    Library fall out of scope failed in teardown with the assertions all passed --
    which reads as a broken test rather than a broken cleanup.
    """

    def __init__(self, obj):
        self.obj = obj

    def __enter__(self):
        return self.obj

    def __exit__(self, *exc):
        try:
            self.obj.close()
        except Exception:
            pass
        return False


class Skip(Exception):
    """Raised when a check needs data this dataset does not contain.

    Reported separately from a pass. A suite that silently passes because the
    thing it tests is absent is worse than one that says so -- these checks were
    written against a circuit session and half of them became vacuous the moment
    an autocross dataset arrived, which is exactly what needs surfacing.
    """


def check(name, fn):
    if _muted:
        return
    started = time.perf_counter()
    try:
        detail = fn()
        took = time.perf_counter() - started
        _TIMES[_area] = _TIMES.get(_area, 0.0) + took
        if _TIMING:
            detail = ("%s   [%.2fs]" % (detail, took)) if detail                      else "[%.2fs]" % took
        print("%s %s%s" % (PASS, name, (" -- " + detail) if detail else ""))
    except Skip as exc:
        _TIMES[_area] = _TIMES.get(_area, 0.0) + time.perf_counter() - started
        print("%s %s -- %s" % (SKIP, name, exc))
        _skipped.append(name)
    except AssertionError as exc:
        _TIMES[_area] = _TIMES.get(_area, 0.0) + time.perf_counter() - started
        print("%s %s -- %s" % (FAIL, name, exc))
        _failures.append(name)
    except Exception as exc:
        _TIMES[_area] = _TIMES.get(_area, 0.0) + time.perf_counter() - started
        print("%s %s -- %s: %s" % (FAIL, name, type(exc).__name__, exc))
        _failures.append(name)


_run_cache = {}


def _all_runs():
    """Every session in the input directory, loaded once and reused.

    Around forty checks want the runs; re-reading and re-deriving 14 MB of CSV
    for each of them dominated the suite's runtime. Nothing mutates a `Run`.
    """
    key = SOURCE
    if key not in _run_cache:
        _run_cache[key] = [csv_io.read_run(p)
                           for p in sorted((ROOT / key).glob("*.csv"))]
    return _run_cache[key]


def a_lapped_run():
    """A run with at least two complete laps, or skip.

    Autocross runs cross the line once, so `laps()` is legitimately empty and
    anything lap-shaped has nothing to test against.
    """
    for run in _all_runs():
        if len(laps_mod.laps(run)) >= 2:
            return run
    raise Skip("no session in %s has two or more complete laps"
               % SOURCE)


def a_run_with_footage(cfg=None):
    cfg = cfg or _cfg()
    clips = video.discover(SOURCE, cfg.ffmpeg.ffprobe, recursive=True)
    runs = _all_runs()
    pairings, _ = video.pair(runs, clips, None)
    by = {r.stem: r for r in runs}
    for pr in pairings:
        if pr.clips:
            return by[pr.run_stem], pr
    raise Skip("no session in %s is paired with footage"
               % SOURCE)


def a_run_with_imu():
    from racechrono import imu as imu_mod
    for run in _all_runs():
        if imu_mod.has_imu(run):
            return run
    raise Skip("no session carries a raw accelerometer and gyroscope")


def a_csv():
    got = sorted(SOURCE.rglob("*.csv"))
    assert got, "no CSVs in %s" % SOURCE
    return got[0]


def a_map():
    got = sorted((ROOT / CONFIG["maps_dir"]).glob("*.json"))
    assert got, "no maps in %s; run tools/generate_map.py first" % CONFIG["maps_dir"]
    return trackmap.load(got[0])


def a_mapped_run():
    """A run some map in `maps_dir` actually covers.

    `_all_runs()[0]` is whichever file sorts first, so adding sessions from a new
    venue silently changed which run the gauge and map checks ran against -- and
    when that run had no map they skipped instead of running. Six SCCA autocross
    sessions arriving at a third site turned two real checks into skips exactly
    that way.
    """
    for run in _all_runs():
        cfg = _cfg()
        lat, lon = run.mean_latlon(cfg.quality.max_accuracy_m)
        tm, _dist = trackmap.auto_select(cfg.maps_dir, lat, lon)
        if tm is not None:
            return run
    raise Skip("no map in %s covers any session in %s"
               % (CONFIG["maps_dir"], SOURCE))


def _map_for(run):
    """The map covering this run, or skip -- an autocross site has none."""
    cfg = _cfg()
    lat, lon = run.mean_latlon(cfg.quality.max_accuracy_m)
    tm, dist = trackmap.auto_select(cfg.maps_dir, lat, lon)
    if tm is None:
        raise Skip("no map covers %s (nearest origin %.0f km away)"
                   % (run.stem, dist / 1000.0))
    return tm


# ------------------------------------------------------------------ config

def t_config_loads():
    cfg = config_mod.load(copy.deepcopy(CONFIG))
    names = [e.name for e in cfg.enabled_elements()]
    return "enabled: %s" % ", ".join(names)


def t_unknown_key_rejected():
    bad = copy.deepcopy(CONFIG)
    bad["elements"]["speedo"]["diamter"] = 0.3        # deliberate typo
    try:
        config_mod.load(bad)
    except config_mod.ConfigError as exc:
        assert "diamter" in str(exc), "error did not name the bad key: %s" % exc
        return "typo reported, not ignored"
    raise AssertionError("a misspelled layout key was accepted silently")


def t_out_of_range_rejected():
    bad = copy.deepcopy(CONFIG)
    bad["elements"]["speedo"]["diameter"] = 9.0
    try:
        config_mod.load(bad)
    except config_mod.ConfigError:
        return "range checked"
    raise AssertionError("diameter 9.0 (900% of frame height) was accepted")


def t_stub_element_rejected():
    bad = copy.deepcopy(CONFIG)
    bad["elements"]["lap_timer"] = {"enabled": True}
    try:
        config_mod.load(bad)
    except config_mod.ConfigError as exc:
        assert "lap_timer" in str(exc)
        return "enabling an unimplemented stub fails with a clear message"
    raise AssertionError("enabling the lap_timer stub was accepted")


def t_inverted_range_rejected():
    bad = copy.deepcopy(CONFIG)
    bad["elements"]["tacho"]["max"] = 0
    try:
        config_mod.load(bad)
    except config_mod.ConfigError:
        return "max <= min rejected"
    raise AssertionError("max <= min was accepted")


# ------------------------------------------------------------------ elements

def _build(cfg_dict, w=1920, h=1080, run=None):
    run = run if run is not None else _all_runs()[0]
    cfg = config_mod.bind(config_mod.load(copy.deepcopy(cfg_dict)), run)
    return gauges.build(cfg, Layout(w, h), {"trackmap": a_map(), "fps": 60.0,
                                            "dim_alpha": 0.4})


def t_all_elements_build():
    built = _build(CONFIG)
    for g in built:
        g.static()
    return ", ".join("%s bbox=(%d,%d,%d,%d)" % ((g.name,) + tuple(int(v) for v in g.bbox))
                     for g in built)


def t_disable_is_clean():
    base = {g.name for g in _build(CONFIG)}
    for victim in ("track_map", "gmeter", "tacho"):
        d = copy.deepcopy(CONFIG)
        d["elements"][victim]["enabled"] = False
        left = {g.name for g in _build(d)}
        assert victim not in left, "%s still built when disabled" % victim
        assert left == base - {victim}, \
            "disabling %s changed the others: %s" % (victim, left)
    return "disabling one element leaves the rest untouched"


def t_move_does_not_disturb():
    built = {g.name: g.bbox for g in _build(CONFIG)}
    d = copy.deepcopy(CONFIG)
    d["elements"]["track_map"]["x"] = 0.60
    d["elements"]["track_map"]["y"] = 0.40
    moved = {g.name: g.bbox for g in _build(d)}
    assert moved["track_map"] != built["track_map"], "the map did not move"
    for name in built:
        if name == "track_map":
            continue
        assert moved[name] == built[name], "%s shifted when the map moved" % name
    return "moved the map; speedo, tacho and G-meter unchanged"


def t_resolution_independent():
    small = {g.name: g.bbox for g in _build(CONFIG, 1280, 720)}
    big = {g.name: g.bbox for g in _build(CONFIG, 3840, 2160)}
    for name in small:
        rs = [v / 720.0 for v in small[name]]
        rb = [v / 2160.0 for v in big[name]]
        # x terms scale with width, which has the same 16:9 ratio here.
        for a, b in zip(rs, rb):
            assert abs(a - b) < 0.01, \
                "%s is not resolution independent: %s vs %s" % (name, rs, rb)
    return "720p and 4K layouts agree to within 1% of frame height"


def t_elements_stay_in_frame():
    for w, h in ((1920, 1080), (1280, 720)):
        for g in _build(CONFIG, w, h):
            x, y, bw, bh = g.bbox
            assert x >= -1 and y >= -1, "%s starts off-frame at %s" % (g.name, g.bbox)
            assert x + bw <= w + 1, "%s runs off the right edge" % g.name
            assert y + bh <= h + 1, "%s runs off the bottom edge" % g.name
    return "every bbox fits inside the frame at 1080p and 720p"


# ------------------------------------------------------------------ channels

def _rewrite_csv(src, dst, drop_devices=(), n_rows=400):
    """Copy a CSV keeping only the first n_rows, dropping whole device blocks."""
    rows = list(csv.reader(open(src, newline="", encoding="utf-8-sig")))
    hi = next(i for i, r in enumerate(rows) if r and r[0].strip() == "timestamp")
    cols, units, devs = rows[hi], rows[hi + 1], rows[hi + 2]
    keep = [i for i, d in enumerate(devs)
            if not any(k in d.lower() for k in drop_devices)]
    out = io.StringIO(newline="")
    wr = csv.writer(out, lineterminator="\n")
    for r in rows[:hi]:
        wr.writerow(r)
    for r in (cols, units, devs):
        wr.writerow([r[i] for i in keep])
    for r in rows[hi + 3: hi + 3 + n_rows]:
        r = r + [""] * (len(cols) - len(r))
        wr.writerow([r[i] for i in keep])
    Path(dst).write_text(out.getvalue(), encoding="utf-8")
    return dst


def t_missing_obd_block_names_the_channel():
    """Criterion 7: a CSV with a different channel set either loads, or fails
    with a message naming what is missing."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _rewrite_csv(a_csv(), Path(tmp) / "no_obd.csv",
                            drop_devices=("obd",))
        run = csv_io.read_run(path)          # must not crash
        assert not run.has("rpm"), "the OBD block was not actually removed"
        assert run.has("speed_gps"), "GPS speed went missing too"

        # The tacho asks for rpm_obd, which is now absent.
        from racechrono import render
        cfg = config_mod.load(copy.deepcopy(CONFIG))
        try:
            render.row_source(run, cfg, 60.0, run.t0, run.t1,
                              ["speed_gps", "rpm_obd"])
        except KeyError as exc:
            assert "rpm_obd" in str(exc), \
                "the error did not name the missing channel: %s" % exc
            return "loads without the OBD block; naming rpm_obd fails clearly"
    raise AssertionError("a missing rpm channel did not raise")


def t_bad_units_raise():
    with tempfile.TemporaryDirectory() as tmp:
        src = a_csv()
        rows = list(csv.reader(open(src, newline="", encoding="utf-8-sig")))
        hi = next(i for i, r in enumerate(rows) if r and r[0].strip() == "timestamp")
        i = rows[hi].index("latitude")
        rows[hi + 1][i] = "rad"          # claim radians instead of degrees
        dst = Path(tmp) / "bad_units.csv"
        with open(dst, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerows(rows[:hi + 3 + 50])
        try:
            csv_io.read_run(dst)
        except ValueError as exc:
            assert "latitude" in str(exc), exc
            return "a mislabelled unit raises instead of rendering wrong data"
    raise AssertionError("a mislabelled latitude unit was accepted")


def t_bearing_wraps_correctly():
    run = csv_io.read_run(a_csv())
    g = csv_io.resample(run, 20.0)
    b = g["bearing"].to_numpy(float)
    b = b[np.isfinite(b)]
    assert b.min() >= 0.0 and b.max() <= 360.0, "bearing left 0..360"
    # A circular interpolation never produces the ~180 deg midpoints that a
    # linear one invents at the 359->1 wrap.
    from racechrono import geo
    mid = geo.interp_circular_deg(np.array([0.5]), [0.0, 1.0], [359.0, 1.0])[0]
    assert min(mid, 360.0 - mid) < 1.0, "wrap interpolated the long way: %.1f" % mid
    return "interpolated on the unit circle, not through 180"


# ------------------------------------------------------------------ map

def t_map_transform_is_fixed():
    tm = a_map()
    run = csv_io.read_run(a_csv())
    lat, lon = run.values("latitude"), run.values("longitude")
    m = run.good_mask()
    u1, v1 = tm.project(lat[m], lon[m])
    # Re-projecting a subset must give bit-identical results: the transform
    # comes from the map file, never from the data's own extent.
    half = slice(0, m.sum() // 2)
    u2, v2 = tm.project(lat[m][half], lon[m][half])
    assert np.allclose(u1[half], u2), "projection depends on the input extent"
    assert np.allclose(v1[half], v2), "projection depends on the input extent"
    return "projection of a subset matches the whole; no extent recomputation"


def t_marker_returns_to_start():
    """Criterion 3, as a number: over a full lap the marker must come back to
    within one marker width of where it started."""
    run = a_lapped_run()
    tm = _map_for(run)
    lp = laps_mod.laps(run)
    lap = min(lp, key=lambda l: l.duration)
    m = run.good_mask()[lap.i0:lap.i1]
    u, v = tm.project(run.values("latitude")[lap.i0:lap.i1][m],
                      run.values("longitude")[lap.i0:lap.i1][m])
    span = max(tm.extent_m["x"][1] - tm.extent_m["x"][0],
               tm.extent_m["y"][1] - tm.extent_m["y"][0])
    closure_m = float(np.hypot(u[0] - u[-1], v[0] - v[-1])) * span
    # marker size and map height are both fractions of frame height, so the
    # marker's footprint in metres is independent of resolution
    el = CONFIG["elements"]["track_map"]
    marker_m = el["marker"]["size"] / el["height"] * span
    assert closure_m < marker_m, \
        "closure %.1f m exceeds one marker width (%.1f m)" % (closure_m, marker_m)
    poly = tm.unit_points
    dev = np.sqrt(((np.c_[u, v][:, None, :] - poly[None]) ** 2).sum(-1)).min(1) * span
    return ("lap %d closes to %.1f m (marker is %.1f m wide); "
            "max deviation from the map line %.1f m"
            % (lap.number, closure_m, marker_m, dev.max()))


def t_start_finish_recovered():
    runs = [csv_io.read_run(p)
            for p in sorted(SOURCE.glob("*.csv"))]
    point, normal, origin, n = laps_mod.start_finish(runs)
    assert n >= len(runs), "found only %d crossings across %d runs" % (n, len(runs))
    assert abs(np.hypot(*normal) - 1.0) < 1e-6, "normal is not a unit vector"
    return "%d crossings -> point (%.1f, %.1f) m, heading %.1f deg" % (
        n, point[0], point[1], np.degrees(np.arctan2(normal[0], normal[1])) % 360)


def t_resample_respects_fragments():
    """A synthetic session pause must not be interpolated across."""
    run = csv_io.read_run(a_csv())
    df = run.df.copy()
    half = len(df) // 2
    # Open a 30 s hole and call the far side a new fragment.
    df.loc[half:, "fragment_id"] = 1.0
    df.loc[half:, "timestamp"] = df.loc[half:, "timestamp"] + 30.0
    run2 = csv_io.Run(path=run.path, df=df, units=run.units,
                      devices=run.devices, aliases=run.aliases, meta=run.meta)
    g = csv_io.resample(run2, 10.0)
    gap = ~g["valid"].to_numpy(bool)
    assert gap.any(), "the fragment gap was papered over"
    assert g.loc[gap, "speed_gps"].isna().all(), \
        "values were interpolated across a session pause"
    return "%.1f s of gap left empty rather than bridged" % (gap.sum() / 10.0)


# ------------------------------------------------------------ trim, segments

def _cfg():
    return config_mod.load(copy.deepcopy(CONFIG))


def t_trim_keeps_all_driving():
    """The window must never cut inside the driving, and must actually trim."""
    cfg = _cfg()
    details = []
    for p in sorted(SOURCE.glob("*.csv")):
        run = csv_io.read_run(p)
        t0, t1, _ = window.driving_window(run, cfg.trim.move_threshold_ms,
                                          cfg.trim.pre_s, cfg.trim.post_s)
        t = run.values("timestamp")
        spd = run.values("speed_gps")
        moving = np.isfinite(spd) & (spd > cfg.trim.move_threshold_ms)
        assert t0 <= t[np.argmax(moving)], "window starts after the car moved"
        last = t[len(moving) - 1 - int(np.argmax(moving[::-1]))]
        assert t1 >= last, "window ends before the car stopped"
        # Every moving sample must be inside the window.
        assert ((t[moving] >= t0 - 1e-6) & (t[moving] <= t1 + 1e-6)).all(), \
            "a moving sample fell outside the trim window"
        details.append("%s -%.0fs" % (p.stem[-6:], run.duration - (t1 - t0)))
    return "no driving cut; " + ", ".join(details)


def t_trim_disabled_keeps_everything():
    cfg = _cfg()
    cfg.trim.enabled = False
    run = csv_io.read_run(a_csv())
    tgt = window.session_target(run, cfg)
    assert abs(tgt.t0 - run.t0) < 1e-6 and abs(tgt.t1 - run.t1) < 1e-6, \
        "trim.enabled = False still trimmed"
    return "whole session kept"


def t_trim_survives_a_dead_speed_channel():
    run = csv_io.read_run(a_csv())
    df = run.df.copy()
    df[run.col("speed_gps")] = 0.0
    df[run.col("speed_calc")] = 0.0
    df[run.col("speed_obd")] = 0.0
    dead = csv_io.Run(path=run.path, df=df, units=run.units,
                      devices=run.devices, aliases=run.aliases, meta=run.meta)
    t0, t1, note = window.driving_window(dead, 1.0, 5.0, 5.0)
    assert abs(t0 - dead.t0) < 1e-6 and abs(t1 - dead.t1) < 1e-6, \
        "a never-moving session was trimmed to nothing"
    return note


def _pairing_for(run, clips):
    got, _ = video.pair([run], clips)
    return got[0]


def t_segments_cover_the_window():
    """Segments must tile the requested window, minus only the real camera gaps."""
    cfg = _cfg()
    clips = video.discover(SOURCE, cfg.ffmpeg.ffprobe, recursive=True)
    runs = _all_runs()
    pairings, _ = video.pair(runs, clips)
    paired = [(r, p) for r, p in zip(runs, pairings) if p.clips]
    if not paired:
        raise Skip("no session in %s is paired with footage"
               % SOURCE)
    details = []
    for run, pairing in paired:
        tgt = window.session_target(run, cfg)
        segs = video.plan_segments(pairing.clips, tgt.t0, tgt.t1)
        assert segs, "no segments for %s" % run.stem
        # In order, non-overlapping, inside the window and inside their clip.
        prev = None
        for s in segs:
            assert s.seek >= -1e-6, "negative seek"
            assert s.seek + s.duration <= s.clip.duration + 1e-3, \
                "segment runs past the end of %s" % s.clip.path.name
            assert s.data_t0 >= tgt.t0 - 1e-6, "segment starts before the window"
            assert s.data_t1 <= tgt.t1 + 1e-3, "segment ends after the window"
            if prev is not None:
                assert s.data_t0 >= prev - 1e-6, "segments overlap or are unordered"
            prev = s.data_t1
        covered = sum(s.duration for s in segs)
        gaps = tgt.duration - covered
        details.append("%s: %d seg, %.1f s uncovered" % (run.stem[-6:], len(segs), gaps))
    return "; ".join(details)


def t_boundary_straddling_lap_splits():
    """A lap that spans a camera split must produce two segments, not one."""
    cfg = _cfg()
    clips = video.discover(SOURCE, cfg.ffmpeg.ffprobe, recursive=True)
    runs = [csv_io.read_run(p)
            for p in sorted(SOURCE.glob("*.csv"))]
    pairings, _ = video.pair(runs, clips)
    split = []
    for run, pairing in zip(runs, pairings):
        if len(pairing.clips) < 2:
            continue
        for tgt in window.lap_targets(run, cfg):
            segs = video.plan_segments(pairing.clips, tgt.t0, tgt.t1)
            if len(segs) > 1:
                split.append("%s lap %d" % (run.stem[-6:], tgt.lap_number))
    if not split:
        raise Skip("no lap straddles a camera split in this dataset")
    return "split across clips: " + ", ".join(split)


def t_lap_names_are_safe_filenames():
    cfg = _cfg()
    run = a_lapped_run()
    bad = '<>:"/\\|?*'
    targets = window.lap_targets(run, cfg)
    if not targets:
        raise Skip("no lap targets to name")
    for tgt in targets:
        assert not any(c in tgt.name for c in bad), \
            "lap name %r has a character Windows will not accept" % tgt.name
        assert tgt.t1 > tgt.t0, "empty lap window"
    return "e.g. %s" % targets[0].name


def t_one_output_per_session():
    cfg = _cfg()
    runs = [csv_io.read_run(p)
            for p in sorted(SOURCE.glob("*.csv"))]
    names = []
    for run in runs:
        tg = window.build_targets(run, cfg, "sessions")
        assert len(tg) == 1, "%s produced %d session outputs" % (run.stem, len(tg))
        names.append(tg[0].name)
    assert len(set(names)) == len(names), "session output names collide"
    return "%d sessions -> %d files" % (len(runs), len(names))


def t_encoder_args_are_consistent():
    cfg = _cfg()
    args = render.encoder_args(cfg.encode)
    assert "-cq" in args and "-multipass" in args, args
    # An x264 preset with an NVENC encoder is a common mistake; it must fail.
    bad = copy.deepcopy(CONFIG)
    bad["encode"] = dict(bad["encode"])
    bad["encode"]["preset"] = "medium"
    try:
        config_mod.load(bad)
    except config_mod.ConfigError as exc:
        assert "preset" in str(exc)
        return "nvenc args built; mismatched preset name rejected"
    raise AssertionError("an x264 preset name was accepted for an NVENC encoder")


# ------------------------------------------------------- frame hygiene

def _frame_bytes(comp):
    comp.surface.flush()
    return bytes(comp.surface.get_data())


def _compositor(cfg=None, run=None):
    """A compositor bound to a real run.

    Channel options may name a list of candidates in preference order, so a
    config has to be bound to a run before any gauge can be built from it.
    """
    from racechrono import render
    run = run if run is not None else a_mapped_run()
    cfg = config_mod.bind(cfg or _cfg(), run)
    # The map has to be the one covering *this* run. Taking the first map in the
    # directory instead put an autocross run on the circuit map, 180 km away, and
    # the off-map check then reported a marker 58670 px outside the box: still a
    # true statement about clipping, but no longer the pit-lane case it claims to
    # be testing, and the on-track comparison point was meaningless.
    return render.Compositor(cfg, 1920, 1080, 60.0, _map_for(run))


def _on_track(run):
    """A position that is certainly on the racing line: the fastest sample.

    Hard-coding a lat/lon ties the frame-hygiene checks to one venue.
    """
    lat, lon = run.values("latitude"), run.values("longitude")
    spd = run.values("speed_gps")
    ok = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(spd) & run.good_mask()
    idx = np.flatnonzero(ok)
    j = idx[int(np.argmax(spd[ok]))]
    return float(lat[j]), float(lon[j])


def _row(comp, lat, lon, bearing=90.0):
    row = {c: 0.0 for c in comp.channels}
    row.update({"latitude": lat, "longitude": lon, "bearing": bearing,
                "good": True})
    return row


def _worst_offmap(run, gauge):
    """The run's position that projects furthest outside the map rectangle.

    The offending positions are the pit lane and paddock -- roughly 1% of a
    session -- so a test that samples evenly will miss them. Find the worst case
    instead of hoping to stumble on it.
    """
    tm = gauge.tm
    lat = run.values("latitude")
    lon = run.values("longitude")
    ok = np.isfinite(lat) & np.isfinite(lon)
    u, v = tm.project(lat[ok], lon[ok])
    x, y = gauge._to_px(u, v)
    ox, oy = gauge._origin
    out = np.maximum.reduce([ox - x, x - (ox + gauge.w),
                             oy - y, y - (oy + gauge.h)])
    j = int(np.argmax(out))
    idx = np.flatnonzero(ok)[j]
    return (float(lat[idx]), float(lon[idx]), float(out[j]),
            float(np.asarray(x)[j]), float(np.asarray(y)[j]))


def _worst_offmap_run():
    """The run whose marker projects furthest outside its own map.

    Which run that is depends on the venue -- the circuit has a pit lane and a
    paddock, an autocross course has a staging lane -- so picking the first CSV in
    the directory made the strength of the check depend on filename order. Search
    for the worst case across everything that has a map.
    """
    best = None
    for run in _all_runs():
        try:
            comp = _compositor(run=run)
        except Skip:
            continue
        g = [x for x in comp.gauges if x.name == "track_map"][0]
        lat, lon, over, _, _ = _worst_offmap(run, g)
        if best is None or over > best[0]:
            best = (over, run, comp, lat, lon)
    if best is None:
        raise Skip("no session has a map covering it")
    over, run, comp, lat, lon = best
    return run, comp, lat, lon, over


def t_no_residue_between_frames():
    """The reported bug: anything a gauge draws outside the rectangle it clears
    is never cleared again and stays burnt into every later frame.

    Draw the worst off-map position, then an on-track one, and compare against a
    compositor that only ever saw the on-track position. Any difference is
    residue from the first frame.
    """
    run, comp, off_lat, off_lon, over = _worst_offmap_run()
    assert over > 0, "no off-map position in any run; the test would be vacuous"

    dirty = _compositor(run=run)
    on_lat, on_lon = _on_track(run)
    dirty.draw(_row(dirty, off_lat, off_lon))
    dirty.draw(_row(dirty, on_lat, on_lon))         # on the racing line

    clean = _compositor(run=run)
    clean.draw(_row(clean, on_lat, on_lon))

    a, b = _frame_bytes(dirty), _frame_bytes(clean)
    if a != b:
        diff = [i // 4 for i in range(0, len(a), 4) if a[i:i + 4] != b[i:i + 4]]
        raise AssertionError(
            "%d pixel(s) of residue from the previous frame, first at (%d, %d)"
            % (len(diff), diff[0] % 1920, diff[0] // 1920))
    return ("%s: worst off-map marker sits %.1f px outside the map and leaves "
            "nothing behind" % (run.stem[:16], over))


def t_empty_overlay_is_refused():
    """A config that enables no elements must fail, not render a blank overlay.

    The region path checked this; the full-frame path did not, so a job whose
    config disabled everything -- easily done, since a partially-specified config
    dict defaults every element to off -- ran to completion and wrote a plain
    re-encode of the source. It looks like a successful render right up until
    someone watches it.
    """
    run = _all_runs()[0]
    bad = copy.deepcopy(CONFIG)
    for el in bad["elements"].values():
        el["enabled"] = False
    cfg = config_mod.bind(config_mod.load(bad), run)
    for label, make in (
            ("full frame", lambda: render.Compositor(cfg, 1920, 1080, 60.0, a_map())),
            ("regions", lambda: overlay_mod.RegionCompositor(
                cfg, 1920, 1080, 60.0, a_map()))):
        try:
            make()
        except ValueError as exc:
            assert "no enabled elements" in str(exc),                 "%s raised the wrong error: %s" % (label, exc)
        else:
            raise AssertionError("%s accepted a config with nothing enabled "
                                 "and would have written a blank overlay" % label)
    return "both compositors refuse a config with every element disabled"


def t_frame_is_premultiplied():
    """Every pixel the compositor emits must be premultiplied ARGB.

    The export pipes these bytes to ffmpeg through `unpremultiply=inplace=1`,
    which divides each colour channel by alpha; a channel above its own alpha
    saturates, and glyph edges come out too bright. The preview does the same
    division in JavaScript.

    This was not hypothetical. cairo's default font antialiasing resolved to
    ANTIALIAS_SUBPIXEL, which computes coverage per colour channel against an
    assumed RGB stripe -- so the output was not premultiplied ARGB at all. 3540
    pixels of a 1080p frame had a channel above alpha, by up to 75. Subpixel AA
    is also wrong on principle here: it is only correct over a known background,
    and an overlay sits on arbitrary video.
    """
    run = a_lapped_run()
    comp = _compositor(run=run)
    lookup, _t0, n = render.row_source(run, config_mod.bind(_cfg(), run), 60.0,
                                       run.t0, run.t1, comp.channels)
    worst_n = worst_excess = 0
    where = None
    step = max(1, n // 120)
    for i in range(0, n, step):
        comp.draw(lookup(i))
        comp.surface.flush()
        a = np.ndarray((comp.surface.get_height(), comp.surface.get_width(), 4),
                       np.uint8, comp.surface.get_data()).astype(np.int16)
        over = a[:, :, :3].max(axis=2) - a[:, :, 3]
        bad = int((over > 0).sum())
        if bad > worst_n:
            ys, xs = np.nonzero(over > 0)
            worst_n, where = bad, (int(xs[0]), int(ys[0]))
        worst_excess = max(worst_excess, int(over.max()))
    assert worst_n == 0, (
        "%d pixel(s) are not premultiplied, first at %s, worst channel %d above "
        "its alpha -- unpremultiply will clip them"
        % (worst_n, where, worst_excess))
    return "%d frames sampled, every pixel premultiplied" % len(
        range(0, n, step))


def t_bbox_is_big_enough():
    """Each gauge's drawing must fit inside its own bbox, with the compositor's
    clip bypassed.

    The compositor clips every gauge to its box, so with the clip in place this
    can never fail -- which would make it a tautology. Calling `draw` directly
    instead tests the thing that actually matters: that a gauge's box is large
    enough for everything it draws. If it is not, the clip silently truncates the
    drawing rather than leaving debris, and a half-drawn marker is still a bug.
    """
    import cairo
    run, comp, off_lat, off_lon, over = _worst_offmap_run()
    allowed = np.zeros((1080, 1920), dtype=bool)
    for gg in comp.gauges:
        x, y, w, h = (int(round(v)) for v in gg.bbox)
        allowed[max(0, y):y + h, max(0, x):x + w] = True

    cases = [(off_lat, off_lon), _on_track(run)]
    lat, lon = run.values("latitude"), run.values("longitude")
    ok = np.isfinite(lat) & np.isfinite(lon)
    for f in (np.argmin, np.argmax):
        cases.append((float(lat[ok][f(lat[ok])]), float(lon[ok][f(lat[ok])])))
        cases.append((float(lat[ok][f(lon[ok])]), float(lon[ok][f(lon[ok])])))

    worst, where = 0, None
    for la, lo in cases:
        for bearing in (0.0, 90.0, 200.0, 315.0):
            comp.ctx.save()
            comp.ctx.set_operator(cairo.OPERATOR_CLEAR)
            comp.ctx.paint()
            comp.ctx.restore()
            row = _row(comp, la, lo, bearing)
            for gg in comp.gauges:          # deliberately unclipped
                gg.draw(comp.ctx, row)
            buf = np.frombuffer(_frame_bytes(comp), dtype=np.uint8)
            alpha = buf.reshape(1080, 1920, 4)[:, :, 3]
            stray = (alpha > 0) & ~allowed
            n = int(stray.sum())
            if n > worst:
                ys, xs = np.nonzero(stray)
                worst, where = n, (int(xs[0]), int(ys[0]))
    assert worst == 0, ("%d pixel(s) drawn outside a gauge bbox, first at %s -- "
                        "the box is too small for what the gauge draws"
                        % (worst, where))
    return ("%d positions x 4 bearings, unclipped, including the worst off-map "
            "one (%.1f px out): everything fits" % (len(cases), over))


def t_marker_stays_on_the_map():
    run = a_mapped_run()
    _map_for(run)                     # skip unless the map covers this run
    comp = _compositor(run=run)
    g = [x for x in comp.gauges if x.name == "track_map"][0]
    lat, lon = run.values("latitude"), run.values("longitude")
    ox, oy = g._origin
    ok = np.isfinite(lat) & np.isfinite(lon)
    outside = 0
    for i in np.flatnonzero(ok)[::200]:
        g.draw(comp.ctx, _row(comp, float(lat[i]), float(lon[i])))
        mx, my = g._last_xy
        if not (ox - 0.5 <= mx <= ox + g.w + 0.5
                and oy - 0.5 <= my <= oy + g.h + 0.5):
            outside += 1
    assert outside == 0, "%d marker position(s) outside the map rect" % outside
    return "clamped to the map rect at every sampled position"


# ------------------------------------------------------------ gauge row

def t_row_is_symmetric():
    """The row is gmeter-speedo-tacho-throttle, so the frame centreline must
    fall between the speedo and the tacho, and the two small gauges must mirror
    each other about it -- at any resolution or aspect ratio."""
    details = []
    for w, h in ((1920, 1080), (1280, 720), (3840, 2160), (2560, 1080)):
        cfg = config_mod.bind(_cfg(), _all_runs()[0])
        gs = {g.name: g for g in gauges.build(
            cfg, Layout(w, h),
            {"trackmap": a_map(), "fps": 60.0, "dim_alpha": 0.4})}
        for need in ("speedo", "tacho", "gmeter", "throttle"):
            assert need in gs, "%s is not built" % need
        centre = w / 2.0
        big = (gs["speedo"].cx + gs["tacho"].cx) / 2.0
        small = (gs["gmeter"].cx + gs["throttle"].cx) / 2.0
        assert abs(big - centre) < 0.51,             "%dx%d: speedo/tacho midpoint is %.2f px off centre" % (w, h, big - centre)
        assert abs(small - centre) < 0.51,             "%dx%d: gmeter/throttle midpoint is %.2f px off centre" % (w, h, small - centre)
        assert gs["gmeter"].cx < gs["speedo"].cx < gs["tacho"].cx < gs["throttle"].cx,             "%dx%d: gauges are out of order" % (w, h)
        assert abs(gs["throttle"].d - gs["gmeter"].d) < 0.51,             "throttle and G-meter should be the same size"
        details.append("%dx%d ok" % (w, h))
    return "; ".join(details)


def t_throttle_rescale():
    cfg = config_mod.bind(_cfg(), _all_runs()[0])
    gs = {g.name: g for g in gauges.build(
        cfg, Layout(1920, 1080),
        {"trackmap": a_map(), "fps": 60.0, "dim_alpha": 0.4})}
    th = gs["throttle"]
    lo, hi = float(th.input_min), float(th.input_max)
    assert abs(th._display_value(lo) - 0.0) < 1e-9, "closed rail is not 0%"
    assert abs(th._display_value(hi) - 100.0) < 1e-9, "WOT rail is not 100%"
    assert abs(th._display_value((lo + hi) / 2) - 50.0) < 1e-9, "midpoint is not 50%"
    assert th._display_value(lo - 5) == 0.0, "below the rail is not clamped"
    assert th._display_value(hi + 5) == 100.0, "above the rail is not clamped"
    assert th._display_value(float("nan")) is None, "NaN should read as --"
    assert th.label == "throttle", "centre label should say throttle"

    # A dial with no rails configured must be left alone, including above range.
    sp = gs["speedo"]
    assert sp.input_min is None and not sp.clamp, "speedo should not clamp"
    assert sp._display_value(60.0) > 120.0,         "speedo clamped a real over-range reading"
    return ("rails %.2f..%.2f -> 0..100, clamped outside; speedo untouched"
            % (lo, hi))


def t_throttle_matches_the_data():
    """The configured rails should be the car's actual plateaus, not a guess."""
    run = csv_io.read_run(a_csv())
    cfg = config_mod.bind(_cfg(), run)
    th = [g for g in gauges.build(cfg, Layout(1920, 1080),
                                  {"trackmap": a_map(), "fps": 60.0,
                                   "dim_alpha": 0.4})
          if g.name == "throttle"][0]
    v = run.values(th.channel)
    v = v[np.isfinite(v)]
    p01, p999 = np.percentile(v, 0.1), np.percentile(v, 99.9)
    assert abs(p01 - float(th.input_min)) < 1.0,         "input_min %.2f is not the closed-throttle plateau (p0.1 = %.2f)" % (
            th.input_min, p01)
    assert abs(p999 - float(th.input_max)) < 1.0,         "input_max %.2f is not the WOT plateau (p99.9 = %.2f)" % (
            th.input_max, p999)
    return "p0.1 = %.2f, p99.9 = %.2f vs rails %.2f / %.2f" % (
        p01, p999, th.input_min, th.input_max)


def t_no_gauge_erases_another():
    """Gauges clear their own box with OPERATOR_SOURCE, so a box overlapping a
    neighbour's wipes part of it depending on draw order.

    Each gauge is drawn alone using the *same* gauge objects as the combined
    frame. Rebuilding with the others disabled would move them: the tacho,
    G-meter and throttle are all positioned relative to the speedo, so a solo
    speedo-less tacho lands somewhere else entirely and the comparison would be
    meaningless.
    """
    import cairo
    run = csv_io.read_run(a_csv())
    mid = len(run.df) // 2
    lat = float(run.values("latitude")[mid])
    lon = float(run.values("longitude")[mid])

    comp = _compositor()
    row = _row(comp, lat, lon)
    comp.draw(row)
    combined = np.frombuffer(_frame_bytes(comp), np.uint8).reshape(1080, 1920, 4)

    lost = {}
    for g in comp.gauges:
        comp.ctx.save()
        comp.ctx.set_operator(cairo.OPERATOR_CLEAR)
        comp.ctx.paint()
        comp.ctx.restore()
        x, y, w, h = g.bbox
        comp.ctx.save()
        comp.ctx.rectangle(x, y, w, h)
        comp.ctx.clip()
        g.draw(comp.ctx, row)
        comp.ctx.restore()
        alone = np.frombuffer(_frame_bytes(comp), np.uint8).reshape(1080, 1920, 4)
        missing = int(((alone[:, :, 3] > 128) & (combined[:, :, 3] == 0)).sum())
        if missing:
            lost[g.name] = missing
    assert not lost, "pixels erased by a neighbouring gauge: %s" % lost

    # Report which boxes touch, since an overlap is only harmless while the
    # overlapping strip stays empty.
    touching = []
    for i, a_ in enumerate(comp.gauges):
        for b_ in comp.gauges[i + 1:]:
            ax, ay, aw, ah = a_.bbox
            bx, by, bw, bh = b_.bbox
            ox = min(ax + aw, bx + bw) - max(ax, bx)
            oy = min(ay + ah, by + bh) - max(ay, by)
            if ox > 0 and oy > 0:
                touching.append("%s/%s %dx%d px" % (a_.name, b_.name, ox, oy))
    return ("%d gauges, none erases another%s"
            % (len(comp.gauges),
               "; overlapping but empty: " + ", ".join(touching) if touching else ""))


# ------------------------------------------------------- overlay contract

def t_atlas_matches_full_frame():
    """The region atlas must be pixel-identical to the full-frame reference.

    This is what makes the seam safe: a preview built from regions and an export
    built from the whole frame cannot show different pixels.
    """
    run = a_mapped_run()
    tm = a_map()
    cfg_full = config_mod.bind(_cfg(), run)
    cfg_full.encode.overlay_regions = False
    cfg_reg = config_mod.bind(_cfg(), run)
    cfg_reg.encode.overlay_regions = True
    full = render.make_compositor(cfg_full, 1920, 1080, 60.0, tm)
    reg = render.make_compositor(cfg_reg, 1920, 1080, 60.0, tm)
    assert type(full).__name__ == "Compositor"
    assert type(reg).__name__ == "RegionCompositor"

    lookup, t0, n = render.row_source(run, cfg_reg, 60.0, run.t0, run.t1,
                                      reg.channels)
    stride = reg.surface.get_stride()
    worst = 0
    for i in np.linspace(0, n - 1, 25).astype(int):
        row = lookup(int(i))
        full.draw(row)
        reg.draw(row)
        fa = np.frombuffer(bytes(full.buffer()), np.uint8).reshape(1080, 1920, 4)
        ra = np.frombuffer(bytes(reg.buffer()), np.uint8).reshape(
            reg.atlas_h, stride // 4, 4)
        for r in reg.regions:
            want = fa[r.y:r.y + r.h, r.x:r.x + r.w].astype(int)
            got = ra[r.ay:r.ay + r.h, r.ax:r.ax + r.w].astype(int)
            worst = max(worst, int(np.abs(want - got).max()))
        # Nothing may be drawn outside the planned regions, or the atlas would
        # be silently dropping part of the overlay.
        mask = np.zeros((1080, 1920), bool)
        for r in reg.regions:
            mask[r.y:r.y + r.h, r.x:r.x + r.w] = True
        assert int((fa[:, :, 3][~mask] > 0).sum()) == 0, \
            "the full frame draws outside the planned regions"
    assert worst == 0, "atlas differs from the full frame by up to %d" % worst
    return ("%d regions, %.1f%% coverage, %.2f MB/frame vs %.2f full; "
            "identical over 25 frames"
            % (len(reg.regions), 100 * reg.coverage,
               reg.bytes_per_frame / 1e6, full.bytes_per_frame / 1e6))


def t_region_plan_is_sane():
    run = a_mapped_run()
    cfg = config_mod.bind(_cfg(), run)
    comp = overlay_mod.RegionCompositor(cfg, 1920, 1080, 60.0, a_map())
    assert comp.regions, "no regions planned"
    assert len(comp.regions) <= cfg.encode.max_overlay_regions, \
        "planner exceeded max_overlay_regions"
    seen = []
    for r in comp.regions:
        assert r.w > 0 and r.h > 0
        assert 0 <= r.x and r.x + r.w <= 1920
        assert 0 <= r.y and r.y + r.h <= 1080
        assert r.ax + r.w <= comp.atlas_w and r.ay + r.h <= comp.atlas_h
        for g in r.gauges:
            seen.append(g.name)
            gx, gy, gw, gh = g.bbox
            assert (gx >= r.x - 0.01 and gy >= r.y - 0.01
                    and gx + gw <= r.x + r.w + 0.01
                    and gy + gh <= r.y + r.h + 0.01), \
                "%s does not fit its region" % g.name
    assert sorted(seen) == sorted(g.name for g in comp.gauges), \
        "a gauge is missing from the region plan, or duplicated"
    for i, a in enumerate(comp.regions):
        for b in comp.regions[i + 1:]:
            ox = min(a.ax + a.w, b.ax + b.w) - max(a.ax, b.ax)
            oy = min(a.ay + a.h, b.ay + b.h) - max(a.ay, b.ay)
            assert not (ox > 0 and oy > 0), "atlas slots overlap"
    return "%d regions, atlas %dx%d, all gauges assigned once" % (
        len(comp.regions), comp.atlas_w, comp.atlas_h)


def t_filter_graph_shape():
    run = a_mapped_run()
    cfg = config_mod.bind(_cfg(), run)
    comp = overlay_mod.RegionCompositor(cfg, 1920, 1080, 60.0, a_map())
    g = overlay_mod.filter_graph(comp.regions, 60.0, 1920, 1080)
    n = len(comp.regions)
    assert g.count("overlay=") == n, "expected %d overlays, got %d" % (
        n, g.count("overlay="))
    assert g.count("crop=") == n
    if n > 1:
        assert ("split=%d" % n) in g
    assert g.endswith("[v]"), "graph must end at [v]"
    assert "unpremultiply" in g, "cairo output is premultiplied; must convert"
    one = [overlay_mod.Region(0, 0, 1920, 1080, gauges=list(comp.gauges))]
    simple = overlay_mod.filter_graph(one, 60.0, 1920, 1080)
    assert "crop=" not in simple and "split" not in simple
    return "%d regions -> %d crop/overlay pairs; degenerate case simplifies" % (n, n)


def t_overlay_mode_auto_resolves():
    cfg = _cfg()
    assert cfg.encode.overlay_regions == "auto"
    assert cfg.encode.resolve_overlay_regions(1) is False, \
        "one worker should use the full frame (measured faster)"
    assert cfg.encode.resolve_overlay_regions(4) is True, \
        "several workers should use regions (measured faster)"
    bad = copy.deepcopy(CONFIG)
    bad["encode"] = dict(bad["encode"])
    bad["encode"]["overlay_regions"] = "yes"
    try:
        config_mod.load(bad)
    except config_mod.ConfigError:
        return "auto -> full frame at 1 worker, regions at 4; bad value rejected"
    raise AssertionError("an invalid overlay_regions value was accepted")


# ------------------------------------------------------------- job api

def t_job_plan_structure():
    run, _ = a_run_with_footage()
    p = job_mod.plan(source=SOURCE, job={"select": {"runs": [run.stem], "export": "both"}})
    assert p["schema"] == job_mod.SCHEMA_VERSION
    assert p["runs"], "plan has no runs"
    r = p["runs"][0]
    for key in ("stem", "channels", "laps", "pairing", "map", "outputs",
                "fix_quality", "duration_s"):
        assert key in r, "plan run is missing %r" % key
    assert r["channels"] and "unit" in r["channels"][0]
    outs = [o for o in r["outputs"] if o["renderable"]]
    assert outs, "no renderable outputs planned"
    assert any(o["kind"] == "session" for o in outs)
    for o in outs:
        assert o["segments"], "%s has no segments" % o["name"]
        assert abs(sum(s["duration_s"] for s in o["segments"])
                   - o["duration_s"]) < 0.05
    assert json.loads(json.dumps(p)) == p, "plan is not JSON-round-trippable"
    return "%d run(s), %d output(s), %.0f s of video, JSON clean" % (
        p["totals"]["runs"], p["totals"]["outputs"], p["totals"]["video_seconds"])


def t_job_plan_matches_text_report():
    """The terminal report is a view of the plan, so its numbers must be the
    plan's numbers rather than a second computation."""
    run, _ = a_run_with_footage()
    p = job_mod.plan(source=SOURCE, job={"select": {"runs": [run.stem]}})
    text = job_mod.format_plan(p)
    r = p["runs"][0]
    assert r["stem"] in text
    for o in r["outputs"]:
        if o["renderable"]:
            assert ("%.1f s" % o["duration_s"]) in text, \
                "output duration %s missing from the report" % o["duration_s"]
    assert ("%.2f GB" % p["totals"]["estimated_gb"]) in text
    return "report renders the plan; durations and size agree"


def t_job_probe():
    p = job_mod.probe(_cfg())
    assert p["ffmpeg"]["ffmpeg"], "probe did not find ffmpeg"
    assert p["platform"]["cpus"], "probe did not report cpu count"
    assert any(p["encoders"].values()), "probe found no working encoder"
    rec = job_mod.recommend_encoder(p)
    assert rec and p["encoders"][rec], "recommended encoder is not usable"
    return "recommends %s; %d/%d encoders usable" % (
        rec, sum(1 for v in p["encoders"].values() if v), len(p["encoders"]))


# ----------------------------------------------------------------- imu

def t_no_duplicate_columns():
    """Regression: five `device_update_rate` columns must stay five columns.

    When the phone's accelerometer, gyroscope and magnetometer were switched on,
    the device tags went from three known names to six, and OBD was renumbered
    from `200:` to `300:`. Matching a hard-coded list collapsed three columns
    onto one name -- and a duplicated column makes `values()` hand back a 2-D
    frame instead of a series, which crashed resampling outright.
    """
    from collections import Counter
    details = []
    for run in _all_runs():
        dupes = {k: v for k, v in Counter(run.df.columns).items() if v > 1}
        assert not dupes, "%s has duplicate columns: %s" % (run.stem, dupes)
        for c in run.df.columns:
            v = run.values(c)
            assert v.ndim == 1, "%s.%s is %d-D, not a series" % (run.stem, c, v.ndim)
        rates = [c for c in run.df.columns if c.startswith("device_update_rate")]
        details.append(len(rates))
    return "%d run(s), no duplicates; up to %d device_update_rate columns" % (
        len(details), max(details))


def t_unknown_device_block_is_named():
    """A device this code has never seen still gets a usable suffix."""
    names, aliases = csv_io.disambiguate(
        ["speed", "speed", "rate", "rate", "rate"],
        ["100: gps", "400: lidar", "100: gps", "400: lidar", "calc"])
    assert len(set(names)) == len(names), "names collided: %s" % names
    assert "speed_lidar" in names, names
    assert aliases.get("speed_gps") == "speed_gps"
    return "unseen device 400: lidar -> %r" % [n for n in names if "lidar" in n]


def t_continuous_channels_interpolate():
    """Regression: a channel this code has not heard of must interpolate.

    Listing the continuous channels meant anything unrecognised was step-filled,
    so nine genuinely continuous 100 Hz IMU channels were being held constant
    between samples. Discrete channels are the closed set; everything else is
    continuous.
    """
    run = a_run_with_imu()
    g = csv_io.resample(run, 50.0)
    v = g["x_acc"].to_numpy(float)
    v = v[np.isfinite(v)]
    # A step-filled signal repeats values; an interpolated one rarely does.
    repeats = float(np.mean(np.diff(v) == 0.0))
    assert repeats < 0.05, ("x_acc looks step-filled: %.1f%% of consecutive "
                            "samples are identical" % (100 * repeats))
    lap = g["lap_number"].to_numpy(float)
    lap = lap[np.isfinite(lap)]
    if lap.size:
        assert np.allclose(lap, np.round(lap)), \
            "lap_number was interpolated to fractional values"
    return "x_acc interpolates (%.2f%% repeats); lap_number stays integral" % (
        100 * repeats)


def t_imu_calibration_solves():
    from racechrono import imu as imu_mod
    run = a_run_with_imu()
    cal = run.imu
    assert cal is not None, "no calibration was solved for %s" % run.stem
    assert cal.trusted, "calibration not trusted: %s" % "; ".join(cal.notes)
    assert 0.95 <= cal.g_magnitude <= 1.05, \
        "|a| at rest is %.3f g, so the gravity reference is wrong" % cal.g_magnitude
    assert cal.still_seconds >= 0.5
    assert abs(np.linalg.norm(cal.up) - 1.0) < 1e-6
    assert abs(np.linalg.norm(cal.forward) - 1.0) < 1e-6
    assert abs(float(cal.up @ cal.forward)) < 1e-6, "forward is not horizontal"
    assert abs(float(cal.forward @ cal.right)) < 1e-6, "axes are not orthogonal"
    return ("|g| %.3f, %.1f s still, tilt %.1f deg, heading %.1f deg, fit r %.3f"
            % (cal.g_magnitude, cal.still_seconds, cal.tilt_deg,
               cal.heading_offset_deg, cal.fit_r))


def t_imu_signs_are_physical():
    """+lateral must mean a right turn and +longitudinal must mean speeding up.

    Checked against physics rather than against the fit: a reflection in the
    solved frame once gave lateral r = +0.99 with longitudinal r = -0.62, i.e.
    correct cornering and inverted braking.
    """
    run = a_run_with_imu()
    if not run.imu or not run.imu.trusted:
        raise Skip("no trusted IMU calibration in %s" % run.stem)
    g = csv_io.resample(run, 50.0)
    spd = g["speed_gps"].to_numpy(float)
    lat = g["imu_lateral_acc"].to_numpy(float)
    lon = g["imu_longitudinal_acc"].to_numpy(float)
    yaw = np.radians(g["imu_yaw_rate"].to_numpy(float))
    dv = np.gradient(spd) * 50.0 / 9.80665
    m = (spd > 5.0) & np.isfinite(lat) & np.isfinite(lon)
    r_lat = float(np.corrcoef(lat[m], (spd * yaw)[m])[0, 1])
    r_lon = float(np.corrcoef(lon[m], dv[m])[0, 1])
    assert r_lat > 0.5, "lateral does not track v*yaw (r=%.3f)" % r_lat
    assert r_lon > 0.2, "longitudinal does not track dv/dt (r=%.3f)" % r_lon
    assert float(np.linalg.det(np.c_[run.imu.forward[:2], run.imu.right[:2]])) != 0
    return "lateral vs v*yaw r=%+.3f, longitudinal vs dv/dt r=%+.3f" % (r_lat, r_lon)


def t_imu_beats_calc():
    """The reason this module exists: the IMU tracks physics, `calc` does not.

    On autocross the GPS-derived `calc` channels spike far past what the car can
    do, because differentiating a 1 Hz position cannot follow a slalom.
    """
    run = a_run_with_imu()
    if not run.imu or not run.imu.trusted:
        raise Skip("no trusted IMU calibration in %s" % run.stem)
    if not run.has("combined_acc"):
        raise Skip("no calc combined_acc to compare against")
    g = csv_io.resample(run, 50.0)
    spd = g["speed_gps"].to_numpy(float)
    yaw = np.radians(g["imu_yaw_rate"].to_numpy(float))
    dv = np.gradient(spd) * 50.0 / 9.80665
    expect = np.hypot(spd * yaw / 9.80665, dv)
    imu = g["imu_combined_acc"].to_numpy(float)
    calc = g["combined_acc"].to_numpy(float)
    m = (spd > 5.0) & np.isfinite(expect) & np.isfinite(calc) & np.isfinite(imu)
    r_imu = float(np.corrcoef(imu[m], expect[m])[0, 1])
    r_calc = float(np.corrcoef(calc[m], expect[m])[0, 1])
    assert r_imu > r_calc, ("IMU (r=%.3f) should track physics better than calc "
                            "(r=%.3f)" % (r_imu, r_calc))
    assert np.nanmax(imu[m]) < 3.0, \
        "IMU peak %.2f G is not physically plausible" % np.nanmax(imu[m])
    return "IMU r=%.3f vs calc r=%.3f; peaks %.2f G vs %.2f G" % (
        r_imu, r_calc, np.nanmax(imu[m]), np.nanmax(calc[m]))


def t_imu_calibration_is_per_run():
    """Mount and bias move between runs, so one calibration cannot serve all."""
    from racechrono import imu as imu_mod
    cals = [(r.stem, r.imu) for r in _all_runs() if r.imu is not None]
    if len(cals) < 3:
        raise Skip("need at least three IMU sessions to compare calibrations")
    good = [c for _, c in cals if c.trusted]
    assert good, "no run produced a trusted calibration"
    bias = np.array([c.gyro_bias_dps for c in good])
    tilt = np.array([c.tilt_deg for c in good])
    head = np.array([c.heading_offset_deg for c in good])
    # The phone keeps its orientation, so heading should cluster...
    assert np.ptp(head) < 45.0, \
        "solved heading varies by %.0f deg across runs" % np.ptp(head)
    # ... while bias and tilt drift enough that reusing one run's is wrong.
    return ("%d/%d trusted; heading spread %.1f deg, tilt spread %.1f deg, "
            "gyro bias spread %.2f deg/s"
            % (len(good), len(cals), np.ptp(head), np.ptp(tilt),
               float(np.ptp(bias, axis=0).max())))


def t_untrusted_calibration_is_flagged():
    """A standstill that was not still must be caught, not used."""
    from racechrono import imu as imu_mod
    run = a_run_with_imu()
    df = run.df.copy()
    # Scale the accelerometer so the gravity reference is impossible.
    for c in imu_mod.ACC:
        df[run.col(c)] = df[run.col(c)] * 1.4
    bad = csv_io.Run(path=run.path, df=df, units=run.units,
                     devices=run.devices, aliases=run.aliases, meta=run.meta)
    cal = imu_mod.calibrate(bad)
    assert cal is not None
    assert not cal.trusted, "a 1.4 g gravity vector was accepted as trusted"
    assert any("not still" in n or "1.4" in n for n in cal.notes), cal.notes
    return "|g| %.3f rejected: %s" % (cal.g_magnitude, cal.notes[0][:48])


def t_channel_preference_binds():
    """A channel list resolves to the best available, per run."""
    run = a_run_with_imu()
    bound = config_mod.bind(_cfg(), run)
    gm = bound.elements["gmeter"].opts
    assert gm["lateral_channel"] == "imu_lateral_acc", gm["lateral_channel"]
    assert gm["magnitude_channel"] == "imu_combined_acc"

    plain = csv_io.read_run(run.path, derive=False)
    fallback = config_mod.bind(_cfg(), plain).elements["gmeter"].opts
    assert fallback["lateral_channel"] == "lateral_acc", fallback["lateral_channel"]

    # Neither present must fail by name, not silently.
    df = plain.df.drop(columns=[plain.col("lateral_acc")])
    stripped = csv_io.Run(path=plain.path, df=df, units=plain.units,
                          devices=plain.devices, aliases={}, meta=plain.meta)
    try:
        config_mod.bind(_cfg(), stripped)
    except config_mod.ConfigError as exc:
        assert "lateral_channel" in str(exc)
        return "IMU when present, calc when not, clear error when neither"
    raise AssertionError("binding succeeded with no candidate channel present")


# ------------------------------------------------------------ course maps

def _course_map():
    for p in sorted((ROOT / CONFIG["maps_dir"]).glob("*.json")):
        tm = trackmap.load(p)
        if not tm.closed:
            return tm
    raise Skip("no open-course map in %s" % CONFIG["maps_dir"])


def t_course_map_is_open():
    """An autocross course starts and finishes in different places."""
    tm = _course_map()
    pts = tm.unit_points
    span = max(tm.extent_m["x"][1] - tm.extent_m["x"][0],
               tm.extent_m["y"][1] - tm.extent_m["y"][0])
    gap = float(np.hypot(*(pts[-1] - pts[0]))) * span
    assert tm.closed is False
    assert tm.start.get("point") and tm.finish.get("point"), \
        "an open course needs both a start and a finish"
    assert gap > 5.0, ("first and last point are %.1f m apart; an open course "
                       "should not have been closed" % gap)
    assert not np.allclose(pts[0], pts[-1]), "the polyline was closed"
    return "%.0f m course, ends %.1f m apart, %d runs" % (
        tm.length_m, gap, tm.lap_count)


def t_course_spacing_is_uniform():
    """Regression: smoothing must not splice the finish onto the start.

    The Savitzky-Golay pass padded by wrapping, which is right for a lap and
    wrong for an open course -- it mixed finish coordinates into the first points
    and start coordinates into the last, putting a 14 m spike on each end of a
    line whose points are 1.06 m apart, and inflating the reported course length
    by 59 m.
    """
    tm = _course_map()
    pts = tm.unit_points
    span = max(tm.extent_m["x"][1] - tm.extent_m["x"][0],
               tm.extent_m["y"][1] - tm.extent_m["y"][0])
    step = np.linalg.norm(np.diff(pts, axis=0), axis=1) * span
    typical = float(np.median(step))
    worst = float(step.max())
    assert worst < 2.5 * typical, (
        "step of %.2f m against a %.2f m median: the ends are being dragged"
        % (worst, typical))
    return "step median %.2f m, max %.2f m over %d points" % (
        typical, worst, len(pts))


def t_savgol_open_does_not_wrap():
    """The smoothing helper itself, on a ramp: wrapping is visible as a jump."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "genmap", ROOT / "tools" / "generate_map.py")
    genmap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(genmap)

    y = np.linspace(0.0, 100.0, 400)          # a straight ramp, 0 -> 100
    closed = genmap.savgol(y, 9, closed=True)
    openp = genmap.savgol(y, 9, closed=False)
    # Edge padding must leave a ramp alone; wrap padding cannot.
    assert abs(openp[0] - y[0]) < 1.0, \
        "open padding moved the first point by %.2f" % abs(openp[0] - y[0])
    assert abs(openp[-1] - y[-1]) < 1.0
    assert abs(closed[0] - y[0]) > 1.0, \
        "wrap padding should distort the ends of a non-loop; this test is void"
    return "edge padding keeps the ends (%.2f, %.2f); wrapping moves them %.1f" % (
        openp[0], openp[-1], abs(closed[0] - y[0]))


def t_course_map_tracks_the_runs():
    """Every traversal should sit close to the averaged line.

    Of *its own* venue. This check used to accept any run whose nearest map was
    open, which is the same venue-mixing mistake that made the alignment
    comparison compare 89 km against 89 km -- it just had not bitten yet because
    there was only one open course. A second one arrived as a fixture and the
    figure came out at 166375 m.
    """
    tm = _course_map()
    from racechrono import geo
    span = max(tm.extent_m["x"][1] - tm.extent_m["x"][0],
               tm.extent_m["y"][1] - tm.extent_m["y"][0])
    pts = tm.unit_points
    devs = []
    for run in _all_runs():
        lat, lon = run.mean_latlon()
        near, dist = trackmap.auto_select(_cfg().maps_dir, lat, lon)
        if near is None or near.closed or near.name != tm.name:
            continue
        good = run.good_mask()
        spd = run.values("speed_gps")
        mv = good & np.isfinite(spd) & (spd > 1.0)
        idx = np.flatnonzero(mv)
        sl = slice(int(idx[0]), int(idx[-1]) + 1)
        keep = good[sl]
        u, v = tm.project(run.values("latitude")[sl][keep],
                          run.values("longitude")[sl][keep])
        # Distance from the averaged line, in metres, for the on-course part.
        _s, perp = geo.project_to_polyline(pts[:, 0], pts[:, 1], u, v)
        perp_m = perp * span
        devs.append(np.percentile(perp_m, 90))
    if not devs:
        raise Skip("no run matches the open-course map")
    worst = float(max(devs))
    assert worst < 8.0, ("a traversal sits %.1f m from the averaged line at p90"
                         % worst)
    return "%d traversal(s), worst p90 deviation %.2f m" % (len(devs), worst)


def t_circuit_alignment_uses_projection():
    """The closed-circuit map must use projection, not fraction-of-length.

    Laps of the same circuit differ in path length -- 2.86% across nineteen AMP
    laps -- and parameterising each by a fraction of its own length spreads that
    difference around the lap. Both methods anchor at the start line, so the
    error is invisible exactly where it is easiest to look and peaks a quarter of
    the way round: 24.5 m at p95 there, against 3.4 m at the line.
    """
    import importlib.util
    run = a_lapped_run()
    lp = laps_mod.laps(run)
    kept, _dropped = laps_mod.filter_laps(lp, exclude_edge=True)
    if len(kept) < 3:
        raise Skip("need at least three filtered laps to compare alignments")

    from racechrono import geo
    origin = run.mean_latlon(_cfg().quality.max_accuracy_m)
    lat, lon = run.values("latitude"), run.values("longitude")
    good = run.good_mask()
    paths = []
    for l in kept:
        m = np.zeros(lat.shape, bool)
        m[l.i0:l.i1] = True
        m &= good & np.isfinite(lat) & np.isfinite(lon)
        paths.append(geo.to_xy(lat[m], lon[m], *origin))

    spec = importlib.util.spec_from_file_location(
        "genmap3", ROOT / "tools" / "generate_map.py")
    genmap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(genmap)

    N = 720
    frac = np.stack([genmap.resample_by_distance(x, y, N)[0] for x, y in paths])
    frac = genmap._align_phase(frac)
    dev_frac = np.linalg.norm(frac - np.median(frac, axis=0), axis=2)
    ref, stack, _seed = genmap._align_by_projection(paths, N, 3)
    dev_proj = np.linalg.norm(stack - ref, axis=2)

    p95f = float(np.percentile(dev_frac, 95))
    p95p = float(np.percentile(dev_proj, 95))
    assert p95p < p95f, ("projection (%.2f m p95) should beat fraction-of-length "
                         "(%.2f m p95)" % (p95p, p95f))
    # And the shipped default has to be the better one.
    import inspect
    default = inspect.signature(genmap.build).parameters["align"].default
    assert default == "projection", (
        "generate_map.build defaults to align=%r; the circuit map would be built "
        "the worse way" % default)
    return ("%d laps: p95 deviation %.2f m projected vs %.2f m by fraction; "
            "build() defaults to %s" % (len(paths), p95p, p95f, default))


def t_projection_beats_normalised_distance():
    """The alignment claim, measured rather than asserted.

    Within one venue. The first version pooled every run in the folder and used
    the first one's mean position as the origin, which is meaningless the moment
    the folder holds two sites: six sessions from a course 117 km away turned
    both figures into 89 km and the comparison into a coin toss. Geometry from
    different places cannot be averaged, and a test that does it is not measuring
    what it claims.
    """
    from racechrono import geo
    by_map = {}
    for run in _all_runs():
        near, _d = trackmap.auto_select(_cfg().maps_dir, *run.mean_latlon())
        if near is None or near.closed:
            continue
        by_map.setdefault(near.name, []).append(run)
    if not by_map:
        raise Skip("no run sits on an open course with a map")
    runs = max(by_map.values(), key=len)
    paths = []
    for run in runs:
        good = run.good_mask()
        spd = run.values("speed_gps")
        mv = good & np.isfinite(spd) & (spd > 1.0)
        idx = np.flatnonzero(mv)
        if idx.size < 64:
            continue
        sl = slice(int(idx[0]), int(idx[-1]) + 1)
        keep = good[sl]
        x, y = geo.to_xy(run.values("latitude")[sl][keep],
                         run.values("longitude")[sl][keep], *runs[0].mean_latlon())
        paths.append((x, y))
    if len(paths) < 3:
        raise Skip("need at least three traversals at one venue to compare "
                   "alignments")

    N = 200
    # by fraction of each path's own length
    frac = []
    for x, y in paths:
        d = geo.cumulative_distance(x, y)
        u = np.linspace(0, d[-1], N)
        frac.append(np.c_[np.interp(u, d, x), np.interp(u, d, y)])
    F = np.stack(frac)
    dev_frac = np.linalg.norm(F - np.median(F, axis=0), axis=2)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "genmap2", ROOT / "tools" / "generate_map.py")
    genmap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(genmap)
    ref, stack, _seed = genmap._align_by_projection(paths, N, 3)
    dev_proj = np.linalg.norm(stack - ref, axis=2)

    p50f, p50p = np.percentile(dev_frac, 50), np.percentile(dev_proj, 50)
    assert p50p < p50f, ("projection (%.2f m) should beat normalised distance "
                         "(%.2f m)" % (p50p, p50f))
    return ("%d traversals at one venue: p50 deviation %.2f m projected vs "
            "%.2f m by fraction of length" % (len(paths), p50p, p50f))


# ------------------------------------------------------------ session model

def _describe(run=None):
    from webui import session as sv
    run = run if run is not None else a_lapped_run()
    cfg = config_mod.bind(_cfg(), run)
    tm, _d = job_mod._select_map(cfg, run)
    return run, cfg, sv.describe(run, cfg, tm, ())


def t_session_model_is_json_safe():
    """The whole model has to survive `json.dumps` with no `default=` handler.

    Two things in it are not JSON types, and both were caught this way: the config
    carries `Path` objects, and `sync_confidence` is `inf` for a manually aligned
    clip. `json.dumps` accepts `inf` silently and emits `Infinity`, which is not
    JSON and which `JSON.parse` rejects -- so it is converted to null at the
    source rather than left for the browser to trip over.
    """
    run, _c, d = _describe()
    blob = json.dumps(d, allow_nan=False)
    assert "NaN" not in blob and "Infinity" not in blob
    for key in ("stem", "duration", "rows", "laps", "channels", "map",
                "track", "clips", "driving", "default_channel"):
        assert key in d, "the model is missing %r" % key
    return "%.0f KB of JSON, %d channels, %d laps" % (
        len(blob) / 1024, len(d["channels"]), len(d["laps"]))


def t_session_times_are_run_relative():
    """Every time in the model is seconds from the run's first sample.

    Unix timestamps here are around 1.79e9; a lap boundary expressed that way
    loses precision in a float32 and reads as noise in a slider. The page rests on
    one clock, so a stray absolute timestamp anywhere in the model would show up
    as a playhead pinned to the far end of the timeline.
    """
    run, _c, d = _describe()
    bad = []
    for lap in d["laps"]:
        for k in ("t0", "t1"):
            if not (-1.0 <= lap[k] <= d["duration"] + 1.0):
                bad.append(("lap%d.%s" % (lap["number"], k), lap[k]))
    for k in ("t0", "t1"):
        if not (-1.0 <= d["driving"][k] <= d["duration"] + 1.0):
            bad.append(("driving." + k, d["driving"][k]))
    if d["track"] and d["track"]["t"]:
        ts = d["track"]["t"]
        if not (-1.0 <= ts[0] and ts[-1] <= d["duration"] + 1.0):
            bad.append(("track", (ts[0], ts[-1])))
    assert not bad, "absolute or out-of-range times: %s" % bad
    assert d["t0_unix"] > 1e9, "the unix origin should still be reported once"
    return "laps, driving window and %d track points all within 0..%.1f s" % (
        len(d["track"]["t"]) if d["track"] else 0, d["duration"])


def t_session_channels_are_real():
    """Every listed channel must be loadable and must actually vary.

    A picker offering `altitude` when it is a constant 0, or `fix_type` when it is
    a constant 1, wastes a click and draws a flat line. A name the loader cannot
    resolve would 500 the trace endpoint instead.
    """
    from webui import session as sv
    run, _c, d = _describe()
    for ch in d["channels"]:
        assert run.has(ch["name"]), "%s is listed but will not load" % ch["name"]
        assert ch["max"] > ch["min"], "%s never changes" % ch["name"]
    listed = {c["name"] for c in d["channels"]}
    assert not (listed & set(sv.HIDE)), "a bookkeeping channel is offered as a trace"
    assert d["default_channel"] in listed
    hidden = [c for c in sv.HIDE if run.has(c)]
    return "%d channel(s), %d hidden, default %s" % (
        len(d["channels"]), len(hidden), d["default_channel"])


def t_trace_keeps_the_peaks():
    """Decimation must not flatten a spiky channel.

    Plain subsampling is the obvious way to fit 20000 samples into an 800 px trace
    and it silently loses the peaks, which on a brake or throttle channel is the
    entire signal. The min/max envelope has to reach the same extremes as the raw
    data does.
    """
    from webui import session as sv
    run, _c, d = _describe()
    ch = d["default_channel"]
    raw = run.values(ch)
    raw = raw[np.isfinite(raw)]
    tr = sv.trace(run, ch, 600)
    assert tr["n"] <= 600, "asked for 600 points, got %d" % tr["n"]
    got = np.asarray(tr["v"])
    span = float(raw.max() - raw.min()) or 1.0
    lost_hi = float(raw.max() - got.max()) / span
    lost_lo = float(got.min() - raw.min()) / span
    assert lost_hi < 0.02 and lost_lo < 0.02, (
        "decimation lost %.1f%% of the top and %.1f%% of the bottom"
        % (100 * lost_hi, 100 * lost_lo))
    step = max(1, raw.size // 600)
    naive = float(raw.max() - raw[::step].max()) / span
    return ("%d -> %d points, peak kept to %.2f%%; plain subsampling would lose "
            "%.2f%%" % (raw.size, tr["n"], 100 * lost_hi, 100 * naive))


def t_track_uses_the_map_transform():
    """The driven line must come from the map's own stored transform.

    Projecting with a transform fitted to this run would make the marker drift
    against the drawn map whenever a run went wider or shorter than the laps the
    map was built from -- the exact failure the map file format exists to stop.
    """
    run, cfg, d = _describe()
    if not d["track"] or not d["track"]["t"]:
        raise Skip("no positions survived the accuracy gate")
    tm, _dist = job_mod._select_map(cfg, run)
    lat, lon = run.values("latitude"), run.values("longitude")
    ts_all = run.values("timestamp") - run.t0
    ts = np.asarray(d["track"]["t"])
    # Sample the *payload* and look each entry back up in the source, rather than
    # the other way round: the track is decimated, so sampling the source finds
    # points that were dropped and there is nothing to compare them against.
    take = np.linspace(0, ts.size - 1, 12).astype(int)
    worst, compared = 0.0, 0
    for j in take:
        i = int(np.argmin(np.abs(ts_all - ts[j])))
        if abs(ts_all[i] - ts[j]) > 1e-6:
            continue
        u, v = tm.project(lat[i:i + 1], lon[i:i + 1])
        compared += 1
        worst = max(worst, abs(d["track"]["u"][j] - float(u[0])),
                    abs(d["track"]["v"][j] - float(v[0])))
    if not compared:
        raise Skip("no track point could be matched back to a source row")
    # The payload rounds to five decimals, so that is the whole tolerance.
    assert worst <= 1e-5, ("the model's track is %.2e unit off the map's own "
                           "projection" % worst)
    return "%d points, %d compared exactly, worst disagreement %.1e unit" % (
        len(ts), compared, worst)


def t_lap_flags_agree_with_the_filter():
    """`best` and `representative` must mean what the rest of the code means.

    The page marks one lap green and greys the edge laps out. If those flags were
    computed differently from `laps.filter_laps`, the map would be averaged from
    one set of laps while the list claimed another.
    """
    run, _c, d = _describe()
    got = laps_mod.laps(run)
    if len(got) < 3:
        raise Skip("need at least three laps for edge filtering to bite")
    kept, _dropped = laps_mod.filter_laps(got, exclude_edge=True)
    keep = {(l.number, round(l.t0, 3)) for l in kept}
    n_best = sum(1 for l in d["laps"] if l["best"])
    assert n_best == 1, "%d laps flagged best" % n_best
    best = next(l for l in d["laps"] if l["best"])
    assert abs(best["duration"] - min(l.duration for l in got)) < 1e-6
    for row, lap in zip(d["laps"], got):
        want = (lap.number, round(lap.t0, 3)) in keep
        assert row["representative"] == want, (
            "lap %d: model says representative=%s, filter_laps says %s"
            % (row["number"], row["representative"], want))
    return "%d laps, best %s, %d representative" % (
        len(d["laps"]), best["label"],
        sum(1 for l in d["laps"] if l["representative"]))


def t_session_summary_matches_the_full_model():
    """The cheap list entry must not disagree with the full load.

    The list is served from a cache built with `derive=False`, a different load
    path from the one the session view uses. If the two disagreed, a session would
    change its lap count or its duration the moment someone opened it.
    """
    from webui import session as sv
    run, cfg, d = _describe()
    cheap = csv_io.read_run(run.path, derive=False, validate_units=False)
    s = sv.summarise(cheap, cfg)
    assert s["stem"] == d["stem"]
    assert abs(s["duration"] - d["duration"]) < 1e-6, (
        "summary says %.3f s, model says %.3f s" % (s["duration"], d["duration"]))
    assert s["laps"] == len(d["laps"]), (
        "summary counts %d laps, model lists %d" % (s["laps"], len(d["laps"])))
    if d["laps"]:
        assert abs(s["best_lap"] - min(l["duration"] for l in d["laps"])) < 1e-6
    return "duration, lap count and best lap agree across both load paths"


def t_overlay_payload_is_self_describing():
    """The packed overlay must be decodable from its own header alone.

    The page reads a region count, that many boxes, then the pixels. If the
    header and the payload could disagree, the browser would decode garbage and
    it would look like a rendering bug rather than a protocol one.
    """
    import struct
    from racechrono import ingest as ingest_mod, library as lib_mod
    from webui import server as srv_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as store:
        plan = ingest_mod.plan(_cfg(), store, source=SOURCE,
                               options=ingest_mod.Options(keep_best=1),
                               verbose=False)
        made = ingest_mod.apply(plan, store, _cfg(), encode=False, verbose=False)
        if not made["laps"]:
            raise Skip("nothing importable here")
        cfg = config_mod.load(copy.deepcopy(CONFIG))
        cfg.library_dir = Path(tmp) / "lib"
        lib = srv_mod.Library(cfg)
        lap_id = made["laps"][0]
        body = lib.overlay_frame(lap_id, 1.0)
        lay = lib.layout(lap_id)
        lib.close()

        n = struct.unpack_from("<i", body)[0]
        assert 1 <= n <= 8, "%d regions is not plausible" % n
        assert n == len(lay["regions"]), (
            "header says %d regions, layout says %d" % (n, len(lay["regions"])))
        want = 4 + n * 16
        for i in range(n):
            x, y, w, h = struct.unpack_from("<4i", body, 4 + i * 16)
            r = lay["regions"][i]
            assert (x, y, w, h) == (r["x"], r["y"], r["w"], r["h"]), \
                "region %d disagrees with the layout" % i
            assert x >= 0 and y >= 0 and x + w <= lay["frame"][0] \
                and y + h <= lay["frame"][1], "region %d leaves the frame" % i
            want += w * h * 4
        assert len(body) == want, (
            "payload is %d B, its own header describes %d B" % (len(body), want))
        return "%d region(s), %d B, matches the layout exactly" % (n, len(body))


def _comparison(n_sectors=8, **kw):
    from racechrono import compare as cmp_mod
    run = a_lapped_run()
    cfg = config_mod.bind(_cfg(), run)
    tm, _d = job_mod._select_map(cfg, run)
    if tm is None:
        raise Skip("no map covers %s" % run.stem)
    return run, tm, cmp_mod.compare([run], tm, n_sectors=n_sectors, **kw)


def t_compare_uses_centreline_distance():
    """The shared coordinate is distance along the map, not each lap's own.

    `distance_traveled` is the distance *that car* covered, so a lap that ran
    wide accumulates more of it: across nineteen AMP laps the path length spans
    2858.6 to 2942.3 m, 2.86%. Parameterising by it would slide two laps out of
    step by exactly the line difference, and the resulting delta trace would be a
    picture of the parameterisation rather than of the driving.
    """
    from racechrono import compare as cmp_mod, geo
    run, tm, c = _comparison()
    tvs = cmp_mod.traversals([run], tm)
    if len(tvs) < 2:
        raise Skip("need two traversals to compare")
    line = cmp_mod._line_length(tm)
    # Each traversal's own path length, for contrast with the centreline length.
    own = []
    for tv in tvs:
        lat, lon = run.values("latitude"), run.values("longitude")
        own.append(float(tv.s[-1] - tv.s[0]))
    spread = max(own) - min(own)
    assert abs(np.median(own) - line) < 0.02 * line, (
        "traversals span %.1f m of a %.1f m line" % (np.median(own), line))
    # Every traversal must land on the *same* coordinate, within a few metres.
    assert spread < 0.01 * line, (
        "traversals disagree about the line length by %.1f m" % spread)
    return "%d traversals, %.1f m line, they agree on it within %.1f m" % (
        len(tvs), line, spread)


def t_compare_distance_is_monotonic():
    """Distance must never go backwards along a traversal.

    Two things make it try. On a closed circuit the polyline's ends are the same
    place, so a sample a metre before the line projects to the full lap length
    and the next one to zero -- a 2929 m backwards step mid-lap if left alone.
    And standing still puts a dozen samples within a metre of each other in
    whatever order the noise decides. `np.interp` needs an increasing x, so
    either one silently corrupts every trace built from it.
    """
    from racechrono import compare as cmp_mod
    run, tm, _c = _comparison()
    tvs = cmp_mod.traversals([run], tm)
    if not tvs:
        raise Skip("nothing comparable in %s" % run.stem)
    bad = [t.key for t in tvs if np.any(np.diff(t.s) <= 0)]
    assert not bad, "distance goes backwards in %s" % bad
    steps = np.concatenate([np.diff(t.s) for t in tvs])
    return "%d traversals, %d steps, all forward (median %.2f m, max %.2f m)" % (
        len(tvs), steps.size, float(np.median(steps)), float(steps.max()))


def t_compare_delta_matches_the_times():
    """The delta trace must be recomputable from the two time traces.

    A delta is the one number in this view that nobody can sanity-check by eye,
    so it has to agree exactly with the elapsed times it claims to be the
    difference of.
    """
    _run, _tm, c = _comparison()
    if len(c["traversals"]) < 2:
        raise Skip("need two traversals to compare")
    ref = next(r for r in c["traversals"] if r["is_ref"])
    assert max(abs(v) for v in ref["delta"]) == 0.0, \
        "the reference does not have zero delta"
    worst = 0.0
    for r in c["traversals"]:
        d = np.asarray(r["delta"])
        want = np.asarray(r["t"]) - np.asarray(ref["t"])
        worst = max(worst, float(np.abs(d - want).max()))
    # `delta` and `t` are each rounded to three decimals independently, so the
    # difference of two rounded values can sit 0.001 either side of the rounded
    # difference. Two roundings, two millimetres of slack.
    assert worst <= 2e-3, "delta is %.4f s from the time difference" % worst
    return "%d traversals, worst disagreement %.4f s" % (
        len(c["traversals"]), worst)


def t_compare_sectors_add_up():
    """Sector times must sum to the traversal's time over the compared span.

    And the ideal lap must be the sum of the column minima and no slower than
    any real traversal -- an ideal that beat arithmetic would be a bug that
    flatters the driver.
    """
    _run, _tm, c = _comparison(n_sectors=6)
    sec = c["sectors"]
    if not sec["rows"]:
        raise Skip("nothing comparable")
    by = {r["key"]: r for r in c["traversals"]}
    worst = 0.0
    for row in sec["rows"]:
        r = by[row["key"]]
        span = float(r["t"][-1] - r["t"][0])
        worst = max(worst, abs(sum(row["times"]) - span))
    assert worst <= 0.01, "sector times are %.4f s off the traversal time" % worst

    mins = [min(r["times"][j] for r in sec["rows"])
            for j in range(len(sec["best"]))]
    assert sec["best"] == [round(m, 3) for m in mins], \
        "the best-sector row is not the column minima"
    fastest = min(r["total"] for r in sec["rows"])
    assert sec["ideal"]["total"] <= fastest + 1e-9, (
        "the ideal lap (%.3f) is slower than a real one (%.3f)"
        % (sec["ideal"]["total"], fastest))
    return ("%d sectors, sums agree to %.4f s, ideal %.3f vs fastest %.3f from "
            "%d traversal(s)" % (len(sec["best"]), worst, sec["ideal"]["total"],
                                 fastest, sec["ideal"]["from_traversals"]))


def t_compare_grid_starts_at_zero():
    """The reported distance is metres into the compared span, not raw
    centreline distance.

    Lap slicing puts a lap's first row on the sample *before* the crossing, so
    raw centreline distance starts a few metres negative -- true, and unreadable
    as "-4 m into the lap". `offset_m` keeps the absolute coordinate, which is
    the one that is stable across every session at the venue.
    """
    _run, _tm, c = _comparison()
    if not c["grid"]:
        raise Skip("nothing comparable")
    g = np.asarray(c["grid"])
    assert abs(g[0]) < 1e-9, "the grid starts at %.3f m" % g[0]
    assert np.all(np.diff(g) > 0), "the grid does not increase"
    # `span_m` is rounded to one decimal and `offset_m` to two, so they agree to
    # within half of the coarser rounding rather than exactly.
    assert abs(c["span_m"][0] - c["offset_m"]) <= 0.05, \
        "offset_m (%.2f) does not match the span it came from (%.1f)" % (
            c["offset_m"], c["span_m"][0])
    edges = c["sectors"]["edges"]
    assert abs(edges[0]) < 1e-6 and abs(edges[-1] - g[-1]) < 0.5, \
        "sector edges do not span the grid"
    return "grid 0 .. %.1f m, absolute offset %.2f m, %d sector edges" % (
        g[-1], c["offset_m"], len(edges))


def t_compare_refuses_mixed_venues():
    """Two laps at different tracks have no shared centreline.

    Projecting an autocross run onto a circuit 180 km away produces numbers
    rather than an error, which is the dangerous kind of wrong.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    from webui import server as srv_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as store:
        plan = ingest_mod.plan(_cfg(), store, source=SOURCE, verbose=False)
        made = ingest_mod.apply(plan, store, _cfg(), encode=False, verbose=False)
        by_track = {}
        for lid in made["laps"]:
            by_track.setdefault(store.track_of_lap(lid), []).append(lid)
        if len(by_track) < 2:
            raise Skip("this dataset only has one track")
        cfg = config_mod.load(copy.deepcopy(CONFIG))
        cfg.library_dir = Path(tmp) / "lib"
        lib = srv_mod.Library(cfg)
        (a, laps_a), (b, laps_b) = list(by_track.items())[:2]
        try:
            lib.compare([laps_a[0], laps_b[0]])
        except ValueError as exc:
            assert "different tracks" in str(exc), "wrong error: %s" % exc
        else:
            raise AssertionError("a cross-track comparison was accepted")
        # And the same two laps at one track must be fine.
        if len(laps_a) >= 2:
            got = lib.compare(laps_a[:2])
            assert len(got["traversals"]) == 2, got
        lib.close()
        return "%d track(s) in the library; mixing them is refused" % len(by_track)


def t_compare_handles_open_courses():
    """An autocross run has no laps, so the run itself is the traversal.

    The comparison has to work on a course as well as a circuit, and the two
    differ in ways that matter: the polyline is not closed, so there is no wrap
    to undo, and the runs start and stop in different places, so the overlap is
    genuinely shorter than the course.
    """
    from racechrono import compare as cmp_mod
    # Grouped by map name, not merely by "the map is open". With two open courses
    # in the folder the ungrouped version handed four runs from two venues to one
    # centreline and got two traversals back.
    by_map = {}
    for run in _all_runs():
        near, _d = trackmap.auto_select(_cfg().maps_dir, *run.mean_latlon())
        if near is not None and not near.closed:
            by_map.setdefault(near.name, [near, []])[1].append(run)
    usable = [(tm, rs) for tm, rs in by_map.values() if len(rs) >= 2]
    if not usable:
        raise Skip("no open course here has two runs")
    tm, runs = max(usable, key=lambda pair: len(pair[1]))
    c = cmp_mod.compare(runs, tm, n_sectors=4)
    assert c["closed"] is False
    # A session can hold more than one run, so the count is runs, not sessions.
    from racechrono import runs as runs_mod
    want = sum(len(runs_mod.detect(r)) for r in runs)
    assert len(c["traversals"]) == want, (
        "%d session(s) holding %d run(s) gave %d traversal(s)"
        % (len(runs), want, len(c["traversals"])))
    assert all(r["label"].startswith("run") for r in c["traversals"]), \
        "an open course should not produce lap-labelled traversals"
    assert c["length_m"] > 0.5 * cmp_mod._line_length(tm), (
        "the overlap is only %.0f m of a %.0f m course"
        % (c["length_m"], cmp_mod._line_length(tm)))
    return ("%s: %d session(s), %d run(s), %.0f m overlap of a %.0f m course, "
            "%.1f m outside it" % (tm.name, len(runs), want, c["length_m"],
                                   cmp_mod._line_length(tm), c["uncompared_m"]))


def t_compare_reports_gps_gaps():
    """A stretch with no fixes must be declared, not silently interpolated.

    At AMP fourteen of twenty-seven laps have a single 250-300 m hole, and every
    one is in the same place -- roughly 360 to 690 m along the line, where
    `accuracy` leaves its 10.0 m floor and reaches 49 m. It is a GPS shadow, not
    a random dropout: it recurs across all four sessions, for 7 to 19 s each
    time. Interpolating is unavoidable; a delta trace that draws 9% of a lap as
    confidently as the measured part is not.
    """
    from racechrono import compare as cmp_mod
    _run, _tm, c = _comparison()
    if not c["traversals"]:
        raise Skip("nothing comparable")
    holed = 0
    for r in c["traversals"]:
        covered = sum(b - a for a, b, _dt in r["gaps"])
        assert abs(covered - r["gap_m"]) < 0.5, (
            "%s: gaps cover %.1f m but gap_m says %.1f"
            % (r["key"], covered, r["gap_m"]))
        assert 0.0 <= r["measured_frac"] <= 1.0
        for a, b, dt in r["gaps"]:
            assert b > a, "%s has a gap of %.1f m" % (r["key"], b - a)
            assert -1.0 <= a and b <= c["length_m"] + 1.0, (
                "%s: a gap at %.1f-%.1f m is outside the 0..%.1f grid"
                % (r["key"], a, b, c["length_m"]))
            assert dt > 0, "a gap with no elapsed time in it"
        if r["gap_m"] > 20:
            holed += 1
            assert r["measured_frac"] < 1.0, (
                "%s has a %.0f m gap but claims to be fully measured"
                % (r["key"], r["gap_m"]))

    # And a reported loss must say how much of it was interpolation, or someone
    # will act on a stretch the GPS never saw.
    slow = [r for r in c["traversals"] if not r["is_ref"]]
    if slow:
        row = max(slow, key=lambda r: r["final_delta"])
        for g in cmp_mod.losses(c, row["key"], n=5):
            assert 0.0 <= g["interpolated"] <= 1.0
            held = sum(max(0.0, min(g["to_m"], b) - max(g["from_m"], a))
                       for a, b, _dt in row["gaps"])
            want = held / max(g["to_m"] - g["from_m"], 1e-9)
            assert abs(want - g["interpolated"]) < 0.02, (
                "a stretch claims %.2f interpolated, the gaps say %.2f"
                % (g["interpolated"], want))
    return "%d of %d traversals have a gap over 20 m; worst %.0f m (%.0f%% measured)" % (
        holed, len(c["traversals"]),
        max(r["gap_m"] for r in c["traversals"]),
        100 * min(r["measured_frac"] for r in c["traversals"]))


def t_compare_losses_localise_the_time():
    """`losses` must account for the time the delta trace says was lost.

    A delta read point by point says "1.2 s down at 1400 m", which is true and
    useless. The actionable form is which stretch the time went in, so the worst
    stretches have to be a real decomposition of the total rather than a
    plausible-looking list.
    """
    from racechrono import compare as cmp_mod
    _run, _tm, c = _comparison()
    slow = [r for r in c["traversals"] if not r["is_ref"]]
    if not slow:
        raise Skip("only one traversal")
    row = max(slow, key=lambda r: r["final_delta"])
    got = cmp_mod.losses(c, row["key"], n=5, window_m=100.0)
    assert got, "no stretches reported for a lap that lost %.2f s" % row["final_delta"]
    assert all(g["lost_s"] >= got[-1]["lost_s"] - 1e-9 for g in got), \
        "the stretches are not in order of time lost"
    assert all(g["to_m"] > g["from_m"] for g in got)

    # Each reported figure has to be the rise in the delta across that stretch.
    grid = np.asarray(c["grid"])
    d = np.asarray(row["delta"])
    worst = 0.0
    for g in got:
        a = int(np.argmin(np.abs(grid - g["from_m"])))
        b = int(np.argmin(np.abs(grid - g["to_m"])))
        worst = max(worst, abs((d[b] - d[a]) - g["lost_s"]))
    assert worst <= 2e-3, "a stretch is %.4f s off the delta it spans" % worst

    # The budget is the delta's total *rise*, not its final value: a lap can lose
    # 4.40 s in five places and win 0.75 s of it back elsewhere, finishing 3.65 s
    # down. Comparing the stretches against the final delta calls correct
    # arithmetic a bug, which is what the first version of this check did.
    rise = float(np.diff(d).clip(min=0).sum())
    total = sum(g["lost_s"] for g in got)
    assert total <= rise + 1e-6, (
        "five stretches account for %.2f s but the delta only rises %.2f s in "
        "total" % (total, rise))
    return ("%s finished %.2f s down, having lost %.2f s and won %.2f s back; "
            "worst 5 stretches hold %.2f s of it, biggest %.0f-%.0f m"
            % (row["key"].split("#")[-1], row["final_delta"], rise,
               rise - row["final_delta"], total,
               got[0]["from_m"], got[0]["to_m"]))


# ------------------------------------------------------------ dataset shapes

def _shapes():
    from racechrono import shapes as shapes_mod, video
    cfg = _cfg()
    clips = video.discover(SOURCE, cfg.ffmpeg.ffprobe, recursive=True)
    out = []
    for run in _all_runs():
        mine = [c for c in clips if c.start_unix
                and run.t0 - 300 <= c.start_unix <= run.t1 + 300]
        out.append((run, shapes_mod.classify(run, cfg, mine)))
    return out


def t_every_session_classifies():
    """Every session must land in a stated shape, and the shape must be
    self-consistent.

    This exists because three datasets in a row broke something by arriving in a
    shape nothing had declared. `shapes.classify` is where the assumptions now
    live, so it has to hold for everything in the folder rather than for whatever
    the code happened to be written against.
    """
    from racechrono import shapes as shapes_mod
    got = _shapes()
    assert got, "no sessions to classify"
    for run, sh in got:
        assert sh.stem == run.stem
        assert sh.n_runs >= 0 and sh.n_laps >= 0
        if sh.venue is None:
            assert sh.course is None, "a course kind without a map"
            assert not sh.laps_usable or sh.n_laps >= 2, (
                "%s: laps called usable with no map and %d lap(s)"
                % (sh.stem, sh.n_laps))
        else:
            assert sh.course in ("circuit", "course")
            if sh.course == "course":
                assert not sh.laps_usable, (
                    "%s sits on an open course but its laps are called usable"
                    % sh.stem)
        assert sh.duration_s > 0 and sh.sample_hz > 0
        assert isinstance(sh.cell, tuple) and len(sh.cell) == 5
    cells = shapes_mod.matrix([s for _r, s in got])
    return "%d session(s) in %d shape(s): %s" % (
        len(got), len(cells),
        "; ".join(" / ".join(c) for c in sorted(cells))[:150])


def t_laps_are_corroborated():
    """A lap count nothing corroborates must not make a session lap-shaped.

    `laps.laps()` reads `lap_number`, which RaceChrono increments on its own
    terms: on SCCA session 121241 -- an autocross course with no start/finish
    trap -- it reports exactly one lap. `compare.traversals` branched on
    `if laps:` and would have treated that session as a circuit, producing one
    lap-shaped traversal of nothing in particular. Nothing would have said so.
    """
    from racechrono import compare as cmp_mod
    got = _shapes()
    doubtful = [(r, s) for r, s in got if s.n_laps >= 1 and not s.laps_usable]
    if not doubtful:
        raise Skip("no session here reports laps that nothing corroborates")
    checked = 0
    for run, sh in doubtful:
        tm, _d = job_mod._select_map(config_mod.bind(_cfg(), run), run)
        if tm is None:
            continue
        tvs = cmp_mod.traversals([run], tm)
        labels = {t.label.split()[0] for t in tvs}
        assert labels <= {"run"}, (
            "%s reports %d uncorroborated lap(s) and compare made %s of it"
            % (sh.stem, sh.n_laps, sorted(labels)))
        assert len(tvs) == sh.n_runs, (
            "%s has %d detected run(s) but %d traversal(s)"
            % (sh.stem, sh.n_runs, len(tvs)))
        checked += 1
    if not checked:
        raise Skip("the doubtful sessions have no map to compare against")
    return ("%d session(s) report an uncorroborated lap; all were treated as "
            "runs, not laps" % checked)


def t_capabilities_match_reality():
    """A capability declared applicable must work; one declared blocked must
    refuse rather than produce something.

    The declaration is only worth having if it is checked against what the code
    does. Otherwise it is a second place for the same assumption to be wrong.
    """
    from racechrono import compare as cmp_mod, shapes as shapes_mod
    ran, refused = 0, 0
    for run, sh in _shapes():
        cfg = config_mod.bind(_cfg(), run)
        caps = shapes_mod.applicable(sh)
        tm, _d = job_mod._select_map(cfg, run)

        # compare: applicable exactly when a map covers the venue.
        if caps["compare"] is None:
            assert tm is not None, "%s: compare declared usable with no map" % sh.stem
            c = cmp_mod.compare([run], tm, n_sectors=4)
            assert c.get("traversals"), (
                "%s: compare is declared usable but produced nothing" % sh.stem)
            ran += 1
        else:
            assert tm is None, ("%s: compare declared blocked (%s) but a map "
                               "covers it" % (sh.stem, caps["compare"]))
            refused += 1

        # lap_export: applicable needs usable laps *and* footage.
        if caps["lap_export"] is None:
            assert sh.laps_usable and sh.n_clips, sh.stem
            assert len(window.lap_targets(run, cfg)) >= 1, (
                "%s: lap export is declared usable but yielded no targets"
                % sh.stem)
            ran += 1
        else:
            if not sh.laps_usable:
                assert not laps_mod.filter_laps(
                    laps_mod.laps(run), exclude_edge=True)[0] or sh.n_laps >= 2, \
                    "%s: laps called unusable but filter_laps kept some" % sh.stem
            refused += 1

        # run_segmentation: always applicable once the car moved.
        if caps["run_segmentation"] is None:
            assert sh.n_runs >= 1, sh.stem
            ran += 1

        # map_circuit and map_course are mutually exclusive by construction.
        assert (caps["map_circuit"] is None) != (caps["map_course"] is None), (
            "%s: %s is neither or both a circuit and a course session"
            % (sh.stem, sh.label()))
    return "%d capability run(s) exercised, %d refusal(s) confirmed" % (ran, refused)


def t_run_detection_needs_no_tuned_constant():
    """Run detection must not depend on an absolute course size.

    The rule is "a run is a movement segment comparable in length to the longest
    one in the session", which is self-referential on purpose: a metres threshold
    fitted to these courses is exactly the kind of thing that breaks on the next
    dataset. Scaling a session's geometry must therefore not change how many runs
    it has.
    """
    from racechrono import runs as runs_mod
    import copy as _copy
    subjects = [r for r in _all_runs()][:6]
    if not subjects:
        raise Skip("no sessions")
    lines = []
    for run in subjects:
        base = len(runs_mod.detect(run))
        for factor in (0.5, 2.0, 5.0):
            stretched = _copy.copy(run)
            df = run.df.copy()
            # Scale the path about its own centre. Speed scales with it, so the
            # movement gate sees the same shape of drive at a different size.
            for col, mid in (("latitude", None), ("longitude", None)):
                c = run.col(col)
                m = float(np.nanmean(df[c]))
                df[c] = m + (df[c] - m) * factor
            df[run.col("speed_gps")] = df[run.col("speed_gps")] * factor
            stretched.df = df
            got = len(runs_mod.detect(stretched))
            assert got == base, (
                "%s: %d run(s) at 1x but %d at %.1fx -- detection depends on "
                "absolute size" % (run.stem, base, got, factor))
        lines.append("%s %d" % (run.stem[-8:], base))
    return "%d session(s) give the same run count at 0.5x, 2x and 5x scale" % len(subjects)


def t_walking_is_not_a_run():
    """A walked course must not enter a map average, and a slow run must.

    Length cannot separate them: walking the course covers exactly the right
    distance. Time separates them by a wide margin. Measured on the real runs --
    a 1341 m course walked at 1.4 m/s takes 958 s against a 103 s median, 9.3x,
    while the slowest real run was 1.10x its session's median. So the movement
    gate does the work and the time filter is the second line, set loose enough
    that a slow run is never the thing it catches.
    """
    from racechrono import runs as runs_mod
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "genmap4", ROOT / "tools" / "generate_map.py")
    genmap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(genmap)

    assert runs_mod.MOVE_MS > 1.6, (
        "the movement gate is %.1f m/s, which a walking pace of about 1.4 m/s "
        "would clear" % runs_mod.MOVE_MS)
    factor = genmap.build_course.__defaults__[
        genmap.build_course.__code__.co_varnames.index("max_time_factor")
        - (genmap.build_course.__code__.co_argcount
           - len(genmap.build_course.__defaults__))]
    assert factor >= 2.0, ("max_time_factor is %.2f; a slow run would be "
                           "dropped as a walk" % factor)

    # And check the margin against this dataset rather than asserting it.
    worst_real, walked = 0.0, []
    for run in _all_runs():
        spans = runs_mod.detect(run)
        if len(spans) < 1:
            continue
        durs = np.array([s.duration for s in spans])
        med = float(np.median(durs))
        worst_real = max(worst_real, float(durs.max()) / med)
        dist = float(np.median([s.distance_m for s in spans]))
        walked.append((dist / 1.4) / med)
    if not walked:
        raise Skip("no runs to measure a margin against")
    slowest_walk = min(walked)
    assert worst_real < factor < slowest_walk, (
        "the time filter at %.2fx sits outside the gap between the slowest real "
        "run (%.2fx) and the quickest possible walk (%.2fx)"
        % (factor, worst_real, slowest_walk))
    return ("gate %.1f m/s; time filter %.1fx sits between the slowest real run "
            "at %.2fx and a walked course at %.1fx"
            % (runs_mod.MOVE_MS, factor, worst_real, slowest_walk))


def t_matcher_is_forward_only():
    """Distance along the line must never reverse, wherever a run starts.

    The regression this pins: on the SCCA course, staging sits beside the
    *finish*, 131 m from the start. Matching each sample to its nearest point on
    the line independently and then forcing the result monotonic with a running
    maximum put the first sample of a run at 1097 m of a 1107 m line -- and every
    later sample was then pinned there too. Four of six traversals were lost
    outright and the surviving overlap was 5.6 m of the course.
    """
    from racechrono import compare as cmp_mod
    worst_start, checked, names = 0.0, 0, []
    for run in _all_runs():
        cfg = config_mod.bind(_cfg(), run)
        tm, _d = job_mod._select_map(cfg, run)
        if tm is None:
            continue
        line = cmp_mod._line_length(tm)
        for tv in cmp_mod.traversals([run], tm):
            assert np.all(np.diff(tv.s) > 0), \
                "%s: distance reverses along the line" % tv.key
            span = tv.coverage[1] - tv.coverage[0]
            assert span > 0.5 * line, (
                "%s covers %.0f m of a %.0f m line -- the match collapsed"
                % (tv.key, span, line))
            worst_start = max(worst_start, tv.coverage[0] / line)
            checked += 1
            names.append(tv.key)
    if not checked:
        raise Skip("no traversal has a map to match against")
    assert worst_start < 0.25, (
        "a traversal starts %.0f%% of the way along the line, which is the "
        "collapse this checks for" % (100 * worst_start))
    return ("%d traversal(s), all forward, none starting past %.1f%% of its line"
            % (checked, 100 * worst_start))


def t_fixture_shapes_are_declared():
    """Every shape in the fixture set must be one some capability handles.

    And the working folder's shapes are reported against the fixtures, so a
    dataset arriving in a shape the suite has never seen is visible rather than
    discovered later. That is reported, not asserted: the fixture set is
    deliberately small, and the point is to know what it does not cover.
    """
    from racechrono import shapes as shapes_mod
    got = [s for _r, s in _shapes()]
    un = shapes_mod.unhandled(got)
    assert not un, ("nothing useful can be done with %s"
                    % ", ".join("%s (%s)" % (s.stem, s.label()) for s in un))
    mine = {s.cell for s in got}

    fixtures = SOURCE == ROOT / "tests/data"
    other, other_maps = (("Raw Data", "Maps") if fixtures
                         else ("tests/data", "tests/maps"))
    if not (ROOT / other).is_dir():
        return "%d shape(s), all handled" % len(mine)
    raw = copy.deepcopy(CONFIG)
    raw["maps_dir"] = other_maps
    try:
        cfg2 = config_mod.load(raw)
        from racechrono import video
        clips = video.discover(ROOT / other, cfg2.ffmpeg.ffprobe,
                               recursive=True)
        theirs = set()
        for p in sorted((ROOT / other).rglob("*.csv")):
            r = csv_io.read_run(p, derive=False, validate_units=False)
            mineclips = [c for c in clips if c.start_unix
                         and r.t0 - 300 <= c.start_unix <= r.t1 + 300]
            theirs.add(shapes_mod.classify(r, cfg2, mineclips).cell)
    except Exception as exc:
        return "%d shape(s), all handled (could not read %s: %s)" % (
            len(mine), other, exc)
    missing = sorted(theirs - mine)
    return ("%d shape(s) here, all handled; %d in %s not covered here%s"
            % (len(mine), len(missing), other,
               (": " + "; ".join(" / ".join(c) for c in missing)) if missing else ""))


# ---------------------------------------------------- identity and library

def t_identity_is_shape_and_direction():
    """Every session must identify its own track, and reject the same track
    driven the other way.

    `trackmap.auto_select` answers by nearest origin, which is the wrong question:
    two courses in one parking lot have origins metres apart, and a circuit run
    clockwise shares every coordinate with the same circuit run the other way.
    """
    from racechrono import identify as ident
    maps = {}
    for p in sorted((ROOT / CONFIG["maps_dir"]).glob("*.json")):
        maps[p.stem] = trackmap.load(p)
    if len(maps) < 2:
        raise Skip("need at least two maps to test discrimination")
    runs = _all_runs()
    wrong, checked = [], 0
    for run in runs:
        lat, lon = run.mean_latlon()
        near, _d = trackmap.auto_select(_cfg().maps_dir, lat, lon)
        if near is None:
            continue
        expect = next((k for k, m in maps.items() if m.name == near.name), None)
        name = (run.meta.get("Track name") or "").strip().strip('"') or None
        d = ident.identify(run, maps, source_name=name)
        checked += 1
        if d.verdict != "match" or d.candidate.key != expect:
            wrong.append((run.stem, d.verdict,
                          d.candidate.key if d.candidate else None, expect))
    assert not wrong, "misidentified: %s" % wrong[:3]
    assert checked, "no run had a map to identify against"

    # And the direction test, on a map reversed in place. Reversing a closed
    # centreline leaves every point exactly where it was.
    closed = [m for m in maps.values() if m.closed]
    if not closed:
        raise Skip("no closed circuit to reverse")
    fwd = closed[0]
    rev = trackmap.load(next(p for p in (ROOT / CONFIG["maps_dir"]).glob("*.json")
                             if trackmap.load(p).name == fwd.name))
    rev.name = fwd.name + " reversed"
    rev.points_unit = list(reversed([list(x) for x in fwd.unit_points]))
    pair = {"forward": fwd, "reversed": rev}
    tested = 0
    for run in runs:
        lat, lon = run.mean_latlon()
        near, _d = trackmap.auto_select(_cfg().maps_dir, lat, lon)
        if near is None or near.name != fwd.name:
            continue
        d = ident.identify(run, pair)
        assert d.verdict == "match" and d.candidate.key == "forward", (
            "%s: %s picked %s" % (run.stem, d.verdict,
                                  d.candidate.key if d.candidate else None))
        back = next(c for c in d.candidates if c.key == "reversed")
        assert not back.matched, "the reversed line was accepted"
        assert back.fit.direction < 0.2, (
            "the reversed line reports %.0f%% forward" % (100 * back.fit.direction))
        tested += 1
    if not tested:
        raise Skip("no run sits on the closed circuit")
    return ("%d run(s) identified, none wrong; the same circuit reversed is "
            "rejected on direction for %d of them" % (checked, tested))


def t_library_rename_detaches_nothing():
    """Renaming a track must not detach a lap, a map or a source name.

    Names are labels and ids are keys. The current code keys a session by its
    CSV's stem and a map by its filename, which breaks the first time a track is
    given its real name -- and that is a stated intention.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        tm = a_map()
        tid = lib.add_track("As Imported", closed=bool(tm.closed),
                            source_names=["As Imported"])
        lib.set_map(tid, tm, sources=[("a", None)])
        day = lib.day_for(tid, "2026-08-09")
        sid = lib.add_session(day, "sess", 1786291961.0, 700.0)
        lid = lib.add_lap(sid, 1, 1786292000.0, 1786292085.0, kind="run")

        lib.rename("tracks", tid, "The Real Name")
        lib.rename("sessions", sid, "morning session")
        assert lib.track(tid)["name"] == "The Real Name"
        assert lib.source_names(tid) == ["As Imported"], \
            "renaming changed the names used for matching"
        assert lib.map_of(tid) is not None, "the map came unstuck"
        assert lib.map_of(tid).name == "The Real Name", \
            "the map still carries the old name, so a track has two names"
        assert len(lib.laps(track_id=tid)) == 1, "the lap came unstuck"
        assert lib.track_of_lap(lid) == tid
        return "renamed track, session and map; lap, map and source names intact"

def t_a_failed_write_does_not_lock_the_library():
    """A rename that hits nothing must not leave a write transaction open.

    sqlite3 opens one on the first UPDATE. `rename` raised KeyError when the row
    did not exist without rolling back, so the connection kept the database lock
    and every later write -- from any thread -- failed with "database is locked".
    Through the web app that read as a corrupt library rather than as a typo
    three requests earlier.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        tid = lib.add_track("Real", closed=False)
        try:
            lib.rename("tracks", "no-such-id", "Nope")
        except KeyError:
            pass
        else:
            raise AssertionError("renaming a missing row was accepted")
        # The proof is a second, unrelated write on another connection: the same
        # thread would reuse the transaction and pass regardless.
        out = {}

        def other():
            try:
                second = lib_mod.Library(lib.paths.root)
                second.rename("tracks", tid, "Renamed from another thread")
                second.close()
                out["ok"] = True
            except Exception as exc:
                out["error"] = "%s: %s" % (type(exc).__name__, exc)

        t = threading.Thread(target=other)
        t.start()
        t.join(20)
        assert out.get("ok"), "the library was left locked -- %s" % out.get("error")
        assert lib.track(tid)["name"] == "Renamed from another thread"
        return "a rename that hits nothing rolls back; later writes still work"

def t_library_delete_lap_keeps_the_map():
    """Deleting a lap must not alter a map built from it, and must prune upward.

    A map is expensive and a deletion is one click; silently changing the
    geometry underneath every other lap at that track would be the wrong trade.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        tm = a_map()
        tid = lib.add_track("T", closed=bool(tm.closed))
        day = lib.day_for(tid, "2026-08-09")
        sid = lib.add_session(day, "s", 1786291961.0, 700.0)
        a = lib.add_lap(sid, 1, 1786292000.0, 1786292085.0)
        b = lib.add_lap(sid, 2, 1786292100.0, 1786292207.0)
        lib.set_map(tid, tm, sources=[("lap 1", a), ("lap 2", b)])
        before = lib.map_of(tid).length_m

        # A real file, so the freed figure is real.
        vid = lib.paths.lap_dir(a) / "video.mp4"
        vid.write_bytes(b"\0" * 4096)
        lib.db.execute("UPDATE laps SET video_bytes=? WHERE id=?", (4096, a))
        lib.db.commit()

        got = lib.delete_lap(a)
        assert got["freed_bytes"] >= 4096, got
        assert not vid.exists() and not lib.paths.lap_dir(a).exists(), \
            "the lap's files are still on disk"
        assert lib.lap(a) is None and lib.lap(b) is not None
        assert abs(lib.map_of(tid).length_m - before) < 1e-9, \
            "the map changed when a lap was deleted"
        prov = {p["label"]: p["present"] for p in lib.map_provenance(tid)}
        assert prov == {"lap 1": 0, "lap 2": 1}, prov
        assert got["pruned"] == {"sessions": [], "days": []}

        got = lib.delete_lap(b)
        assert got["pruned"]["sessions"] == [sid], got
        assert got["pruned"]["days"] == [day], got
        assert lib.track(tid) is not None, "the track went with its last lap"
        assert lib.map_of(tid) is not None, "the map went with the last lap"
        return ("map and provenance survive a deletion; the empty session and "
                "day are pruned and the track with its map is kept")


def t_ingest_reads_a_folder_in_place():
    """A folder is a folder: telemetry and footage told apart by extension,
    found however deeply they are nested, and never copied or moved.

    There is no `Raw Data` and no `Raw Footage` any more. Someone points at a
    card copied off a phone or a "day at AMP" directory, and both kinds of file
    are in there somewhere.
    """
    from racechrono import ingest as ingest_mod
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "a day"
        (root / "telemetry").mkdir(parents=True)
        (root / "clips" / "cam1").mkdir(parents=True)
        src = SOURCE
        csvs = sorted(src.glob("*.csv"))[:2]
        vids = sorted(src.glob("*.mp4"))[:1]
        if not csvs:
            raise Skip("no CSVs to copy")
        for f in csvs:
            shutil.copy2(f, root / "telemetry" / f.name)
        for f in vids:
            shutil.copy2(f, root / "clips" / "cam1" / f.name)

        found_csv, found_clips = ingest_mod.scan(root)
        assert len(found_csv) == len(csvs), (
            "found %d CSV(s) of %d, nested" % (len(found_csv), len(csvs)))
        assert len(found_clips) == len(vids), (
            "found %d clip(s) of %d, nested" % (len(found_clips), len(vids)))

        survey = ingest_mod.survey(root)
        assert survey["exists"] and survey["csvs"] == len(csvs)
        assert survey["videos"] == len(vids)
        assert survey["bytes"] > 0

        # Nothing under the source may be touched by planning.
        before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
        from racechrono import library as lib_mod
        with _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
            ingest_mod.plan(_cfg(), lib, source=root, verbose=False)
        after = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
        assert before == after, "planning modified the source folder"
        assert ingest_mod.survey(Path(tmp) / "nope")["exists"] is False
        return ("%d CSV(s) and %d clip(s) found in nested subfolders; the source "
                "is untouched" % (len(found_csv), len(found_clips)))


def t_import_verifies_before_saying_it_is_safe():
    """The "safe to delete" line has to be earned by checking every lap on disk.

    It is the one sentence in the whole tool that invites someone to delete
    irreplaceable footage, so it is only printed when every lap written has a row,
    a telemetry file and, where one was expected, a video -- and none of them
    empty.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        src = SOURCE
        p = ingest_mod.plan(_cfg(), lib, source=src,
                            options=ingest_mod.Options(keep_best=1),
                            verbose=False)
        made = ingest_mod.apply(p, lib, _cfg(), encode=False, verbose=False)
        check = ingest_mod.verify(made, lib, p)
        assert check["ok"], check["problems"]
        assert check["laps"] == len(made["laps"]) == len(p.kept())
        assert check["source_files"] > 0 and check["source_bytes"] > 0
        assert Path(check["source"]).resolve() == src.resolve()

        # And it must notice when something is missing.
        lid = made["laps"][0]
        row = lib.lap(lid)
        (lib.paths.root / row["data_path"]).unlink()
        broken = ingest_mod.verify(made, lib, p)
        assert not broken["ok"], "a missing telemetry file passed the check"
        assert any("telemetry" in why for why in broken["problems"]),             broken["problems"]
        return ("%d lap(s) verified against %d source file(s); deleting one "
                "telemetry file makes the check fail"
                % (check["laps"], check["source_files"]))

def t_importing_the_same_folder_twice_changes_nothing():
    """A second import of the same folder must be a no-op, not a second copy.

    Pointing at the same folder again is a normal thing to do -- a day's footage
    arrives on more than one card, and the obvious way to add the last one is to
    import the folder again. Nothing compared a session about to be written
    against what the library already held, so the whole library doubled: every
    session, every lap, every byte of video. The within-import md5 check cannot
    see across imports.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        first = ingest_mod.plan(_cfg(), lib, source=SOURCE, verbose=False)
        made = ingest_mod.apply(first, lib, _cfg(), encode=False, verbose=False)
        before = lib.summary()
        assert before["laps"] == len(made["laps"]) > 0, before

        second = ingest_mod.plan(_cfg(), lib, source=SOURCE, verbose=False)
        assert not second.sessions, (
            "%d session(s) would be imported a second time"
            % len(second.sessions))
        assert len(second.already) == len(first.sessions), (
            "%d skipped as already held, %d were imported"
            % (len(second.already), len(first.sessions)))
        again = ingest_mod.apply(second, lib, _cfg(), encode=False,
                                 verbose=False)
        after = lib.summary()
        assert not again["laps"], "the second import wrote %d lap(s)" % len(
            again["laps"])
        assert after == before, "the library changed: %s -> %s" % (before, after)

        # The report has to say so, and the check has to admit the folder holds
        # sessions the library skipped rather than claiming a clean import.
        text = ingest_mod.format_plan(second)
        assert "already in the library" in text, text
        check = ingest_mod.verify(again, lib, second)
        assert check["ok"], check["problems"]
        assert check["already"] == len(second.already), check

        # A session the library does not hold still gets in. Nudging one row's
        # clock is enough to make it a different recording.
        row = lib.sessions()[0]
        lib.db.execute("UPDATE sessions SET t0_unix = t0_unix + 3600 WHERE id=?",
                       (row["id"],))
        lib.db.commit()
        third = ingest_mod.plan(_cfg(), lib, source=SOURCE, verbose=False)
        assert len(third.sessions) == 1, (
            "%d session(s) after moving one recording's clock"
            % len(third.sessions))
        return ("%d session(s) imported, %d skipped on a second pass, library "
                "unchanged at %d lap(s)"
                % (len(first.sessions), len(second.already), before["laps"]))


def t_verify_refuses_when_a_file_could_not_be_read():
    """An unreadable CSV must block the "safe to delete" line.

    It is the one sentence in the tool that invites someone to delete
    irreplaceable footage, and a file that could not be parsed is not in the
    library. The check used to look only at what was written, so a folder with
    one corrupt export was declared safe to delete along with the rest.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        src = Path(tmp) / "src"
        src.mkdir()
        for path in sorted(SOURCE.rglob("*.csv")):
            shutil.copy2(path, src / path.name)
        (src / "truncated.csv").write_text("this is not a RaceChrono export\n",
                                           encoding="utf-8")
        p = ingest_mod.plan(_cfg(), lib, source=src,
                            options=ingest_mod.Options(keep_best=1),
                            verbose=False)
        assert p.skipped, "a file that is not a CSV export was read anyway"
        made = ingest_mod.apply(p, lib, _cfg(), encode=False, verbose=False)
        check = ingest_mod.verify(made, lib, p)
        assert not check["ok"], "an unreadable file still said safe to delete"
        assert any("truncated.csv" in why for why in check["problems"]), \
            check["problems"]
        return ("%d lap(s) written, and the unreadable file blocks the "
                "safe-to-delete line" % check["laps"])

def t_ingest_plan_groups_by_track():
    """A day of single-run sessions must arrive as one track, not one each.

    This is where a ten-session autocross day became ten tracks: an open-course
    average needs two traversals, a single-run session offers one, so the
    provisional line came back empty and every session started its own cluster.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        p = ingest_mod.plan(_cfg(), lib, source=SOURCE,
                            verbose=False)
        assert p.sessions, "nothing to ingest"
        by_track = {}
        for sp in p.sessions:
            by_track.setdefault(sp.track_key, []).append(sp)
        # Every session must land on a track whose other members share its venue.
        for key, members in by_track.items():
            venues = {m.shape.venue for m in members}
            assert len(venues) == 1, ("track %s mixes venues %s" % (key, venues))
        # And no venue should be split across several new tracks.
        venue_tracks = {}
        for key, members in by_track.items():
            v = members[0].shape.venue
            venue_tracks.setdefault(v, set()).add(key)
        split = {v: ks for v, ks in venue_tracks.items() if len(ks) > 1}
        assert not split, "one venue became several tracks: %s" % split
        assert len(p.tracks) == len(by_track), "a track plan has no sessions"
        return "%d session(s) grouped into %d track(s): %s" % (
            len(p.sessions), len(p.tracks),
            ", ".join("%s x%d" % (t.name, len(t.sessions)) for t in p.tracks))


def t_ingest_skips_duplicates_and_slices_by_shape():
    """Byte-identical files are imported once, and a session is sliced the way
    its shape says -- laps when the laps are corroborated, runs otherwise.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        p = ingest_mod.plan(_cfg(), lib, source=SOURCE,
                            verbose=False)
        for sp in p.sessions:
            kinds = {l.kind for l in sp.laps}
            if sp.shape.laps_usable:
                assert kinds <= {"lap"}, "%s has %s" % (sp.stem, kinds)
                assert len(sp.laps) == sp.shape.n_laps, (
                    "%s: %d laps sliced of %d" % (sp.stem, len(sp.laps),
                                                  sp.shape.n_laps))
            else:
                assert kinds <= {"run"}, "%s has %s" % (sp.stem, kinds)
                assert len(sp.laps) == sp.shape.n_runs, (
                    "%s: %d runs sliced of %d" % (sp.stem, len(sp.laps),
                                                  sp.shape.n_runs))
        seen = {}
        import hashlib
        for path in sorted(SOURCE.glob("*.csv")):
            h = hashlib.md5(path.read_bytes()).hexdigest()
            seen.setdefault(h, []).append(path.name)
        dupes = [v for v in seen.values() if len(v) > 1]
        assert len(p.duplicates) == sum(len(v) - 1 for v in dupes), (
            "%d duplicate(s) reported, %d expected"
            % (len(p.duplicates), sum(len(v) - 1 for v in dupes)))
        return "%d session(s), %d lap/run(s), %d duplicate(s) skipped" % (
            len(p.sessions), sum(len(s.laps) for s in p.sessions),
            len(p.duplicates))


def t_lap_video_window_is_recorded():
    """A lap must record where its video actually starts in data time.

    Not `t0 - pad_s`. Padding is clamped to the footage that exists, so a lap
    sitting at the end of a clip starts later than its padding asked for -- by up
    to 1.8 s across the laps measured here. A player that assumed the padding was
    always granted would put the overlay that far out of step, and it would look
    like a sync bug rather than an arithmetic one.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        p = ingest_mod.plan(_cfg(), lib, source=SOURCE,
                            verbose=False)
        withvid = [(sp, lp) for sp in p.sessions for lp in sp.laps if lp.has_video]
        if not withvid:
            raise Skip("no lap here has footage")
        # Keep one session so the check is quick, and skip the encode: the point
        # is the recorded window, not the pixels.
        sp0 = next(sp for sp, _l in withvid)
        p.sessions = [sp0]
        p.tracks = [t for t in p.tracks if t.key == sp0.track_key]
        p.tracks[0].sessions = [sp0]
        made = ingest_mod.apply(p, lib, _cfg(), encode=False, verbose=False)
        by_number = {(l.kind, l.number): l for l in sp0.laps}
        checked, clamped = 0, 0
        for lid in made["laps"]:
            row = lib.lap(lid)
            lp = by_number[(row["kind"], row["number"])]
            if not lp.has_video:
                assert row["video_t0"] is None,                     "a lap with no footage claims a video start"
                continue
            want_t0 = lp.segments[0].data_t0
            want_s = sum(sg.duration for sg in lp.segments)
            assert abs(row["video_t0"] - want_t0) < 1e-6, (
                "%s %d: video_t0 %.3f, segments start at %.3f"
                % (row["kind"], row["number"], row["video_t0"], want_t0))
            assert abs(row["video_s"] - want_s) < 1e-6
            naive = row["t0_unix"] - row["pad_s"]
            if abs(naive - want_t0) > 0.05:
                clamped += 1
            checked += 1
        assert checked, "no lap with video was written"
        return ("%d lap(s) record their real video window; %d had padding "
                "clamped by the end of the footage" % (checked, clamped))


def t_ingested_lap_stands_alone():
    """A lap's data file must be loadable on its own.

    The source session is not kept, so anything needed to read these rows has to
    be in this file -- including the session's calibration.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        p = ingest_mod.plan(_cfg(), lib, source=SOURCE,
                            verbose=False)
        if not p.sessions:
            raise Skip("nothing to ingest")
        # One session, one lap, no video: the point is the data file.
        p.sessions = p.sessions[:1]
        p.tracks = [t for t in p.tracks if t.key == p.sessions[0].track_key]
        p.tracks[0].sessions = p.sessions
        p.sessions[0].laps = p.sessions[0].laps[:1]
        made = ingest_mod.apply(p, lib, _cfg(), encode=False, verbose=False)
        assert made["laps"], "no lap written"
        lap = lib.lap(made["laps"][0])
        path = lib.paths.root / lap["data_path"]
        assert path.is_file(), path
        back = csv_io.read_run(path, validate_units=False)
        assert back.df.shape[0] > 32, "the lap file has %d rows" % back.df.shape[0]
        want = lap["duration_s"] + 2 * lap["pad_s"]
        assert abs(back.duration - want) < 2.0, (
            "the lap file spans %.1f s, expected about %.1f" % (back.duration, want))
        assert "Calibration" in back.meta, \
            "the calibration did not travel with the lap"
        return ("%s %d: %d rows, %.1f s including %.0f s of padding, calibration "
                "carried" % (lap["kind"], lap["number"], back.df.shape[0],
                             back.duration, 2 * lap["pad_s"]))


def _stocked(lib, name="T", closed=False, dates=("2026-08-22",), laps=3):
    """A track with days, sessions and laps, for the organising checks."""
    tid = lib.add_track(name, closed=closed, source_names=[name])
    tm = a_map()
    lib.set_map(tid, tm, sources=[("seed", None)])
    made = []
    for date in dates:
        day = lib.day_for(tid, date)
        sid = lib.add_session(day, "%s %s" % (name, date), 1787427759.0, 44.0)
        for i in range(1, laps + 1):
            made.append(lib.add_lap(sid, i, 1787427800.0 + i * 100,
                                    1787427840.0 + i * 100, kind="run",
                                    duration_s=40.0 + i,
                                    representative=(i != laps)))
    return tid, made


def t_library_video_deletes_separately():
    """A lap's video must be removable without its telemetry.

    Video is 99% of a lap: 276 MB against 2 MB on a typical AMP lap. A lap
    someone has finished watching is still a lap they want in a comparison, and
    keeping the telemetry should not cost a quarter of a gigabyte.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        tid, laps = _stocked(lib, "V", laps=2)
        lid = laps[0]
        d = lib.paths.lap_dir(lid)
        (d / "video.mp4").write_bytes(b"v" * 8192)
        (d / "data.csv").write_bytes(b"t" * 256)
        lib.db.execute(
            "UPDATE laps SET video_path=?, data_path=?, video_bytes=?, "
            "data_bytes=?, video_t0=?, video_s=? WHERE id=?",
            ("laps/%s/video.mp4" % lid, "laps/%s/data.csv" % lid, 8192, 256,
             1787427797.0, 46.0, lid))
        lib.db.commit()

        got = lib.delete_video(lid)
        assert got["freed_bytes"] == 8192, got
        row = lib.lap(lid)
        assert row is not None, "the lap went with its video"
        assert not (d / "video.mp4").exists(), "the video file is still there"
        assert (d / "data.csv").exists(), "the telemetry went too"
        assert row["video_path"] == "" and row["video_bytes"] == 0
        assert row["video_t0"] is None and row["video_s"] == 0,             "the video window survived the video"
        assert row["data_path"] and row["data_bytes"] == 256,             "the telemetry record was disturbed"

        # And it must be selectable afterwards as a lap with no video.
        assert lid in [r["id"] for r in lib.select_laps(track_id=tid,
                                                        no_video=True)]
        assert lid not in [r["id"] for r in lib.select_laps(track_id=tid,
                                                            has_video=True)]
        return ("video removed, %d B freed, telemetry and lap record intact"
                % got["freed_bytes"])


def t_ingest_apply_honours_the_filters():
    """`apply` must write only the laps the options kept.

    Filters mark rather than remove, so that the import screen can show what a
    checkbox leaves behind -- which means `apply` has to check the mark. It did
    not: an import with "keep the quickest one per session" planned five laps and
    wrote ten, and the filters were decoration.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        p = ingest_mod.plan(_cfg(), lib, source=SOURCE,
                            options=ingest_mod.Options(keep_best=1),
                            verbose=False)
        want = p.estimate()["laps"]
        total = sum(len(sp.laps) for sp in p.sessions)
        if not (0 < want < total):
            raise Skip("this dataset does not exercise the filter")
        made = ingest_mod.apply(p, lib, _cfg(), encode=False, verbose=False)
        assert len(made["laps"]) == want, (
            "planned %d lap(s) and wrote %d" % (want, len(made["laps"])))
        assert lib.summary()["laps"] == want
        # And every lap written is one the plan kept.
        kept = {(sp.stem, lp.kind, lp.number) for sp, lp in p.kept()}
        for lid in made["laps"]:
            row = lib.lap(lid)
            sess = lib.session(row["session_id"])
            assert (sess["name"], row["kind"], row["number"]) in kept,                 "wrote a lap the filter dropped: %s %d" % (row["kind"],
                                                           row["number"])
        return "planned %d of %d lap(s) and wrote exactly those" % (want, total)


def t_ingest_options_filter_without_removing():
    """Import filters mark laps rather than dropping them from the plan.

    The import screen has to show what a checkbox is about to leave behind. A lap
    that silently vanished from the list when a box was ticked would be the worst
    version of this.
    """
    from racechrono import ingest as ingest_mod, library as lib_mod
    with tempfile.TemporaryDirectory() as tmp,             _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        p = ingest_mod.plan(_cfg(), lib, source=SOURCE,
                            verbose=False)
        total = sum(len(sp.laps) for sp in p.sessions)
        if total < 3:
            raise Skip("too few laps here to filter")
        base = p.estimate()
        assert base["laps"] == total and base["dropped"] == 0

        p.options = ingest_mod.Options(skip_edge_laps=True)
        ingest_mod.apply_filters(p)
        after = p.estimate()
        assert sum(len(sp.laps) for sp in p.sessions) == total,             "filtering removed laps from the plan instead of marking them"
        assert after["laps"] + after["dropped"] == total
        assert all(lp.dropped for _sp, lp in p.dropped()),             "a dropped lap has no reason attached"

        # Un-ticking must put them back, which means filters cannot be cumulative.
        p.options = ingest_mod.Options()
        ingest_mod.apply_filters(p)
        assert p.estimate()["laps"] == total, "unfiltering did not restore"

        # And the size estimate has to track the video options -- which only
        # means anything when there is footage. A folder of telemetry with no
        # video estimates zero however the video options are set, and comparing
        # zero against zero would assert nothing.
        p.options = ingest_mod.Options()
        ingest_mod.apply_filters(p)
        full = p.estimate()
        if not full["bytes"]:
            return ("%d laps, no footage in this folder: filters mark and "
                    "unmark; the size estimate has nothing to size" % total)

        p.options = ingest_mod.Options(height=720, fps=30)
        ingest_mod.apply_filters(p)
        small = p.estimate()
        assert small["bytes"] < full["bytes"] * 0.4, (
            "720p30 estimated at %.2f of full size"
            % (small["bytes"] / max(full["bytes"], 1)))
        p.options = ingest_mod.Options(import_video=False)
        ingest_mod.apply_filters(p)
        assert p.estimate()["bytes"] == 0, "a data-only import estimates video"
        return ("%d laps: filters mark and unmark, 720p30 estimates %.0f%% of "
                "full size, data-only estimates nothing"
                % (total, 100 * small["bytes"] / max(full["bytes"], 1)))


def t_library_migrates_forward():
    """An older library must open and come forward, not fail on the next insert.

    The first version of this schema had no `laps.video_t0`, and
    `CREATE TABLE IF NOT EXISTS` does not add a column to a table that already
    exists -- so an old library opened cleanly and then broke on the next lap
    written. A store people keep data in needs a way forward.
    """
    import sqlite3
    from racechrono import library as lib_mod
    if lib_mod.SCHEMA_VERSION < 2:
        raise Skip("nothing to migrate from yet")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "lib"
        with _closing(lib_mod.Library(root)) as lib:
            tid, laps = _stocked(lib, "Old", laps=2)
            before = (len(lib.tracks()), len(lib.laps()))

        # Roll it back to v1 the way the old code left it.
        conn = sqlite3.connect(root / "library.db")
        for col in ("video_t0", "video_s"):
            try:
                conn.execute("ALTER TABLE laps DROP COLUMN %s" % col)
            except sqlite3.OperationalError:
                pass
        conn.execute("UPDATE meta SET value='1' WHERE key='schema'")
        conn.commit()
        conn.close()

        with _closing(lib_mod.Library(root)) as lib:
            assert lib.migrated, "opening a v1 library ran no migration"
            assert (len(lib.tracks()), len(lib.laps())) == before, \
                "the migration lost data"
            # And the thing the migration exists for: an insert must work.
            sid = lib.sessions()[0]["id"]
            new = lib.add_lap(sid, 99, 1787428000.0, 1787428040.0,
                              kind="run", video_t0=1787427997.0, video_s=46.0)
            assert lib.lap(new)["video_t0"] == 1787427997.0
        with _closing(lib_mod.Library(root)) as lib:
            assert not lib.migrated, "reopening migrated again"
        return ("v1 opened, migrated to v%d, data intact, inserts work, second "
                "open is a no-op" % lib_mod.SCHEMA_VERSION)


def t_library_verify_finds_drift():
    """The three ways a library and its files come apart, and the repair.

    Months of editing and deleting is what makes this necessary: an interrupted
    ingest leaves a directory with no row, a hand-deleted file leaves a row with
    no file, and a byte count recorded at write time stops being true if anything
    touches the file.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        tid, laps = _stocked(lib, "V", laps=2)
        lid = laps[0]

        # a file smaller than the index claims
        vid = lib.paths.lap_dir(lid) / "video.mp4"
        vid.write_bytes(b"x" * 5000)
        lib.db.execute("UPDATE laps SET video_path=?, video_bytes=? WHERE id=?",
                       ("laps/%s/video.mp4" % lid, 9999, lid))
        # a row pointing at a file that is not there
        lib.db.execute("UPDATE laps SET data_path=?, data_bytes=? WHERE id=?",
                       ("laps/%s/data.csv" % lid, 1234, lid))
        lib.db.commit()
        # a directory with no row
        stray = lib.paths.root / "laps" / "0123456789abcdef"
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "video.mp4").write_bytes(b"y" * 777)

        r = lib.verify()
        assert len(r["byte_drift"]) == 1 and r["byte_drift"][0][2:] == (9999, 5000), \
            r["byte_drift"]
        assert [x[:2] for x in r["missing_files"]] == [(lid, "data")], \
            r["missing_files"]
        assert [n for n, _sz in r["orphan_dirs"]] == [stray.name], r["orphan_dirs"]
        assert not r["orphan_rows"], r["orphan_rows"]

        fixed = lib.verify(repair=True)
        assert fixed["repaired"] == 3, fixed
        after = lib.verify()
        assert not (after["byte_drift"] or after["missing_files"]
                    or after["orphan_dirs"]), after
        assert lib.lap(lid)["video_bytes"] == 5000, "the byte count was not re-read"
        assert lib.lap(lid)["data_path"] == "", "a dead path was left in place"
        assert not stray.exists(), "the orphan directory survived"
        # Repair must not invent or destroy rows.
        assert len(lib.laps()) == len(laps), "repair changed the number of laps"
        return ("found drift, a dead path and an orphan directory; repaired 3 "
                "and left every row alone")


def t_library_merge_and_split():
    """Resolving an identity mistake in either direction, without touching laps.

    The identifier reports an ambiguity rather than guessing, which is only
    useful if the user can then act: merge two tracks that were one, or split one
    that was two.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        a, laps_a = _stocked(lib, "Course A", dates=("2026-08-22",), laps=3)
        b, laps_b = _stocked(lib, "Course B",
                             dates=("2026-08-22", "2026-08-23"), laps=3)
        total = len(laps_a) + len(laps_b)

        got = lib.merge_tracks(a, b)
        assert got["days"] == 2, got
        assert lib.track(b) is None, "the absorbed track is still there"
        assert len(lib.laps(track_id=a)) == total, "laps were lost in the merge"
        # The shared date must become one day, not two.
        dates = [d["date"] for d in lib.days(a)]
        assert sorted(dates) == ["2026-08-22", "2026-08-23"], dates
        assert set(lib.source_names(a)) == {"Course A", "Course B"}, \
            "a later import under the absorbed name would not match"
        assert any("(merged)" in p["label"] for p in lib.map_provenance(a)), \
            "the absorbed track's provenance was dropped"

        later = [s["id"] for s in lib.sessions()
                 if "2026-08-23" in s["name"]]
        out = lib.split_track(a, later, name="Really Course B")
        assert out["sessions"] == len(later), out
        new = out["track"]
        assert lib.map_of(new) is None, \
            "the split track inherited a map describing the other shape"
        assert len(lib.laps(track_id=a)) + len(lib.laps(track_id=new)) == total, \
            "laps were lost in the split"
        assert set(lib.source_names(new)) == {"Course A", "Course B"}
        return ("merged two tracks into %d day(s) keeping %d lap(s), then split "
                "%d session(s) back out with no map"
                % (len(lib.days(a)) + len(lib.days(new)), total, out["sessions"]))


def t_library_bulk_selection():
    """Selecting laps by rule, and deleting only when asked.

    Reclaiming space one lap at a time is not how anyone would do it, and a rule
    that quietly deleted while you were still looking at it would be the worst
    possible version of this feature.
    """
    from racechrono import library as lib_mod
    with tempfile.TemporaryDirectory() as tmp, \
            _closing(lib_mod.Library(Path(tmp) / "lib")) as lib:
        tid, laps = _stocked(lib, "S", laps=4)      # 41, 42, 43, 44 s; 44 is edge
        assert len(lib.laps(track_id=tid)) == 4

        edge = lib.select_laps(track_id=tid, non_representative=True)
        assert [round(l["duration_s"], 1) for l in edge] == [44.0], edge
        slow = lib.select_laps(track_id=tid, slower_than=42.0)
        assert sorted(round(l["duration_s"], 1) for l in slow) == [43.0, 44.0]
        rest = lib.select_laps(track_id=tid, keep_best=2)
        assert sorted(round(l["duration_s"], 1) for l in rest) == [43.0, 44.0]
        novid = lib.select_laps(track_id=tid, no_video=True)
        assert len(novid) == 4, "these laps have no video, so all should match"

        # Selecting is not deleting.
        assert len(lib.laps(track_id=tid)) == 4, "selection removed something"

        got = lib.delete_laps([l["id"] for l in edge])
        assert got["count"] == 1
        assert len(lib.laps(track_id=tid)) == 3
        return ("four rules select 1, 2, 2 and 4 laps; selecting removes "
                "nothing, deleting the 1 leaves 3")


# ------------------------------------------------- environment and encoders

def t_env_overrides_config():
    """LAPVIEW_* variables configure a container; unset, nothing changes."""
    env_of = config_mod.apply_env
    assert env_of(CONFIG, {}) == CONFIG, "an empty environment changed CONFIG"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        cfg = config_mod.load(env_of(CONFIG, {"LAPVIEW_DATA_DIR": tmp}))
        for key, sub in (("library_dir", "library"), ("output_dir", "output"),
                         ("cache_dir", "cache"), ("inbox_dir", "inbox")):
            got = Path(getattr(cfg, key))
            assert got == base / sub, "%s is %s" % (key, got)
            assert got.is_absolute(), "%s depends on the working dir" % key
        assert Path(cfg.maps_dir) == (ROOT / CONFIG["maps_dir"]).resolve(), \
            "with no maps/ under the data dir, the shipped maps should be used"
        (base / "maps").mkdir()
        cfg = config_mod.load(env_of(CONFIG, {"LAPVIEW_DATA_DIR": tmp}))
        assert Path(cfg.maps_dir) == base / "maps"
        own = base / "elsewhere"
        cfg = config_mod.load(env_of(CONFIG, {"LAPVIEW_DATA_DIR": tmp,
                                              "LAPVIEW_LIBRARY_DIR": str(own)}))
        assert Path(cfg.library_dir) == own, "the specific override must win"

    cfg = config_mod.load(env_of(CONFIG, {"LAPVIEW_ENCODER": "libx264"}))
    assert (cfg.encode.encoder, cfg.encode.preset) == ("libx264", "medium")
    cfg = config_mod.load(env_of(CONFIG, {"LAPVIEW_ENCODER": "h264_nvenc",
                                          "LAPVIEW_NVENC_PROFILE": "pascal",
                                          "LAPVIEW_NVENC_TEMPORAL_AQ": "0"}))
    assert cfg.encode.encoder == "h264_nvenc" and cfg.encode.preset == "p7"
    assert cfg.encode.nvenc_profile == "pascal" and not cfg.encode.temporal_aq
    for bad in ({"LAPVIEW_ENCODER": "av1_nvenc"},
                {"LAPVIEW_NVENC_PROFILE": "kepler"},
                {"LAPVIEW_NVENC_TEMPORAL_AQ": "maybe"},
                {"LAPVIEW_CPU_FALLBACK": "x266"}):
        try:
            config_mod.load(env_of(CONFIG, bad))
        except config_mod.ConfigError:
            continue
        raise AssertionError("%s was accepted" % bad)
    return ("unset is a no-op; data dir, per-dir and encoder overrides apply; "
            "4 bad values rejected")


def _enc(**kw):
    cfg = config_mod.load(copy.deepcopy(CONFIG))
    for k, v in kw.items():
        setattr(cfg.encode, k, v)
    return cfg


def _pairs(args):
    return {args[i]: args[i + 1] for i in range(0, len(args) - 1)
            if args[i].startswith("-")}


def t_pascal_args_are_safe():
    """A GTX 10-series refuses HEVC B-frames and B-frame references.

    Pascal's NVENC opens no session that asks for them, so the profile has to
    leave them out -- in the export and in the import's re-encode alike -- and
    a Turing-or-newer card keeps the tuned settings.
    """
    from racechrono import encoders, ingest as ingest_mod
    modern = _pairs(render.encoder_args(_enc(nvenc_profile="turing").encode))
    assert modern["-bf"] == "3" and modern["-b_ref_mode"] == "middle", modern
    hevc = _pairs(render.encoder_args(_enc(nvenc_profile="pascal").encode))
    assert hevc["-bf"] == "0" and "-b_ref_mode" not in hevc, hevc
    h264 = _pairs(render.encoder_args(
        _enc(encoder="h264_nvenc", nvenc_profile="pascal").encode))
    assert h264["-bf"] == "3" and "-b_ref_mode" not in h264, h264

    names = {"NVIDIA GeForce GTX 1070": "pascal",
             "NVIDIA GeForce GTX 1070 with Max-Q Design": "pascal",
             "Quadro P2000": "pascal", "Tesla P4": "pascal",
             "NVIDIA GeForce GTX 980": "pascal",
             "NVIDIA GeForce RTX 4090": "turing",
             "NVIDIA GeForce GTX 1660 SUPER": "turing", "NVIDIA L4": "turing"}
    for name, want in names.items():
        assert encoders.profile_for(name) == want, name

    opt = ingest_mod.Options()
    got = opt.video_args("hevc_nvenc", "pascal")
    assert _pairs(got).get("-bf") == "0", got
    for enc, tagged in (("hevc_nvenc", True), ("libx265", True),
                        ("h264_nvenc", False), ("libx264", False)):
        args = opt.video_args(enc, "pascal")
        assert (_pairs(args).get("-tag:v") == "hvc1") == tagged, (enc, args)
        if "h264" in enc or "264" in enc:
            assert "-bf" not in args, args
    return ("pascal: HEVC -bf 0, no b_ref_mode; ingest HEVC tagged hvc1; "
            "%d card names classified" % len(names))


def t_encoder_falls_back():
    """The startup probe settles on something that opens, best first."""
    from racechrono import encoders

    def runner(fails):
        seen = []

        def run(cmd):
            seen.append(cmd)
            text = " ".join(cmd)
            return (not any(f(text) for f in fails), "simulated failure")
        run.seen = seen
        return run

    # A GTX 1070 with no HEVC temporal AQ: the pascal profile from the name,
    # then the same encoder again without temporal AQ.
    cfg = _enc()
    run = runner([lambda t: "-temporal-aq 1" in t])
    got = encoders.resolve(cfg, runner=run, ffmpeg="ffmpeg",
                           gpu_list=[{"name": "NVIDIA GeForce GTX 1070"}])
    first = _pairs(run.seen[0])
    assert first["-bf"] == "0" and "-b_ref_mode" not in first, first
    assert cfg.encode.encoder == "hevc_nvenc" and not cfg.encode.temporal_aq
    assert got["profile"] == "pascal" and got["fallback"], got

    # An unnamed card that refuses B-frame references drops to pascal.
    cfg = _enc()
    run = runner([lambda t: "-b_ref_mode" in t])
    got = encoders.resolve(cfg, runner=run, ffmpeg="ffmpeg", gpu_list=[])
    assert (cfg.encode.encoder, cfg.encode.nvenc_profile) == ("hevc_nvenc",
                                                              "pascal"), got

    # No GPU at all: the CPU, same codec, with an x26x preset and no CUDA.
    for asked, want in (("hevc_nvenc", "libx265"), ("h264_nvenc", "libx264")):
        cfg = _enc(encoder=asked)
        got = encoders.resolve(cfg, runner=runner([lambda t: "nvenc" in t]),
                               ffmpeg="ffmpeg", gpu_list=[])
        e = cfg.encode
        assert (e.encoder, e.preset, e.hwaccel_decode) == (want, "medium",
                                                           False), got
        config_mod.EncodeCfg(**{f.name: getattr(e, f.name) for f in
                                config_mod.fields(e)}).validate()
        assert render.encoder_args(e)[:2] == ["-preset", "medium"]

    # Fallback switched off: nothing is quietly swapped.
    cfg = _enc(cpu_fallback="none")
    got = encoders.resolve(cfg, runner=runner([lambda t: "nvenc" in t]),
                           ffmpeg="ffmpeg", gpu_list=[])
    assert cfg.encode.encoder == "hevc_nvenc" and not got.get("chosen"), got

    # A CPU encoder asked for is tested once and kept.
    cfg = _enc(encoder="libx264", preset="medium")
    run = runner([])
    encoders.resolve(cfg, runner=run, ffmpeg="ffmpeg")
    assert len(run.seen) == 1 and cfg.encode.encoder == "libx264"
    return "temporal AQ, pascal, CPU (hevc and h264), off and CPU-asked paths"


def t_real_probe_settles():
    """On this machine, with its real ffmpeg: whatever is chosen opens."""
    from racechrono import encoders
    try:
        video.find_binary("ffmpeg")
    except FileNotFoundError:
        raise Skip("no ffmpeg here")
    cfg = _enc()
    got = encoders.resolve(cfg)
    assert got.get("chosen"), got
    # The pascal arguments are a subset of what any newer card takes, so on a
    # newer card they must open too -- which checks the syntax ffmpeg is given.
    forced = _enc(nvenc_profile="pascal")
    got2 = encoders.resolve(forced)
    assert got2.get("chosen"), got2
    return "%s; forced pascal: %s" % (encoders.describe(cfg.encode, got),
                                      encoders.describe(forced.encode))


# ------------------------------------------------------------ web server

def _server_library(tmp, **kw):
    """The web app's Library over a library, output and inbox in `tmp`."""
    from webui import server as srv_mod
    cfg = config_mod.load(copy.deepcopy(CONFIG))
    cfg.library_dir = Path(tmp) / "lib"
    cfg.output_dir = Path(tmp) / "out"
    cfg.inbox_dir = Path(tmp) / "inbox"
    cfg.cache_dir = Path(tmp) / "cache"
    return srv_mod.Library(cfg, **kw)


def t_reset_answers_after_wiping():
    """Resetting the library has to answer, not 500 after the damage is done.

    The caches it clears were never created, so the wipe happened and then the
    request failed -- the page reported an error for something that had worked.
    The import worker's `finally` tripped over the same missing attribute.
    """
    with tempfile.TemporaryDirectory() as tmp:
        app = _server_library(tmp)
        with _closing(app):
            _stocked(app.library(), "S", laps=3)
            assert app.library().summary()["laps"] == 3
            got = app.reset_library()
            assert got["library"]["laps"] == 0, got
            assert got["cleared"]["laps"] == 3, got
            # what the import worker's `finally` does, by hand
            with app._lock:
                app._listings.clear()
        return "3 laps cleared, the reset answered, the worker's cleanup runs"


def main():
    if "--list" in sys.argv:
        _WANTED.append("--none--")       # matches no area: names only
    print("source: %s   maps: %s" % (SOURCE, CONFIG["maps_dir"]))
    area("config")
    check("CONFIG validates", t_config_loads)
    check("misspelled key rejected", t_unknown_key_rejected)
    check("out-of-range value rejected", t_out_of_range_rejected)
    check("unimplemented stub rejected", t_stub_element_rejected)
    check("inverted range rejected", t_inverted_range_rejected)
    area("elements")
    check("all enabled elements build", t_all_elements_build)
    check("disabling one is clean", t_disable_is_clean)
    check("moving one disturbs nothing", t_move_does_not_disturb)
    check("layout is resolution independent", t_resolution_independent)
    check("every element fits in frame", t_elements_stay_in_frame)
    area("channels")
    check("missing OBD block is handled", t_missing_obd_block_names_the_channel)
    check("wrong units raise", t_bad_units_raise)
    check("bearing interpolates on the circle", t_bearing_wraps_correctly)
    area("map and laps")
    check("map transform is fixed", t_map_transform_is_fixed)
    check("marker returns to its start", t_marker_returns_to_start)
    check("start/finish recovered", t_start_finish_recovered)
    check("fragment gaps are not bridged", t_resample_respects_fragments)
    area("trim, segments and targets")
    check("trim never cuts driving", t_trim_keeps_all_driving)
    check("trim can be disabled", t_trim_disabled_keeps_everything)
    check("dead speed channel is safe", t_trim_survives_a_dead_speed_channel)
    check("segments tile the window", t_segments_cover_the_window)
    check("lap across a split makes 2 parts", t_boundary_straddling_lap_splits)
    check("lap names are valid filenames", t_lap_names_are_safe_filenames)
    check("one output per session", t_one_output_per_session)
    check("encoder args are consistent", t_encoder_args_are_consistent)
    area("frame hygiene")
    check("no residue between frames", t_no_residue_between_frames)
    check("an empty overlay is refused", t_empty_overlay_is_refused)
    check("frame is premultiplied", t_frame_is_premultiplied)
    check("bboxes are big enough (unclipped)", t_bbox_is_big_enough)
    check("marker stays on the map", t_marker_stays_on_the_map)
    area("gauge row")
    check("row is symmetric about centre", t_row_is_symmetric)
    check("throttle rescales to 0-100", t_throttle_rescale)
    check("throttle rails match the data", t_throttle_matches_the_data)
    check("no gauge erases another", t_no_gauge_erases_another)
    area("overlay contract")
    check("atlas is identical to full frame", t_atlas_matches_full_frame)
    check("region plan is sane", t_region_plan_is_sane)
    check("filter graph shape", t_filter_graph_shape)
    check("overlay mode auto-resolves", t_overlay_mode_auto_resolves)
    area("imu and channel binding")
    check("no duplicate columns", t_no_duplicate_columns)
    check("unknown device block is named", t_unknown_device_block_is_named)
    check("continuous channels interpolate", t_continuous_channels_interpolate)
    check("calibration solves", t_imu_calibration_solves)
    check("signs are physical", t_imu_signs_are_physical)
    check("IMU beats calc against physics", t_imu_beats_calc)
    check("calibration is per run", t_imu_calibration_is_per_run)
    check("untrusted calibration flagged", t_untrusted_calibration_is_flagged)
    check("channel preference binds", t_channel_preference_binds)
    area("course maps")
    check("course map is open", t_course_map_is_open)
    check("course spacing is uniform", t_course_spacing_is_uniform)
    check("open smoothing does not wrap", t_savgol_open_does_not_wrap)
    check("course map tracks the runs", t_course_map_tracks_the_runs)
    check("projection beats fraction-of-length", t_projection_beats_normalised_distance)
    check("circuit map aligns by projection", t_circuit_alignment_uses_projection)
    area("session model")
    check("model is JSON-safe", t_session_model_is_json_safe)
    check("times are run-relative", t_session_times_are_run_relative)
    check("channels are real", t_session_channels_are_real)
    check("decimation keeps the peaks", t_trace_keeps_the_peaks)
    check("track uses the map transform", t_track_uses_the_map_transform)
    check("lap flags agree with the filter", t_lap_flags_agree_with_the_filter)
    check("summary matches the full model", t_session_summary_matches_the_full_model)
    check("overlay payload is self-describing", t_overlay_payload_is_self_describing)
    area("dataset shapes")
    check("every session classifies", t_every_session_classifies)
    check("laps are corroborated", t_laps_are_corroborated)
    check("capabilities match reality", t_capabilities_match_reality)
    check("run detection needs no tuned constant",
          t_run_detection_needs_no_tuned_constant)
    check("walking is not a run", t_walking_is_not_a_run)
    check("the matcher is forward only", t_matcher_is_forward_only)
    check("fixture shapes are declared", t_fixture_shapes_are_declared)
    area("comparison")
    check("centreline distance is the coordinate", t_compare_uses_centreline_distance)
    check("distance is monotonic", t_compare_distance_is_monotonic)
    check("delta matches the times", t_compare_delta_matches_the_times)
    check("sectors add up", t_compare_sectors_add_up)
    check("grid starts at zero", t_compare_grid_starts_at_zero)
    check("mixed venues are refused", t_compare_refuses_mixed_venues)
    check("open courses compare", t_compare_handles_open_courses)
    check("GPS gaps are declared", t_compare_reports_gps_gaps)
    check("losses localise the time", t_compare_losses_localise_the_time)
    area("identity and library")
    check("identity is shape and direction", t_identity_is_shape_and_direction)
    check("renaming detaches nothing", t_library_rename_detaches_nothing)
    check("a failed write does not lock the library",
          t_a_failed_write_does_not_lock_the_library)
    check("deleting a lap keeps the map", t_library_delete_lap_keeps_the_map)
    check("ingest reads a folder in place", t_ingest_reads_a_folder_in_place)
    check("import verifies before saying safe", t_import_verifies_before_saying_it_is_safe)
    check("ingest groups by track", t_ingest_plan_groups_by_track)
    check("ingest skips duplicates", t_ingest_skips_duplicates_and_slices_by_shape)
    check("a lap records its video window", t_lap_video_window_is_recorded)
    check("an ingested lap stands alone", t_ingested_lap_stands_alone)
    area("storage")
    check("an older library migrates forward", t_library_migrates_forward)
    check("verify finds drift", t_library_verify_finds_drift)
    check("merge and split resolve identity", t_library_merge_and_split)
    check("bulk selection is not deletion", t_library_bulk_selection)
    check("video deletes separately", t_library_video_deletes_separately)
    check("import filters mark, not remove", t_ingest_options_filter_without_removing)
    check("apply honours the filters", t_ingest_apply_honours_the_filters)
    check("a second import of the same folder is a no-op",
          t_importing_the_same_folder_twice_changes_nothing)
    check("an unreadable file blocks \"safe to delete\"",
          t_verify_refuses_when_a_file_could_not_be_read)
    area("job api")
    check("plan structure", t_job_plan_structure)
    check("report is a view of the plan", t_job_plan_matches_text_report)
    check("probe finds a usable encoder", t_job_probe)
    area("environment and encoders")
    check("LAPVIEW_* overrides config", t_env_overrides_config)
    check("pascal arguments are safe", t_pascal_args_are_safe)
    check("the encoder falls back", t_encoder_falls_back)
    check("the real probe settles", t_real_probe_settles)
    area("web server")
    check("a reset answers after wiping", t_reset_answers_after_wiping)

    print()
    if _TIMING:
        for name, took in sorted(_TIMES.items(), key=lambda kv: -kv[1]):
            print("  %5.1fs  %s" % (took, name))
        print("  %5.1fs  total" % sum(_TIMES.values()))
    if _skipped:
        print("%d skipped (data not present): %s"
              % (len(_skipped), ", ".join(_skipped)))
    if _failures:
        print("%d failure(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    if "--list" in sys.argv:
        print("areas: %s" % ", ".join(_AREAS))
        return 0
    if _WANTED:
        ran = [a for a in _AREAS if any(w in a.lower() for w in _WANTED)]
        print("all checks passed in %s" % (", ".join(ran) or "(no matching area)"))
        return 0
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
