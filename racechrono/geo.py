"""Geodesy helpers: lat/lon -> local metres, circular stats, PCA orientation."""
from __future__ import annotations

import numpy as np

R_EARTH = 6_378_137.0  # WGS84 equatorial radius, metres


def to_xy(lat, lon, origin_lat: float, origin_lon: float):
    """Equirectangular projection about `origin`. Exact enough over a track
    (sub-centimetre error across a few km) and, unlike UTM, has no zone seams.

    Returns (x_east_m, y_north_m).
    """
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    k = np.cos(np.radians(origin_lat))
    x = np.radians(lon - origin_lon) * R_EARTH * k
    y = np.radians(lat - origin_lat) * R_EARTH
    return x, y


def to_latlon(x, y, origin_lat: float, origin_lon: float):
    """Inverse of :func:`to_xy`."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    k = np.cos(np.radians(origin_lat))
    lat = origin_lat + np.degrees(y / R_EARTH)
    lon = origin_lon + np.degrees(x / (R_EARTH * k))
    return lat, lon


def circular_mean_deg(deg) -> float:
    """Mean of angles in degrees, wrap-safe. NaNs ignored."""
    d = np.radians(np.asarray(deg, dtype=float))
    d = d[np.isfinite(d)]
    if d.size == 0:
        return float("nan")
    return float(np.degrees(np.arctan2(np.sin(d).mean(), np.cos(d).mean())) % 360.0)


def interp_circular_deg(grid, t, deg):
    """Linear interpolation of a degree-valued angle on the unit circle.

    Interpolating `bearing` directly sweeps the long way round the 359->1 wrap;
    interpolating sin and cos separately does not.
    """
    t = np.asarray(t, dtype=float)
    r = np.radians(np.asarray(deg, dtype=float))
    ok = np.isfinite(r) & np.isfinite(t)
    if ok.sum() < 2:
        return np.full(len(grid), np.nan)
    s = np.interp(grid, t[ok], np.sin(r[ok]))
    c = np.interp(grid, t[ok], np.cos(r[ok]))
    return np.degrees(np.arctan2(s, c)) % 360.0


def pca_rotation(x, y) -> np.ndarray:
    """2x2 rotation putting the point cloud's long axis on +X.

    Returned matrix R is applied as `R @ [dx, dy]` to centred coordinates.
    """
    p = np.c_[np.asarray(x, float) - np.mean(x), np.asarray(y, float) - np.mean(y)]
    _, _, vt = np.linalg.svd(p, full_matrices=False)
    # vt rows are principal axes; make the transform a proper rotation
    if np.linalg.det(vt) < 0:
        vt[1] *= -1.0
    return vt


def identity_rotation() -> np.ndarray:
    """No rotation — keeps north up, which is what the reference layout shows."""
    return np.eye(2)


def apply_rotation(rot: np.ndarray, x, y):
    p = np.c_[np.asarray(x, float), np.asarray(y, float)]
    r = p @ np.asarray(rot, float).T
    return r[:, 0], r[:, 1]


def cumulative_distance(x, y) -> np.ndarray:
    """Arc length along a polyline, starting at 0."""
    dx = np.diff(np.asarray(x, float))
    dy = np.diff(np.asarray(y, float))
    return np.concatenate([[0.0], np.cumsum(np.hypot(dx, dy))])


def project_to_polyline(poly_x, poly_y, qx, qy, chunk=4096):
    """Map points onto a polyline: arc length along it, and distance from it.

    Returns ``(s, perp)`` -- for each query point, the arc length of the nearest
    point on the polyline and how far off the line it was.

    This is the honest way to give two traversals of the same course a common
    coordinate. Parameterising each by a fraction of its own total length looks
    equivalent and is not: two runs of one autocross course differ in path length
    by 8%, so the fraction slides progressively out of step -- measured as a
    deviation from the median line growing smoothly from 2.5 m at the start to
    17.6 m at the end, which reads like the course falling apart when it is
    really just the yardstick stretching. Arc length along a shared reference
    does not drift.
    """
    px = np.asarray(poly_x, float)
    py = np.asarray(poly_y, float)
    qx = np.asarray(qx, float)
    qy = np.asarray(qy, float)

    ax, ay = px[:-1], py[:-1]
    dx, dy = np.diff(px), np.diff(py)
    seg_len2 = dx * dx + dy * dy
    seg_len2[seg_len2 == 0] = 1e-12
    starts = cumulative_distance(px, py)[:-1]

    s_out = np.empty(qx.size)
    perp_out = np.empty(qx.size)
    for lo in range(0, qx.size, chunk):
        hi = min(qx.size, lo + chunk)
        vx = qx[lo:hi, None] - ax[None, :]
        vy = qy[lo:hi, None] - ay[None, :]
        # Fraction along each segment of the closest point, clamped to it.
        t = np.clip((vx * dx[None, :] + vy * dy[None, :]) / seg_len2[None, :],
                    0.0, 1.0)
        cx = vx - t * dx[None, :]
        cy = vy - t * dy[None, :]
        d2 = cx * cx + cy * cy
        j = np.argmin(d2, axis=1)
        rows = np.arange(hi - lo)
        s_out[lo:hi] = starts[j] + t[rows, j] * np.sqrt(seg_len2[j])
        perp_out[lo:hi] = np.sqrt(d2[rows, j])
    return s_out, perp_out


def match_polyline(poly_x, poly_y, qx, qy, closed=False, slack_m=8.0,
                   min_window_m=25.0, window_factor=4.0):
    """Walk a path along a polyline, forward only. Returns ``(s, perp)``.

    `project_to_polyline` answers each point independently, which is right for
    "how far off the line was this sample" and wrong for "how far round is the
    car". Independent answers are not ordered, so the result has to be made
    monotonic afterwards -- and the obvious way to do that, a running maximum,
    fails badly the moment the geometry is not convenient.

    It failed on the SCCA course. Staging there sits beside the *finish*, 131 m
    from the start, so the first sample of a run projects to about 1097 m of a
    1107 m line. `np.maximum.accumulate` then pinned every later sample at
    1097 m too, and the whole run collapsed into the last ten metres of the
    course: four traversals out of six survived at all, their common overlap was
    5.6 m of 1107, and the sector table showed an 82-second "sector". Nothing
    about that was a tuning problem.

    So the match is sequential instead. Each sample is matched only within a
    window ahead of where the previous one landed, and the window is the
    distance the car actually moved between the two samples -- times
    `window_factor` for slack, floored at `min_window_m`. A car cannot advance
    1100 m along the line between two samples 30 ms apart, so it no longer can
    here either. `slack_m` of backward tolerance absorbs GPS jitter without
    letting the match reverse.

    On a closed circuit `closed=True` lets `s` run past the end and continue from
    the start, so a lap that begins a metre before the line does not need
    unwrapping afterwards.
    """
    px = np.asarray(poly_x, float)
    py = np.asarray(poly_y, float)
    qx = np.asarray(qx, float)
    qy = np.asarray(qy, float)
    n = qx.size
    if px.size < 2 or n == 0:
        return np.zeros(n), np.zeros(n)

    starts = cumulative_distance(px, py)
    total = float(starts[-1])
    ax, ay = px[:-1], py[:-1]
    dx, dy = np.diff(px), np.diff(py)
    seg_len2 = dx * dx + dy * dy
    seg_len2[seg_len2 == 0] = 1e-12
    seg_len = np.sqrt(seg_len2)
    nseg = ax.size

    # How far the car moved between consecutive samples, which is what bounds
    # how far it can have advanced along the line.
    step = np.r_[0.0, np.hypot(np.diff(qx), np.diff(qy))]

    s_out = np.empty(n)
    perp_out = np.empty(n)
    s_prev = 0.0
    laps_done = 0.0

    for i in range(n):
        reach = max(float(min_window_m), window_factor * float(step[i]))
        lo_s = s_prev - float(slack_m)
        hi_s = s_prev + reach
        # Candidate segments: those whose span overlaps [lo_s, hi_s].
        lo_i = int(np.searchsorted(starts, lo_s, side="right") - 1)
        hi_i = int(np.searchsorted(starts, hi_s, side="left"))
        lo_i = max(0, min(lo_i, nseg - 1))
        hi_i = max(lo_i + 1, min(hi_i + 1, nseg))
        idx = np.arange(lo_i, hi_i)
        wrapped = False
        if closed and hi_s > total:
            # The window runs off the end; continue it from the start.
            extra = int(np.searchsorted(starts, hi_s - total, side="left") + 1)
            idx = np.r_[idx, np.arange(0, max(1, min(extra, nseg)))]
            wrapped = True

        vx = qx[i] - ax[idx]
        vy = qy[i] - ay[idx]
        t = np.clip((vx * dx[idx] + vy * dy[idx]) / seg_len2[idx], 0.0, 1.0)
        cx = vx - t * dx[idx]
        cy = vy - t * dy[idx]
        d2 = cx * cx + cy * cy
        # Prefer a forward match: a candidate behind the current position is only
        # taken when nothing ahead is close, which is what `slack_m` allows for.
        cand_s = starts[idx] + t * seg_len[idx]
        if wrapped:
            behind = cand_s < s_prev - float(slack_m) - total * 0.5
            cand_s = np.where(behind, cand_s + total, cand_s)
        k = int(np.argmin(d2))
        s_here = float(cand_s[k])
        if closed and s_here + laps_done * total < s_prev - float(slack_m):
            # Crossed the line: the same place is now one lap further on.
            laps_done += 1.0
        s_abs = s_here + laps_done * total if closed else s_here
        if s_abs < s_prev:
            s_abs = s_prev
        s_out[i] = s_abs
        perp_out[i] = float(np.sqrt(d2[k]))
        s_prev = s_abs
    return s_out, perp_out
