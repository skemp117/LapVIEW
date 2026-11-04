"""Import sessions and footage into the lap library.

    python tools/ingest.py "D:/track day" --dry-run   # decide, print, write nothing
    python tools/ingest.py "D:/track day"             # do it
    python tools/ingest.py "D:/day" --library other/ --cq 28 --pad 4

A folder, read where it sits. Nothing is copied and nothing is deleted: the
sessions and footage stay exactly where you pointed at them, and the import says
when the library holds a verified copy so you can remove them yourself.

`--dry-run` first, every time. Ingest re-encodes video and the originals are not
kept, so a lap cut at the wrong bitrate cannot be recut. The plan is the last
point at which anything is reversible.

Looking at, renaming, tagging, merging, splitting and deleting are
`tools/library.py`. This tool only imports.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod                                       # noqa: E402
from racechrono import ingest as ingest_mod, library as library_mod  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="the folder to import from")
    ap.add_argument("--library", default=None,
                    help="default: CONFIG['library_dir'], or LAPVIEW_DATA_DIR/"
                         "library when that is set")
    ap.add_argument("--maps")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-encode", action="store_true",
                    help="write records and lap data, skip the video")
    ap.add_argument("--cq", type=int, default=ingest_mod.DEFAULT_CQ,
                    help="lower is bigger and better; 28 is a third smaller "
                         "than source and never larger, 24 is near-source and "
                         "can grow a section, 30 is half")
    ap.add_argument("--pad", type=float, default=ingest_mod.PAD_S)

    g = ap.add_argument_group("what to keep")
    g.add_argument("--skip-edge-laps", action="store_true",
                   help="leave out each run's out-lap and in-lap")
    g.add_argument("--keep-best", type=int, metavar="N",
                   help="only the N quickest laps of each session")
    g.add_argument("--within-pct", type=float, metavar="PCT",
                   help="only laps within this %% of their session's best")
    g.add_argument("--max-gap", type=float, metavar="M",
                   help="leave out laps with a GPS hole bigger than this")
    g.add_argument("--require-video", action="store_true",
                   help="leave out laps no footage covers")
    g.add_argument("--no-video", action="store_true",
                   help="import telemetry only, write no video at all")

    v = ap.add_argument_group("video")
    v.add_argument("--height", type=int, metavar="PX",
                   help="downscale, e.g. 720 (about 52%% smaller)")
    v.add_argument("--fps", type=int, metavar="N",
                   help="re-time, e.g. 30 (about 44%% smaller)")
    v.add_argument("--mute", action="store_true",
                   help="drop the audio (about 1%% smaller)")

    o = ap.add_argument_group("organisation")
    o.add_argument("--event", metavar="NAME", default="",
                   help="tag every day this import touches")
    o.add_argument("--track", metavar="NAME", default="",
                   help="name the track(s) this import creates; several courses "
                        "at one event are numbered from it")
    o.add_argument("--track-id", metavar="ID", default="",
                   help="put every session on this existing track, whatever the "
                        "shape says")
    o.add_argument("--date", metavar="YYYY-MM-DD", default="",
                   help="override the date these sessions are filed under")
    args = ap.parse_args(argv)

    import main as main_mod
    raw = config_mod.apply_env(copy.deepcopy(main_mod.CONFIG))
    if args.maps:
        raw["maps_dir"] = args.maps
    cfg = config_mod.load(raw)
    lib = library_mod.Library(ROOT / (args.library or cfg.library_dir))
    if not args.dry_run and not args.no_encode:
        from racechrono import encoders
        encoders.resolve(cfg, log=print)

    options = ingest_mod.Options(
        pad_s=args.pad, cq=args.cq,
        skip_edge_laps=args.skip_edge_laps, keep_best=args.keep_best,
        slower_than_best_pct=args.within_pct, max_gap_m=args.max_gap,
        require_video=args.require_video, import_video=not args.no_video,
        height=args.height, fps=args.fps, audio=not args.mute,
        event=args.event, track_name=args.track, track_id=args.track_id,
        date_override=args.date)
    plan = ingest_mod.plan(cfg, lib, source=args.source, options=options)
    print(ingest_mod.format_plan(plan))
    if args.dry_run:
        print()
        print("dry run: nothing written")
        return 0
    if not plan.sessions:
        return 0

    print()
    made = ingest_mod.apply(plan, lib, cfg, encode=not args.no_encode)
    print()
    print("wrote %d track(s), %d day(s), %d session(s), %d lap(s), %s"
          % (len(made["tracks"]), len(made["days"]), len(made["sessions"]),
             len(made["laps"]), library_mod.human(made["bytes"])))

    check = ingest_mod.verify(made, lib, plan)
    print()
    if check["ok"]:
        print("checked: %d lap(s), %s on disk."
              % (check["laps"], library_mod.human(check["bytes"])))
        print("the %d file(s) in %s (%s) are now safe to delete."
              % (check["source_files"], check["source"],
                 library_mod.human(check["source_bytes"])))
    else:
        print("the check found %d problem(s) -- leave the originals alone:"
              % len(check["problems"]))
        for prob in check["problems"][:10]:
            print("  ! %s" % prob)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
