"""LapVIEW -- entry point and user-editable CONFIG.

Edit CONFIG below to change layout, channels and encoding. Everything in it is
validated on load by config.py, so a typo is reported rather than ignored.

    python main.py                          # one trimmed video per session
    python main.py --run session_..._155859 # one session
    python main.py --export laps            # one video per lap instead
    python main.py --export both            # sessions and laps
    python main.py --preview 20 --at 300    # 20 s, 300 s into the output
    python main.py --sync-check             # contact sheet, no render
    python main.py --no-trim                # keep the dead air at each end
    python main.py --concat                 # join the sessions into one file
    python main.py --dry-run                # pair, sync, report; render nothing

Each session's camera splits are joined automatically, so one CSV in Raw Data
produces exactly one video -- the 2 GB splits are an artefact of the recorder,
not something anyone wants to watch as separate files.

Geometry is in fractions of frame height, except horizontal anchors which are
fractions of frame width, so the layout survives a resolution change.
"""
from __future__ import annotations

CONFIG = {
    "maps_dir":      "Maps",
    "library_dir":   "library",   # the lap library ingest writes to
    "output_dir":    "Output",
    "cache_dir":     "cache",

    "map_file": None,              # None = auto-select by location
    "manifest": "runs.json",       # optional CSV-stem -> clip-list mapping
    "workers":  None,              # None = min(cpu_count - 1, n_clips)

    # The reference channel is chosen automatically: throttle_pos first, since
    # audio energy follows engine load with no intrinsic lag. Correlating
    # against speed instead would bake the throttle->speed delay (about +3.5 s
    # on this car) into the overlay as a bogus clock offset.
    "sync":    {"strategy": "audio_rpm", "search_window_s": 10.0,
                "min_peak_ratio": 1.5, "min_prominence": 3.5,
                "highpass_s": 30.0, "ref_channel": "auto", "consensus": True},
    "quality": {"max_accuracy_m": 10.0, "dim_alpha": 0.4},

    # When a session carries a raw accelerometer and gyroscope, the sensor axes
    # are rotated into the car's frame using a calibration solved per run: up
    # from gravity during the standstill every run starts with, gyro bias from
    # the same window, and the remaining horizontal rotation by least squares
    # against dv/dt and v*yaw. Nothing here is car-specific.
    "imu": {"enabled": True, "lowpass_hz": 5.0, "min_fit_r": 0.45},

    # One output per session, trimmed to the driving. The window is the first
    # and last time the car exceeds `move_threshold_ms`, padded either side.
    "trim":    {"enabled": True, "move_threshold_ms": 1.0,
                "pre_s": 5.0, "post_s": 5.0, "lap_buffer_s": 3.0},

    # hevc_nvenc at cq 26 with the tuned settings below was measured at
    # 15.0 Mbps / 42.1 dB PSNR against a visually lossless reference on a fast
    # lap -- 2.5x smaller than h264 cq21 and indistinguishable at 1:1. See the
    # README for the full curve. Switch to "h264_nvenc" for maximum
    # compatibility, or "libx265"/"libx264" (which use `crf`) for a CPU encode.
    "encode":  {"encoder": "hevc_nvenc", "cq": 26, "preset": "p7",
                "tune": "hq", "multipass": "disabled", "rc_lookahead": 32,
                "spatial_aq": True, "temporal_aq": True, "aq_strength": 8,
                "bframes": 3, "b_ref_mode": "middle", "maxrate": None,
                "crf": 20, "hwaccel_decode": True, "copy_audio": True,
                "audio_bitrate": "192k", "fps": None},

    "ffmpeg":  {"ffmpeg": None, "ffprobe": None},   # None = PATH, then fallbacks

    "elements": {
        "track_map": {
            "enabled": True, "anchor": "top-left",
            "x": 0.03, "y": 0.02, "height": 0.25,
            "stroke": 0.004, "outline": 0.0015,
            "color": (1, 1, 1, 1), "outline_color": (0, 0, 0, 1),
            "marker": {"color": (0.9, 0.1, 0.1, 1), "size": 0.018},
            # The map is the racing line, so the pit lane and paddock project
            # outside it. Pinning the marker to the nearest edge keeps a whole
            # arrow on the map; set False to let it slide off instead.
            "clamp_marker": True,
            "show_start_finish": False,
        },
        # The row is gmeter - speedo - tacho - throttle, which is symmetric,
        # so the frame's centreline belongs *between* the speedo and the tacho
        # rather than through the speedo. With x = None the speedo is placed
        # relative to frame centre in its own diameters; everything else is
        # placed relative to the speedo, so the group stays symmetric at any
        # resolution or aspect ratio.
        #   speedo   0.5W - 0.65 D        tacho     0.5W + 0.65 D
        #   gmeter   0.5W - 1.60 D        throttle  0.5W + 1.60 D
        "speedo": {
            "enabled": True, "anchor": "bottom-center",
            "x": None, "offset_d": -0.65,
            "y": 0.98, "diameter": 0.26,
            "channel": "speed_gps", "unit": "mph",
            "min": 0, "max": 120, "major": 20, "minor": 5,
            "backing_alpha": 0.35, "sweep_deg": 270,
        },
        "tacho": {
            "enabled": True, "anchor": "bottom-center",
            "x": None, "offset_d": 1.30,     # relative to speedo, in diameters
            "y": 0.98, "diameter": 0.26,
            "channel": "rpm_obd", "unit": "rpm",
            "min": 0, "max": 7000, "major": 1000, "minor": 250,
            "label_divisor": 1000,
            "backing_alpha": 0.35, "sweep_deg": 270,
            "redline": None,                 # e.g. 6200 to draw the arc
        },
        "gmeter": {
            "enabled": True, "anchor": "bottom-center",
            "x": None, "offset_d": -0.95,
            "y": 0.98, "diameter": 0.13,
            "rings": [1, 2],
            # Prefer the calibrated IMU, fall back to RaceChrono's GPS-derived
            # `calc` channels. The fallback matters: the AMP sessions predate the
            # phone IMU being logged. But where both exist the IMU wins clearly
            # -- on autocross the `calc` channels spike to 5.93 G in runs whose
            # real peak is 1.3 G, because differentiating 1 Hz GPS cannot follow
            # a slalom, while the IMU tracks physics at r = 0.99 lateral.
            "lateral_channel": ["imu_lateral_acc", "lateral_acc"],
            "longitudinal_channel": ["imu_longitudinal_acc", "longitudinal_acc"],
            "magnitude_channel": ["imu_combined_acc", "combined_acc"],
            # The calibration resolves sign itself, so no inversion is needed
            # for IMU channels. `calc` needs lateral inverted: measured across
            # the AMP sessions, lateral_acc = -1.00 * (v * omega / g) with
            # r = -0.94..-0.99, i.e. positive means a LEFT turn.
            "invert_lateral": False,
            "invert_longitudinal": False, "trail_s": 0.0,
            "backing_alpha": 0.0,
        },
        "throttle": {
            "enabled": True, "anchor": "bottom-center",
            "x": None, "offset_d": 2.25,     # mirrors the G-meter about centre
            "y": 0.98, "diameter": 0.13,
            "channel": "throttle_pos", "label": "throttle",
            # Only 0/50/100 are labelled: five numerals crowd the ticks
            # on a dial this small.
            "min": 0, "max": 100, "major": 50, "minor": 5,
            # The pedal sender does not use its full range. Measured over all
            # four sessions (203k samples) it rests at 11.37 and pegs at 85.49,
            # both hard plateaus -- p95 through p99.9 are all exactly 85.49.
            # These two rails rescale the channel linearly onto 0-100%.
            "input_min": 11.37, "input_max": 85.49,
            "backing_alpha": 0.35, "sweep_deg": 270,
        },

        # Registered but not implemented in v1. The reference image shows lap
        # timer boxes across the top; the slot is reserved, nothing is drawn.
        "lap_timer":    {"enabled": False},
        "sector_delta": {"enabled": False},
        "speed_bar":    {"enabled": False},
        "coolant_temp": {"enabled": False},
    },
}

# ---------------------------------------------------------------------------

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_mod                                    # noqa: E402
from racechrono import (csv_io, laps as laps_mod, sync, trackmap, video,  # noqa: E402
                        window)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Composite a RaceChrono-style overlay onto track footage.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source",
                    help="a folder of sessions and footage to render from; "
                         "the lap library is imported with tools/ingest.py and "
                         "played in the web app")
    ap.add_argument("--run", action="append", default=None,
                    help="CSV stem to process (repeatable); default is all")
    ap.add_argument("--dry-run", action="store_true",
                    help="pair, sync and report; render nothing")
    ap.add_argument("--sync-check", action="store_true",
                    help="write a contact sheet of overlay-burned frames")
    ap.add_argument("--preview", type=float, default=None, metavar="SECONDS",
                    help="render only this many seconds")
    ap.add_argument("--at", type=float, default=0.0, metavar="SECONDS",
                    help="preview start, seconds into the run's data")
    ap.add_argument("--export", default="sessions",
                    choices=("sessions", "laps", "both"),
                    help="what to write: one file per session (default), one "
                         "file per lap, or both")
    ap.add_argument("--no-trim", action="store_true",
                    help="do not trim to the driving; render the whole session")
    ap.add_argument("--lap", action="append", type=int, default=None,
                    metavar="N", help="only export these lap numbers "
                                      "(repeatable); implies --export laps")
    ap.add_argument("--lap-buffer", type=float, default=None, metavar="SECONDS",
                    help="padding either side of an exported lap (default 3)")
    ap.add_argument("--keep-parts", action="store_true",
                    help="keep the per-clip parts that get joined, for debugging")
    ap.add_argument("--concat", action="store_true",
                    help="join the selected sessions' finished outputs into one "
                         "file, in chronological order")
    ap.add_argument("--map", default=None, help="override map .json")
    ap.add_argument("--offset", type=float, default=None,
                    help="manual sync offset in seconds, applied to every clip")
    ap.add_argument("--strategy", default=None,
                    choices=("container", "audio_rpm", "audio_accel", "manual"))
    ap.add_argument("--refresh-sync", action="store_true",
                    help="recompute auto sync, ignoring cached auto offsets "
                         "(a cached manual offset still wins)")
    ap.add_argument("--clear-sync", action="store_true",
                    help="forget cached offsets for the selected clips, "
                         "including hand-entered ones, and sync from scratch")
    ap.add_argument("--encoder", default=None,
                    choices=("libx264", "libx265", "h264_nvenc", "hevc_nvenc"))
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--overwrite", action="store_true")
    return ap.parse_args(argv)


def apply_overrides(cfg, args):
    if args.strategy:
        cfg.sync.strategy = args.strategy
    if args.offset is not None:
        cfg.sync.offset = args.offset
        cfg.sync.strategy = "manual"
    if args.refresh_sync:
        cfg.sync.refresh = True
    if args.encoder:
        cfg.encode.encoder = args.encoder
    if args.workers:
        cfg.workers = args.workers
    if args.out_dir:
        cfg.output_dir = Path(args.out_dir)
    if args.map:
        cfg.map_file = Path(args.map)
    if args.lap and args.export == "sessions":
        args.export = "laps"
    if args.no_trim:
        cfg.trim.enabled = False
    if args.lap_buffer is not None:
        cfg.trim.lap_buffer_s = args.lap_buffer
    cfg.trim.validate()
    cfg.sync.validate()
    cfg.encode.validate()
    return cfg


def find_runs(cfg, source, wanted=None):
    paths = sorted(Path(source).rglob("*.csv"))
    if wanted:
        keep = []
        for w in wanted:
            hits = [p for p in paths if p.stem == w or w in p.stem]
            if not hits:
                print("  ! no CSV matching %r" % w)
            keep.extend(hits)
        paths = sorted(set(keep))
    runs = []
    for p in paths:
        try:
            runs.append(csv_io.read_run(p))
        except Exception as exc:
            print("  ! %s: %s" % (p.name, exc))
    return runs


def select_map(cfg, run):
    if cfg.map_file:
        return trackmap.load(cfg.map_file), 0.0
    lat, lon = run.mean_latlon(cfg.quality.max_accuracy_m)
    tm, dist = trackmap.auto_select(cfg.maps_dir, lat, lon)
    return tm, dist


def prepare(cfg, args):
    """Load runs, pair clips, resolve sync. Shared by every mode."""
    runs = find_runs(cfg, args.source, args.run)
    if not runs:
        raise SystemExit("no runs loaded from %s" % args.source)

    clips = video.discover(args.source, cfg.ffmpeg.ffprobe, recursive=True)
    manifest = video.load_manifest(Path(args.source) / Path(cfg.manifest).name)
    pairings, orphans = video.pair(runs, clips, manifest)

    cache = sync.load_cache(Path(cfg.cache_dir) / "sync.json")
    if args.clear_sync:
        for p in pairings:
            for clip in p.clips:
                if cache.pop(sync.cache_key(clip), None) is not None:
                    print("  cleared cached sync for %s" % clip.path.name)
    by_stem = {r.stem: r for r in runs}
    jobs = []
    for p in pairings:
        run = by_stem[p.run_stem]
        if not p.clips:
            print("  ! %s: no clips matched -- skipping this run" % run.stem)
            continue
        video.check_uniform(p.clips)
        print("  %s: syncing %d clip(s)" % (run.stem, len(p.clips)))
        for clip in p.clips:
            print("    %s" % clip.path.name)
            sync.sync_clip(clip, run, cfg.sync.as_dict(), cache,
                           cfg.ffmpeg.ffmpeg, verbose=True)
        if cfg.sync.consensus and len(p.clips) > 1:
            sync.reconcile_run(p.clips, cfg.sync.min_prominence)
        for clip in p.clips:
            cache[sync.cache_key(clip)] = sync._entry(clip)
        jobs.append((run, p))
    sync.save_cache(cache, Path(cfg.cache_dir) / "sync.json")
    return jobs, orphans


def _job_from_args(raw_config, args):
    """The CLI's arguments as a job document, so --dry-run and the JSON API run
    the exact same code path."""
    return {
        "config": raw_config,
        # The folder to render from. Without it `job.plan` refuses, which is
        # what --dry-run did after the fixed input directories were removed.
        "source": str(args.source),
        "select": {"runs": args.run, "export": args.export, "laps": args.lap},
        "preview": ({"seconds": args.preview, "at": args.at}
                    if args.preview else None),
        "overwrite": args.overwrite,
        "keep_parts": args.keep_parts,
    }


def main(argv=None):
    args = parse_args(argv)
    cfg = apply_overrides(config_mod.load(CONFIG), args)
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.cache_dir).mkdir(parents=True, exist_ok=True)

    if args.concat:
        from racechrono import render
        return render.concat_runs(cfg, args)

    jobs, orphans = prepare(cfg, args)
    if not jobs:
        raise SystemExit("nothing to do")

    if args.dry_run:
        from racechrono import job as job_mod
        print(job_mod.format_plan(job_mod.plan(_job_from_args(CONFIG, args))))
        return 0

    from racechrono import render
    if args.sync_check:
        return render.sync_check(cfg, jobs, args)
    return render.render_all(cfg, jobs, args)


if __name__ == "__main__":
    raise SystemExit(main())
