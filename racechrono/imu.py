"""Vehicle-frame acceleration and yaw rate from a phone's raw IMU.

The phone is mounted at some unknown angle, so its accelerometer and gyroscope
axes mean nothing until they are rotated into the car's frame. Everything needed
to solve that rotation is already in the data:

* **Which way is up** comes from the accelerometer while the car is stationary.
  At rest it reads +1 g along its own up axis, and every run here starts with
  2.7-3.7 s of standstill.
* **Gyro bias** is the mean gyro reading over that same standstill.
* **Which way is forward** is the one remaining unknown -- a single rotation
  angle in the horizontal plane. It is solved by least squares against physics:
  longitudinal acceleration must match ``dv/dt`` and lateral must match
  ``v * yaw_rate``, both of which come from GPS and the now-unbiased gyro.

Three things make this worth doing rather than trusting RaceChrono's own
`calc` channels:

* The `calc` block is derived from GPS. On a circuit with long corners that
  works well -- measured against `v * omega` on the AMP sessions it came out at
  slope -1.00, r -0.99. On autocross, where the car changes direction faster
  than 1 Hz GPS can describe, it breaks: peaks of 2.22, 4.79 and **5.93 G**
  appear in runs where the real peak is about 1.3 G.
* Scored against an independent reference on the same autocross runs, the raw
  IMU tracks physics at r = 0.74..0.90 where `calc` manages 0.07..0.61.
* The calibration also yields a real yaw rate, at 100 Hz rather than
  interpolated up from a 1 Hz GPS bearing.

The mount is not perfectly repeatable -- tilt varied between 78 and 88 degrees
across ten runs of one session, and gyro bias between -0.28 and -1.31 deg/s --
so calibration is solved per run, never once and reused.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

G = 9.80665

ACC = ("x_acc", "y_acc", "z_acc")
GYRO = ("x_rate_of_rotation", "y_rate_of_rotation", "z_rate_of_rotation")

#: Channels this module adds to a run when the raw IMU is present.
DERIVED = {
    "imu_lateral_acc": "G",
    "imu_longitudinal_acc": "G",
    "imu_combined_acc": "G",
    "imu_vertical_acc": "G",
    "imu_yaw_rate": "deg/s",
}

# Defaults, all overridable through CONFIG["imu"].
STILL_SPEED_MS = 0.3        # below this the car is considered stopped
STILL_GYRO_DPS = 5.0        # ... and not being rocked
STILL_ACCEL_SD = 0.05       # ... and the accelerometer is steady, in g
MIN_STILL_S = 0.5           # shortest standstill worth calibrating from
LOWPASS_HZ = 5.0            # vehicle dynamics live well below this
GRID_HZ = 100.0             # the IMU's true rate; the file is interpolated above it
FIT_MIN_SPEED_MS = 4.0      # ignore near-standstill when fitting forward
MIN_FIT_R = 0.45            # below this the calibration is not trusted


@dataclass
class Calibration:
    """The solved sensor-to-vehicle transform for one run."""

    up: np.ndarray                      # unit vector, sensor frame
    forward: np.ndarray                 # unit vector, horizontal, car forward
    right: np.ndarray                   # unit vector, horizontal, car right
    gyro_bias_dps: np.ndarray
    g_magnitude: float                  # measured |a| at rest, in g
    tilt_deg: float                     # mount tilt away from screen-up
    heading_offset_deg: float           # the solved horizontal rotation
    still_seconds: float
    fit_r: float                        # joint agreement with dv/dt and v*omega
    r_lon: float = 0.0                  # longitudinal axis vs dv/dt
    r_lat: float = 0.0                  # lateral axis vs v*omega
    handed: float = 1.0                 # det of the fitted map: -1 = reflected
    trusted: bool = False
    notes: list = field(default_factory=list)

    def as_dict(self):
        return {"up": [round(float(v), 5) for v in self.up],
                "forward": [round(float(v), 5) for v in self.forward],
                "gyro_bias_dps": [round(float(v), 4) for v in self.gyro_bias_dps],
                "g_magnitude": round(self.g_magnitude, 4),
                "tilt_deg": round(self.tilt_deg, 2),
                "heading_offset_deg": round(self.heading_offset_deg, 2),
                "still_seconds": round(self.still_seconds, 2),
                "fit_r": round(self.fit_r, 4),
                "r_lon": round(self.r_lon, 4),
                "r_lat": round(self.r_lat, 4),
                "handed": round(self.handed, 3),
                "trusted": bool(self.trusted),
                "notes": list(self.notes)}


def has_imu(run):
    return all(run.has(c) for c in ACC) and all(run.has(c) for c in GYRO)


def _lowpass(v, hz, cutoff):
    """Zero-phase low-pass on a uniform grid, with a moving-average fallback."""
    if cutoff <= 0 or cutoff >= hz / 2.0:
        return v
    try:
        from scipy.signal import butter, filtfilt
        b, a = butter(2, cutoff / (hz / 2.0), btype="low")
        return filtfilt(b, a, v, axis=0)
    except Exception:
        k = max(1, int(round(hz / cutoff)))
        ker = np.ones(k) / k
        if v.ndim == 1:
            return np.convolve(v, ker, mode="same")
        return np.stack([np.convolve(v[:, i], ker, mode="same")
                         for i in range(v.shape[1])], axis=1)


def _longest_true(mask):
    """(start, stop) of the longest contiguous True run; (0, 0) if none."""
    if not mask.any():
        return 0, 0
    d = np.diff(np.r_[0, mask.astype(np.int8), 0])
    starts = np.flatnonzero(d == 1)
    stops = np.flatnonzero(d == -1)
    i = int(np.argmax(stops - starts))
    return int(starts[i]), int(stops[i])


def _solve_frame(p, q, want_lon, want_lat):
    """The orthogonal 2x2 map taking measured horizontal accel onto (lon, lat).

    Orthogonal Procrustes: ``R = U V^T`` from the SVD of the cross-covariance.

    Crucially the determinant is *not* forced to +1, so a reflection is allowed.
    The basis this fit starts from is built by crossing an arbitrary seed with
    the gravity vector, and whether that lands right- or left-handed relative to
    the car depends on how the phone happens to be lying. Restricting the fit to
    rotations produced a frame whose lateral axis matched physics at r = +0.99
    while its longitudinal axis matched at r = -0.62 -- correct cornering,
    inverted braking -- which no rotation can do. Allowing the reflection fixes
    both axes at once.
    """
    m = np.stack([p, q], axis=1)                      # (n, 2) measured
    e = np.stack([want_lon, want_lat], axis=1)        # (n, 2) expected
    cov = e.T @ m
    u, _, vt = np.linalg.svd(cov)
    return u @ vt                                     # 2x2, det = +/-1


def calibrate(run, still_speed_ms=STILL_SPEED_MS, still_gyro_dps=STILL_GYRO_DPS,
              still_accel_sd=STILL_ACCEL_SD, min_still_s=MIN_STILL_S,
              lowpass_hz=LOWPASS_HZ, grid_hz=GRID_HZ,
              fit_min_speed_ms=FIT_MIN_SPEED_MS, min_fit_r=MIN_FIT_R):
    """Solve the sensor-to-vehicle transform for one run, or return None."""
    if not has_imu(run):
        return None
    notes = []

    t_raw = run.values("timestamp")
    if t_raw.size < 32:
        return None
    # Work on a uniform grid: the file merges devices onto an irregular ~306 Hz
    # index, and a Butterworth filter assumes even spacing.
    n = max(32, int((t_raw[-1] - t_raw[0]) * grid_hz) + 1)
    grid = np.linspace(t_raw[0], t_raw[-1], n)

    def onto(name):
        v = run.values(name)
        ok = np.isfinite(v) & np.isfinite(t_raw)
        if ok.sum() < 2:
            return None
        return np.interp(grid, t_raw[ok], v[ok])

    acc = [onto(c) for c in ACC]
    gyr = [onto(c) for c in GYRO]
    spd = onto("speed_gps") if run.has("speed_gps") else None
    if any(v is None for v in acc + gyr) or spd is None:
        return None
    a = np.stack(acc, axis=1)
    w = np.radians(np.stack(gyr, axis=1))

    af = _lowpass(a, grid_hz, lowpass_hz)
    wf = _lowpass(w, grid_hz, lowpass_hz)

    # "Stopped" is not the same as "still". One run of ten sat below the speed
    # and gyro thresholds while clearly being rocked -- its gravity vector came
    # out at 1.137 g and its solved heading 85 degrees away from the other nine.
    # Requiring the accelerometer magnitude to be *steady* as well rejects that.
    gyro_mag_dps = np.degrees(np.linalg.norm(w, axis=1))
    a_mag = np.linalg.norm(a, axis=1)
    k = max(3, int(0.2 * grid_hz))
    ker = np.ones(k) / k
    mean_local = np.convolve(a_mag, ker, mode="same")
    var_local = np.convolve(a_mag ** 2, ker, mode="same") - mean_local ** 2
    steady = np.sqrt(np.maximum(0.0, var_local)) < still_accel_sd
    still = (spd < still_speed_ms) & (gyro_mag_dps < still_gyro_dps) & steady
    s0, s1 = _longest_true(still)
    still_s = (s1 - s0) / grid_hz
    if still_s < min_still_s:
        # Without a standstill there is no gravity reference and no bias.
        return Calibration(up=np.array([0.0, 0.0, 1.0]),
                           forward=np.array([1.0, 0.0, 0.0]),
                           right=np.array([0.0, 1.0, 0.0]),
                           gyro_bias_dps=np.zeros(3), g_magnitude=float("nan"),
                           tilt_deg=float("nan"), heading_offset_deg=float("nan"),
                           still_seconds=still_s, fit_r=0.0, trusted=False,
                           notes=["no standstill of at least %.1f s; cannot "
                                  "solve orientation" % min_still_s])

    rest = a[s0:s1]
    gvec = rest.mean(axis=0)
    g_mag = float(np.linalg.norm(gvec))
    up = gvec / (g_mag or 1.0)
    bias = w[s0:s1].mean(axis=0)
    tilt = float(np.degrees(np.arccos(min(1.0, abs(float(up[2]))))))

    wc = wf - bias
    # Compass sense: bearing grows clockwise, so negate the up component.
    yaw = -(wc @ up)                                          # rad/s

    # Horizontal acceleration, gravity removed.
    a_h = af - np.outer(af @ up, up)
    # An arbitrary orthonormal basis of that plane; the fit finds the rotation
    # from it to the car's axes.
    seed = np.array([1.0, 0.0, 0.0])
    if abs(float(up @ seed)) > 0.9:
        seed = np.array([0.0, 1.0, 0.0])
    e1 = seed - up * float(up @ seed)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    p = a_h @ e1
    q = a_h @ e2

    want_lat = spd * yaw / G
    want_lon = np.gradient(_lowpass(spd, grid_hz, 2.0)) * grid_hz / G

    m = (spd > fit_min_speed_ms) & np.isfinite(want_lat) & np.isfinite(want_lon)
    if m.sum() < grid_hz:
        notes.append("not enough time above %.1f m/s to solve forward"
                     % fit_min_speed_ms)
        R = np.eye(2)
        fit_r, r_lon, r_lat = 0.0, 0.0, 0.0
    else:
        R = _solve_frame(p[m], q[m], want_lon[m], want_lat[m])
        lon = R[0, 0] * p + R[0, 1] * q
        lat = R[1, 0] * p + R[1, 1] * q
        got = np.concatenate([lon[m], lat[m]])
        exp = np.concatenate([want_lon[m], want_lat[m]])
        fit_r = float(np.corrcoef(got, exp)[0, 1])
        # Per-axis agreement, reported separately: lateral is well constrained
        # by v*omega, longitudinal only as well as dv/dt from 1 Hz GPS allows,
        # so a mediocre longitudinal r is expected and not a failure.
        r_lon = float(np.corrcoef(lon[m], want_lon[m])[0, 1])
        r_lat = float(np.corrcoef(lat[m], want_lat[m])[0, 1])

    forward = e1 * R[0, 0] + e2 * R[0, 1]
    right = e1 * R[1, 0] + e2 * R[1, 1]
    heading = float(np.degrees(np.arctan2(R[0, 1], R[0, 0]))) % 360.0

    plausible_g = 0.9 <= g_mag <= 1.1
    # Signs must come out physical: +lateral in a right turn, +longitudinal
    # under acceleration. The fit is anchored to both, so a negative here means
    # the frame is wrong rather than merely noisy.
    signs_ok = r_lat > 0.0 and r_lon > 0.0
    trusted = fit_r >= min_fit_r and plausible_g and signs_ok
    if not signs_ok:
        notes.append("axis signs came out inverted (lat r=%.3f, lon r=%.3f)"
                     % (r_lat, r_lon))
    if fit_r < min_fit_r:
        notes.append("forward fit r=%.3f below %.2f; calibration not trusted"
                     % (fit_r, min_fit_r))
    if not plausible_g:
        # A gravity vector this far off means the "standstill" was not still, so
        # up is wrong -- and up is what every derived channel is built on.
        notes.append("|a| at rest is %.3f g, not ~1.0: the standstill was not "
                     "still, so orientation is unreliable" % g_mag)

    return Calibration(up=up, forward=forward, right=right,
                       gyro_bias_dps=np.degrees(bias), g_magnitude=g_mag,
                       tilt_deg=tilt,
                       heading_offset_deg=heading, still_seconds=still_s,
                       fit_r=fit_r, r_lon=r_lon, r_lat=r_lat,
                       handed=float(np.linalg.det(R)), trusted=trusted,
                       notes=notes)


def derive(run, cal=None, lowpass_hz=LOWPASS_HZ, grid_hz=GRID_HZ, **kw):
    """Vehicle-frame channels for one run, on the run's own row timestamps.

    Returns ``(dict of name -> array, calibration)``. Values are computed on a
    uniform grid and interpolated back, so they line up with every other column.
    """
    cal = cal if cal is not None else calibrate(run, lowpass_hz=lowpass_hz,
                                                grid_hz=grid_hz, **kw)
    if cal is None:
        return {}, None

    t_raw = run.values("timestamp")
    n = max(32, int((t_raw[-1] - t_raw[0]) * grid_hz) + 1)
    grid = np.linspace(t_raw[0], t_raw[-1], n)

    def onto(name):
        v = run.values(name)
        ok = np.isfinite(v) & np.isfinite(t_raw)
        return np.interp(grid, t_raw[ok], v[ok])

    a = np.stack([onto(c) for c in ACC], axis=1)
    w = np.radians(np.stack([onto(c) for c in GYRO], axis=1))
    af = _lowpass(a, grid_hz, lowpass_hz)
    wc = _lowpass(w, grid_hz, lowpass_hz) - np.radians(cal.gyro_bias_dps)

    vertical = af @ cal.up - cal.g_magnitude      # gravity removed
    lon = af @ cal.forward
    lat = af @ cal.right
    yaw = np.degrees(-(wc @ cal.up))

    out = {
        "imu_longitudinal_acc": lon,
        "imu_lateral_acc": lat,
        "imu_combined_acc": np.hypot(lon, lat),
        "imu_vertical_acc": vertical,
        "imu_yaw_rate": yaw,
    }
    # Back onto the run's own irregular timestamps.
    return {k: np.interp(t_raw, grid, v) for k, v in out.items()}, cal


def augment(run, **kw):
    """Add the derived channels to `run.df` and hang the calibration on the run.

    A no-op when the run has no raw IMU, so a session logged without one keeps
    working untouched.
    """
    run.imu = None
    if not has_imu(run):
        return None
    cols, cal = derive(run, **kw)
    if not cols:
        return None
    for name, values in cols.items():
        run.df[name] = values
        run.units[name] = DERIVED[name]
        run.devices[name] = "derived: imu"
    run.imu = cal
    return cal
