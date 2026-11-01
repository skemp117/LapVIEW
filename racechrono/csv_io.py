"""RaceChrono Pro "CSV v3" loading, column disambiguation and resampling.

The file layout is: a block of metadata lines, a column-name row, a units row,
a *device* row, then data. The device row is not mentioned in the format notes
but every v10.2.x export here carries it, and it is the authoritative source for
which device block a duplicated channel name belongs to -- better than counting
occurrences positionally.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import geo

# The device row tags each column with the device that produced it: "100: gps",
# "calc", "300: obd", "200: acc", "201: gyro", "202: magn". The numeric prefix is
# a device id that RaceChrono renumbers between sessions -- OBD was "200: obd"
# with one channel set and "300: obd" once the phone's IMU was added -- so the
# id is stripped and only the name is kept.
_TAG_RE = re.compile(r"^\s*(?:\d+\s*:\s*)?(.*?)\s*$")

# Units that must hold for any channel we compute from. A silently mislabelled
# channel renders a plausible-looking wrong video, so mismatches raise.
EXPECTED_UNITS = {
    "timestamp": "unix time",
    "elapsed_time": "s",
    "distance_traveled": "m",
    "accuracy": "m",
    "altitude": "m",
    "bearing": "deg",
    "latitude": "deg",
    "longitude": "deg",
    "speed_gps": "m/s",
    "speed_calc": "m/s",
    "speed_obd": "m/s",
    "combined_acc": "G",
    "lateral_acc": "G",
    "longitudinal_acc": "G",
    "lean_angle": "deg",
    "rpm": "rpm",
    "throttle_pos": "%",
    "coolant_temp": ".C",
    "intake_temp": ".C",
}

# Channels carried forward, never blended: a lap number half way between 3 and 4
# is meaningless. Everything not named here is treated as continuous and
# interpolated linearly.
#
# That default matters. Listing the continuous channels instead meant any channel
# this code had not heard of was silently step-filled -- so when the phone's
# accelerometer, gyroscope and magnetometer appeared, nine genuinely continuous
# 100 Hz channels were being held constant between samples rather than
# interpolated. Naming the small, closed set of discrete channels is the safer
# way round.
STEP = {"lap_number", "fragment_id", "fix_type"}
# Interpolated on the unit circle, or the 359->1 wrap sweeps the wrong way.
CIRCULAR = {"bearing"}


def _block_of(device_tag):
    """'200: obd' -> 'obd'; '201: gyro' -> 'gyro'; 'calc' -> 'calc'; '' -> ''.

    Reading the name off the tag rather than matching a fixed list means a
    device this code has never seen still gets a sensible suffix.
    """
    m = _TAG_RE.match((device_tag or "").strip().lower())
    return (m.group(1) if m else "").replace(" ", "_")


def disambiguate(cols, devices):
    """Canonical column names plus an alias map.

    A name appearing once keeps its bare form (`rpm`, `latitude`). A repeated
    name gets its device block appended (`speed` -> `speed_gps`, `speed_calc`,
    `speed_obd`). Every device-attributed column additionally gets a
    `<name>_<block>` alias, so config may spell out `rpm_obd` and still resolve.

    Uniqueness is enforced at the end whatever the device row says. Without it a
    session logging the phone's accelerometer, gyroscope and magnetometer -- five
    `device_update_rate` columns -- collapsed three of them onto one name, and a
    duplicated column makes `run.values()` hand back a 2-D frame instead of a
    series, which breaks resampling outright rather than degrading quietly.
    """
    counts = {}
    for c in cols:
        counts[c] = counts.get(c, 0) + 1

    names, aliases = [], {}
    used = {}
    for c, d in zip(cols, devices):
        block = _block_of(d)
        name = "%s_%s" % (c, block) if (counts[c] > 1 and block) else c
        if name in used:
            used[name] += 1
            name = "%s_%d" % (name, used[name])
        used.setdefault(name, 0)
        names.append(name)
        if block:
            aliases.setdefault("%s_%s" % (c, block), name)
        aliases.setdefault(c, name)
    return names, aliases


@dataclass
class Run:
    """One CSV: a session's worth of telemetry on the GPS clock."""

    path: Path
    df: pd.DataFrame
    units: dict = field(default_factory=dict)
    devices: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    imu: object = None          # imu.Calibration when one was solved

    @property
    def stem(self):
        return self.path.stem

    @property
    def t0(self):
        return float(self.df["timestamp"].iloc[0])

    @property
    def t1(self):
        return float(self.df["timestamp"].iloc[-1])

    @property
    def duration(self):
        return self.t1 - self.t0

    def col(self, channel):
        """Resolve a config channel name to an actual column."""
        if channel in self.df.columns:
            return channel
        target = self.aliases.get(channel)
        if target and target in self.df.columns:
            return target
        raise KeyError(
            "%s: channel %r not present. Available: %s"
            % (self.path.name, channel, ", ".join(self.df.columns))
        )

    def has(self, channel):
        try:
            self.col(channel)
            return True
        except KeyError:
            return False

    def values(self, channel):
        return self.df[self.col(channel)].to_numpy(dtype=float)

    def good_mask(self, max_accuracy_m=10.0):
        """Rows with a usable position fix.

        `fix_type` is a constant 1 in this dataset and `altitude` a constant 0,
        so the accuracy channel does all the work. Its floor is exactly 10.0 m,
        which is why the gate is `<=` rather than `<`.
        """
        acc = self.values("accuracy")
        ok = np.isfinite(acc) & (acc <= max_accuracy_m)
        ok &= np.isfinite(self.values("latitude"))
        ok &= np.isfinite(self.values("longitude"))
        if self.has("fix_type"):
            ok &= self.values("fix_type") != 0
        return ok

    def mean_latlon(self, max_accuracy_m=10.0):
        m = self.good_mask(max_accuracy_m)
        if not m.any():
            m = np.isfinite(self.values("latitude"))
        return (float(np.nanmean(self.values("latitude")[m])),
                float(np.nanmean(self.values("longitude")[m])))


def _is_float(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _find_header(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    for i, r in enumerate(rows):
        if r and r[0].strip().lower() == "timestamp":
            return rows, i
    raise ValueError("%s: no header row starting with 'timestamp'" % path)


def _read_meta(preamble):
    meta = {}
    for r in preamble:
        if len(r) >= 2 and r[0].strip():
            meta[r[0].strip()] = ",".join(x for x in r[1:] if x).strip()
    return meta


def _assert_units(path, unit_map):
    bad = []
    for name, want in EXPECTED_UNITS.items():
        got = unit_map.get(name)
        if got is not None and got != want:
            bad.append("%s: expected %r, file says %r" % (name, want, got))
    if bad:
        raise ValueError("%s: unit mismatch -- %s" % (path.name, "; ".join(bad)))


def read_run(path, validate_units=True, derive=True, imu_opts=None):
    """Load one CSV v3 file into a :class:`Run`.

    With `derive`, a session that carries a raw accelerometer and gyroscope also
    gets vehicle-frame channels (`imu_lateral_acc` and friends) solved from a
    per-run calibration. Sessions without an IMU are unaffected.
    """
    path = Path(path)
    rows, hi = _find_header(path)

    cols = [c.strip() for c in rows[hi]]
    units = [u.strip() for u in rows[hi + 1]] if hi + 1 < len(rows) else []
    units = units[:len(cols)] + [""] * max(0, len(cols) - len(units))

    # The row after units is a device row iff its first cell is not numeric.
    devices = [""] * len(cols)
    data_i = hi + 2
    nxt = rows[data_i] if data_i < len(rows) else []
    if nxt and not _is_float(nxt[0]):
        devices = [d.strip() for d in nxt][:len(cols)]
        devices += [""] * max(0, len(cols) - len(devices))
        data_i += 1
    # Skip any remaining non-numeric preamble.
    while data_i < len(rows) and not (rows[data_i] and _is_float(rows[data_i][0])):
        data_i += 1
    if data_i >= len(rows):
        raise ValueError("%s: no data rows after header" % path)

    names, aliases = disambiguate(cols, devices)
    width = len(cols)
    body = [r[:width] + [""] * (width - len(r)) for r in rows[data_i:] if r]
    df = pd.DataFrame(body, columns=names).replace("", np.nan)
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df[df["timestamp"].notna()].reset_index(drop=True)

    unit_map = dict(zip(names, units))
    if validate_units:
        _assert_units(path, unit_map)

    run = Run(path=path, df=df, units=unit_map,
              devices=dict(zip(names, devices)),
              aliases=aliases, meta=_read_meta(rows[:hi]))
    run.imu = None
    if derive:
        from . import imu as imu_mod
        imu_mod.augment(run, **(imu_opts or {}))
    return run


def constant_channels(run):
    """Channels with at most one distinct value -- nothing to display."""
    return [c for c in run.df.columns if run.df[c].nunique(dropna=True) <= 1]


def channel_report(run):
    """Channel inventory with units, for --dry-run."""
    lines = []
    for c in run.df.columns:
        v = run.df[c]
        n = int(v.notna().sum())
        u = run.units.get(c) or "-"
        d = run.devices.get(c) or "-"
        if n:
            lines.append("    %-24s %-10s %-9s n=%-7d [%.4g .. %.4g]"
                         % (c, u, d, n, v.min(), v.max()))
        else:
            lines.append("    %-24s %-10s %-9s n=0       (empty)" % (c, u, d))
    return "\n".join(lines)


def resample(run, fps, t_start=None, t_end=None, max_accuracy_m=10.0):
    """Put every channel on a uniform grid at `fps` on the GPS clock.

    Continuous channels interpolate linearly, `bearing` on the unit circle, step
    channels carry forward. Fragment boundaries are hard walls: nothing is
    interpolated across a session pause. Adds columns:
      ``t``      grid time (unix seconds, same as ``timestamp``)
      ``good``   accuracy gate passed at the nearest preceding source row
      ``valid``  grid sample falls inside some fragment's span
    """
    df = run.df
    t = df["timestamp"].to_numpy(dtype=float)
    t_start = t[0] if t_start is None else float(t_start)
    t_end = t[-1] if t_end is None else float(t_end)
    n = int(np.floor((t_end - t_start) * fps + 1e-9)) + 1
    if n <= 0:
        raise ValueError("empty resample window")
    grid = t_start + np.arange(n) / float(fps)

    if "fragment_id" in df.columns:
        frag = pd.Series(df["fragment_id"].to_numpy(dtype=float)).ffill().bfill()
        frag = frag.fillna(0.0).to_numpy()
    else:
        frag = np.zeros(len(df))

    out = {}
    valid = np.zeros(n, dtype=bool)
    good = np.zeros(n, dtype=bool)
    src_good = run.good_mask(max_accuracy_m)

    for fid in np.unique(frag):
        sel = frag == fid
        ts = t[sel]
        if ts.size < 2:
            continue
        seg = (grid >= ts[0]) & (grid <= ts[-1])
        if not seg.any():
            continue
        valid |= seg
        g = grid[seg]
        # Nearest preceding source row, shared by every step channel.
        idx = np.clip(np.searchsorted(ts, g, side="right") - 1, 0, len(ts) - 1)
        for c in df.columns:
            if c == "timestamp":
                continue
            v = df[c].to_numpy(dtype=float)[sel]
            dst = out.setdefault(c, np.full(n, np.nan))
            ok = np.isfinite(v)
            if not ok.any():
                continue
            if c in CIRCULAR:
                dst[seg] = geo.interp_circular_deg(g, ts[ok], v[ok])
            elif c in STEP:
                dst[seg] = pd.Series(v).ffill().to_numpy()[idx]
            else:
                dst[seg] = np.interp(g, ts[ok], v[ok])
        good[seg] = src_good[sel][idx]

    out["t"] = grid
    out["timestamp"] = grid
    out["valid"] = valid
    out["good"] = good & valid
    cols = ["t", "timestamp"] + [c for c in df.columns if c != "timestamp"]
    cols += ["valid", "good"]
    return pd.DataFrame(out)[cols]
