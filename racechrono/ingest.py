"""Ingest: files in, lap library out.

    plan = ingest.plan(cfg, library)          # decides everything, writes nothing
    ingest.apply(plan, library)               # builds maps, cuts laps, inserts rows

The split is deliberate. Deciding is seconds and encoding is minutes, and the
decisions are the part that needs to be inspectable before anything irreversible
happens -- there is no raw archive, so a lap cut at the wrong bitrate cannot be
recut. `tools/ingest.py --dry-run` prints the plan.

## Order, and why it is this order

1. **Classify** each session. `shapes.classify` decides circuit or course, whether
   its laps are corroborated, how many runs it holds.
2. **Identify** the track, by shape and direction, not position (`identify`).
   Sessions that match nothing are grouped among themselves by mutual fit, so a
   day at a brand-new course arrives as one new track rather than six.
3. **Build maps** for new tracks, from the sessions being ingested. This has to
   come before slicing, because an open course has no lap boundaries and the
   centreline is what a run is measured against.
4. **Slice** each session into laps or runs.
5. **Cut** each lap: trim the video, re-encode, write the data file.

Steps 1-3 are `plan`. Steps 4-5 are `apply`.

## Grouping unknown sessions

A new venue arrives as several sessions at once, none of which matches anything.
Creating a track per session would give six tracks for one course. So unmatched
sessions are clustered: build a provisional centreline from the first, then test
the rest against it with the same fit/coverage/direction test used for existing
tracks. Ones that fit join it; ones that do not start another cluster. That is
the same machinery deciding both questions, which is the point.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import (compare as compare_mod, csv_io, identify as identify_mod,
               imu as imu_mod, laps as laps_mod, runs as runs_mod,
               shapes as shapes_mod, trackmap, video)

# Default padding either side of a lap. Enough to see the corner before the line
# and the exit after it.
PAD_S = 3.0

# Measured across four 105 s windows from three phones, against a stream copy of
# the same window -- which is the only honest baseline, because these files vary
# from 12.0 to 20.6 Mbps *within themselves*:
#
#     cq 24   -12% overall, and +11% on one window
#     cq 28   -36% overall, worst case -7%
#     cq 30   -51% overall
#
# CQ 24 was the first default, chosen from a single slice that happened to be a
# low-bitrate section. It grows some sections, which is the exact failure it was
# picked to avoid, and the resulting library came out at 15.7 Mbps -- above its
# own source. CQ 28 never grows a section and is a third smaller. With no raw
# archive the choice cannot be revisited, so it has to be right rather than
# timid.
DEFAULT_CQ = 28




@dataclass
class Options:
    """Everything the import screen can set.

    Measured on a real 105 s AMP window against a stream copy of the same window,
    so the interface can say what a choice costs instead of implying it is free:

    | | vs source |
    |---|---|
    | 1080p60 CQ 28 | -44% |
    | 1080p60 CQ 28, muted | -45% |
    | 1080p30 CQ 28 | -68% |
    | 720p60 CQ 28 | -73% |
    | 720p30 CQ 28 | -85% |

    Muting saves 1%, which is worth saying out loud: it is not a space control.
    Frame rate and resolution are the real levers after CQ, and both cost
    something a driver notices, which is why neither is on by default.
    """

    # what to keep
    pad_s: float = PAD_S
    skip_edge_laps: bool = False          # the out-lap and in-lap of each run
    keep_best: int | None = None          # per session
    slower_than_best_pct: float | None = None
    max_gap_m: float | None = None        # drop laps with a GPS hole this big
    require_video: bool = False
    import_video: bool = True

    # video
    cq: int = DEFAULT_CQ
    height: int | None = None             # 720 to downscale, None to keep
    fps: int | None = None                # 30 to halve, None to keep
    audio: bool = True

    # organisation. Both of these are required by the import screen: a library
    # is worth having because its names mean something, and the names a phone
    # writes into a CSV do not ("Grip 8-22" for a season's sixth event).
    event: str = ""
    track_name: str = ""                  # names the tracks this import creates
    track_id: str = ""                    # or put every session on this one
    date_override: str = ""

    def video_args(self, encoder, nvenc_profile=None):
        """The ffmpeg arguments these options imply.

        HEVC is tagged `hvc1`. ffmpeg's default is `hev1`, which Safari -- and
        so every iPhone -- refuses to play, though the bitstream is identical.
        On a Pascal card HEVC gets no B-frames, which that NVENC cannot do.
        """
        out = []
        if self.height:
            out += ["-vf", "scale=-2:%d" % int(self.height)]
        if self.fps:
            out += ["-r", str(int(self.fps))]
        if encoder.endswith("nvenc"):
            out += ["-c:v", encoder, "-cq", str(self.cq), "-preset", "p5"]
            if nvenc_profile == "pascal" and "hevc" in encoder:
                out += ["-bf", "0"]
        else:
            out += ["-c:v", encoder, "-crf", str(self.cq), "-preset", "medium"]
        if "hevc" in encoder or "265" in encoder:
            out += ["-tag:v", "hvc1"]
        out += ["-pix_fmt", "yuv420p"]
        out += ["-c:a", "aac", "-b:a", "128k"] if self.audio else ["-an"]
        return out

    def estimated_mbps(self):
        """A rough stored bitrate, from the measurements above.

        Rough on purpose. It exists so the import screen can put a number next
        to a checkbox, not to predict a file.
        """
        mbps = 11.6                            # 1080p60 at CQ 28
        mbps *= 1.32 ** ((28 - self.cq) / 4.0)  # each 4 CQ is about a third
        if self.fps and self.fps <= 30:
            mbps *= 0.56
        if self.height and self.height <= 720:
            mbps *= 0.48
        if not self.audio:
            mbps *= 0.99
        return mbps


DEFAULTS = Options()



@dataclass
class LapPlan:
    number: int
    kind: str                  # "lap" or "run"
    t0: float                  # unix, the timed boundary
    t1: float
    distance_m: float
    representative: bool
    segments: list = field(default_factory=list)   # video.Segment, may be empty
    gap_m: float = 0.0
    measured_frac: float = 1.0
    dropped: str = ""              # why this lap will not be imported

    @property
    def duration(self):
        return self.t1 - self.t0

    @property
    def has_video(self):
        return bool(self.segments)

    @property
    def video_s(self):
        return sum(sg.duration for sg in self.segments)

    @property
    def keep(self):
        return not self.dropped


@dataclass
class SessionPlan:
    path: Path
    run: object = None
    shape: object = None
    clips: list = field(default_factory=list)
    laps: list = field(default_factory=list)
    track_key: str = ""            # existing track id, or "new:<n>"
    track_name: str = ""
    verdict: str = ""              # match | ambiguous | new
    note: str = ""
    date: str = ""
    calibration: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    @property
    def stem(self):
        return self.path.stem


@dataclass
class TrackPlan:
    key: str
    name: str
    closed: bool
    existing_id: str | None
    needs_map: bool
    sessions: list = field(default_factory=list)
    source_names: set = field(default_factory=set)


@dataclass
class Plan:
    tracks: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    already: list = field(default_factory=list)   # (file, session row) in the library
    options: Options = field(default_factory=Options)
    source: Path | None = None

    @property
    def pad_s(self):
        return self.options.pad_s

    @property
    def cq(self):
        return self.options.cq

    def track(self, key):
        return next((t for t in self.tracks if t.key == key), None)

    def kept(self):
        return [(sp, lp) for sp in self.sessions for lp in sp.laps if lp.keep]

    def dropped(self):
        return [(sp, lp) for sp in self.sessions for lp in sp.laps if lp.dropped]

    def estimate(self):
        """Laps, seconds and an approximate size for what is currently selected."""
        secs = sum(lp.video_s for _sp, lp in self.kept() if lp.has_video)
        mbps = self.options.estimated_mbps()
        return {"laps": len(self.kept()),
                "dropped": len(self.dropped()),
                "video_s": round(secs, 1),
                "bytes": int(secs * mbps * 1e6 / 8) if self.options.import_video
                         else 0,
                "mbps": round(mbps, 1)}




def scan(source, ffprobe=None):
    """What is in a folder someone pointed at: sessions and footage together.

    There is no `Raw Data` and no `Raw Footage` any more. A folder is a folder --
    a card copied off a phone, a directory of exports, a day's worth of both --
    and the two kinds of file are told apart by their extension, not by which
    directory they sit in. Scanned recursively, because a camera's own tree and a
    "day at AMP" folder both nest.
    """
    root = Path(source)
    if not root.is_dir():
        raise FileNotFoundError("%s is not a folder" % root)
    csvs = sorted(p for p in root.rglob("*.csv") if p.is_file())
    clips = video.discover(root, ffprobe, recursive=True)
    return csvs, clips


def survey(source, ffprobe=None):
    """A cheap look at a folder, for a picker. No parsing, no probing."""
    # Absolute, because this path is shown to the person who chose it and
    # later named in the "safe to delete" line. A relative one would be read
    # against wherever the server happens to have been started.
    root = Path(source).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    if not root.is_dir():
        return {"path": str(root), "exists": False, "csvs": 0, "videos": 0,
                "bytes": 0}
    csvs = vids = 0
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix == ".csv":
            csvs += 1
        elif suffix in video.VIDEO_EXTS:
            vids += 1
        else:
            continue
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return {"path": str(root), "exists": True, "csvs": csvs, "videos": vids,
            "bytes": total}



# ------------------------------------------------------------------ planning

def _date_of(run):
    return dt.datetime.fromtimestamp(run.t0, dt.UTC).strftime("%Y-%m-%d")


def _md5(path, chunk=1 << 20):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _genmap():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_genmap", Path(__file__).resolve().parents[1] / "tools" / "generate_map.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line_from_run(run, closed, name, points=480, max_accuracy_m=10.0):
    """A centreline from a single traversal, for clustering only.

    Averaging needs two traversals, and an autocross session often holds exactly
    one -- which is how a ten-session day became ten tracks: `build_course`
    refused a single path, the provisional line came back None, and every
    subsequent session had nothing to be tested against.

    One driven path is a perfectly good answer to "is this the same course". It
    is not a good *map*, and it is never stored: `apply` builds the real one by
    averaging everything that clustered together.
    """
    from . import geo
    genmap = _genmap()
    if closed:
        lp = laps_mod.laps(run, max_accuracy_m)
        if not lp:
            return None
        best = min(lp, key=lambda l: l.duration)
        sl = slice(best.i0, best.i1)
    else:
        spans = runs_mod.detect(run, max_accuracy_m=max_accuracy_m)
        if not spans:
            return None
        sl = slice(spans[0].i0, spans[0].i1)
    good = run.good_mask(max_accuracy_m)[sl]
    lat = run.values("latitude")[sl][good]
    lon = run.values("longitude")[sl][good]
    if lat.size < 64:
        return None
    origin = run.mean_latlon(max_accuracy_m)
    x, y = geo.to_xy(lat, lon, *origin)
    pts, total = genmap.resample_by_distance(x, y, points)
    if pts is None:
        return None
    rot = geo.identity_rotation()
    extent = trackmap.extent_from(pts[:, 0], pts[:, 1])
    ux, uy = trackmap._normalise(pts[:, 0], pts[:, 1], extent)
    unit = np.c_[ux, uy]
    if closed:
        unit = np.vstack([unit, unit[:1]])
    return trackmap.TrackMap(
        name=name, origin={"lat": origin[0], "lon": origin[1]},
        rotation=[[float(rot[0][0]), float(rot[0][1])],
                  [float(rot[1][0]), float(rot[1][1])]],
        extent_m=extent, points_unit=unit.tolist(), closed=bool(closed),
        length_m=float(total), lap_count=1)


def _provisional_map(runs, closed, name, points=480):
    # This is where an import spends most of its non-encoding time: every sample
    # of every run is projected onto a candidate centreline, measured at 20 s of
    # the 25 s that planning tests/data takes. Worth revisiting if a big day's
    # import feels slow -- projecting a decimated run would probably fit the same
    # centreline -- but not worth guessing at without measuring the accuracy cost.
    """A centreline good enough to test other sessions against.

    Not the final map -- that is built in `apply` with the full averaging and
    trimming. This exists only to answer "is this other session the same course",
    which needs a line, not a good line. Falls back to a single traversal when
    there is not enough to average.
    """
    genmap = _genmap()
    paths = [str(r.path) for r in runs]
    try:
        if closed:
            return genmap.build(paths, name, points=points, verbose=False)
        return genmap.build_course(paths, name, points=points, verbose=False)
    except SystemExit:
        pass
    except Exception:
        pass
    return _line_from_run(runs[0], closed, name, points=points) if runs else None


def plan(cfg, library=None, source=None, options=None, pad_s=None, cq=None,
         verbose=True):
    """Decide everything about importing one folder. Writes nothing."""
    if source is None:
        raise ValueError("plan needs a source folder to import from")
    options = options or Options()
    if pad_s is not None:
        options.pad_s = pad_s
    if cq is not None:
        options.cq = cq
    source = Path(source)
    csv_paths, clips = scan(source, cfg.ffmpeg.ffprobe)
    manifest = video.load_manifest(source / Path(cfg.manifest).name)
    out = Plan(options=options, source=source)

    # --- load and classify -------------------------------------------------
    seen_hash = {}
    loaded = []
    for path in csv_paths:
        h = _md5(path)
        if h in seen_hash:
            out.duplicates.append((path.name, seen_hash[h]))
            continue
        seen_hash[h] = path.name
        try:
            run = csv_io.read_run(path, imu_opts=(cfg.imu.as_kwargs()
                                                  if cfg.imu.enabled else None),
                                  derive=cfg.imu.enabled)
        except Exception as exc:
            out.skipped.append((path.name, "%s: %s" % (type(exc).__name__, exc)))
            continue
        # Pointing at the same folder twice is a normal thing to do -- a day's
        # footage arrives in more than one place, and the obvious way to add the
        # last card is to import the folder again. Without this the library
        # doubled: every session, every lap, every byte of video.
        if library is not None:
            held = library.session_recorded_at(run.t0, run.duration)
            if held is not None:
                out.already.append((path.name, held))
                continue
        mine = [c for c in clips
                if c.start_unix and run.t0 - 300 <= c.start_unix <= run.t1 + 300]
        sp = SessionPlan(path=path, run=run, clips=mine, date=_date_of(run))
        sp.shape = shapes_mod.classify(run, cfg, mine)
        sp.calibration = _calibration(run)
        loaded.append(sp)

    # --- identify ----------------------------------------------------------
    existing = library.maps() if library is not None else {}
    unknown = []
    for sp in loaded:
        name = (sp.run.meta.get("Track name") or "").strip().strip('"') or None
        # An explicit track wins over identification, because the person doing
        # the import knows where they were. It is still measured against that
        # track's centreline, and a session that does not fit says so on the
        # review rather than being quietly filed in the wrong place.
        if options.track_id and options.track_id in existing:
            tm = existing[options.track_id]
            fit = identify_mod.fit_run(tm, sp.run,
                                       max_accuracy_m=cfg.quality.max_accuracy_m)
            sp.track_key = options.track_id
            sp.track_name = tm.name
            sp.verdict = "assigned"
            sp.note = "put on %s by hand" % tm.name
            if not fit.matched:
                sp.problems.append(
                    "assigned to %s but the shape does not fit it: %s"
                    % (tm.name, fit.why_not()))
            out.sessions.append(sp)
            continue
        if existing:
            d = identify_mod.identify(sp.run, existing, source_name=name)
            sp.verdict, sp.note = d.verdict, d.note
            if d.verdict == "match":
                sp.track_key = d.candidate.key
                sp.track_name = d.candidate.name
                out.sessions.append(sp)
                continue
            if d.verdict == "ambiguous":
                sp.problems.append("ambiguous: " + d.note)
        else:
            sp.verdict, sp.note = "new", "the library has no tracks yet"
        unknown.append(sp)

    # --- cluster the unknown sessions into new tracks ----------------------
    clusters = []
    for sp in unknown:
        placed = False
        for cl in clusters:
            tm = cl["map"]
            if tm is None:
                continue
            fit = identify_mod.fit_run(tm, sp.run,
                                       max_accuracy_m=cfg.quality.max_accuracy_m)
            if fit.matched:
                cl["sessions"].append(sp)
                placed = True
                break
        if placed:
            continue
        closed = bool(sp.shape.laps_usable)
        cl = {"sessions": [sp], "closed": closed, "map": None,
              "name": options.track_name
                      or (sp.run.meta.get("Track name") or "").strip().strip('"')
                      or sp.stem}
        cl["map"] = _provisional_map([sp.run], closed, cl["name"])
        clusters.append(cl)

    for i, cl in enumerate(clusters, start=1):
        key = "new:%d" % i
        # An event that ran two courses makes two tracks, and they cannot share
        # a name. The second and later ones are numbered from the name given.
        name = cl["name"]
        if options.track_name and len(clusters) > 1:
            name = "%s %d" % (options.track_name, i)
        tp = TrackPlan(key=key, name=name, closed=cl["closed"],
                       existing_id=None, needs_map=True)
        cl["name"] = name
        for sp in cl["sessions"]:
            sp.track_key = key
            sp.track_name = cl["name"]
            tp.sessions.append(sp)
            nm = (sp.run.meta.get("Track name") or "").strip().strip('"')
            if nm:
                tp.source_names.add(nm)
            out.sessions.append(sp)
        out.tracks.append(tp)

    # existing tracks that gained sessions
    for sp in out.sessions:
        if sp.track_key.startswith("new:"):
            continue
        tp = out.track(sp.track_key)
        if tp is None:
            tm = existing.get(sp.track_key)
            tp = TrackPlan(key=sp.track_key, name=sp.track_name,
                           closed=bool(tm.closed) if tm else True,
                           existing_id=sp.track_key, needs_map=False)
            out.tracks.append(tp)
        tp.sessions.append(sp)
        nm = (sp.run.meta.get("Track name") or "").strip().strip('"')
        if nm:
            tp.source_names.add(nm)

    # --- slice into laps ---------------------------------------------------
    for sp in out.sessions:
        sp.laps = _slice(sp, cfg, options.pad_s)
        if not sp.laps:
            sp.problems.append("no complete lap or run in this session")
    _measure_quality(out, cfg, existing, library)
    apply_filters(out)
    out.sessions.sort(key=lambda s: (s.track_name, s.date, s.run.t0))
    return out


def _measure_quality(out, cfg, existing, library):
    """Record each lap's GPS coverage, so a filter can use it later.

    At AMP fourteen of twenty-seven laps have a 250-300 m hole in the same
    place, under trees. Whether that matters is the user's call, but they can
    only make it if the number is in front of them.
    """
    from . import compare as cmp_mod
    for tp in out.tracks:
        tm = None
        if tp.existing_id and library is not None:
            tm = library.map_of(tp.existing_id)
        if tm is None:
            runs = [sp.run for sp in tp.sessions]
            tm = _provisional_map(runs, tp.closed, tp.name)
        if tm is None:
            continue
        line = cmp_mod._line_length(tm)
        for sp in tp.sessions:
            for lp in sp.laps:
                try:
                    tv = cmp_mod.traversal(sp.run, tm, _index_of(sp.run, lp.t0),
                                           _index_of(sp.run, lp.t1) + 1,
                                           key="q", label="q",
                                           max_accuracy_m=cfg.quality.max_accuracy_m)
                except Exception:
                    tv = None
                if tv is None:
                    continue
                gaps = cmp_mod.gaps_of(tv)
                lp.gap_m = round(sum(b - a for a, b, _dt in gaps), 1)
                lp.measured_frac = round(
                    max(0.0, 1.0 - lp.gap_m / line) if line else 1.0, 4)


def _index_of(run, t):
    ts = run.values("timestamp")
    return int(np.searchsorted(ts, t))


def apply_filters(out):
    """Mark laps the options exclude. Re-runnable: clears its own marks first.

    Filtering is a mark rather than a removal so the import screen can show what
    a checkbox is about to leave behind. A lap that silently vanished from a list
    when a box was ticked would be the worst version of this.
    """
    o = out.options
    for sp in out.sessions:
        for lp in sp.laps:
            lp.dropped = ""
    for sp in out.sessions:
        laps = sp.laps
        if o.skip_edge_laps:
            for lp in laps:
                if not lp.representative:
                    lp.dropped = "out-lap or in-lap"
        if o.max_gap_m is not None:
            for lp in laps:
                if not lp.dropped and lp.gap_m > o.max_gap_m:
                    lp.dropped = "%.0f m with no GPS fix" % lp.gap_m
        if o.require_video:
            for lp in laps:
                if not lp.dropped and not lp.has_video:
                    lp.dropped = "no footage covers it"
        alive = [lp for lp in laps if lp.keep]
        if o.slower_than_best_pct is not None and alive:
            best = min(lp.duration for lp in alive)
            limit = best * (1.0 + o.slower_than_best_pct / 100.0)
            for lp in alive:
                if lp.duration > limit:
                    lp.dropped = ("%.1f%% off the session best"
                                  % (100 * (lp.duration / best - 1.0)))
        alive = [lp for lp in laps if lp.keep]
        if o.keep_best is not None and len(alive) > o.keep_best:
            for lp in sorted(alive, key=lambda x: x.duration)[o.keep_best:]:
                lp.dropped = "not among the %d quickest here" % o.keep_best
    return out


def _calibration(run):
    cal = getattr(run, "imu", None)
    if cal is None:
        return {"imu": None, "has_raw_imu": bool(imu_mod.has_imu(run))}
    return {
        "imu": {
            "trusted": bool(cal.trusted),
            "fit_r": round(float(cal.fit_r), 4),
            "g_magnitude": round(float(cal.g_magnitude), 4),
            "tilt_deg": round(float(cal.tilt_deg), 2),
            "heading_offset_deg": round(float(cal.heading_offset_deg), 2),
            "still_seconds": round(float(cal.still_seconds), 2),
            "notes": list(cal.notes or []),
        },
        "has_raw_imu": True,
    }


def _slice(sp, cfg, pad_s):
    """Laps when the shape says laps, otherwise the detected runs."""
    run, shape = sp.run, sp.shape
    out = []
    if shape.laps_usable:
        lp = laps_mod.laps(run, cfg.quality.max_accuracy_m)
        kept, _dropped = laps_mod.filter_laps(lp, exclude_edge=True)
        keep = {(l.number, round(l.t0, 3)) for l in kept}
        for l in lp:
            out.append(LapPlan(number=l.number, kind="lap", t0=l.t0, t1=l.t1,
                               distance_m=float(l.distance_m),
                               representative=(l.number, round(l.t0, 3)) in keep))
    else:
        for s in runs_mod.detect(run, max_accuracy_m=cfg.quality.max_accuracy_m):
            out.append(LapPlan(number=s.index, kind="run", t0=s.t0, t1=s.t1,
                               distance_m=float(s.distance_m),
                               representative=True))
    for lp in out:
        if sp.clips:
            lp.segments = video.plan_segments(sp.clips, lp.t0 - pad_s,
                                              lp.t1 + pad_s)
    return out


# -------------------------------------------------------------------- applying

def _write_lap_csv(src, dst, t0, t1, header_extra):
    """The lap's rows, with the session's calibration in the preamble.

    Written so the lap stands alone: the source CSV is not kept, so anything
    needed to interpret these rows has to be in this file.
    """
    with open(src, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    hi = next(i for i, r in enumerate(rows)
              if r and r[0].strip().lower() == "timestamp")
    head = [r[:] for r in rows[:hi]]
    for key, val in header_extra.items():
        head.append([key, json.dumps(val) if not isinstance(val, str) else val])
    head.append([])
    head += rows[hi:hi + 3]
    kept = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerows(head)
        for r in rows[hi + 3:]:
            if not r or not r[0]:
                continue
            try:
                t = float(r[0])
            except ValueError:
                continue
            if t0 <= t <= t1:
                w.writerow(r)
                kept += 1
    return kept


def _encode(ffmpeg, segments, dst, options, encoder="hevc_nvenc", verbose=False,
            nvenc_profile=None, fallback=None):
    """Trim and re-encode one lap, joining camera splits if it straddles one.

    `fallback` is the CPU encoder to retry with when an NVENC encode fails the
    way a missing or busy GPU does. The server proves its encoder at startup,
    so this is for the GPU going away later, not for choosing one.
    """
    try:
        return _encode_with(ffmpeg, segments, dst, options, encoder, verbose,
                            nvenc_profile)
    except subprocess.CalledProcessError as exc:
        from . import encoders
        err = exc.stderr.decode("utf-8", "replace") if isinstance(
            exc.stderr, bytes) else str(exc.stderr or "")
        if not (fallback and encoder.endswith("nvenc")
                and encoders.looks_like_nvenc_failure(err or encoder)):
            raise
        return _encode_with(ffmpeg, segments, dst, options, fallback, verbose,
                            None)


def _encode_with(ffmpeg, segments, dst, options, encoder, verbose,
                 nvenc_profile):
    dst.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, seg in enumerate(segments):
        part = dst if len(segments) == 1 else dst.with_name(
            "%s.part%d%s" % (dst.stem, i, dst.suffix))
        # Two-stage seek: coarse before -i, the remainder after. Measured on a
        # 111 s window, all three strategies land within 0.010 s of the asked-for
        # duration, but this one took 21.7 s against 35.3 for a single fast seek
        # and 51.8 for a fully accurate one.
        coarse = max(0.0, seg.seek - 5.0)
        fine = seg.seek - coarse
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", "%.3f" % coarse, "-i", str(seg.clip.path),
               "-ss", "%.3f" % fine, "-t", "%.3f" % seg.duration]
        cmd += options.video_args(encoder, nvenc_profile)
        cmd += ["-movflags", "+faststart", str(part)]
        subprocess.run(cmd, check=True, capture_output=not verbose)
        parts.append(part)
    if len(parts) > 1:
        listing = dst.with_suffix(".txt")
        listing.write_text("".join("file '%s'\n" % p.name for p in parts),
                           encoding="utf-8")
        # The join keeps the tag and the front-loaded index the parts were
        # written with: without them a lap that straddles a camera split was
        # the one clip a phone would not play, or not until it had all of it.
        tag = (["-tag:v", "hvc1"] if "hevc" in encoder or "265" in encoder
               else [])
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-c", "copy"] + tag + ["-movflags", "+faststart",
                                               str(dst)],
                       check=True, capture_output=not verbose, cwd=str(dst.parent))
        for p in parts:
            p.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
    return dst.stat().st_size if dst.exists() else 0


def apply(plan_obj, library, cfg, encode=True, verbose=True, on_progress=None,
          on_phase=None):
    """Build maps, cut laps, write records. Irreversible where it touches files."""
    ffmpeg = video.find_binary("ffmpeg", cfg.ffmpeg.ffmpeg)
    made = {"tracks": [], "days": [], "sessions": [], "laps": [], "bytes": 0}
    say = on_phase or (lambda _name: None)
    say("building track maps")

    # --- tracks and maps ---------------------------------------------------
    keymap = {}
    for tp in plan_obj.tracks:
        if tp.existing_id:
            keymap[tp.key] = tp.existing_id
            for nm in tp.source_names:
                library.observe_source_name(tp.existing_id, nm)
            continue
        tid = library.add_track(tp.name, closed=tp.closed,
                               source_names=sorted(tp.source_names))
        keymap[tp.key] = tid
        made["tracks"].append(tid)
        if tp.needs_map:
            runs = [sp.run for sp in tp.sessions]
            tm = _provisional_map(runs, tp.closed, tp.name, points=720)
            if tm is None:
                if verbose:
                    print("  ! could not build a map for %s" % tp.name)
            else:
                library.set_map(tid, tm,
                                sources=[(r.stem, None) for r in runs])
                if verbose:
                    print("  map %s: %.0f m, %s" % (
                        tp.name, tm.length_m,
                        "circuit" if tm.closed else "open course"))

    # --- days, sessions, laps ---------------------------------------------
    say("cutting laps")
    for sp in plan_obj.sessions:
        tid = keymap.get(sp.track_key)
        if tid is None:
            continue
        date = plan_obj.options.date_override or sp.date
        day_id = library.day_for(tid, date)
        if plan_obj.options.event:
            found = next((e for e in library.events()
                          if e["name"] == plan_obj.options.event), None)
            eid = found["id"] if found else library.add_event(
                plan_obj.options.event)
            library.set_event(day_id, eid)
        if day_id not in made["days"]:
            made["days"].append(day_id)
        sid = library.add_session(
            day_id, sp.stem, sp.run.t0, sp.run.duration,
            source_file=sp.path.name,
            source_track_name=(sp.run.meta.get("Track name") or "").strip('"'),
            sample_hz=sp.shape.sample_hz,
            calibration=sp.calibration,
            channels=[c for c in sp.run.df.columns],
            shape=sp.shape.as_dict())
        made["sessions"].append(sid)

        for lp in sp.laps:
            # A lap the options exclude is marked rather than removed, so that
            # the import screen can show what a checkbox leaves behind. Which
            # means `apply` has to honour the mark: without this it wrote every
            # lap and the filters were decoration.
            if not lp.keep:
                continue
            # The video's first frame in data time, and how much video there is.
            # Padding is clamped to the footage that exists, so this is not
            # t0 - pad on a lap that sits at the end of a clip.
            want_video = plan_obj.options.import_video and lp.has_video
            vt0 = lp.segments[0].data_t0 if want_video else None
            vsec = lp.video_s if want_video else 0.0
            lid = library.add_lap(
                sid, lp.number, lp.t0, lp.t1, kind=lp.kind,
                duration_s=lp.duration, distance_m=lp.distance_m,
                pad_s=plan_obj.pad_s, video_t0=vt0, video_s=vsec,
                representative=lp.representative,
                gap_m=lp.gap_m, measured_frac=lp.measured_frac)
            d = library.paths.lap_dir(lid)
            data_path = d / "data.csv"
            _write_lap_csv(sp.path, data_path,
                           lp.t0 - plan_obj.pad_s, lp.t1 + plan_obj.pad_s,
                           {"Lap": "%s %d" % (lp.kind, lp.number),
                            "Lap time": "%.3f" % lp.duration,
                            "Calibration": sp.calibration,
                            "Source session": sp.path.name})
            vbytes = 0
            if encode and lp.has_video and plan_obj.options.import_video:
                from . import encoders
                vbytes = _encode(ffmpeg, lp.segments, d / "video.mp4",
                                 plan_obj.options, cfg.encode.encoder,
                                 verbose=False,
                                 nvenc_profile=cfg.encode.nvenc_profile,
                                 fallback=encoders.cpu_encoder(cfg.encode))
            library.db.execute(
                "UPDATE laps SET video_path=?, data_path=?, video_bytes=?, "
                "data_bytes=? WHERE id=?",
                ("laps/%s/video.mp4" % lid if vbytes else "",
                 "laps/%s/data.csv" % lid,
                 vbytes, data_path.stat().st_size, lid))
            library.db.commit()
            made["laps"].append(lid)
            made["bytes"] += vbytes + data_path.stat().st_size
            if on_progress:
                on_progress(sp, lp, lid, vbytes)
    return made




def verify(made, library, plan_obj):
    """Check that what was planned is actually on disk, before saying so.

    The point of this is the sentence it lets the interface print: *these files
    are now in the library, you can delete the originals*. That sentence has to
    be earned. It checks every lap written -- the row, its data file, its video
    file where one was expected, and that neither is empty.
    """
    problems = []
    laps = 0
    data_bytes = video_bytes = 0
    for lid in made["laps"]:
        row = library.lap(lid)
        if row is None:
            problems.append("lap %s has no row" % lid)
            continue
        laps += 1
        data = library.paths.root / row["data_path"] if row["data_path"] else None
        if data is None or not data.is_file() or data.stat().st_size == 0:
            problems.append("lap %s has no telemetry file" % lid)
        else:
            data_bytes += data.stat().st_size
        if row["video_path"]:
            vid = library.paths.root / row["video_path"]
            if not vid.is_file() or vid.stat().st_size == 0:
                problems.append("lap %s has an empty video" % lid)
            else:
                video_bytes += vid.stat().st_size
    expected = len(plan_obj.kept())
    if laps != expected:
        problems.append("planned %d lap(s), wrote %d" % (expected, laps))
    # A file that could not be read is not in the library, so the folder holding
    # it is not safe to delete -- and this is the one sentence in the tool that
    # invites someone to delete irreplaceable footage.
    for name, why in plan_obj.skipped:
        problems.append("%s could not be read (%s) and was not imported"
                        % (name, why))

    sources = []
    if plan_obj.source is not None:
        csvs, clips = scan(plan_obj.source)
        sources = [str(p) for p in csvs] + [str(c.path) for c in clips]
    total = 0
    for p in sources:
        try:
            total += Path(p).stat().st_size
        except OSError:
            pass
    return {
        "ok": not problems,
        "problems": problems,
        "laps": laps,
        "bytes": data_bytes + video_bytes,
        "source": str(plan_obj.source) if plan_obj.source else "",
        "source_files": len(sources),
        "source_bytes": total,
        # Deliberate omissions, reported rather than hidden: laps a filter left
        # behind, and sessions the library already held. Both mean the folder
        # holds more than the library does, which someone about to delete it
        # should be told plainly.
        "dropped": len(plan_obj.dropped()),
        "already": len(plan_obj.already),
    }



# ------------------------------------------------------------------ reporting

def format_plan(p):
    lines = []
    lines.append("%s" % (p.source or "(no source)"))
    lines.append("%d session(s) to ingest across %d track(s)"
                 % (len(p.sessions), len(p.tracks)))
    if p.duplicates:
        lines.append("")
        lines.append("skipped as byte-identical duplicates:")
        for name, of in p.duplicates:
            lines.append("  %s  ==  %s" % (name, of))
    if p.already:
        lines.append("")
        lines.append("already in the library, skipped:")
        for name, held in p.already:
            lines.append("  %-44s %s  (%s)" % (name, held["name"], held["id"]))
    if p.skipped:
        lines.append("")
        lines.append("unreadable:")
        for name, why in p.skipped:
            lines.append("  ! %-44s %s" % (name, why))

    for tp in p.tracks:
        lines.append("")
        lines.append("%s  [%s]%s" % (
            tp.name, "circuit" if tp.closed else "open course",
            "   NEW TRACK, map will be built" if tp.needs_map
            else "   existing track"))
        if tp.source_names:
            lines.append("  names seen: %s" % ", ".join(sorted(tp.source_names)))
        for sp in tp.sessions:
            n_vid = sum(1 for l in sp.laps if l.has_video)
            split = sum(1 for l in sp.laps if len(l.segments) > 1)
            lines.append("  %-34s %s  %d %s%s%s"
                         % (sp.stem[:34], sp.date, len(sp.laps),
                            "lap(s)" if sp.shape.laps_usable else "run(s)",
                            ", %d with video" % n_vid if n_vid else
                            ", no footage",
                            ", %d across a camera split" % split if split else ""))
            lines.append("       %s: %s" % (sp.verdict, sp.note))
            for prob in sp.problems:
                lines.append("       ! %s" % prob)

    dropped = p.dropped()
    if dropped:
        lines.append("")
        why = {}
        for _sp, lp in dropped:
            why[lp.dropped] = why.get(lp.dropped, 0) + 1
        lines.append("%d lap(s) left out:" % len(dropped))
        for reason, n in sorted(why.items(), key=lambda kv: -kv[1]):
            lines.append("  %3d  %s" % (n, reason))

    est = p.estimate()
    o = p.options
    shape = "%s%s at CQ %d" % (
        "%dp" % o.height if o.height else "source resolution",
        ", %d fps" % o.fps if o.fps else "", o.cq)
    lines.append("")
    if not o.import_video:
        lines.append("%d lap(s), telemetry only -- no video will be written"
                     % est["laps"])
    else:
        lines.append("%d lap(s) to cut, %.0f s of video, %s, %.1f s padding "
                     "either side"
                     % (est["laps"], est["video_s"], shape, o.pad_s))
        lines.append("roughly %s at about %.1f Mbps -- an estimate from measured "
                     "encodes, not a promise"
                     % (_human(est["bytes"]), est["mbps"]))
    return "\n".join(lines)


def _human(n):
    from .library import human
    return human(n)
