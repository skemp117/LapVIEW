"""What span of a session to render: the driving window, and per-lap windows.

A recording starts before the car moves and stops after it has parked, so most
sessions carry minutes of dead air at the head. The trim is derived from the
telemetry rather than the video: the first and last time the car exceeds a walking
pace, padded either side.

The first/last exceedance over the *whole* session is used deliberately, rather
than the longest continuous moving stretch. A track session legitimately contains
long stops -- one run here sits still for 138 s mid-session -- and cutting those
would drop real footage between them. Trimming only the ends is the conservative
choice.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import laps as laps_mod


@dataclass
class Target:
    """One output file: a window of data time, with a name for it."""

    kind: str                # "session" or "lap"
    name: str                # output filename stem
    t0: float                # data time of the first frame, inclusive
    t1: float                # data time of the last frame
    label: str = ""          # for logging
    lap_number: int = 0
    lap_time: float = 0.0

    @property
    def duration(self):
        return self.t1 - self.t0


def _speed(run):
    for ch in ("speed_gps", "speed_calc", "speed_obd"):
        if run.has(ch):
            return run.values(ch)
    return None


def driving_window(run, move_threshold_ms=1.0, pre_s=5.0, post_s=5.0):
    """``(t0, t1)`` spanning the driving, padded, clamped to the run.

    Returns the whole run if the car never exceeds the threshold, so a session
    with a dead speed channel is never silently reduced to nothing.
    """
    spd = _speed(run)
    t = run.values("timestamp")
    if spd is None:
        return run.t0, run.t1, "no speed channel; keeping the whole session"

    moving = np.isfinite(spd) & (spd > float(move_threshold_ms))
    if not moving.any():
        return run.t0, run.t1, ("never exceeded %.1f m/s; keeping the whole "
                                "session" % move_threshold_ms)

    i0 = int(np.argmax(moving))
    i1 = int(len(moving) - 1 - np.argmax(moving[::-1]))
    t0 = max(run.t0, float(t[i0]) - float(pre_s))
    t1 = min(run.t1, float(t[i1]) + float(post_s))
    note = ("moving from t+%.1f s to t+%.1f s; trimmed %.1f s head + %.1f s tail"
            % (t[i0] - run.t0, t[i1] - run.t0, t0 - run.t0, run.t1 - t1))
    return t0, t1, note


def format_lap_time(seconds):
    """`119.4` -> `1m59.4s`, for a filename."""
    m, s = divmod(float(seconds), 60.0)
    if m:
        return "%dm%04.1fs" % (int(m), s)
    return "%.1fs" % s


def session_target(run, cfg):
    if cfg.trim.enabled:
        t0, t1, note = driving_window(run, cfg.trim.move_threshold_ms,
                                      cfg.trim.pre_s, cfg.trim.post_s)
    else:
        t0, t1, note = run.t0, run.t1, "trim disabled"
    return Target(kind="session", name=run.stem, t0=t0, t1=t1, label=note)


def lap_targets(run, cfg):
    """One target per complete lap, padded by `cfg.trim.lap_buffer_s`.

    The buffer means consecutive laps overlap slightly, which is what you want:
    each exported lap shows the approach to the line and the run-off past it.
    """
    out = []
    got = laps_mod.laps(run, cfg.quality.max_accuracy_m)
    buf = float(cfg.trim.lap_buffer_s)
    for lp in got:
        t0 = max(run.t0, lp.t0 - buf)
        t1 = min(run.t1, lp.t1 + buf)
        out.append(Target(
            kind="lap",
            name="%s__lap%02d_%s" % (run.stem, lp.number,
                                     format_lap_time(lp.duration)),
            t0=t0, t1=t1,
            lap_number=lp.number, lap_time=lp.duration,
            label="lap %d, %.1f s, +/- %.1f s buffer" % (lp.number, lp.duration, buf)))
    return out


def build_targets(run, cfg, export="sessions"):
    """Targets for one run. `export` is "sessions", "laps" or "both"."""
    out = []
    if export in ("sessions", "both"):
        out.append(session_target(run, cfg))
    if export in ("laps", "both"):
        out.extend(lap_targets(run, cfg))
    return out
