"""Cut the test-data set in `tests/data/` from real sessions.

Why a separate directory at all: `tools/selftest.py` used to read whatever was in
`Raw Data/`, so the suite's coverage moved whenever the working data changed.
When the AMP sessions were swapped for autocross runs, lap slicing, lap export,
start/finish recovery, the camera-split join and circuit map building all
silently stopped being tested. Tests need a fixed input.

Why the telemetry is real rather than synthetic: synthetic telemetry would encode
what this code *believes* about the format, and most of what was worth learning
here came from the data disagreeing -- the undocumented device row, an accuracy
channel that floors at exactly 10.0, `bearing` interpolated up from about 1 Hz,
`calc` channels spiking to 5.93 G, a sensor frame that turned out to be reflected
rather than rotated. A generated file would only ever confirm the assumptions.

Why the *video* is generated: the clips cannot be shipped -- the circuit ones are
2 GB each -- and the split tests do not care what the picture shows. They
exercise timing metadata: `creation_time`, duration, frame rate, and where a lap
falls relative to a split. Generating them is better than using real footage,
because the exact adversarial case can be built on purpose: a split landing
mid-lap with a realistic inter-file gap.

    python tools/make_test_data.py            # cut everything
    python tools/make_test_data.py --clips    # just regenerate the clips
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                              # noqa: E402

from racechrono import (csv_io, laps as laps_mod, runs as runs_mod,  # noqa: E402
                        trackmap, video)

OUT = ROOT / "tests" / "data"

CIRCUIT = "session_20260819_165433_amp_full_v3.csv"
# Three traversals, not one. The open-course map is a median across runs, and
# the alignment check compares projection against fraction-of-length -- both are
# meaningless with a single pass, so the fixture has to carry a small field of
# them. These are the three smallest of the ten autocross sessions.
AUTOCROSS = ("session_20260822_155808_grip_8-22_lap1_v3.csv",
             "session_20260822_164207_grip_8-22_lap1_v3.csv",
             "session_20260822_123245_grip_8-22_lap1_v3.csv")
# A session holding two runs with staging between them, from a course whose
# staging area sits beside the *finish*. That geometry is what broke the
# distance matcher: the first sample of a run projected to 1097 m of a 1107 m
# line, and a running-maximum monotonicity fix pinned the whole run there. It is
# in the fixture set so that failure cannot come back unnoticed.
MULTIRUN = "session_20260809_121241_scca_autocross_v3.csv"
MULTIRUN_FIXTURE = "course_2runs.csv"
CIRCUIT_FIXTURE = "circuit_5laps.csv"
AUTOCROSS_FIXTURE = "autocross_run%d.csv"
CLIP_A, CLIP_B = "split_a.mp4", "split_b.mp4"
MAPS = ROOT / "tests" / "maps"

# Enough complete laps that the edge-lap filter still leaves several: it drops
# the first and last of each run, so five in means three usable.
LAPS_WANTED = 5
PAD_S = 10.0
GAP_S = 1.5          # what a real camera leaves between chaptered files
CLIP_PAD_S = 12.0    # footage either side of the straddled lap


def _rows(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return list(csv.reader(f))


def _header_index(rows):
    for i, r in enumerate(rows):
        if r and r[0].strip().lower() == "timestamp":
            return i
    raise SystemExit("%s: no header row" % path)


def slice_csv(src, dst, t0, t1, note=None):
    """Copy a session keeping only rows inside [t0, t1], structure intact.

    The preamble, column, unit and device rows are carried over untouched, so the
    fixture is the same format the loader has to cope with -- including the
    device row that is not in the published spec.
    """
    rows = _rows(src)
    hi = _header_index(rows)
    head = rows[:hi + 3]
    kept = []
    for r in rows[hi + 3:]:
        if not r or not r[0]:
            continue
        try:
            t = float(r[0])
        except ValueError:
            continue
        if t0 <= t <= t1:
            kept.append(r)
    if note:
        head = [r[:] for r in head]
        for r in head:
            if r and r[0].strip() == "Note":
                r[1] = note
                break
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerows(head)
        w.writerows(kept)
    return len(kept)


def cut_circuit(data_dir, verbose=True):
    src = data_dir / CIRCUIT
    if not src.exists():
        print("  ! %s not in %s; skipping the circuit fixture" % (CIRCUIT, data_dir))
        return None
    run = csv_io.read_run(src)
    lp = laps_mod.laps(run)
    if len(lp) < LAPS_WANTED:
        print("  ! %s has only %d complete laps" % (src.name, len(lp)))
        return None
    # Skip the first lap: it is the out-lap's neighbour and often untypical.
    chosen = lp[1:1 + LAPS_WANTED]
    t0 = chosen[0].t0 - PAD_S
    t1 = chosen[-1].t1 + PAD_S
    dst = OUT / CIRCUIT_FIXTURE
    n = slice_csv(src, dst, t0, t1,
                  note="test fixture: %d laps cut from %s" % (len(chosen), src.name))
    if verbose:
        print("  %s: %d rows, %.1f s, %d laps (%s), %.1f MB"
              % (dst.name, n, t1 - t0, len(chosen),
                 ", ".join("%.1fs" % l.duration for l in chosen),
                 dst.stat().st_size / 1e6))
    return dst


def cut_autocross(data_dir, verbose=True):
    made = []
    for i, name in enumerate(AUTOCROSS, start=1):
        src = data_dir / name
        if not src.exists():
            print("  ! %s not in %s; skipping it" % (name, data_dir))
            continue
        run = csv_io.read_run(src)
        dst = OUT / (AUTOCROSS_FIXTURE % i)
        n = slice_csv(src, dst, run.t0, run.t1,
                      note="test fixture: whole run from %s" % src.name)
        made.append(dst)
        if verbose:
            print("  %s: %d rows, %.1f s, raw IMU present, %.1f MB"
                  % (dst.name, n, run.duration, dst.stat().st_size / 1e6))
    if len(made) < 3:
        print("  ! only %d autocross traversal(s); the course-alignment checks "
              "need three" % len(made))
    return made


def cut_multirun(data_dir, verbose=True):
    """One session with two runs in it, trimmed to just around them."""
    src = data_dir / MULTIRUN
    if not src.exists():
        print("  ! %s not in %s; skipping the multi-run fixture"
              % (MULTIRUN, data_dir))
        return None
    run = csv_io.read_run(src, derive=False)
    spans = runs_mod.detect(run)
    if len(spans) < 2:
        print("  ! %s has %d run(s); the fixture needs two"
              % (src.name, len(spans)))
        return None
    t0 = spans[0].t0 - PAD_S
    t1 = spans[-1].t1 + PAD_S
    dst = OUT / MULTIRUN_FIXTURE
    n = slice_csv(src, dst, t0, t1,
                  note="test fixture: %d runs cut from %s" % (len(spans), src.name))
    if verbose:
        print("  %s: %d rows, %.1f s, %d runs (%s), %.1f MB"
              % (dst.name, n, t1 - t0, len(spans),
                 ", ".join("%.0fm" % s.distance_m for s in spans),
                 dst.stat().st_size / 1e6))
    return dst


def make_clips(cfg_ffmpeg=None, verbose=True):
    """Two clips whose split lands mid-lap on the circuit fixture.

    Content is a test pattern. What matters is that clip A ends partway through a
    lap and clip B picks up `GAP_S` later, so a lap target has to be rendered as
    two segments and joined.
    """
    fixture = OUT / CIRCUIT_FIXTURE
    if not fixture.exists():
        print("  ! %s missing; cut it before the clips" % CIRCUIT_FIXTURE)
        return None
    run = csv_io.read_run(fixture)
    lp = laps_mod.laps(run)
    if len(lp) < 3:
        print("  ! the circuit fixture has too few laps for a split")
        return None

    # Put the split in the middle of one lap, and cover only that lap plus a
    # little either side. Footage covering just part of a session is realistic,
    # and it keeps the fixtures small.
    target = lp[len(lp) // 2]
    split_at = target.t0 + target.duration * 0.5
    start = max(run.t0 + 1.0, target.t0 - CLIP_PAD_S)
    a_dur = split_at - start
    b_start = split_at + GAP_S
    b_dur = min(run.t1 - 1.0, target.t1 + CLIP_PAD_S) - b_start
    if a_dur < 5 or b_dur < 5:
        print("  ! not enough room either side of the split")
        return None

    exe = video.find_binary("ffmpeg", cfg_ffmpeg)
    made = []
    for name, t_start, dur, colour in ((CLIP_A, start, a_dur, "0x1d2b3a"),
                                       (CLIP_B, b_start, b_dur, "0x2a1d3a")):
        dst = OUT / name
        stamp = dt.datetime.fromtimestamp(t_start, dt.UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        # A flat colour, not a test pattern: the picture is irrelevant here and
        # a high-entropy source made these 86 MB apiece.
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i",
               "color=c=%s:size=1920x1080:rate=60:duration=%.3f" % (colour, dur),
               "-f", "lavfi", "-i",
               "sine=frequency=440:sample_rate=48000:duration=%.3f" % dur,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "34",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
               "-metadata", "creation_time=%s" % stamp,
               "-movflags", "+faststart", str(dst)]
        subprocess.run(cmd, check=True, capture_output=True)
        made.append(dst)
        if verbose:
            print("  %s: %.1f s, starts %s, %.1f MB"
                  % (dst.name, dur, stamp, dst.stat().st_size / 1e6))
    if verbose:
        print("  split falls %.1f s into lap %d (%.1f s long), %.1f s gap"
              % (split_at - target.t0, target.number, target.duration, GAP_S))
    return made


def make_maps(verbose=True):
    """Build both map kinds from the fixtures, into `tests/maps/`.

    The suite must not read the working `Maps/` directory: those are rebuilt from
    whatever is in `Raw Data/` and their point counts, extents and start/finish
    traps move with it. A map is an input to the marker-stability checks, so it
    has to be pinned like any other fixture.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "genmap", ROOT / "tools" / "generate_map.py")
    genmap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(genmap)

    MAPS.mkdir(parents=True, exist_ok=True)
    made = []
    jobs = [("circuit", ["--runs", str(OUT / CIRCUIT_FIXTURE), "--circuit",
                         "--name", "Test Circuit",
                         "--out", str(MAPS / "test_circuit.json")]),
            ("course", ["--runs", str(OUT / "autocross_run*.csv"), "--course",
                        "--name", "Test Course",
                        "--out", str(MAPS / "test_course.json")]),
            # Two runs of one session are enough to average a course, and this
            # is the venue whose staging sits by the finish.
            ("multi-run course",
             ["--runs", str(OUT / MULTIRUN_FIXTURE), "--course",
              "--name", "Test Course Two", "--points", "480",
              "--out", str(MAPS / "test_course2.json")])]
    for kind, argv in jobs:
        target = Path(argv[-1])
        try:
            genmap.main(argv)
        except SystemExit as exc:
            if exc.code:
                print("  ! %s map failed (exit %s)" % (kind, exc.code))
                continue
        except Exception as exc:
            print("  ! %s map failed: %s: %s" % (kind, type(exc).__name__, exc))
            continue
        if not target.exists():
            print("  ! %s map produced nothing" % kind)
            continue
        tm = trackmap.load(target)
        made.append(target)
        if verbose:
            print("  %s: %.0f m, %d points, %d lap(s), %s"
                  % (target.name, tm.length_m, len(tm.points_unit), tm.lap_count,
                     "closed" if tm.closed else "open"))
    return made


def manifest():
    """Pair the fixture CSVs with the fixture clips explicitly.

    The clips carry a `creation_time` that overlaps the circuit fixture, so
    timestamp pairing would find them anyway -- but naming it removes any doubt
    about which run the split test is exercising.
    """
    path = OUT / "runs.json"
    import json
    path.write_text(json.dumps(
        {Path(CIRCUIT_FIXTURE).stem: [CLIP_A, CLIP_B]}, indent=1), encoding="utf-8")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="Raw Data")
    ap.add_argument("--clips", action="store_true",
                    help="only regenerate the video clips")
    args = ap.parse_args(argv)
    data_dir = ROOT / args.data_dir

    OUT.mkdir(parents=True, exist_ok=True)
    print("writing to %s" % OUT)
    if not args.clips:
        cut_circuit(data_dir)
        cut_autocross(data_dir)
        cut_multirun(data_dir)
    make_clips()
    print("  %s" % manifest().name)
    if not args.clips:
        make_maps()

    files = [p for d in (OUT, MAPS) if d.exists()
             for p in d.iterdir() if p.is_file()]
    print("test data: %.1f MB across %d file(s)"
          % (sum(p.stat().st_size for p in files) / 1e6, len(files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
