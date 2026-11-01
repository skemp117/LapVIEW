"""ffprobe wrappers, clip discovery, run pairing and the data<->clip timeline."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".mkv", ".avi", ".insv", ".lrv")

# Where ffmpeg tends to live on this machine when it is not on PATH.
_FALLBACK_DIRS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Migaku" / "MigakuShared",
    Path("C:/ffmpeg/bin"),
    Path("C:/Program Files/ffmpeg/bin"),
)

_cache = {}


def find_binary(name, configured=None):
    """Resolve ffmpeg/ffprobe. Explicit config wins, then PATH, then fallbacks."""
    key = (name, str(configured))
    if key in _cache:
        return _cache[key]
    if configured:
        p = Path(configured)
        if p.is_dir():
            p = p / (name + (".exe" if os.name == "nt" else ""))
        if p.exists():
            _cache[key] = str(p)
            return _cache[key]
        raise FileNotFoundError("configured %s not found: %s" % (name, configured))
    onpath = shutil.which(name)
    if onpath:
        _cache[key] = onpath
        return onpath
    for d in _FALLBACK_DIRS:
        cand = d / (name + (".exe" if os.name == "nt" else ""))
        if cand.exists():
            _cache[key] = str(cand)
            return _cache[key]
    raise FileNotFoundError(
        "%s not found on PATH. Install it or set CONFIG['ffmpeg']['%s']."
        % (name, name))


@dataclass
class Clip:
    path: Path
    start_unix: float          # container creation_time, UTC
    duration: float
    fps: float
    width: int
    height: int
    rotation: int = 0
    has_audio: bool = False
    audio_rate: int = 0
    codec: str = ""
    offset: float = 0.0        # data_time = start_unix + offset + n / fps
    sync_method: str = "none"
    sync_confidence: float = 0.0    # prominence in sd, or inf for manual
    sync_info: dict = field(default_factory=dict)
    probed_frames: int = 0     # ffprobe nb_frames, 0 when unavailable

    @property
    def stem(self):
        return self.path.stem

    @property
    def n_frames(self):
        """Frame count, preferring ffprobe's own count over duration * fps.

        The container duration is rounded and can sit a few frames short of the
        real stream length.
        """
        if self.probed_frames > 0:
            return self.probed_frames
        return int(round(self.duration * self.fps))

    @property
    def display_size(self):
        """Frame size as displayed, i.e. after rotation metadata is applied."""
        if self.rotation % 180 == 90:
            return self.height, self.width
        return self.width, self.height

    @property
    def data_t0(self):
        return self.start_unix + self.offset

    @property
    def data_t1(self):
        return self.start_unix + self.offset + self.duration

    def frame_time(self, n):
        return self.start_unix + self.offset + n / self.fps


def probe(path, ffprobe=None):
    """One ffprobe call per clip; everything the pipeline needs about it."""
    path = Path(path)
    exe = find_binary("ffprobe", ffprobe)
    cmd = [exe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    d = json.loads(raw)

    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError("%s: no video stream" % path.name)
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)

    fmt = d.get("format", {})
    ct = (fmt.get("tags", {}).get("creation_time")
          or v.get("tags", {}).get("creation_time"))
    if not ct:
        # Fall back to the file's mtime; better than refusing to pair at all.
        start = path.stat().st_mtime - float(fmt.get("duration", 0.0))
    else:
        start = _parse_iso_utc(ct)

    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"]))) % 360
    if not rot and "rotate" in (v.get("tags") or {}):
        rot = int(float(v["tags"]["rotate"])) % 360

    return Clip(
        path=path,
        start_unix=start,
        duration=float(fmt.get("duration") or v.get("duration") or 0.0),
        fps=_fps(v),
        width=int(v["width"]),
        height=int(v["height"]),
        rotation=rot,
        has_audio=a is not None,
        audio_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else 0,
        codec=v.get("codec_name", ""),
        probed_frames=int(v.get("nb_frames") or 0),
    )


def _fps(stream):
    """Nominal frame rate. `r_frame_rate` is the CFR target; `avg_frame_rate`
    drifts a few ppm below it on phone recordings, and using the drifted value
    accumulates error over a 20-minute clip."""
    for key in ("r_frame_rate", "avg_frame_rate"):
        val = stream.get(key)
        if val and val != "0/0":
            f = float(Fraction(val))
            if f > 0:
                return f
    return 30.0


def _parse_iso_utc(s):
    s = s.strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S"):
        try:
            d = dt.datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.UTC)
            return d.timestamp()
        except ValueError:
            continue
    raise ValueError("unparseable creation_time %r" % s)


def discover(footage_dir, ffprobe=None, recursive=False):
    """Probe every video in the footage directory, ordered by start time.

    `recursive` for a folder someone points at: people keep a session's video in
    a subfolder as often as not, and a card copied off a camera has its own tree.
    """
    root = Path(footage_dir)
    if not root.is_dir():
        return []
    walk = sorted(root.rglob("*")) if recursive else sorted(root.iterdir())
    clips = []
    for p in walk:
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            try:
                clips.append(probe(p, ffprobe))
            except Exception as exc:
                print("  ! ffprobe failed on %s: %s" % (p.name, exc))
    clips.sort(key=lambda c: c.start_unix)
    return clips


@dataclass
class Pairing:
    run_stem: str
    clips: list = field(default_factory=list)
    strategy: str = "none"
    notes: list = field(default_factory=list)


def pair(runs, clips, manifest=None, drift_margin_s=120.0):
    """Match each run to its ordered clip list.

    Strategy order: explicit manifest, then filename prefix, then container
    timestamp overlap. The chosen strategy is recorded per run.
    """
    used = set()
    out = []
    for run in runs:
        p = Pairing(run_stem=run.stem)

        if manifest and run.stem in manifest:
            names = manifest[run.stem]
            if isinstance(names, dict):
                names = names.get("clips", [])
            by_name = {c.path.name: c for c in clips}
            by_stem = {c.path.stem: c for c in clips}
            for n in names:
                c = by_name.get(n) or by_stem.get(Path(n).stem)
                if c is None:
                    p.notes.append("manifest names missing clip %r" % n)
                else:
                    p.clips.append(c)
            p.strategy = "manifest"

        if not p.clips:
            got = [c for c in clips if c.path.stem.startswith(run.stem)]
            if got:
                p.clips = sorted(got, key=lambda c: c.start_unix)
                p.strategy = "filename-prefix"

        if not p.clips:
            lo = run.t0 - drift_margin_s
            hi = run.t1 + drift_margin_s
            got = [c for c in clips
                   if c.start_unix < hi and (c.start_unix + c.duration) > lo]
            got = [c for c in got if id(c) not in used]
            if got:
                p.clips = sorted(got, key=lambda c: c.start_unix)
                p.strategy = "timestamp-overlap"

        for c in p.clips:
            used.add(id(c))
        if not p.clips:
            p.notes.append("no clips matched")
        out.append(p)

    orphans = [c for c in clips if id(c) not in used]
    return out, orphans


def load_manifest(path="runs.json"):
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


@dataclass
class Segment:
    """A contiguous piece of one clip that a target needs.

    A target's window is expressed in data time; a segment is where that lands
    inside a particular clip. A window spanning a clip boundary produces two
    segments, which is not exotic -- two laps in this session do exactly that,
    because the camera split mid-lap.
    """

    clip: Clip
    seek: float          # seconds into the clip
    duration: float      # seconds of clip to render
    data_t0: float       # data time of the segment's first frame

    @property
    def data_t1(self):
        return self.data_t0 + self.duration


def plan_segments(clips, t0, t1, min_duration=0.25):
    """Pieces of `clips` covering data time [t0, t1], in order.

    Gaps between clips simply do not appear: the camera was not recording, and
    the spec is explicit that a gap must never be papered over by stretching
    data across it.
    """
    segs = []
    for clip in sorted(clips, key=lambda c: c.start_unix):
        a = max(t0, clip.data_t0)
        b = min(t1, clip.data_t1)
        if b - a < min_duration:
            continue
        segs.append(Segment(clip=clip, seek=a - clip.data_t0,
                            duration=b - a, data_t0=a))
    return segs


def timeline(pairing, run):
    """Per-clip data-time coverage, plus the gaps between clips.

    Gaps are reported, never papered over: stretching data across a gap would
    quietly desynchronise everything after it.
    """
    rows = []
    prev_end = None
    for c in pairing.clips:
        gap = None if prev_end is None else c.data_t0 - prev_end
        rows.append({
            "clip": c,
            "data_t0": c.data_t0,
            "data_t1": c.data_t1,
            "gap_before_s": gap,
            "covers_data": (max(c.data_t0, run.t0), min(c.data_t1, run.t1)),
            "data_missing_head": max(0.0, c.data_t0 - run.t0) if prev_end is None else 0.0,
        })
        prev_end = c.data_t1
    return rows


def check_uniform(clips):
    """Mismatched resolutions across a run's clips must fail loudly -- a single
    overlay geometry cannot serve two frame sizes."""
    sizes = {c.display_size for c in clips}
    if len(sizes) > 1:
        raise ValueError(
            "clips have mismatched display sizes: "
            + ", ".join("%s=%dx%d" % (c.path.name, *c.display_size) for c in clips))
    rates = {round(c.fps, 3) for c in clips}
    return sizes.pop() if sizes else (0, 0), sorted(rates)


def fmt_unix(t):
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%H:%M:%S.%f")[:-3] + "Z"
