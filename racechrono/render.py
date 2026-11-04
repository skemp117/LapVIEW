"""Frame loop, ffmpeg pipe, compositing.

One ffmpeg process per clip: it decodes the source, receives a full-frame RGBA
overlay on a pipe, composites, encodes and copies the audio through untouched.

Frame *n* maps to data time ``clip.start_unix + offset + n / fps``, and the row
is looked up by integer index into a pre-resampled grid -- no per-frame search.
"""
from __future__ import annotations

import math
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cairo
import numpy as np

from . import csv_io, gauges, overlay as overlay_mod, video
from .gauges.base import Layout


# ---------------------------------------------------------------- ffmpeg

# The longest lookahead measured to open on a Pascal NVENC.
PASCAL_LOOKAHEAD = 20


def encoder_args(enc):
    """Video encoder settings.

    For NVENC the defaults are deliberately not the driver's: the slowest preset,
    a 32-frame lookahead, spatial *and* temporal AQ, and B-frames usable as
    references together save about 15% of bitrate at the same quality, which is
    what lets a higher `cq` -- a much smaller file -- look like a low one.

    `multipass` is deliberately off. Measured on this footage it changed neither
    size nor quality at constant `cq` (74.2 MB either way), while being the most
    encoder-expensive option available -- it earns its keep only when a bitrate
    target is being hit, i.e. alongside `maxrate`.

    The `pascal` NVENC profile (GTX 10-series and older) drops what that
    encoder cannot do and would refuse to open a session for. Measured on a GTX
    1070 Max-Q (driver 580): hevc_nvenc fails at init with -22 on any of
    `-temporal-aq 1`, `-bf 3` or `-b_ref_mode middle`, and is fine at p7, cq 26,
    spatial AQ and a 20-frame lookahead; h264_nvenc takes `-bf 3`. So HEVC gets
    none of the three, H.264 keeps its B-frames but not as references, and the
    lookahead stays within the 20 frames that were measured. See `encoders.py`.
    """
    if not enc.is_nvenc:
        return ["-preset", enc.preset, "-crf", str(enc.crf)]

    legacy = getattr(enc, "nvenc_profile", "auto") == "pascal"
    hevc = "hevc" in enc.encoder
    bframes = 0 if (legacy and hevc) else enc.bframes
    temporal = enc.temporal_aq and not (legacy and hevc)
    lookahead = min(enc.rc_lookahead, PASCAL_LOOKAHEAD) if legacy \
        else enc.rc_lookahead
    args = ["-preset", enc.preset, "-tune", enc.tune,
            "-rc", "vbr", "-cq", str(enc.cq), "-b:v", "0",
            "-multipass", enc.multipass,
            "-rc-lookahead", str(lookahead),
            "-spatial-aq", "1" if enc.spatial_aq else "0",
            "-temporal-aq", "1" if temporal else "0",
            "-aq-strength", str(enc.aq_strength),
            "-bf", str(bframes)]
    if bframes > 0 and not legacy:
        args += ["-b_ref_mode", enc.b_ref_mode]
    if enc.maxrate:
        args += ["-maxrate", enc.maxrate, "-bufsize", enc.maxrate]
    return args


def build_ffmpeg_cmd(clip, out_path, cfg, fps, width, height, duration=None,
                     seek=None, comp=None):
    """Decode source, take the overlay on stdin, composite, encode.

    Cairo's ARGB32 is premultiplied; ffmpeg's `bgra` is straight alpha. Feeding
    one as the other darkens every antialiased edge, so the overlay is
    un-premultiplied in the filter graph before it is composited.

    `comp` is the compositor: its atlas size sets the raw input geometry and its
    regions build the crop/overlay chain.
    """
    enc = cfg.encode
    exe = video.find_binary("ffmpeg", cfg.ffmpeg.ffmpeg)
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]

    if enc.hwaccel_decode and enc.encoder.endswith("nvenc"):
        cmd += ["-hwaccel", "cuda"]
    if seek:
        cmd += ["-ss", "%.3f" % seek]
    cmd += ["-i", str(clip.path)]

    ov_w, ov_h = ((comp.atlas_w, comp.atlas_h) if comp is not None
                  else (width, height))
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgra",
            "-s", "%dx%d" % (ov_w, ov_h), "-r", "%.6f" % fps, "-i", "-"]
    if duration:
        cmd += ["-t", "%.3f" % duration]

    # `fps=` forces CFR on the source before compositing. These recordings are
    # nominally 60 fps but their container average rate sits a few ppm below it,
    # and letting that through would slide the overlay against the picture over a
    # 20-minute clip. `eof_action=endall` stops at whichever input ends first, so
    # the source decides the tail rather than our frame count.
    if comp is not None:
        graph = overlay_mod.filter_graph(comp.regions, fps, width, height)
    else:
        graph = ("[0:v]fps=%.6f,scale=%d:%d,setsar=1[base];"
                 "[1:v]unpremultiply=inplace=1[ov];"
                 "[base][ov]overlay=0:0:format=auto:eof_action=endall[v]"
                 % (fps, width, height))
    cmd += ["-filter_complex", graph, "-map", "[v]"]

    if clip.has_audio:
        cmd += ["-map", "0:a:0"]
        if enc.copy_audio and not seek:
            cmd += ["-c:a", "copy"]
        else:
            # A copied AAC stream can only start on a frame boundary, so a
            # mid-clip seek leaves up to ~21 ms of A/V skew -- and that skew
            # would accumulate at every join when parts are concatenated.
            # Re-encoding makes each part exactly as long as it was asked to be.
            cmd += ["-c:a", "aac", "-b:a", enc.audio_bitrate]

    cmd += ["-c:v", enc.encoder] + encoder_args(enc)
    cmd += ["-pix_fmt", enc.pix_fmt]
    if "hevc" in enc.encoder or "265" in enc.encoder:
        # Players and YouTube both prefer hvc1 over ffmpeg's default hev1 tag.
        cmd += ["-tag:v", "hvc1"]
    cmd += list(enc.extra_args)
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


# ---------------------------------------------------------------- frames

class Compositor:
    """Full-frame compositor.

    Kept alongside the region path as the reference implementation: the region
    atlas has to produce identical pixels, and `tools/selftest.py` asserts that
    against this class. It is also the fallback for a layout whose elements
    cover most of the frame, where slicing the overlay up buys nothing.
    """

    def __init__(self, cfg, width, height, fps, trackmap):
        self.cfg = cfg
        self.width, self.height = int(width), int(height)
        self.frame_w, self.frame_h = self.width, self.height
        self.atlas_w, self.atlas_h = self.width, self.height
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                          self.width, self.height)
        self.ctx = cairo.Context(self.surface)
        self.layout = Layout(self.width, self.height)
        ctx = {"trackmap": trackmap, "fps": fps,
               "dim_alpha": cfg.quality.dim_alpha, "skipped": []}
        self.gauges = gauges.build(cfg, self.layout, ctx)
        self.skipped = ctx["skipped"]
        if not self.gauges:
            # The region path already refuses this; the full-frame path used to
            # accept it and quietly produce a plain re-encode with no overlay on
            # it at all. A config that enables nothing -- a mistyped element name,
            # a partial config dict where every element defaults to off -- has to
            # fail loudly, because an overlay-free video looks like a successful
            # render until someone watches it.
            raise ValueError(
                "no enabled elements, so there is no overlay to draw"
                + (" (skipped: %s)" % ", ".join(self.skipped) if self.skipped
                   else ""))
        self.regions = [overlay_mod.Region(0, 0, self.width, self.height,
                                           gauges=list(self.gauges))]
        # Warm every static layer before the first frame so the cost is not
        # charged to frame 0 (and so a layout error surfaces immediately).
        for g in self.gauges:
            g.static()

    @property
    def coverage(self):
        return 1.0

    @property
    def bytes_per_frame(self):
        return self.width * self.height * 4

    def describe(self):
        return {"frame": [self.width, self.height],
                "atlas": [self.width, self.height],
                "coverage": 1.0,
                "bytes_per_frame": self.bytes_per_frame,
                "regions": [r.as_dict() for r in self.regions]}

    @property
    def channels(self):
        out = set()
        for g in self.gauges:
            for c in g.channels:
                out.add(c)
        return sorted(out)

    def draw(self, row):
        # Clip every gauge to the rectangle it clears. Without this, anything a
        # gauge draws outside its own bbox is never cleared again and stays burnt
        # into the rest of the render -- which is exactly what happened to the
        # map marker in the pit lane, where the car's position projects outside
        # the racing-line map's extent.
        for g in self.gauges:
            x, y, w, h = g.bbox
            if w <= 0 or h <= 0:
                continue
            self.ctx.save()
            self.ctx.rectangle(x, y, w, h)
            self.ctx.clip()
            g.draw(self.ctx, row)
            self.ctx.restore()
        self.surface.flush()

    def buffer(self):
        return self.surface.get_data()


def make_compositor(cfg, width, height, fps, trackmap, for_preview=False):
    """Full frame for export, regions for preview.

    Both produce identical pixels -- `tools/selftest.py` asserts that, and an
    exported segment comes out bit-identical either way. They differ only in how
    much data leaves the process: 8.29 MB per frame versus 2.14 MB. That is the
    wrong trade for a local ffmpeg pipe (an extra `overlay` filter per region
    costs more than the bytes saved) and the right one for a preview client on
    the other end of a socket.
    """
    want = getattr(cfg.encode, "overlay_regions", False)
    if for_preview or want is True:
        return overlay_mod.RegionCompositor(cfg, width, height, fps, trackmap)
    return Compositor(cfg, width, height, fps, trackmap)


def row_source(run, cfg, fps, t_start, t_end, channels):
    """Pre-resample once, then hand out rows by integer index.

    Returns ``(lookup, grid_t0)`` where lookup(i) is a plain dict of floats.
    Building a small dict per frame is cheaper than any dataframe access, and it
    keeps the gauges free of pandas.
    """
    grid = csv_io.resample(run, fps, t_start, t_end,
                           cfg.quality.max_accuracy_m)
    cols = {}
    for name in channels:
        if name in ("good", "valid"):
            cols[name] = grid[name].to_numpy(dtype=bool)
            continue
        try:
            col = run.col(name)
        except KeyError:
            raise KeyError(
                "element wants channel %r, which this CSV does not have. "
                "Available: %s" % (name, ", ".join(run.df.columns))) from None
        cols[name] = grid[col].to_numpy(dtype=float)
    n = len(grid)
    t0 = float(grid["t"].iloc[0])

    def lookup(i):
        j = 0 if i < 0 else (n - 1 if i >= n else i)
        return {k: v[j] for k, v in cols.items()}

    return lookup, t0, n


# ---------------------------------------------------------------- one segment

def render_segment(cfg, run_path, segment, out_path, trackmap_path, quiet=False,
                   label=None, on_frame=None):
    """Render one segment of one clip. Runs in a worker, so it takes paths."""
    from . import trackmap as tm_mod

    run = csv_io.read_run(run_path, imu_opts=cfg.imu.as_kwargs()
                          if cfg.imu.enabled else None,
                          derive=cfg.imu.enabled)
    # Resolve channel preferences against this run's actual channel set.
    import config as config_mod
    cfg = config_mod.bind(cfg, run)
    tm = tm_mod.load(trackmap_path) if trackmap_path else None
    clip = segment.clip

    fps = float(cfg.encode.fps or clip.fps)
    width, height = clip.display_size
    tag = label or clip.path.name

    seek = segment.seek if segment.seek > 1e-6 else None
    duration = segment.duration

    # Overshoot the frame count by a couple of frames and let ffmpeg decide where
    # to stop. Rounding a container duration truncates the tail -- one clip here
    # reports 43.240 s while holding 2599 frames, i.e. 43.317 s at 60 fps -- and
    # `-t` plus `eof_action=endall` already bounds the output exactly. The pipe
    # closing under us is therefore the normal way this loop finishes, not an
    # error; the exit code decides that.
    n_frames = int(math.ceil(duration * fps)) + 2
    if n_frames <= 0:
        return {"clip": tag, "skipped": "zero frames"}

    comp = make_compositor(cfg, width, height, fps, tm)
    for note in getattr(comp, "skipped", []):
        if not quiet:
            print("      ! %s" % note, flush=True)
    # Frame 0 of the output is the frame at `seek` into the clip.
    frame0 = int(round((seek or 0.0) * fps))
    t_start = clip.frame_time(frame0)
    t_end = t_start + (n_frames + 1) / fps
    lookup, grid_t0, n_grid = row_source(run, cfg, fps,
                                         min(t_start, run.t0), max(t_end, run.t1),
                                         comp.channels)
    base = int(round((t_start - grid_t0) * fps))

    cmd = build_ffmpeg_cmd(clip, out_path, cfg, fps, width, height,
                           duration=duration, seek=seek, comp=comp)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    started = time.time()
    written = 0
    closed_early = False
    try:
        for f in range(n_frames):
            comp.draw(lookup(base + f))
            try:
                proc.stdin.write(comp.buffer())
            except (BrokenPipeError, OSError):
                # ffmpeg stopped reading because the source ran out. Whether
                # that is clean is decided by the exit code, not here.
                closed_early = True
                break
            written += 1
            # A caller with a progress bar hears about it a couple of times a
            # second; the terminal still only speaks once a minute.
            if on_frame is not None and f % max(1, int(fps / 2)) == 0:
                on_frame(f, n_frames)
            if not quiet and (f % (int(fps) * 60) == 0) and f:
                el = time.time() - started
                print("      %-34s %5.1f%%  %.2fx realtime"
                      % (tag, 100.0 * f / n_frames,
                         (f / fps) / max(el, 1e-6)), flush=True)
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    err = proc.stderr.read().decode("utf-8", "replace")
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(
            "ffmpeg exited %d for %s%s\n%s"
            % (rc, tag,
               " (pipe closed after %d frames)" % written if closed_early else "",
               err.strip()[-4000:]))

    elapsed = time.time() - started
    # Report what actually landed, not what was offered: the last frames are
    # deliberate overshoot.
    seconds = _probe_duration(cfg, out_path) or (written / fps)
    return {"clip": tag, "out": str(out_path), "frames": written,
            "seconds": seconds, "elapsed": elapsed,
            "speed": seconds / max(elapsed, 1e-6),
            "stderr": err.strip()}


# ---------------------------------------------------------------- one target

def render_target(cfg, run_path, target, segments, out_path, trackmap_path,
                  keep_parts=False, quiet=False):
    """Render every segment a target needs and join them into one file.

    The join is a lossless stream copy -- the parts come out of the same encoder
    with the same settings, so there is nothing to re-encode and no second
    generation of loss. Where a target needs only one segment (the common case)
    the segment is rendered straight to the destination and nothing is joined.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    if len(segments) == 1:
        res = render_segment(cfg, run_path, segments[0], out_path, trackmap_path,
                             quiet=quiet, label=target.name)
        res["target"] = target.name
        res["parts"] = 1
        return res

    parts_dir = Path(cfg.cache_dir) / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts, sub = [], []
    try:
        for i, seg in enumerate(segments):
            part = parts_dir / ("%s.part%02d.mp4" % (out_path.stem, i))
            sub.append(render_segment(
                cfg, run_path, seg, part, trackmap_path, quiet=quiet,
                label="%s [%d/%d]" % (target.name, i + 1, len(segments))))
            parts.append(part)
        concat_files(cfg, parts, out_path)
    finally:
        if not keep_parts:
            for p in parts:
                try:
                    p.unlink()
                except OSError:
                    pass

    seconds = _probe_duration(cfg, out_path) or sum(s["seconds"] for s in sub)
    elapsed = time.time() - started
    return {"clip": target.name, "target": target.name, "out": str(out_path),
            "frames": sum(s["frames"] for s in sub), "seconds": seconds,
            "elapsed": elapsed, "speed": seconds / max(elapsed, 1e-6),
            "parts": len(segments),
            "note": "joined %d camera splits" % len(segments)}


def concat_files(cfg, parts, out_path):
    """Lossless join of identically encoded parts, via the concat demuxer."""
    exe = video.find_binary("ffmpeg", cfg.ffmpeg.ffmpeg)
    listing = Path(cfg.cache_dir) / ("concat_%s.txt" % Path(out_path).stem)
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text("".join("file '%s'\n" % Path(p).resolve().as_posix()
                               for p in parts), encoding="utf-8")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-c", "copy", "-movflags", "+faststart", str(out_path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        listing.unlink()
    except OSError:
        pass
    if res.returncode != 0:
        raise RuntimeError("concat failed for %s\n%s"
                           % (Path(out_path).name, res.stderr.strip()[-2000:]))
    return out_path


def _probe_duration(cfg, path):
    """Output duration per ffprobe, or None if it cannot be read."""
    try:
        exe = video.find_binary("ffprobe", cfg.ffmpeg.ffprobe)
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:
        return None


# ---------------------------------------------------------------- batch

def out_name(cfg, target, preview=None):
    tag = "_preview" if preview else ""
    return Path(cfg.output_dir) / ("%s%s.mp4" % (target.name, tag))


def plan(cfg, jobs, args):
    """Work out every output file and the segments each one needs.

    One output per *session*, not per clip: the camera's 2 GB splits are an
    artefact of the recorder, not a unit anyone wants to watch.
    """
    import main as main_mod
    from . import window as window_mod

    tasks, notes = [], []
    for run, pairing in jobs:
        tm, _ = main_mod.select_map(cfg, run)
        if tm is None and cfg.elements["track_map"].enabled:
            notes.append("%s: no map covers this location, so the track map "
                         "element will be left out of the overlay" % run.stem)
        tm_path = str(tm.path) if tm is not None and tm.path else None

        for target in window_mod.build_targets(run, cfg, args.export):
            wanted = getattr(args, "lap", None)
            if wanted and target.kind == "lap" and target.lap_number not in wanted:
                continue
            t0, t1 = target.t0, target.t1
            if args.preview:
                # `--at` is seconds into this target's window, so 0 is the start
                # of the video you would actually watch, not of the raw data.
                t0 = min(t1, target.t0 + (args.at or 0.0))
                t1 = min(target.t1, t0 + float(args.preview))
            segs = video.plan_segments(pairing.clips, t0, t1)
            if not segs:
                notes.append("%s: no footage covers %s" % (target.name, target.label))
                continue
            out = out_name(cfg, target, args.preview)
            if out.exists() and not args.overwrite:
                notes.append("%s exists (use --overwrite)" % out.name)
                continue
            tasks.append({"cfg": cfg, "run_path": str(run.path), "target": target,
                          "segments": segs, "out": str(out), "tm": tm_path,
                          "keep_parts": args.keep_parts})
    return tasks, notes


def render_all(cfg, jobs, args):
    tasks, notes = plan(cfg, jobs, args)
    for n in notes:
        print("  = %s" % n)
    if not tasks:
        print("nothing to render")
        return 0

    print()
    for t in tasks:
        tgt = t["target"]
        span = sum(s.duration for s in t["segments"])
        print("  %-46s %7.1f s  %d segment(s)  %s"
              % (tgt.name, span, len(t["segments"]), tgt.label))
    print()

    workers = cfg.workers or max(1, min((_cpu() or 2) - 1, len(tasks)))
    # Resolve "auto" now that the concurrency is known, and stamp the decision
    # onto every task's config so each worker agrees.
    use_regions = cfg.encode.resolve_overlay_regions(workers)
    for t in tasks:
        t["cfg"].encode.overlay_regions = use_regions
    # Parallelise across targets. A target's own segments run sequentially --
    # they are joined afterwards, so there is nothing to gain by interleaving
    # them, and fewer concurrent encoders keeps each one's throughput up.
    print("rendering %d output(s) with %d worker(s), overlay=%s"
          % (len(tasks), workers, "regions" if use_regions else "full frame"))
    started = time.time()
    results = []
    if workers == 1 or len(tasks) == 1:
        for t in tasks:
            results.append(_run_task(t))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_run_task, t) for t in tasks]
            for fut in as_completed(futs):
                results.append(fut.result())

    ok = [r for r in results if r.get("out")]
    bad = [r for r in results if not r.get("out")]
    total = sum(r["seconds"] for r in ok)
    print("-" * 78)
    total_bytes = 0
    for r in sorted(ok, key=lambda r: r["clip"]):
        size = Path(r["out"]).stat().st_size
        total_bytes += size
        print("  %-46s %7.1f s  %6.1f MB  %5.1f Mbps  %.2fx"
              % (Path(r["out"]).name, r["seconds"], size / 1e6,
                 size * 8 / max(r["seconds"], 1e-6) / 1e6, r["speed"]))
    for r in ok:
        if r.get("note"):
            print("     note: %s -- %s" % (Path(r["out"]).name, r["note"]))
    for r in bad:
        print("  %-46s FAILED: %s"
              % (r["clip"], r.get("skipped") or r.get("error")))
    elapsed = time.time() - started
    if total:
        print("total %.1f s of video (%.2f GB) in %.1f s wall "
              "(%.2fx realtime aggregate)"
              % (total, total_bytes / 1e9, elapsed, total / elapsed))
    return 0 if not bad else 1


_NVENC_EXHAUSTED = ("OpenEncodeSessionEx failed", "out of memory",
                    "No capable devices found", "sessions")


def cpu_fallback_cfg(cfg):
    """A copy of `cfg` that encodes on the CPU, or None when that is off."""
    from . import encoders
    fb_enc = encoders.cpu_equivalent(cfg.encode)
    if fb_enc is None:
        return None
    import copy
    fb = copy.deepcopy(cfg)
    fb.encode = fb_enc
    return fb


def _run_task(t):
    try:
        return render_target(t["cfg"], t["run_path"], t["target"], t["segments"],
                             t["out"], t["tm"], keep_parts=t["keep_parts"])
    except Exception as exc:
        msg = str(exc)
        # GeForce cards cap concurrent NVENC sessions (8 on current drivers).
        # One output too many fails at encoder init rather than mid-stream, so
        # fall that one back to software instead of losing it.
        cfg = t["cfg"]
        fb = cpu_fallback_cfg(cfg) if cfg.encode.is_nvenc else None
        if (fb is not None
                and any(k.lower() in msg.lower() for k in _NVENC_EXHAUSTED)):
            try:
                res = render_target(fb, t["run_path"], t["target"], t["segments"],
                                    t["out"], t["tm"], keep_parts=t["keep_parts"])
                res["note"] = ("NVENC session unavailable; encoded with %s"
                               % fb.encode.encoder)
                return res
            except Exception as exc2:
                return {"clip": t["target"].name, "error": str(exc2)}
        return {"clip": t["target"].name, "error": msg}


def _cpu():
    import os
    return os.cpu_count()


# ---------------------------------------------------------------- sync check

def sync_check(cfg, jobs, args, n_frames=6, columns=3):
    """Contact sheet of overlay-burned frames at wide spacing.

    Verifying alignment should take seconds, not a full render: the needle
    should be up on a straight and the G dot should swing the right way through
    a corner you recognise.
    """
    import main as main_mod

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for run, pairing in jobs:
        tm, _ = main_mod.select_map(cfg, run)
        tm_path = str(tm.path) if tm is not None and tm.path else None
        for clip in pairing.clips:
            path = out_dir / ("synccheck_%s__%s.png" % (run.stem, clip.stem))
            try:
                _contact_sheet(cfg, run, clip, tm_path, path, n_frames, columns)
                written.append(path)
                print("  wrote %s" % path)
            except Exception as exc:
                print("  ! %s: %s" % (clip.path.name, exc))
    return 0 if written else 1


def _contact_sheet(cfg, run, clip, tm_path, out_path, n_frames, columns):
    import config as config_mod
    from . import trackmap as tm_mod

    cfg = config_mod.bind(cfg, run)
    tm = tm_mod.load(tm_path) if tm_path else None
    fps = float(clip.fps)
    width, height = clip.display_size
    comp = make_compositor(cfg, width, height, fps, tm)
    lookup, grid_t0, n_grid = row_source(cfg=cfg, run=run, fps=fps,
                                         t_start=run.t0, t_end=run.t1,
                                         channels=comp.channels)

    # Sample inside the clip, avoiding the very ends.
    lo = max(clip.data_t0, run.t0) + 5.0
    hi = min(clip.data_t1, run.t1) - 5.0
    if hi <= lo:
        raise ValueError("no usable overlap with the data")
    times = np.linspace(lo, hi, n_frames)

    thumb_w = max(320, width // columns)
    scale = thumb_w / float(width)
    thumb_h = int(round(height * scale))
    rows = int(math.ceil(n_frames / float(columns)))
    sheet = cairo.ImageSurface(cairo.FORMAT_ARGB32, thumb_w * columns,
                              thumb_h * rows)
    sctx = cairo.Context(sheet)
    sctx.set_source_rgb(0.08, 0.09, 0.11)
    sctx.paint()

    exe = video.find_binary("ffmpeg", cfg.ffmpeg.ffmpeg)
    for i, t in enumerate(times):
        seek = t - clip.data_t0
        raw = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % seek,
             "-i", str(clip.path), "-frames:v", "1", "-f", "rawvideo",
             "-pix_fmt", "bgra", "-s", "%dx%d" % (width, height), "-"],
            capture_output=True, check=True).stdout
        want = width * height * 4
        if len(raw) < want:
            raise RuntimeError("short frame read at %.1f s" % seek)
        frame = cairo.ImageSurface.create_for_data(
            bytearray(raw[:want]), cairo.FORMAT_ARGB32, width, height, width * 4)

        idx = int(round((t - grid_t0) * fps))
        comp.ctx.save()
        comp.ctx.set_operator(cairo.OPERATOR_CLEAR)
        comp.ctx.paint()
        comp.ctx.restore()
        comp.draw(lookup(idx))

        col = i % columns
        rowi = i // columns
        sctx.save()
        sctx.translate(col * thumb_w, rowi * thumb_h)
        sctx.scale(scale, scale)
        sctx.set_source_surface(frame, 0, 0)
        sctx.paint()
        sctx.set_source_surface(comp.surface, 0, 0)
        sctx.paint()
        sctx.restore()
        row = lookup(idx)
        label = "t+%.1fs  %.0f mph  %.0f rpm  %.2fG" % (
            t - run.t0,
            _first(row, "speed_gps", "speed_calc") * 2.2369362920544,
            _first(row, "rpm_obd", "rpm"),
            _first(row, "imu_combined_acc", "combined_acc"))
        from .gauges.base import TEXT
        TEXT.draw(sctx, label, col * thumb_w + 8,
                  rowi * thumb_h + thumb_h - 10, 15.0, (1, 1, 0.4, 1),
                  align="left")

    sheet.flush()
    sheet.write_to_png(str(out_path))
    return out_path


# ---------------------------------------------------------------- concat

def _first(row, *keys):
    """First finite value among `keys`, else 0 -- for the sync-check caption."""
    for k in keys:
        v = row.get(k)
        if v is not None and v == v:
            return float(v)
    return 0.0


def concat_runs(cfg, args):
    """Join the finished session videos into one file, in session order.

    A session's own camera splits are already joined during the render, so this
    is the next level up: the whole day as one upload. The gap between sessions
    is not filled -- nothing was recorded in the paddock between them.
    """
    out_dir = Path(cfg.output_dir)
    sessions = sorted(p for p in out_dir.glob("*.mp4")
                      if "__" not in p.stem and not p.stem.endswith("_preview"))
    if args.run:
        sessions = [p for p in sessions if any(w in p.stem for w in args.run)]
    if len(sessions) < 2:
        print("need at least two session videos in %s to join; found %d"
              % (out_dir, len(sessions)))
        return 1

    out = out_dir / ("%s__all_sessions.mp4" % _common_prefix(sessions))
    print("  joining %d session(s) -> %s" % (len(sessions), out.name))
    for p in sessions:
        print("    %s" % p.name)
    try:
        concat_files(cfg, sessions, out)
    except RuntimeError as exc:
        print("    ! %s" % exc)
        return 1
    print("  wrote %s (%.2f GB)" % (out.name, out.stat().st_size / 1e9))
    return 0


def _common_prefix(paths):
    stems = [p.stem for p in paths]
    pre = stems[0]
    for s in stems[1:]:
        while pre and not s.startswith(pre):
            pre = pre[:-1]
    return pre.rstrip("_-") or "sessions"
