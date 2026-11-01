"""What is in a folder of sessions, and what this tool can do with each one.

    python tools/dataset_report.py "D:/track day"
    python tools/dataset_report.py tests/data --json

Run this first when a dataset arrives. It answers the question that went
unanswered three times -- "does anything here not fit what the code assumes" --
and it answers it out loud, per session, with a reason.

The last section is the important one. A session that no capability can use is
reported as **unhandled** rather than quietly skipped, and a shape that appears
in the folder but not in `tests/data/` is reported as **untested**, because a
suite that has never seen a shape cannot be said to cover it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod                                       # noqa: E402
from racechrono import (csv_io, runs as runs_mod, shapes as shapes_mod,  # noqa: E402
                        video)


def load(cfg, source, verbose=True):
    """Classify every session in a folder."""
    from racechrono import ingest as ingest_mod
    csv_paths, clips = ingest_mod.scan(source, cfg.ffmpeg.ffprobe)
    out, failed = [], []
    for p in csv_paths:
        try:
            run = csv_io.read_run(p, derive=False, validate_units=False)
        except Exception as exc:
            failed.append((p.name, "%s: %s" % (type(exc).__name__, exc)))
            continue
        mine = [c for c in clips if c.start_unix
                and run.t0 - 300 <= c.start_unix <= run.t1 + 300]
        spans = runs_mod.detect(run, max_accuracy_m=cfg.quality.max_accuracy_m)
        out.append((shapes_mod.classify(run, cfg, mine, spans), spans))
    return out, failed


def duplicates(source):
    """Byte-identical CSVs, which are a real thing that happens on export.

    `session_20260809_125705_scca_autocross_1_v3.csv` and its sibling had the
    same md5. Two copies of one run double its weight in every average the map
    builder and the ideal lap take, so they are worth naming.
    """
    import hashlib
    by = {}
    for p in sorted(Path(source).rglob("*.csv")):
        h = hashlib.md5(p.read_bytes()).hexdigest()
        by.setdefault(h, []).append(p.name)
    return [names for names in by.values() if len(names) > 1]


def report(cfg, source, tested_cells=None):
    rows, failed = load(cfg, source)
    lines = []
    lines.append("%d session(s) in %s" % (len(rows), source))
    if failed:
        lines.append("")
        lines.append("unreadable:")
        for name, why in failed:
            lines.append("  ! %-46s %s" % (name, why))

    lines.append("")
    lines.append("%-30s %6s %5s %5s %5s %5s %6s  %s"
                 % ("session", "dur", "laps", "runs", "IMU", "clips",
                    "venue", "shape"))
    for shape, _spans in rows:
        lines.append("%-30s %6.0f %5s %5d %5s %5d %6s  %s"
                     % (shape.stem[:30], shape.duration_s,
                        "%d%s" % (shape.n_laps, "" if shape.laps_usable else "?"),
                        shape.n_runs, "yes" if shape.has_imu else "-",
                        shape.n_clips,
                        (shape.venue or "NONE")[:6],
                        shape.label()))

    lines.append("")
    lines.append("shapes present (a cell is a combination every capability "
                 "should treat alike):")
    cells = shapes_mod.matrix([s for s, _ in rows])
    for cell, members in sorted(cells.items()):
        mark = ""
        if tested_cells is not None and cell not in tested_cells:
            mark = "   <- no fixture covers this"
        lines.append("  %-58s %2d session(s)%s"
                     % (" / ".join(cell), len(members), mark))

    lines.append("")
    lines.append("what each session supports:")
    for shape, _spans in rows:
        ok = shapes_mod.supported(shape)
        lines.append("  %-30s %s" % (shape.stem[:30], ", ".join(ok) or "nothing"))
        for cap, why in sorted(shapes_mod.blocked(shape).items()):
            lines.append("       %-18s %s" % ("- " + cap, why))

    multi = [(s, sp) for s, sp in rows if s.n_runs > 1]
    if multi:
        lines.append("")
        lines.append("sessions holding more than one run:")
        for shape, spans in multi:
            lines.append("  %s" % shape.stem)
            for line in runs_mod.describe(None, spans):
                lines.append("    %s" % line)

    dups = duplicates(source)
    if dups:
        lines.append("")
        lines.append("byte-identical files (one of each pair is a duplicate "
                     "export, and doubles that run's weight in any average):")
        for names in dups:
            lines.append("  %s" % "  ==  ".join(names))

    un = shapes_mod.unhandled([s for s, _ in rows])
    lines.append("")
    if un:
        lines.append("UNHANDLED -- nothing useful can be done with these yet:")
        for s in un:
            lines.append("  %-30s %s" % (s.stem[:30], s.label()))
            for cap, why in sorted(shapes_mod.blocked(s).items()):
                lines.append("       %-18s %s" % ("- " + cap, why))
    else:
        lines.append("every session supports at least one capability.")
    return "\n".join(lines), rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="the folder to look at")
    ap.add_argument("--maps")
    ap.add_argument("--compare-to", default="tests/data",
                    help="folder whose shapes count as tested")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import main as main_mod
    raw = dict(main_mod.CONFIG)
    if args.maps:
        raw["maps_dir"] = args.maps
    cfg = config_mod.load(raw)

    tested = None
    if args.compare_to and (ROOT / args.compare_to).is_dir() \
            and Path(args.source).resolve() != (ROOT / args.compare_to).resolve():
        raw2 = dict(raw)
        raw2["maps_dir"] = "tests/maps"
        fixture_rows, _f = load(config_mod.load(raw2), ROOT / args.compare_to)
        tested = {s.cell for s, _ in fixture_rows}

    text, rows = report(cfg, args.source, tested)
    if args.json:
        print(json.dumps({
            "source": str(args.source),
            "sessions": [s.as_dict() | {"supported": shapes_mod.supported(s),
                                        "blocked": shapes_mod.blocked(s)}
                         for s, _ in rows],
            "cells": [list(c) for c in sorted(shapes_mod.matrix([s for s, _ in rows]))],
            "tested_cells": [list(c) for c in sorted(tested)] if tested else None,
            "duplicates": duplicates(args.source),
            "unhandled": [s.stem for s in
                          shapes_mod.unhandled([s for s, _ in rows])],
        }, indent=1))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
