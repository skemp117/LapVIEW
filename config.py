"""CONFIG validation.

`main.py` holds a plain dict the user edits. This module turns it into typed,
range-checked objects and complains about unknown keys, because a typo in a
layout key that silently does nothing is worse than a crash.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

ENCODERS = ("libx264", "libx265", "h264_nvenc", "hevc_nvenc")
NVENC_PROFILES = ("auto", "pascal", "turing")

KNOWN_ELEMENTS = ("track_map", "speedo", "tacho", "gmeter", "throttle",
                  # Registered but unimplemented; enabling one raises.
                  "lap_timer", "sector_delta", "speed_bar", "coolant_temp")

IMPLEMENTED_ELEMENTS = ("track_map", "speedo", "tacho", "gmeter", "throttle")

ANCHORS = ("top-left", "top-right", "top-center",
           "bottom-left", "bottom-right", "bottom-center",
           "center", "center-left", "center-right")


class ConfigError(ValueError):
    pass


def _check_unknown(where, given, allowed):
    extra = sorted(set(given) - set(allowed))
    if extra:
        raise ConfigError(
            "%s: unknown key(s) %s. Allowed: %s"
            % (where, ", ".join(repr(e) for e in extra), ", ".join(sorted(allowed))))


def _rng(where, value, lo, hi):
    if value is None:
        return value
    if not (lo <= float(value) <= hi):
        raise ConfigError("%s: %r out of range [%g, %g]" % (where, value, lo, hi))
    return float(value)


def _colour(where, v):
    if v is None:
        return None
    t = tuple(float(x) for x in v)
    if len(t) not in (3, 4):
        raise ConfigError("%s: colour needs 3 or 4 components, got %r" % (where, v))
    if any(not (0.0 <= x <= 1.0) for x in t):
        raise ConfigError("%s: colour components must be 0..1, got %r" % (where, v))
    return t if len(t) == 4 else t + (1.0,)


# --------------------------------------------------------------- sections

@dataclass
class SyncCfg:
    strategy: str = "audio_rpm"
    search_window_s: float = 10.0
    min_peak_ratio: float = 1.5
    correlate_span_s: float = 0.0      # 0 = correlate the whole overlap
    audio_sample_rate: int = 8000
    env_hz: int = 25
    highpass_s: float = 30.0
    wide_lag_s: float = 60.0
    guard_s: float = 3.0
    min_span_s: float = 60.0
    min_prominence: float = 3.5
    ref_channel: str = "auto"
    consensus: bool = True
    offset: float | None = None
    refresh: bool = False

    def validate(self):
        if self.strategy not in ("container", "audio_rpm", "audio_accel", "manual"):
            raise ConfigError("sync.strategy: %r not one of container, audio_rpm, "
                              "audio_accel, manual" % self.strategy)
        _rng("sync.search_window_s", self.search_window_s, 0.1, 120.0)
        _rng("sync.min_peak_ratio", self.min_peak_ratio, 1.0, 100.0)
        if self.correlate_span_s:
            _rng("sync.correlate_span_s", self.correlate_span_s, 30.0, 3600.0)
        _rng("sync.env_hz", self.env_hz, 5, 400)
        _rng("sync.highpass_s", self.highpass_s, 0.0, 600.0)
        _rng("sync.wide_lag_s", self.wide_lag_s, 5.0, 600.0)
        _rng("sync.guard_s", self.guard_s, 0.05, 60.0)
        _rng("sync.min_span_s", self.min_span_s, 5.0, 3600.0)
        _rng("sync.min_prominence", self.min_prominence, 0.0, 100.0)
        if self.wide_lag_s < self.search_window_s:
            raise ConfigError("sync.wide_lag_s must be >= sync.search_window_s "
                              "(the noise floor is measured over the wider range)")
        if self.strategy == "manual" and self.offset is None:
            raise ConfigError("sync.strategy is 'manual' but sync.offset is unset "
                              "(set it, or pass --offset)")
        return self

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class QualityCfg:
    max_accuracy_m: float = 10.0
    dim_alpha: float = 0.4

    def validate(self):
        # The GPS accuracy channel floors at exactly 10.0 m in this data, so a
        # gate below 10 discards every row. Guard against that footgun.
        _rng("quality.max_accuracy_m", self.max_accuracy_m, 1.0, 1000.0)
        _rng("quality.dim_alpha", self.dim_alpha, 0.0, 1.0)
        return self


@dataclass
class EncodeCfg:
    encoder: str = "hevc_nvenc"
    crf: int = 20               # libx264 / libx265
    cq: int = 26                # nvenc constant-quality target
    audio_bitrate: str = "192k"
    preset: str = "p7"          # nvenc p1..p7 (p7 = slowest, best); x264 names
    tune: str = "hq"
    multipass: str = "disabled"  # disabled | qres | fullres
    rc_lookahead: int = 32
    spatial_aq: bool = True
    temporal_aq: bool = True
    aq_strength: int = 8
    bframes: int = 3
    b_ref_mode: str = "middle"  # B-frames usable as references (Turing+)
    maxrate: str | None = None  # e.g. "40M" to cap peaks
    hwaccel_decode: bool = True
    copy_audio: bool = True
    pix_fmt: str = "yuv420p"
    fps: float | None = None    # None = match source
    # Slice the overlay into dirty regions for the ffmpeg pipe, instead of
    # shipping the whole frame. Which is faster depends on how many renders are
    # running at once, measured on 30 s segments:
    #
    #     workers   full frame   2 regions   3 regions
    #        1         2.35x       2.24x       1.86x
    #        4         3.83x       4.50x       3.92x
    #
    # Alone, the extra `overlay` filter per region costs more than the smaller
    # payload saves. Concurrently, the four streams contend for memory bandwidth
    # and cutting each one's payload to 26% wins by 17%. "auto" picks regions
    # only when more than one render is in flight. The preview path always uses
    # regions regardless, since there the payload is the whole point.
    overlay_regions: object = "auto"        # "auto" | True | False
    max_overlay_regions: int = 2
    extra_args: list = field(default_factory=list)
    # Which NVENC generation the arguments are written for. "turing" is Turing
    # and newer (HEVC B-frames, B-frames as references); "pascal" leaves both
    # out, because a GTX 10-series refuses to open an encoder that asks for
    # them. "auto" asks nvidia-smi at startup (racechrono/encoders.py).
    nvenc_profile: str = "auto"
    # What to encode with when NVENC cannot be opened at all: "auto" keeps the
    # codec (libx265 for HEVC, libx264 for H.264), "none" fails instead.
    cpu_fallback: str = "auto"

    @property
    def is_nvenc(self):
        return self.encoder.endswith("nvenc")

    def resolve_overlay_regions(self, workers):
        """Turn "auto" into a decision now that the worker count is known."""
        if self.overlay_regions == "auto":
            return workers > 1
        return bool(self.overlay_regions)

    def validate(self):
        if self.encoder not in ENCODERS:
            raise ConfigError("encode.encoder: %r not one of %s"
                              % (self.encoder, ", ".join(ENCODERS)))
        if self.nvenc_profile not in NVENC_PROFILES:
            raise ConfigError("encode.nvenc_profile: %r not one of %s"
                              % (self.nvenc_profile, ", ".join(NVENC_PROFILES)))
        if self.cpu_fallback not in ("auto", "none", "libx264", "libx265"):
            raise ConfigError("encode.cpu_fallback: %r not one of auto, none, "
                              "libx264, libx265" % self.cpu_fallback)
        _rng("encode.crf", self.crf, 0, 51)
        _rng("encode.cq", self.cq, 0, 51)
        _rng("encode.rc_lookahead", self.rc_lookahead, 0, 250)
        _rng("encode.aq_strength", self.aq_strength, 1, 15)
        _rng("encode.bframes", self.bframes, 0, 16)
        _rng("encode.max_overlay_regions", self.max_overlay_regions, 1, 16)
        if self.overlay_regions not in ("auto", True, False):
            raise ConfigError("encode.overlay_regions: %r not one of 'auto', "
                              "True, False" % (self.overlay_regions,))
        if self.multipass not in ("disabled", "qres", "fullres"):
            raise ConfigError("encode.multipass: %r not one of disabled, qres, "
                              "fullres" % self.multipass)
        if self.b_ref_mode not in ("disabled", "each", "middle"):
            raise ConfigError("encode.b_ref_mode: %r not one of disabled, each, "
                              "middle" % self.b_ref_mode)
        if self.is_nvenc and not self.preset.startswith("p"):
            raise ConfigError("encode.preset: %r is an x264 preset name, but the "
                              "encoder is %s, which wants p1..p7"
                              % (self.preset, self.encoder))
        if not self.is_nvenc and self.preset.startswith("p"):
            raise ConfigError("encode.preset: %r is an NVENC preset, but the "
                              "encoder is %s, which wants a name like 'medium'"
                              % (self.preset, self.encoder))
        if self.fps is not None:
            _rng("encode.fps", self.fps, 1.0, 240.0)
        return self


@dataclass
class TrimCfg:
    """Where a session's output starts and ends, derived from the telemetry."""

    enabled: bool = True
    move_threshold_ms: float = 1.0   # m/s; above GPS standstill noise
    pre_s: float = 5.0
    post_s: float = 5.0
    lap_buffer_s: float = 3.0        # padding either side of an exported lap

    def validate(self):
        _rng("trim.move_threshold_ms", self.move_threshold_ms, 0.0, 30.0)
        _rng("trim.pre_s", self.pre_s, 0.0, 600.0)
        _rng("trim.post_s", self.post_s, 0.0, 600.0)
        _rng("trim.lap_buffer_s", self.lap_buffer_s, 0.0, 120.0)
        return self


@dataclass
class ImuCfg:
    """Per-run calibration of a raw accelerometer/gyroscope, if the session has
    one. Everything is solved from the data; nothing here is car-specific."""

    enabled: bool = True
    still_speed_ms: float = 0.3
    still_gyro_dps: float = 5.0
    still_accel_sd: float = 0.05
    min_still_s: float = 0.5
    lowpass_hz: float = 5.0
    grid_hz: float = 100.0
    fit_min_speed_ms: float = 4.0
    min_fit_r: float = 0.45

    def validate(self):
        _rng("imu.still_speed_ms", self.still_speed_ms, 0.0, 10.0)
        _rng("imu.still_gyro_dps", self.still_gyro_dps, 0.1, 90.0)
        _rng("imu.still_accel_sd", self.still_accel_sd, 0.001, 1.0)
        _rng("imu.min_still_s", self.min_still_s, 0.1, 60.0)
        _rng("imu.lowpass_hz", self.lowpass_hz, 0.0, 200.0)
        _rng("imu.grid_hz", self.grid_hz, 10.0, 1000.0)
        _rng("imu.fit_min_speed_ms", self.fit_min_speed_ms, 0.0, 50.0)
        _rng("imu.min_fit_r", self.min_fit_r, 0.0, 1.0)
        if self.lowpass_hz >= self.grid_hz / 2.0:
            raise ConfigError("imu.lowpass_hz must be below half imu.grid_hz")
        return self

    def as_kwargs(self):
        return {k: getattr(self, k) for k in
                ("still_speed_ms", "still_gyro_dps", "still_accel_sd",
                 "min_still_s", "lowpass_hz", "grid_hz", "fit_min_speed_ms",
                 "min_fit_r")}


@dataclass
class FfmpegCfg:
    ffmpeg: str | None = None
    ffprobe: str | None = None

    def validate(self):
        return self


@dataclass
class ElementCfg:
    """Common element geometry plus a free-form tail for gauge-specific keys."""

    name: str
    enabled: bool = False
    anchor: str = "bottom-center"
    x: float | None = None
    y: float = 0.98
    opts: dict = field(default_factory=dict)

    def validate(self):
        if self.anchor not in ANCHORS:
            raise ConfigError("elements.%s.anchor: %r not one of %s"
                              % (self.name, self.anchor, ", ".join(ANCHORS)))
        _rng("elements.%s.x" % self.name, self.x, -1.0, 2.0)
        _rng("elements.%s.y" % self.name, self.y, -1.0, 2.0)
        if self.enabled and self.name not in IMPLEMENTED_ELEMENTS:
            raise ConfigError(
                "elements.%s is enabled but not implemented in v1. Registered "
                "stubs: %s" % (self.name,
                               ", ".join(e for e in KNOWN_ELEMENTS
                                         if e not in IMPLEMENTED_ELEMENTS)))
        return self

    def get(self, key, default=None):
        return self.opts.get(key, default)


@dataclass
class Config:
    maps_dir: Path = Path("Maps")
    library_dir: Path = Path("library")
    output_dir: Path = Path("Output")
    cache_dir: Path = Path("cache")
    inbox_dir: Path = Path("inbox")
    map_file: Path | None = None
    manifest: Path = Path("runs.json")
    workers: int | None = None
    sync: SyncCfg = field(default_factory=SyncCfg)
    quality: QualityCfg = field(default_factory=QualityCfg)
    trim: TrimCfg = field(default_factory=TrimCfg)
    imu: ImuCfg = field(default_factory=ImuCfg)
    encode: EncodeCfg = field(default_factory=EncodeCfg)
    ffmpeg: FfmpegCfg = field(default_factory=FfmpegCfg)
    elements: dict = field(default_factory=dict)

    def element(self, name):
        return self.elements.get(name)

    def enabled_elements(self):
        return [e for e in self.elements.values() if e.enabled]


_TOP_KEYS = {"maps_dir", "output_dir", "cache_dir", "library_dir", "inbox_dir",
             "map_file", "manifest", "workers", "sync", "quality", "trim", "imu",
             "encode", "ffmpeg", "elements"}

_ELEMENT_COMMON = {"enabled", "anchor", "x", "y"}


def bind(cfg, run):
    """Resolve list-valued channel options against what this run actually has.

    A channel option may name a list of candidates in preference order, so a
    layout can ask for the calibrated IMU channel and fall back to RaceChrono's
    GPS-derived one on a session logged without an accelerometer. Resolution has
    to happen per run, because two sessions of the same car can carry different
    channel sets -- which is exactly what happened when the phone's IMU was
    switched on.
    """
    import copy as _copy
    out = _copy.deepcopy(cfg)
    for el in out.elements.values():
        if not el.enabled:
            continue
        for key, val in list(el.opts.items()):
            if not (key == "channel" or key.endswith("_channel")):
                continue
            if isinstance(val, str) or val is None:
                continue
            chosen = next((c for c in val if run.has(c)), None)
            if chosen is None:
                raise ConfigError(
                    "elements.%s.%s: none of %s is present in %s. Available: %s"
                    % (el.name, key, ", ".join(map(repr, val)), run.path.name,
                       ", ".join(run.df.columns)))
            el.opts[key] = chosen
            el.opts.setdefault("_resolved", {})[key] = chosen
    return out


def load(raw):
    """Validate a CONFIG dict into a :class:`Config`."""
    if not isinstance(raw, dict):
        raise ConfigError("CONFIG must be a dict")
    _check_unknown("CONFIG", raw.keys(), _TOP_KEYS)

    cfg = Config()
    for key in ("maps_dir", "output_dir", "cache_dir", "library_dir",
                "inbox_dir", "manifest"):
        if key in raw and raw[key] is not None:
            setattr(cfg, key, Path(raw[key]))
    if raw.get("map_file"):
        cfg.map_file = Path(raw["map_file"])
    if raw.get("workers") is not None:
        cfg.workers = int(_rng("CONFIG.workers", raw["workers"], 1, 256))

    for key, klass in (("sync", SyncCfg), ("quality", QualityCfg),
                       ("trim", TrimCfg), ("imu", ImuCfg),
                       ("encode", EncodeCfg), ("ffmpeg", FfmpegCfg)):
        sub = raw.get(key) or {}
        allowed = {f.name for f in fields(klass)}
        _check_unknown("CONFIG['%s']" % key, sub.keys(), allowed)
        setattr(cfg, key, klass(**sub).validate())

    elements = raw.get("elements") or {}
    _check_unknown("CONFIG['elements']", elements.keys(), KNOWN_ELEMENTS)
    for name in KNOWN_ELEMENTS:
        spec = dict(elements.get(name) or {})
        common = {k: spec.pop(k) for k in list(spec) if k in _ELEMENT_COMMON}
        el = ElementCfg(name=name, opts=spec,
                        enabled=bool(common.get("enabled", False)),
                        anchor=common.get("anchor", "bottom-center"),
                        x=common.get("x"),
                        y=common.get("y", 0.98))
        _validate_element_opts(el)
        cfg.elements[name] = el.validate()

    return cfg


_ELEMENT_OPTS = {
    "track_map": {"height", "stroke", "outline", "color", "outline_color",
                  "marker", "show_start_finish", "start_finish_color",
                  "clamp_marker"},
    "speedo": {"diameter", "channel", "unit", "label", "min", "max", "major",
              "minor", "backing_alpha", "needle_color", "text_color",
              "sweep_deg", "label_divisor", "decimals", "redline",
              "redline_color", "input_min", "input_max", "clamp", "_resolved"},
    "tacho": {"diameter", "channel", "unit", "label", "min", "max", "major",
              "minor", "backing_alpha", "needle_color", "text_color",
              "sweep_deg", "label_divisor", "decimals", "redline",
              "redline_color", "input_min", "input_max", "clamp", "_resolved"},
    "throttle": {"diameter", "channel", "unit", "label", "min", "max", "major",
              "minor", "backing_alpha", "needle_color", "text_color",
              "sweep_deg", "label_divisor", "decimals", "redline",
              "redline_color", "input_min", "input_max", "clamp", "_resolved"},
    "gmeter": {"diameter", "rings", "invert_lateral", "invert_longitudinal",
               "trail_s", "backing_alpha", "dot_color", "text_color",
               "lateral_channel", "longitudinal_channel", "magnitude_channel",
               "_resolved"},
    "lap_timer": set(), "sector_delta": set(), "speed_bar": set(),
    "coolant_temp": set(),
}
_OFFSET_KEYS = {"offset_d"}


def _validate_element_opts(el):
    allowed = set(_ELEMENT_OPTS.get(el.name, set())) | _OFFSET_KEYS
    _check_unknown("CONFIG['elements']['%s']" % el.name, el.opts.keys(), allowed)

    for key, val in list(el.opts.items()):
        if key == "channel" or key.endswith("_channel"):
            if isinstance(val, (list, tuple)):
                if not val or not all(isinstance(c, str) and c for c in val):
                    raise ConfigError(
                        "elements.%s.%s: a channel list must hold non-empty "
                        "names in preference order" % (el.name, key))
                el.opts[key] = list(val)
            elif val is not None and not isinstance(val, str):
                raise ConfigError("elements.%s.%s: expected a channel name or a "
                                  "list of them, got %r" % (el.name, key, val))

    for key in ("height", "diameter", "stroke", "outline"):
        if key in el.opts:
            _rng("elements.%s.%s" % (el.name, key), el.opts[key], 0.0, 2.0)
    if "offset_d" in el.opts:
        _rng("elements.%s.offset_d" % el.name, el.opts["offset_d"], -20.0, 20.0)
    for key in ("color", "outline_color", "needle_color", "text_color",
                "dot_color", "redline_color", "start_finish_color"):
        if key in el.opts:
            el.opts[key] = _colour("elements.%s.%s" % (el.name, key), el.opts[key])
    if "marker" in el.opts:
        m = dict(el.opts["marker"])
        _check_unknown("elements.%s.marker" % el.name, m.keys(),
                       {"color", "size", "shape"})
        if "color" in m:
            m["color"] = _colour("elements.%s.marker.color" % el.name, m["color"])
        if "size" in m:
            _rng("elements.%s.marker.size" % el.name, m["size"], 0.0, 0.5)
        el.opts["marker"] = m
    if "min" in el.opts and "max" in el.opts:
        if float(el.opts["max"]) <= float(el.opts["min"]):
            raise ConfigError("elements.%s: max must exceed min" % el.name)
    lo, hi = el.opts.get("input_min"), el.opts.get("input_max")
    if (lo is None) != (hi is None):
        raise ConfigError("elements.%s: input_min and input_max must be set "
                          "together (they define the sender's rails)" % el.name)
    if lo is not None and float(hi) <= float(lo):
        raise ConfigError("elements.%s: input_max must exceed input_min"
                          % el.name)
    if "rings" in el.opts:
        rings = list(el.opts["rings"])
        if not rings or any(float(r) <= 0 for r in rings):
            raise ConfigError("elements.%s.rings must be positive values" % el.name)
        el.opts["rings"] = sorted(float(r) for r in rings)
    if "trail_s" in el.opts:
        _rng("elements.%s.trail_s" % el.name, el.opts["trail_s"], 0.0, 10.0)
    if "backing_alpha" in el.opts:
        _rng("elements.%s.backing_alpha" % el.name, el.opts["backing_alpha"], 0.0, 1.0)
    if "sweep_deg" in el.opts:
        _rng("elements.%s.sweep_deg" % el.name, el.opts["sweep_deg"], 30.0, 350.0)


# ------------------------------------------------------------ environment
#
# A container is configured with environment variables, not by editing main.py.
# Everything here is optional, and with none of them set CONFIG comes back
# exactly as it went in -- the desktop install is configured the way it always
# was. Paths given this way are made absolute, so nothing depends on the
# directory the server happened to be started from (`cache_dir` used to).

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

# config key, its own variable, its folder under LAPVIEW_DATA_DIR
ENV_DIRS = (("library_dir", "LAPVIEW_LIBRARY_DIR", "library"),
            ("output_dir", "LAPVIEW_OUTPUT_DIR", "output"),
            ("cache_dir", "LAPVIEW_CACHE_DIR", "cache"),
            ("inbox_dir", "LAPVIEW_INBOX_DIR", "inbox"),
            ("maps_dir", "LAPVIEW_MAPS_DIR", "maps"))


def env_flag(value, name="value"):
    """A yes/no environment value. Anything else is a mistake worth naming."""
    v = str(value).strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError("%s: %r is not a yes/no value (use 1 or 0)" % (name, value))


def apply_env(raw, environ=None, root=None):
    """CONFIG with any LAPVIEW_* environment variables laid over it.

    LAPVIEW_DATA_DIR puts library/, output/, cache/ and inbox/ under one folder
    (and maps/, when that folder exists -- otherwise the maps shipped with the
    code are used). Each also has its own override, e.g. LAPVIEW_LIBRARY_DIR,
    which wins over the data dir. LAPVIEW_ENCODER, LAPVIEW_ENCODER_PRESET,
    LAPVIEW_NVENC_PROFILE, LAPVIEW_NVENC_TEMPORAL_AQ, LAPVIEW_HWACCEL_DECODE and
    LAPVIEW_CPU_FALLBACK set the matching `encode` keys; LAPVIEW_FFMPEG and
    LAPVIEW_FFPROBE the binaries. The result still goes through `load`, so a bad
    value is reported the same way a bad CONFIG entry is.
    """
    env = os.environ if environ is None else environ
    root = Path(root) if root else Path(__file__).resolve().parent
    out = copy.deepcopy(raw)

    def get(name):
        return str(env.get(name) or "").strip()

    data = get("LAPVIEW_DATA_DIR")
    if data:
        base = Path(data).expanduser().resolve()
        for key, _var, sub in ENV_DIRS:
            if key != "maps_dir":
                out[key] = str(base / sub)
        maps = base / "maps"
        out["maps_dir"] = str(maps if maps.is_dir()
                              else (root / (raw.get("maps_dir") or "Maps")).resolve())
    for key, var, _sub in ENV_DIRS:
        if get(var):
            out[key] = str(Path(get(var)).expanduser().resolve())

    enc = dict(out.get("encode") or {})
    name = get("LAPVIEW_ENCODER").lower()
    if name:
        if name not in ENCODERS:
            raise ConfigError("LAPVIEW_ENCODER: %r not one of %s"
                              % (name, ", ".join(ENCODERS)))
        enc["encoder"] = name
        # NVENC and x264/x265 name their presets differently, and `load`
        # rejects a mismatch. Switching encoder switches the preset family.
        preset = str(enc.get("preset") or "")
        if name.endswith("nvenc"):
            if not preset.startswith("p"):
                enc["preset"] = "p7"
        elif not preset or preset.startswith("p"):
            enc["preset"] = "medium"
    if get("LAPVIEW_ENCODER_PRESET"):
        enc["preset"] = get("LAPVIEW_ENCODER_PRESET")
    if get("LAPVIEW_NVENC_PROFILE"):
        enc["nvenc_profile"] = get("LAPVIEW_NVENC_PROFILE").lower()
    for key, var in (("temporal_aq", "LAPVIEW_NVENC_TEMPORAL_AQ"),
                     ("hwaccel_decode", "LAPVIEW_HWACCEL_DECODE")):
        if get(var):
            enc[key] = env_flag(get(var), var)
    if get("LAPVIEW_CPU_FALLBACK"):
        enc["cpu_fallback"] = get("LAPVIEW_CPU_FALLBACK").lower()
    if enc != (out.get("encode") or {}):
        out["encode"] = enc

    ff = dict(out.get("ffmpeg") or {})
    for key, var in (("ffmpeg", "LAPVIEW_FFMPEG"), ("ffprobe", "LAPVIEW_FFPROBE")):
        if get(var):
            ff[key] = get(var)
    if ff != (out.get("ffmpeg") or {}):
        out["ffmpeg"] = ff
    return out
