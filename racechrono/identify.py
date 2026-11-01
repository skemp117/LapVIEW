"""Which track is this run on?

`trackmap.auto_select` answers by position: the map whose origin is nearest,
within 5 km. That is the wrong question. Two autocross courses laid out in the
same parking lot have origins metres apart and are different tracks, and AMP
clockwise and AMP counter-clockwise share every coordinate they have.

A track is a **shape travelled in a direction**, so identity is three
measurements taken by matching the driven path onto a candidate centreline:

- **fit** -- 90th-percentile perpendicular distance, over interior samples only
- **coverage** -- how much of the centreline the run traverses
- **direction** -- the fraction of consecutive samples that advance along it

## Interior samples only, and why

An open-course map is trimmed to the span every traversal agrees on, so a driven
path runs past both ends of it -- about 220 m of a 1330 m SCCA run. Those samples
project onto the endpoint whatever their real distance from it, and including them
put the SCCA fit at 57.5 m against its own map while AMP and Grip sat at 5.6 and
3.0. Dropping the samples pinned at either end gives 3.5 m, and the threshold
stops needing to accommodate an artefact.

It also turns the wrong answer into a clearer one. A Grip run tested against the
SCCA course -- real geometry, translated onto the same origin so that "a different
course in the same lot" is measurable -- has **no** interior samples at all: the
whole path pins to one end. Unmatchable is a better verdict than a large number.

## Measured separation

| | fit | coverage | direction |
|---|---|---|---|
| its own track | 1.8-5.6 m | 0.99-8.0 | 90-95% |
| another venue | 117000+ m | 0.00 | - |
| another course, same lot | unmatchable | 0.00 | 14-15% |
| its own track, reversed | 6.8 m | nonsense | **0.1%** |

The reversed row is the one that matters most: reversing a closed centreline
leaves every point where it was, so fit cannot separate a lap from the same lap
driven the other way -- 5.58 m against 6.80 m. Only direction can, and it does so
completely.

## The name in the file is a shortlist, never a verdict

RaceChrono exports a `Track name` the user typed, and across twenty sessions it
is exactly right: "AMP Full", "Grip 8-22", "SCCA Autocross". It is used to order
candidates and is reported, and it decides nothing, for three reasons: one name
covers both directions of a circuit, a name gets reused for next month's
different course in the same lot, and the user is going to rename things -- after
which RaceChrono still exports the old string.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import geo, runs as runs_mod

# Right answers measured at 1.8-5.6 m, so this is roughly a factor of three of
# margin. Wrong answers are unmatchable or four orders of magnitude out; nothing
# observed lands between.
MAX_FIT_M = 15.0

# A run must traverse most of the line. Right answers: 0.99 and up (a circuit
# session covers it several times over). Wrong answers: 0.00.
MIN_COVERAGE = 0.60

# Right answers 90-95%, a reversed line 0.0-0.1%. Anything near the middle is a
# genuine ambiguity and should be reported rather than resolved.
MIN_DIRECTION = 0.60

# Below this there is not enough of a match to describe.
MIN_INTERIOR = 50

# Samples this close to either end of an open line are pinned there rather than
# matched, as a fraction of the line's length.
END_FRACTION = 0.002


@dataclass
class Fit:
    """How well one run sits on one candidate centreline."""

    fit_m: float | None
    coverage: float
    direction: float
    interior: int
    samples: int

    @property
    def matched(self):
        return (self.fit_m is not None
                and self.interior >= MIN_INTERIOR
                and self.fit_m <= MAX_FIT_M
                and self.coverage >= MIN_COVERAGE
                and self.direction >= MIN_DIRECTION)

    def why_not(self):
        """The first reason this is not a match, worst violation first.

        Fit is reported before direction: a run from another venue is 171 km off
        the line, and saying it was "driven the other way" is true but absurd as
        the headline.
        """
        if self.fit_m is None or self.interior < MIN_INTERIOR:
            return ("the path never runs along this line (%d of %d samples "
                    "matched its interior)" % (self.interior, self.samples))
        if self.fit_m > MAX_FIT_M:
            return "%.0f m off the line at p90" % self.fit_m
        if self.direction < MIN_DIRECTION:
            return ("driven the other way (%.0f%% of samples advance along the "
                    "line)" % (100 * self.direction))
        if self.coverage < MIN_COVERAGE:
            return "covers only %.0f%% of the line" % (100 * self.coverage)
        return ""

    def summary(self):
        return "fit %s, coverage %.2f, direction %.0f%%" % (
            ("%.1f m" % self.fit_m) if self.fit_m is not None else "unmatchable",
            self.coverage, 100 * self.direction)


@dataclass
class Candidate:
    """A track this run might be on, with the evidence."""

    key: str                    # whatever the caller uses to identify a track
    name: str
    fit: Fit
    name_matches: bool = False

    @property
    def matched(self):
        return self.fit.matched

    def __str__(self):
        return "%s: %s%s" % (self.name, self.fit.summary(),
                             "  (name matches)" if self.name_matches else "")


@dataclass
class Decision:
    """What ingest should do, and what to tell the user."""

    verdict: str                # "match" | "ambiguous" | "new"
    candidate: Candidate | None
    candidates: list = field(default_factory=list)
    note: str = ""


def _driving_mask(run, max_accuracy_m, span=None):
    """Rows belonging to one traversal, with a usable fix.

    Defaults to the session's first detected run rather than the whole file: on a
    session holding two runs with staging between them, the staging drive is not
    on the course and would drag every statistic.
    """
    if span is None:
        spans = runs_mod.detect(run, max_accuracy_m=max_accuracy_m)
        if not spans:
            return None
        span = spans[0]
    m = np.zeros(run.df.shape[0], bool)
    m[span.i0:span.i1] = True
    m &= run.good_mask(max_accuracy_m)
    return m if m.sum() >= MIN_INTERIOR else None


def fit_run(tm, run, span=None, max_accuracy_m=10.0):
    """Measure one run against one map."""
    from . import compare as compare_mod       # local: compare imports geo too

    m = _driving_mask(run, max_accuracy_m, span)
    if m is None:
        return Fit(None, 0.0, 0.0, 0, 0)

    pts, unit_span = compare_mod.centreline(tm)
    u, v = tm.project(run.values("latitude")[m], run.values("longitude")[m])
    s, perp = geo.match_polyline(pts[:, 0], pts[:, 1], u, v,
                                 closed=bool(tm.closed),
                                 slack_m=8.0 / unit_span,
                                 min_window_m=25.0 / unit_span)
    length = float(geo.cumulative_distance(pts[:, 0], pts[:, 1])[-1])
    perp_m = perp * unit_span
    line_m = length * unit_span

    if tm.closed:
        inside = np.ones(s.shape, bool)
    else:
        eps = length * END_FRACTION
        inside = ~((s <= eps) | (s >= length - eps))

    coverage = float((s[-1] - s[0]) / length) if length > 0 else 0.0

    # Direction from plain projection, not from the matcher: the matcher is
    # forward-only by construction, so asking it which way the car went would
    # always get the same answer.
    #
    # Measured over interior samples only, for the same reason the fit is. An
    # SCCA run travels 1361 m over a 1107 m trimmed line, so 254 m of it pins to
    # the endpoints where consecutive projections jitter about zero and advance
    # nowhere. Counting those dropped a correct match to 57% forward, just under
    # the threshold, and the session was proposed as a new track.
    raw, _p = geo.project_to_polyline(pts[:, 0], pts[:, 1], u, v)
    d = np.diff(raw)
    pair = inside[:-1] & inside[1:]           # both ends of the step on the line
    d = d[pair]
    d = d[np.abs(d) < length * 0.5]           # ignore wraps at a closed line
    direction = float((d > 0).mean()) if d.size else 0.0

    n = int(inside.sum())
    fit_m = (float(np.percentile(perp_m[inside], 90)) if n >= MIN_INTERIOR
             else None)
    return Fit(fit_m, coverage, direction, n, int(s.size))


def rank(run, maps, source_name=None, span=None, max_accuracy_m=10.0):
    """Every candidate, best first.

    `maps` is `{key: TrackMap}`. Ordering puts matches before non-matches, then
    a matching source name before a non-matching one, then the closest fit --
    so the name only ever breaks a tie between two geometric matches, which is
    exactly the AMP-both-directions case it is useful for.
    """
    out = []
    for key, tm in maps.items():
        fit = fit_run(tm, run, span=span, max_accuracy_m=max_accuracy_m)
        names = getattr(tm, "source_names", None) or [tm.name]
        hit = bool(source_name) and source_name in set(names)
        out.append(Candidate(key=key, name=tm.name, fit=fit, name_matches=hit))
    out.sort(key=lambda c: (not c.matched, not c.name_matches,
                            c.fit.fit_m if c.fit.fit_m is not None else 1e18))
    return out


def decide(candidates):
    """Match, ambiguous, or new -- with the reason spelled out.

    Ambiguity is reported rather than resolved. Two courses in one parking lot
    that are genuinely similar have not been measured, and guessing there would
    silently merge two tracks -- which is worse than one question at import time.
    """
    good = [c for c in candidates if c.matched]
    if not good:
        note = "no existing track fits this run"
        if candidates:
            best = candidates[0]
            note += "; closest was %s (%s)" % (best.name, best.fit.why_not())
        return Decision("new", None, candidates, note)
    if len(good) == 1:
        return Decision("match", good[0], candidates,
                        "%s (%s)" % (good[0].name, good[0].fit.summary()))
    good.sort(key=lambda c: c.fit.fit_m)
    note = "%d tracks fit: %s" % (len(good), ", ".join(
        "%s at %.1f m" % (c.name, c.fit.fit_m) for c in good))
    return Decision("ambiguous", good[0], candidates, note)


def identify(run, maps, source_name=None, span=None, max_accuracy_m=10.0):
    return decide(rank(run, maps, source_name=source_name, span=span,
                       max_accuracy_m=max_accuracy_m))
