"""Data <-> video alignment.

`offset` is defined throughout as the correction added to a clip's container
timestamp: ``data_time = clip.start_unix + offset + frame / fps``. A positive
offset means the camera clock ran early relative to GPS time.

Three strategies:
  container  -- trust `creation_time`. Free, but cameras stamp inconsistently
                and phone clocks drift against GPS.
  audio_rpm  -- cross-correlate the audio energy envelope against an engine-load
                channel. Two things matter here and both were found the hard way
                on real footage:

                * The reference channel must have **no intrinsic lag** against
                  audio energy. `throttle_pos` is instantaneous: press the
                  pedal, the exhaust note rises. `speed_gps` is not -- speed
                  builds over seconds after the throttle opens, so correlating
                  against it peaks at the throttle->speed lag (measured at
                  +3.1..+4.0 s on this car) and would bake that physical delay
                  into the overlay as a bogus clock offset.
                * The lap-scale trend has to go. Audio energy and speed both
                  rise and fall once per lap, which produces a correlation hump
                  over 100 s wide -- useless for locating a one-second offset,
                  and it makes peak-to-second-peak meaningless because every
                  neighbouring lag is nearly as high. High-passing at ~30 s
                  leaves a peak about 3 s wide.

  manual     -- a hand-entered offset, which always wins and is cached in the
                same format so one correction stays corrected.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from . import video

CACHE_PATH = Path("cache/sync.json")

# Preference order for the correlation reference. Earliest wins if present and
# not constant. throttle_pos first for the reason above.
REF_CHANNELS = ("throttle_pos", "rpm", "combined_acc", "speed_gps")


# ---------------------------------------------------------------- cache

def load_cache(path=CACHE_PATH):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache, path=CACHE_PATH):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def cache_key(clip):
    return clip.path.name


# ---------------------------------------------------------------- signals

def audio_envelope(clip, sample_rate=8000, env_hz=25, t_from=None, t_to=None,
                   ffmpeg=None):
    """Mono audio energy envelope at `env_hz`, as ``(times, values)``.

    Times are clip-relative seconds. Decoding only the window being correlated
    keeps this cheap even on a 20-minute clip.
    """
    exe = video.find_binary("ffmpeg", ffmpeg)
    cmd = [exe, "-v", "error"]
    if t_from:
        cmd += ["-ss", "%.3f" % max(0.0, t_from)]
    cmd += ["-i", str(clip.path)]
    if t_to is not None:
        span = t_to - max(0.0, t_from or 0.0)
        if span > 0:
            cmd += ["-t", "%.3f" % span]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    win = max(1, int(round(sample_rate / float(env_hz))))
    n = (a.size // win) * win
    if n == 0:
        return np.zeros(0), np.zeros(0)
    env = np.abs(a[:n].reshape(-1, win)).mean(axis=1)
    t = max(0.0, t_from or 0.0) + np.arange(env.size) / float(env_hz)
    return t, env


def _z(v):
    v = np.asarray(v, dtype=float)
    s = v.std()
    if s < 1e-12:
        return np.zeros_like(v)
    return (v - v.mean()) / s


def highpass(v, hz, tau_s):
    """Subtract a centred moving average of length `tau_s`, edges included."""
    v = np.asarray(v, dtype=float)
    k = max(1, int(round(tau_s * hz)))
    if k <= 1 or v.size <= k:
        return v - v.mean()
    c = np.concatenate([[0.0], np.cumsum(v)])
    half = k // 2
    i = np.arange(v.size)
    lo = np.clip(i - half, 0, v.size)
    hi = np.clip(i - half + k, 0, v.size)
    mean = (c[hi] - c[lo]) / np.maximum(1, hi - lo)
    return v - mean


def resample_to(grid, t, v):
    """Interpolate `v` onto `grid`, dropping non-finite source samples first.

    OBD channels drop out for a few samples at a time. Feeding those NaNs into
    np.interp poisons the whole correlation to NaN, and np.argmax of an all-NaN
    array silently returns index 0 -- a wrong answer that looks like an answer.
    """
    t = np.asarray(t, dtype=float)
    v = np.asarray(v, dtype=float)
    ok = np.isfinite(t) & np.isfinite(v)
    if ok.sum() < 2:
        return None
    return np.interp(grid, t[ok], v[ok])


# ---------------------------------------------------------------- correlation

def correlate_offset(env_t, env_v, sig_t, sig_v, env_hz, max_lag_s,
                     highpass_s=30.0, wide_lag_s=60.0, guard_s=3.0):
    """Offset (seconds) to add to the clip's container timestamp.

    Both series are put on `env_t`, built assuming offset 0. A correlation peak
    at a shift of `k` samples -- envelope sample ``n + k`` matching data sample
    ``n`` -- means the audio is late by ``k / hz`` on the assumed clock, so the
    correcting offset is ``-k / hz``.

    The correlation is computed over the wide lag range so the noise floor can
    be measured, but the accepted peak is confined to ``+-max_lag_s``.

    Returns ``(offset, info)`` where info carries:
      ``peak``        correlation at the accepted peak
      ``prominence``  (peak - median) / sd of the wide curve; the primary gate,
                      because it is meaningful for a peak several seconds wide
      ``ratio``       peak / best rival outside the guard, i.e. the spec's
                      peak-to-second-peak; catches a doubled or shifted lock
      ``halfwidth``   width of the peak at half height, in seconds
    """
    info = {"peak": 0.0, "prominence": 0.0, "ratio": 0.0, "halfwidth": 0.0,
            "n_samples": int(env_v.size)}
    if env_v.size < 64:
        return None, info
    sig = resample_to(env_t, sig_t, sig_v)
    if sig is None:
        return None, info

    a = highpass(env_v, env_hz, highpass_s) if highpass_s else env_v
    b = highpass(sig, env_hz, highpass_s) if highpass_s else sig
    a, b = _z(a), _z(b)
    if not (np.any(a) and np.any(b)):
        return None, info

    c = np.correlate(a, b, mode="full") / len(a)
    shifts = np.arange(c.size) - (len(b) - 1)
    wide = np.abs(shifts) <= int(round(wide_lag_s * env_hz))
    if wide.sum() < 8:
        return None, info
    cw, sw = c[wide], shifts[wide]
    if not np.isfinite(cw).all():
        return None, info

    near = np.abs(sw) <= int(round(max_lag_s * env_hz))
    if not near.any():
        return None, info
    idx_near = np.flatnonzero(near)
    i = int(idx_near[np.argmax(cw[idx_near])])
    peak = float(cw[i])
    sd = float(np.std(cw)) or 1e-12
    info["peak"] = peak
    info["prominence"] = (peak - float(np.median(cw))) / sd

    # Half-height width, measured on the wide curve.
    half = peak / 2.0
    lo = i
    while lo > 0 and cw[lo] > half:
        lo -= 1
    hi = i
    while hi < cw.size - 1 and cw[hi] > half:
        hi += 1
    info["halfwidth"] = float(sw[hi] - sw[lo]) / env_hz

    guard = max(1, int(round(guard_s * env_hz)))
    masked = cw.copy()
    masked[max(0, i - guard):i + guard + 1] = -np.inf
    rival = float(np.max(masked)) if np.isfinite(masked).any() else 0.0
    info["ratio"] = (peak / rival) if rival > 1e-9 else float("inf")

    k = float(sw[i])
    if 0 < i < cw.size - 1:
        y0, y1, y2 = cw[i - 1], cw[i], cw[i + 1]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            k += 0.5 * (y0 - y2) / denom
    if peak <= 0:
        return None, info
    return -k / float(env_hz), info


# ---------------------------------------------------------------- driver

def pick_ref_channel(run, configured="auto"):
    if configured and configured != "auto":
        return configured if run.has(configured) else None
    for name in REF_CHANNELS:
        if run.has(name) and run.df[run.col(name)].nunique(dropna=True) > 8:
            return name
    return None


def sync_clip(clip, run, cfg, cache=None, ffmpeg=None, verbose=True):
    """Resolve one clip's offset, mutating the clip. Returns the clip."""
    cache = cache if cache is not None else {}
    key = cache_key(clip)
    entry = cache.get(key) or {}
    strategy = cfg.get("strategy", "audio_rpm")
    forced = cfg.get("offset")

    # A manual offset -- from config or a hand-edited cache entry -- always wins
    # and is never silently overwritten by an auto-sync.
    if forced is not None:
        clip.offset = float(forced)
        clip.sync_method, clip.sync_confidence = "manual", float("inf")
        cache[key] = _entry(clip)
        return clip
    if entry.get("method") == "manual":
        clip.offset = float(entry["offset"])
        clip.sync_method, clip.sync_confidence = "manual(cached)", float("inf")
        return clip
    if (entry.get("method", "").startswith(strategy)
            and not cfg.get("refresh")
            and abs(entry.get("clip_start_unix", 0.0) - clip.start_unix) < 0.01):
        clip.offset = float(entry["offset"])
        clip.sync_method = entry["method"] + "(cached)"
        clip.sync_confidence = float(entry.get("confidence") or 0.0)
        return clip

    clip.offset, clip.sync_method, clip.sync_confidence = 0.0, "container", 0.0

    if strategy in ("audio_rpm", "audio_accel"):
        channel = (cfg.get("ref_channel", "auto")
                   if strategy == "audio_rpm" else "longitudinal_acc")
        try:
            lag, info, span, used = _audio_sync(clip, run, cfg, channel, ffmpeg)
        except Exception as exc:
            if verbose:
                print("      audio sync unavailable (%s); keeping container "
                      "offset" % exc)
            return _finish(clip, cache)

        min_prom = float(cfg.get("min_prominence", 3.5))
        min_ratio = float(cfg.get("min_peak_ratio", 1.5))
        desc = ("%s vs %s: %+.3f s (peak %.3f, prominence %.1f sd, ratio %.2f, "
                "width %.1f s, over %.0f s)"
                % (strategy, used, lag if lag is not None else float("nan"),
                   info["peak"], info["prominence"], info["ratio"],
                   info["halfwidth"], span))
        if lag is None:
            if verbose:
                print("      no usable correlation (%s); keeping container" % used)
        elif info["prominence"] < min_prom or info["ratio"] < min_ratio:
            if verbose:
                print("      rejected %s" % desc)
                print("        needs prominence >= %.1f sd and ratio >= %.2f"
                      % (min_prom, min_ratio))
            clip.sync_confidence = info["prominence"]
        else:
            clip.offset = float(lag)
            clip.sync_method = strategy
            clip.sync_confidence = info["prominence"]
            clip.sync_info = info
            if verbose:
                print("      %s" % desc)

    return _finish(clip, cache)


def _finish(clip, cache):
    cache[cache_key(clip)] = _entry(clip)
    return clip


def _entry(clip):
    conf = clip.sync_confidence
    return {"offset": round(float(clip.offset), 4),
            "method": clip.sync_method.replace("(cached)", ""),
            "confidence": (None if conf == float("inf") else round(float(conf), 3)),
            "clip_start_unix": round(float(clip.start_unix), 3),
            "duration": round(float(clip.duration), 3)}


def _audio_sync(clip, run, cfg, channel, ffmpeg):
    """Correlate the audio envelope against a data channel.

    The whole data/video overlap is used, minus the wide-lag margin: a longer
    span averages down the noise floor, and there is no cost to it beyond audio
    decode, which is cheap.
    """
    if not clip.has_audio:
        raise ValueError("clip has no audio stream")
    used = pick_ref_channel(run, channel)
    if used is None:
        raise ValueError("no usable reference channel (wanted %s)" % channel)

    window = float(cfg.get("search_window_s", 10.0))
    wide = float(cfg.get("wide_lag_s", 60.0))
    env_hz = int(cfg.get("env_hz", 25))
    sr = int(cfg.get("audio_sample_rate", 8000))
    cap = float(cfg.get("correlate_span_s", 0.0))  # 0 = use the whole overlap

    t = run.values("timestamp")
    overlap = (min(clip.start_unix + clip.duration, t[-1])
               - max(clip.start_unix, t[0]))

    # Scale the search to the run. The noise floor wants a wide lag range either
    # side of the correlated span, but an autocross run is 43 s long -- shorter
    # than the default +/-60 s window -- so a fixed window can never fit and the
    # sync simply never ran. Shrink the window instead of refusing.
    want_span = max(10.0, min(float(cfg.get("min_span_s", 60.0)), overlap * 0.5))
    wide = max(window, min(wide, (overlap - want_span) / 2.0))
    if overlap - 2.0 * wide < 5.0:
        raise ValueError("clip/data overlap is only %.1f s, too short to "
                         "correlate with a +/-%.1f s search" % (overlap, window))

    lo = max(clip.start_unix, t[0]) + wide
    hi = min(clip.start_unix + clip.duration, t[-1]) - wide
    if cap and (hi - lo) > cap:
        # Keep the busiest stretch: the envelope tracks engine load well under
        # power and poorly at idle, so pit lane and cool-down are noise.
        speed = run.values("speed_gps")
        best_t0, best_v = lo, -1.0
        step = max(5.0, cap / 8.0)
        p = lo
        while p + cap <= hi + 1e-6:
            m = (t >= p) & (t < p + cap)
            v = float(np.nanmean(speed[m])) if m.any() else -1.0
            if v > best_v:
                best_t0, best_v = p, v
            p += step
        lo, hi = best_t0, best_t0 + cap

    env_t, env_v = audio_envelope(clip, sr, env_hz,
                                  lo - clip.start_unix - wide,
                                  hi - clip.start_unix + wide, ffmpeg)
    if env_v.size == 0:
        raise ValueError("no audio decoded")
    # Envelope times on the data clock assuming zero offset; the lag solved for
    # is then exactly the offset.
    env_data_t = clip.start_unix + env_t

    m = (t >= lo - 3 * wide) & (t <= hi + 3 * wide)
    lag, info = correlate_offset(
        env_data_t, env_v, t[m], run.values(used)[m], env_hz, window,
        highpass_s=float(cfg.get("highpass_s", 30.0)),
        wide_lag_s=wide,
        guard_s=float(cfg.get("guard_s", 3.0)))
    return lag, info, hi - lo, used


def reconcile_run(clips, min_prominence=3.5, tolerance_s=1.0, verbose=True):
    """Share a confident lock with the clips that could not get one.

    Every clip in a run comes off the same camera in one sitting, so their true
    offsets are the same to well within a frame. A 43-second tail clip has no
    hope of locking on its own, but it does not need to: adopt the median of its
    siblings' good locks. Clips already set manually are left alone.
    """
    good = [c for c in clips
            if c.sync_method.startswith(("audio", "manual"))
            and (c.sync_confidence >= min_prominence
                 or c.sync_method.startswith("manual"))]
    if not good:
        return clips
    consensus = float(np.median([c.offset for c in good]))
    spread = max(abs(c.offset - consensus) for c in good)
    if spread > tolerance_s and verbose:
        print("      ! sibling clips disagree by %.2f s (%s) -- not sharing"
              % (spread, ", ".join("%s %+.2f" % (c.path.name, c.offset)
                                   for c in good)))
        return clips
    for c in clips:
        if c.sync_method.startswith("manual"):
            continue
        if c.sync_confidence >= min_prominence and c.sync_method.startswith("audio"):
            continue
        c.offset = consensus
        c.sync_method = "consensus"
        c.sync_confidence = min(cc.sync_confidence for cc in good)
        if verbose:
            print("      %s: adopting run consensus offset %+.3f s"
                  % (c.path.name, consensus))
    return clips
