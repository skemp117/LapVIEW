"""What shape a session is, and which capabilities that shape supports.

This module exists because of a pattern of failures, not because of a design
idea. Three times, adding a dataset broke something that had been quietly wrong
all along:

- Circuit sessions were swapped for autocross ones. Ten checks started reporting
  `skip` -- lap slicing, lap export, start/finish recovery, the camera-split join,
  closed-circuit map building -- and the suite still said everything passed.
- A new phone produced five columns that collapsed onto one name, and the loader
  returned a three-column frame where callers expected one.
- Six sessions arrived from a third venue. Two checks were reaching for
  `_all_runs()[0]`, whichever file sorted first, so they silently switched to a
  run with no map and became skips; a fourth pooled geometry from two sites
  117 km apart and compared 89 km against 89 km.

Each was the same mistake: **code selected data by an incidental property** --
sort order, "the first map in the directory", "one run per file", "laps exist" --
instead of by a stated requirement. Nothing announced the assumption, so nothing
could notice when it stopped holding.

So the assumptions live here, once, as data. `classify` turns a session into a
`Shape`; `applicable` says which capabilities that shape supports and, for the
ones it does not, why. `tools/dataset_report.py` prints it for any folder, and
`tools/selftest.py` asserts that every shape present is handled by something and
that each capability agrees with its own declaration. A new dataset now either
lands in a declared cell or is reported as unhandled -- which is the outcome that
was missing every time this went wrong.

## The one rule that was implicit and wrong

`laps.laps()` reads `lap_number`, which RaceChrono increments on its own terms. On
SCCA session 121241 -- an autocross course with no start/finish trap -- it reports
exactly one lap. `compare.traversals` used to branch on `if laps: ... else: whole
run`, so that session would have been treated as a circuit and produced one
lap-shaped traversal of nothing in particular. Laps are therefore only usable
when something corroborates them:

    with a map:     the map is closed, and there is at least one lap
    without a map:  at least two laps, since a single boundary is not a lap
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from . import imu as imu_mod, laps as laps_mod, runs as runs_mod, trackmap


@dataclass(frozen=True)
class Shape:
    """The facts about a session that decide what can be done with it."""

    stem: str
    venue: str | None           # the map covering it, by name
    course: str | None          # "circuit" | "course", None when no map
    n_laps: int
    laps_usable: bool
    n_runs: int
    has_imu: bool
    n_clips: int
    sample_hz: float
    duration_s: float

    @property
    def split(self):
        """Footage arrives as more than one file, so a window can straddle it."""
        return self.n_clips > 1

    @property
    def cell(self):
        """The coarse matrix key: the combination, not the measurements.

        Two sessions in the same cell should be interchangeable as far as every
        capability is concerned. If they are not, the cell is missing a
        dimension -- which is a thing worth finding out.
        """
        return (self.course or "no-map",
                "laps" if self.laps_usable else "no-laps",
                "multi-run" if self.n_runs > 1 else "single-run",
                "imu" if self.has_imu else "no-imu",
                "split" if self.split else ("footage" if self.n_clips else "no-footage"))

    def label(self):
        return " / ".join(self.cell)

    def as_dict(self):
        d = asdict(self)
        d["cell"] = list(self.cell)
        d["label"] = self.label()
        return d


def classify(run, cfg, clips=(), spans=None):
    """Work out a session's shape. No side effects, no file writes."""
    max_acc = cfg.quality.max_accuracy_m
    tm, _dist = trackmap.auto_select(cfg.maps_dir, *run.mean_latlon(max_acc))
    lp = laps_mod.laps(run, max_acc)
    if tm is not None:
        usable = bool(tm.closed) and len(lp) >= 1
    else:
        # Without a map there is nothing to corroborate a lap count, and one
        # boundary is not a lap. See the module docstring.
        usable = len(lp) >= 2
    spans = runs_mod.detect(run, max_accuracy_m=max_acc) if spans is None else spans
    return Shape(
        stem=run.stem,
        venue=(tm.name if tm is not None else None),
        course=(None if tm is None else ("circuit" if tm.closed else "course")),
        n_laps=len(lp),
        laps_usable=usable,
        n_runs=len(spans),
        has_imu=imu_mod.has_imu(run),
        n_clips=len(clips),
        sample_hz=round(run.df.shape[0] / max(run.duration, 1e-6), 1),
        duration_s=round(float(run.duration), 1),
    )


# --------------------------------------------------------------- capabilities
#
# Each entry answers one question: given this shape, can this capability run, and
# if not, what is missing? The reason string is the whole point -- "not
# applicable" with no explanation is how a silent skip starts.

def _needs_map(s):
    return None if s.venue else "no map covers this venue"


def _needs_laps(s):
    if s.laps_usable:
        return None
    if s.course == "course":
        return "an open course is crossed once; there are no laps to slice"
    if s.n_laps == 0:
        return "the session records no start/finish crossings"
    return ("%d lap boundary but nothing to corroborate it (no closed-circuit "
            "map)" % s.n_laps)


CAPABILITIES = {
    "overlay": lambda s: _needs_map(s),
    "session_export": lambda s: (
        "no footage is paired with this session" if not s.n_clips
        else _needs_map(s)),
    "lap_export": lambda s: (
        "no footage is paired with this session" if not s.n_clips
        else _needs_laps(s) or _needs_map(s)),
    "split_join": lambda s: (
        None if s.split else "the footage is a single file, so nothing to join"),
    "map_circuit": lambda s: (
        None if s.laps_usable else _needs_laps(s)),
    "map_course": lambda s: (
        None if not s.laps_usable else
        "this session has usable laps, so it belongs to a circuit map"),
    "compare": lambda s: _needs_map(s),
    "run_segmentation": lambda s: (
        None if s.n_runs >= 1 else "the car never moved"),
    "imu_channels": lambda s: (
        None if s.has_imu else "no raw accelerometer or gyroscope in this file"),
}


def applicable(shape):
    """`{capability: None or the reason it does not apply}`."""
    return {name: fn(shape) for name, fn in CAPABILITIES.items()}


def supported(shape):
    return sorted(k for k, v in applicable(shape).items() if v is None)


def blocked(shape):
    return {k: v for k, v in applicable(shape).items() if v is not None}


def matrix(shapes):
    """Group shapes into cells, so a dataset's coverage is one small table."""
    cells = {}
    for s in shapes:
        cells.setdefault(s.cell, []).append(s)
    return cells


def unhandled(shapes):
    """Shapes that no capability can do anything useful with.

    "Useful" excludes `run_segmentation` and `imu_channels` on their own: a
    session those are the only things that work on is a session this tool cannot
    yet show anyone, which is exactly what needs reporting rather than skipping.
    """
    trivial = {"run_segmentation", "imu_channels"}
    out = []
    for s in shapes:
        if not (set(supported(s)) - trivial):
            out.append(s)
    return out
