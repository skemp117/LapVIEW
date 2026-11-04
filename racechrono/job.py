"""Headless job API: JSON in, JSON out.

The CLI in `main.py` is one caller. A UI is another, and it needs the same
information as structured data rather than as printed text -- which is why
`plan()` returns the whole dry-run as a dict and `main.py` now formats that dict
instead of computing its own. One source of truth, so the terminal report and
the UI can never disagree.

    python -m racechrono.job probe
    python -m racechrono.job plan   [job.json]
    python -m racechrono.job render [job.json]

A job:

    {
      "config":  {...},                 # optional; defaults to main.CONFIG
      "select":  {"runs": ["session_..."], "export": "sessions",
                  "laps": [4, 6]},
      "preview": {"seconds": 20, "at": 600},
      "overwrite": true,
      "keep_parts": false
    }
"""
from __future__ import annotations

import copy
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import config as config_mod

from . import csv_io, laps as laps_mod, sync, trackmap, video, window

SCHEMA_VERSION = 1


# --------------------------------------------------------------- selection

@dataclass
class Selection:
    """What to work on. Field names match the CLI's argparse namespace, so the
    same object serves both callers."""

    run: list | None = None
    export: str = "sessions"
    lap: list | None = None
    preview: float | None = None
    at: float = 0.0
    overwrite: bool = False
    keep_parts: bool = False
    dry_run: bool = False
    sync_check: bool = False
    concat: bool = False

    @classmethod
    def from_job(cls, job):
        sel = job.get("select") or {}
        pre = job.get("preview") or {}
        return cls(run=sel.get("runs"),
                   export=sel.get("export", "sessions"),
                   lap=sel.get("laps"),
                   preview=pre.get("seconds"),
                   at=float(pre.get("at", 0.0) or 0.0),
                   overwrite=bool(job.get("overwrite", False)),
                   keep_parts=bool(job.get("keep_parts", False)))


def load_config(job):
    """The job's config, or the CONFIG in main.py when it does not carry one."""
    raw = job.get("config")
    if raw is None:
        # The same CONFIG every other entry point uses, environment included,
        # so a job run in a container writes under LAPVIEW_DATA_DIR and not
        # into the read-only code directory it was started from.
        import main as main_mod
        raw = config_mod.apply_env(copy.deepcopy(main_mod.CONFIG))
    cfg = config_mod.load(raw)
    if job.get("select", {}).get("export") not in (None, "sessions", "laps", "both"):
        raise config_mod.ConfigError(
            "select.export: %r not one of sessions, laps, both"
            % job["select"]["export"])
    return cfg


# --------------------------------------------------------------- probe

def probe(cfg=None):
    """What this machine can do. A UI needs this before it offers any choices --
    which encoders exist decides whether a render takes 20 minutes or 3 hours."""
    cfg = cfg or config_mod.load({}) \
        if False else cfg
    ffmpeg = ffprobe = None
    err = None
    try:
        ffmpeg = video.find_binary("ffmpeg", cfg.ffmpeg.ffmpeg if cfg else None)
        ffprobe = video.find_binary("ffprobe", cfg.ffmpeg.ffprobe if cfg else None)
    except Exception as exc:
        err = str(exc)

    out = {
        "schema": SCHEMA_VERSION,
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine(),
                     "python": platform.python_version(),
                     "cpus": os.cpu_count()},
        "ffmpeg": {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "error": err},
        "encoders": {}, "hwaccels": [], "gpu": None,
        "python_packages": {},
    }

    for mod in ("numpy", "pandas", "cairo", "scipy", "PIL"):
        try:
            m = __import__(mod)
            out["python_packages"][mod] = getattr(
                m, "__version__", getattr(m, "version", "?"))
        except Exception:
            out["python_packages"][mod] = None

    if not ffmpeg:
        return out

    try:
        banner = subprocess.run([ffmpeg, "-hide_banner", "-version"],
                                capture_output=True, text=True).stdout
        out["ffmpeg"]["version"] = banner.splitlines()[0] if banner else None
    except Exception:
        pass

    try:
        text = subprocess.run([ffmpeg, "-hide_banner", "-hwaccels"],
                              capture_output=True, text=True).stdout
        out["hwaccels"] = [l.strip() for l in text.splitlines()[1:] if l.strip()]
    except Exception:
        pass

    # Compiled-in is not the same as usable: a one-frame encode is the only
    # honest test, and it is what stops a UI offering NVENC on a machine with an
    # AMD card.
    for enc in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv",
                "h264_amf", "hevc_amf", "h264_videotoolbox",
                "hevc_videotoolbox", "libx264", "libx265"):
        out["encoders"][enc] = _encoder_works(ffmpeg, enc)

    # nvidia-smi is on PATH on Linux (the container toolkit mounts it in) and
    # in System32 on Windows; `gpus` looks in both, and copes with more than one
    # card, which the old single-line parse did not.
    from . import encoders
    found = encoders.gpus()
    if found:
        out["gpu"] = found[0]
        out["gpu"]["nvenc_profile"] = encoders.profile_for(found[0]["name"])
    return out


def _encoder_works(ffmpeg, enc):
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=size=256x144:rate=30:duration=1",
             "-c:v", enc, "-f", "null", "-"],
            capture_output=True, timeout=90)
        return r.returncode == 0
    except Exception:
        return False


def recommend_encoder(probed):
    """Best available encoder, preferring hardware."""
    for enc in ("hevc_nvenc", "h264_nvenc", "hevc_videotoolbox",
                "h264_videotoolbox", "hevc_qsv", "h264_qsv",
                "hevc_amf", "h264_amf", "libx265", "libx264"):
        if probed.get("encoders", {}).get(enc):
            return enc
    return None


# --------------------------------------------------------------- plan

def plan(job=None, source=None):
    """Everything a caller needs before committing to a render, as data.

    This is the dry-run: which CSVs were found, what channels they carry, how
    clips were paired, what sync offset each got and how confident it is, which
    map was chosen, and exactly which output files would be written from which
    source segments.
    """
    source = source or (job or {}).get("source")
    if source is None:
        raise ValueError("this needs a source folder; there is no "
                         "fixed input directory any more")
    job = job or {}
    cfg = load_config(job)
    sel = Selection.from_job(job)
    started = time.time()

    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.cache_dir).mkdir(parents=True, exist_ok=True)

    result = {"schema": SCHEMA_VERSION, "action": "plan", "runs": [],
              "unpaired_clips": [], "notes": [], "totals": {}}

    runs = _load_runs(cfg, sel.run, result["notes"], source)
    if not runs:
        result["notes"].append("no runs loaded from %s" % source)
        return result

    clips = video.discover(source, cfg.ffmpeg.ffprobe, recursive=True)
    manifest = video.load_manifest(cfg.manifest)
    pairings, orphans = video.pair(runs, clips, manifest)
    cache = sync.load_cache(Path(cfg.cache_dir) / "sync.json")

    by_stem = {r.stem: r for r in runs}
    total_out_s = 0.0
    n_targets = 0

    for pairing in pairings:
        run = by_stem[pairing.run_stem]
        entry = _describe_run(cfg, run)
        entry["pairing"] = {"strategy": pairing.strategy,
                            "notes": list(pairing.notes), "clips": []}
        # Set unconditionally: a consumer should not have to guess whether a run
        # entry carries these keys. A run with no footage still has a map and a
        # (empty) output list, and a UI reading the plan would rather see that
        # than a KeyError.
        tm_pre, dist_pre = _select_map(cfg, run)
        entry["map"] = (None if tm_pre is None else
                        {"name": tm_pre.name,
                         "path": str(tm_pre.path) if tm_pre.path else None,
                         "origin_distance_m": round(dist_pre, 1),
                         "lap_count": tm_pre.lap_count,
                         "length_m": tm_pre.length_m,
                         "orientation": tm_pre.orientation})
        entry["outputs"] = []

        if not pairing.clips:
            entry["pairing"]["notes"].append("no clips matched; run skipped")
            result["runs"].append(entry)
            continue

        try:
            size, rates = video.check_uniform(pairing.clips)
            entry["pairing"]["display_size"] = list(size)
            entry["pairing"]["frame_rates"] = rates
        except ValueError as exc:
            entry["pairing"]["notes"].append(str(exc))
            result["runs"].append(entry)
            continue

        for clip in pairing.clips:
            sync.sync_clip(clip, run, cfg.sync.as_dict(), cache,
                           cfg.ffmpeg.ffmpeg, verbose=False)
        if cfg.sync.consensus and len(pairing.clips) > 1:
            sync.reconcile_run(pairing.clips, cfg.sync.min_prominence,
                               verbose=False)
        for clip in pairing.clips:
            cache[sync.cache_key(clip)] = sync._entry(clip)

        for row in video.timeline(pairing, run):
            c = row["clip"]
            entry["pairing"]["clips"].append({
                "name": c.path.name, "path": str(c.path),
                "width": c.width, "height": c.height, "fps": round(c.fps, 4),
                "codec": c.codec, "duration_s": round(c.duration, 3),
                "frames": c.n_frames, "has_audio": c.has_audio,
                "start_unix": c.start_unix,
                "start_utc": video.fmt_unix(c.start_unix),
                "offset_s": round(c.offset, 4),
                "sync_method": c.sync_method,
                "sync_confidence": (None if c.sync_confidence == float("inf")
                                    else round(c.sync_confidence, 3)),
                "gap_before_s": (None if row["gap_before_s"] is None
                                 else round(row["gap_before_s"], 3)),
            })

        for target in window.build_targets(run, cfg, sel.export):
            if sel.lap and target.kind == "lap" and target.lap_number not in sel.lap:
                continue
            t0, t1 = target.t0, target.t1
            if sel.preview:
                t0 = min(t1, target.t0 + (sel.at or 0.0))
                t1 = min(target.t1, t0 + float(sel.preview))
            segs = video.plan_segments(pairing.clips, t0, t1)
            out_path = Path(cfg.output_dir) / (
                "%s%s.mp4" % (target.name, "_preview" if sel.preview else ""))
            span = sum(s.duration for s in segs)
            entry["outputs"].append({
                "name": target.name, "kind": target.kind,
                "file": str(out_path), "exists": out_path.exists(),
                "label": target.label,
                "lap_number": target.lap_number or None,
                "lap_time_s": round(target.lap_time, 3) or None,
                "window_utc": [video.fmt_unix(t0), video.fmt_unix(t1)],
                "window_offset_s": [round(t0 - run.t0, 3), round(t1 - run.t0, 3)],
                "duration_s": round(span, 3),
                "segments": [{"clip": s.clip.path.name,
                              "seek_s": round(s.seek, 3),
                              "duration_s": round(s.duration, 3)}
                             for s in segs],
                "renderable": bool(segs),
            })
            if segs:
                total_out_s += span
                n_targets += 1
        result["runs"].append(entry)

    sync.save_cache(cache, Path(cfg.cache_dir) / "sync.json")

    # Only meaningful when every run is in scope: with a --run filter the other
    # sessions' clips are "unpaired" only because their run was excluded.
    for c in (orphans if not sel.run else []):
        result["unpaired_clips"].append(
            {"name": c.path.name, "start_utc": video.fmt_unix(c.start_unix),
             "duration_s": round(c.duration, 3)})

    mbps = _est_mbps(cfg)
    result["encode"] = {"encoder": cfg.encode.encoder, "cq": cfg.encode.cq,
                        "crf": cfg.encode.crf, "preset": cfg.encode.preset,
                        "estimated_mbps": mbps,
                        "overlay_regions": cfg.encode.overlay_regions}
    result["totals"] = {
        "runs": len(result["runs"]), "outputs": n_targets,
        "video_seconds": round(total_out_s, 1),
        "estimated_gb": round(total_out_s * mbps / 8.0 / 1000.0, 2),
    }
    result["elapsed_s"] = round(time.time() - started, 2)
    return result


def _load_runs(cfg, wanted, notes, source):
    paths = sorted(Path(source).rglob("*.csv"))
    if wanted:
        keep = []
        for w in wanted:
            hits = [p for p in paths if p.stem == w or w in p.stem]
            if not hits:
                notes.append("no CSV matching %r" % w)
            keep.extend(hits)
        paths = sorted(set(keep))
    runs = []
    for p in paths:
        try:
            runs.append(csv_io.read_run(p))
        except Exception as exc:
            notes.append("%s: %s" % (p.name, exc))
    return runs


def _describe_run(cfg, run):
    gate = run.good_mask(cfg.quality.max_accuracy_m)
    lp = laps_mod.laps(run, cfg.quality.max_accuracy_m)
    channels = []
    for c in run.df.columns:
        v = run.df[c]
        n = int(v.notna().sum())
        channels.append({
            "name": c, "unit": run.units.get(c) or "",
            "device": run.devices.get(c) or "", "samples": n,
            "min": None if not n else round(float(v.min()), 4),
            "max": None if not n else round(float(v.max()), 4),
            "constant": bool(v.nunique(dropna=True) <= 1)})
    return {
        "stem": run.stem, "path": str(run.path),
        "session_title": run.meta.get("Session title", ""),
        "track_name": run.meta.get("Track name", ""),
        "rows": len(run.df),
        "t0_unix": run.t0, "t1_unix": run.t1,
        "window_utc": [video.fmt_unix(run.t0), video.fmt_unix(run.t1)],
        "duration_s": round(run.duration, 3),
        "fix_quality": round(float(gate.mean()), 4),
        "max_accuracy_m": cfg.quality.max_accuracy_m,
        "constant_channels": csv_io.constant_channels(run),
        "channels": channels,
        "laps": [{"number": l.number, "duration_s": round(l.duration, 3),
                  "distance_m": round(l.distance_m, 1),
                  "offset_s": [round(l.t0 - run.t0, 2), round(l.t1 - run.t0, 2)]}
                 for l in lp],
    }


def _select_map(cfg, run):
    if cfg.map_file:
        return trackmap.load(cfg.map_file), 0.0
    lat, lon = run.mean_latlon(cfg.quality.max_accuracy_m)
    return trackmap.auto_select(cfg.maps_dir, lat, lon)


# Measured on a fast lap of the reference session; only used to put a rough
# size on a plan.
_MBPS = {("hevc_nvenc", 26): 15.0, ("hevc_nvenc", 28): 12.0,
         ("hevc_nvenc", 31): 8.2, ("h264_nvenc", 21): 31.8,
         ("h264_nvenc", 25): 19.6, ("h264_nvenc", 28): 13.4}


def _est_mbps(cfg):
    enc = cfg.encode
    if not enc.is_nvenc:
        return 20.0
    hit = _MBPS.get((enc.encoder, enc.cq))
    if hit:
        return hit
    base = 15.0 if "hevc" in enc.encoder else 19.0
    return round(base * (2.0 ** ((26 - enc.cq) / 6.0)), 1)


# --------------------------------------------------------------- render

def render(job=None, source=None):
    """Execute a job. Returns the plan plus per-output results."""
    source = source or (job or {}).get("source")
    if source is None:
        raise ValueError("this needs a source folder; there is no "
                         "fixed input directory any more")
    from . import render as render_mod

    job = job or {}
    cfg = load_config(job)
    sel = Selection.from_job(job)
    the_plan = plan(job)

    runs = _load_runs(cfg, sel.run, the_plan["notes"])
    clips = video.discover(source, cfg.ffmpeg.ffprobe, recursive=True)
    manifest = video.load_manifest(cfg.manifest)
    pairings, _ = video.pair(runs, clips, manifest)
    cache = sync.load_cache(Path(cfg.cache_dir) / "sync.json")
    by_stem = {r.stem: r for r in runs}

    jobs = []
    for p in pairings:
        run = by_stem[p.run_stem]
        if not p.clips:
            continue
        video.check_uniform(p.clips)
        for clip in p.clips:
            sync.sync_clip(clip, run, cfg.sync.as_dict(), cache,
                           cfg.ffmpeg.ffmpeg, verbose=False)
        if cfg.sync.consensus and len(p.clips) > 1:
            sync.reconcile_run(p.clips, cfg.sync.min_prominence, verbose=False)
        jobs.append((run, p))
    sync.save_cache(cache, Path(cfg.cache_dir) / "sync.json")

    started = time.time()
    rc = render_mod.render_all(cfg, jobs, sel)
    the_plan["action"] = "render"
    the_plan["render"] = {"exit_code": rc,
                          "elapsed_s": round(time.time() - started, 2)}
    for entry in the_plan["runs"]:
        for out in entry.get("outputs", []):
            f = Path(out["file"])
            out["written"] = f.exists()
            out["size_bytes"] = f.stat().st_size if f.exists() else None
    return the_plan


# --------------------------------------------------------------- text report

def format_plan(p, verbose_channels=True):
    """The plan as the terminal report. Same data the UI gets, so the two can
    never disagree."""
    out = []
    w = out.append
    for entry in p.get("runs", []):
        w("=" * 78)
        w("RUN  %s" % entry["path"])
        w("  session       %s / %s" % (entry["session_title"] or "?",
                                       entry["track_name"] or "?"))
        w("  data window   %s .. %s  (%.1f s, %d rows)"
          % (entry["window_utc"][0], entry["window_utc"][1],
             entry["duration_s"], entry["rows"]))
        w("  fix quality   %.2f%% of rows within %.0f m accuracy"
          % (100.0 * entry["fix_quality"], entry["max_accuracy_m"]))
        if entry["constant_channels"]:
            w("  constant      %s  (no display value)"
              % ", ".join(entry["constant_channels"]))
        if verbose_channels:
            w("  channels")
            for c in entry["channels"]:
                if c["samples"]:
                    w("    %-24s %-10s %-9s n=%-7d [%.4g .. %.4g]"
                      % (c["name"], c["unit"] or "-", c["device"] or "-",
                         c["samples"], c["min"], c["max"]))
                else:
                    w("    %-24s %-10s %-9s n=0       (empty)"
                      % (c["name"], c["unit"] or "-", c["device"] or "-"))
        w("  laps          %d complete: %s"
          % (len(entry["laps"]),
             ", ".join("%.1fs" % l["duration_s"] for l in entry["laps"])))
        m = entry.get("map")
        if m is None:
            w("  map           NONE FOUND -- run tools/generate_map.py")
        else:
            w("  map           %s (%s)  origin %.1f m away, %d laps, %.0f m"
              % (m["name"], Path(m["path"]).name if m["path"] else "?",
                 m["origin_distance_m"], m["lap_count"], m["length_m"]))

        pr = entry["pairing"]
        w("  clips         paired by %s" % pr["strategy"])
        for i, c in enumerate(pr["clips"]):
            w("    [%d] %-16s %dx%d@%.3f %s %.1f s"
              % (i, c["name"], c["width"], c["height"], c["fps"], c["codec"],
                 c["duration_s"]))
            conf = ("manual" if c["sync_confidence"] is None
                    else "%.1f sd" % c["sync_confidence"])
            w("        offset %+.3f s via %s (confidence %s)%s"
              % (c["offset_s"], c["sync_method"], conf,
                 "" if c["gap_before_s"] is None
                 else "   gap before %.2f s" % c["gap_before_s"]))
        for n in pr["notes"]:
            w("  note          %s" % n)

        outs = entry.get("outputs") or []
        w("  outputs       %d" % len(outs))
        for o in outs:
            if not o["renderable"]:
                w("    %-44s NO FOOTAGE (%s)" % (o["name"], o["label"]))
                continue
            w("    %-44s %7.1f s  %s"
              % (Path(o["file"]).name, o["duration_s"], o["label"]))
            if len(o["segments"]) > 1:
                for j, s in enumerate(o["segments"]):
                    w("        part %d: %-16s from %7.1f s for %6.1f s"
                      % (j + 1, s["clip"], s["seek_s"], s["duration_s"]))
            if o.get("written"):
                w("        wrote %.1f MB" % ((o["size_bytes"] or 0) / 1e6))

    if p.get("unpaired_clips"):
        w("=" * 78)
        w("UNPAIRED CLIPS (no run covers them):")
        for c in p["unpaired_clips"]:
            w("  %-16s %s  %.1f s"
              % (c["name"], c["start_utc"], c["duration_s"]))
    for n in p.get("notes", []):
        w("  ! %s" % n)
    t = p.get("totals", {})
    e = p.get("encode", {})
    if t:
        w("-" * 78)
        w("%d output(s), %.1f s of video (%.1f min), about %.2f GB at %s cq%s "
          "(~%.0f Mbps)"
          % (t["outputs"], t["video_seconds"], t["video_seconds"] / 60.0,
             t["estimated_gb"], e.get("encoder"), e.get("cq"),
             e.get("estimated_mbps", 0)))
    return "\n".join(out)


# --------------------------------------------------------------- cli

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "plan"
    job = {}
    if len(argv) > 1:
        job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            job = json.loads(text)

    if action == "probe":
        cfg = None
        try:
            cfg = load_config(job)
        except Exception:
            pass
        print(json.dumps(probe(cfg), indent=1))
        return 0
    if action == "plan":
        print(json.dumps(plan(job), indent=1))
        return 0
    if action == "render":
        res = render(job)
        print(json.dumps(res, indent=1))
        return res.get("render", {}).get("exit_code", 0)
    if action == "report":
        print(format_plan(plan(job)))
        return 0
    sys.stderr.write("unknown action %r; use probe, plan, report or render\n"
                     % action)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
