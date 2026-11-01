"""Look at and reorganise the lap library.

    python tools/library.py                              # the whole tree
    python tools/library.py --tracks                     # just the tracks
    python tools/library.py --verify                     # find drift
    python tools/library.py --reset                      # empty it (asks first)
    python tools/library.py --verify --repair            # and fix the safe half

    python tools/library.py --rename tracks <id> "Peachtree Course B"
    python tools/library.py --notes days <id> "damp in the morning"
    python tools/library.py --event <day-id> "SCCA Points Event 4"
    python tools/library.py --splits 0 300 700 --track <id>

    python tools/library.py --merge <keep-id> <other-id> [...]
    python tools/library.py --split <track-id> --sessions <id> [...] --name "Course B"

    python tools/library.py --select --track <id> --non-representative
    python tools/library.py --select --slower-than 130 --delete
    python tools/library.py --select --gap-over 50 --delete-video

Selection prints what it would remove and its size; `--delete` then does it.
Nothing here is undoable, because the originals are not kept -- so selection and
deletion are two commands rather than one flag.

`--delete-video` drops only the video and keeps the telemetry. Video is 99% of a
lap's size -- 276 MB against 2 MB on a typical AMP lap -- and a lap someone has
finished watching is still a lap they want in a comparison.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from racechrono import library as library_mod                     # noqa: E402


def show_tree(lib, deep=True):
    s = lib.summary()
    print("%d track(s), %d day(s), %d session(s), %d lap(s); %s indexed, "
          "%s on disk"
          % (s["tracks"], s["days"], s["sessions"], s["laps"],
             library_mod.human(s["bytes"]), library_mod.human(s["on_disk"])))
    for t in lib.tracks():
        tm = lib.map_of(t["id"])
        prov = lib.map_provenance(t["id"])
        gone = sum(1 for p in prov if not p["present"])
        laps = lib.laps(track_id=t["id"])
        size = sum(l["video_bytes"] + l["data_bytes"] for l in laps)
        print()
        print("%-28s %-13s %s  %d lap(s), %s"
              % (t["name"], "circuit" if t["closed"] else "open course",
                 t["id"], len(laps), library_mod.human(size)))
        if tm:
            note = "%d source(s)" % len(prov) if prov else "no provenance"
            if gone:
                note += ", %d since deleted" % gone
            print("    map %.0f m, %d points  (%s)"
                  % (tm.length_m, len(tm.points_unit), note))
        else:
            print("    no map yet")
        names = lib.source_names(t["id"])
        if names:
            print("    imported as: %s" % ", ".join(names))
        sp = lib.splits(t["id"])
        if sp:
            print("    splits at %s m"
                  % ", ".join("%.0f" % x["distance_m"] for x in sp))
        if not deep:
            continue
        events = {e["id"]: e["name"] for e in lib.events()}
        for day in lib.days(t["id"]):
            dl = lib.laps(day_id=day["id"])
            tag = events.get(day["event_id"])
            print("    %s  %s  %d session(s), %d lap(s), %s"
                  % (day["date"], ("[%s]" % tag) if tag else "",
                     len(lib.sessions(day_id=day["id"])), len(dl),
                     library_mod.human(sum(l["video_bytes"] + l["data_bytes"] for l in dl))))
            for sess in lib.sessions(day_id=day["id"]):
                sl = lib.laps(session_id=sess["id"])
                print("      %-16s %-30s %d lap(s)"
                      % (sess["id"], sess["name"][:30], len(sl)))
                for lap in sl:
                    flag = "" if lap["representative"] else "  out/in"
                    vid = library_mod.human(lap["video_bytes"]) if lap["video_bytes"] \
                        else "no video"
                    print("        %-16s %-4s %-3d %8.3f s  %10s%s"
                          % (lap["id"], lap["kind"], lap["number"],
                             lap["duration_s"], vid, flag))


def show_verify(report):
    total = (len(report["missing_files"]) + len(report["orphan_dirs"])
             + len(report["byte_drift"]) + len(report["orphan_rows"]))
    if not total:
        print("nothing adrift")
        return 0
    if report["byte_drift"]:
        print("%d file(s) are not the size the index recorded:"
              % len(report["byte_drift"]))
        for lid, kind, said, real in report["byte_drift"][:10]:
            print("  %s %-5s index %s, file %s"
                  % (lid, kind, library_mod.human(said), library_mod.human(real)))
    if report["missing_files"]:
        print("%d row(s) point at a file that is gone:"
              % len(report["missing_files"]))
        for lid, kind, rel in report["missing_files"][:10]:
            print("  %s %-5s %s" % (lid, kind, rel))
    if report["orphan_dirs"]:
        print("%d directory(ies) with no row, %s in total:"
              % (len(report["orphan_dirs"]),
                 library_mod.human(sum(sz for _n, sz in report["orphan_dirs"]))))
        for name, sz in report["orphan_dirs"][:10]:
            print("  laps/%s  %s" % (name, library_mod.human(sz)))
    if report["orphan_rows"]:
        print("%d row(s) with no parent:" % len(report["orphan_rows"]))
        for what, rid in report["orphan_rows"][:10]:
            print("  %s: %s" % (what, rid))
    if report["repaired"]:
        print("repaired %d" % report["repaired"])
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="library")
    ap.add_argument("--tracks", action="store_true", help="tracks only")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="empty the library; needs --yes")
    ap.add_argument("--yes", action="store_true",
                    help="confirm a --reset")

    ap.add_argument("--rename", nargs=3, metavar=("KIND", "ID", "NAME"))
    ap.add_argument("--notes", nargs=3, metavar=("KIND", "ID", "TEXT"))
    ap.add_argument("--event", nargs=2, metavar=("DAY_ID", "NAME"),
                    help="tag a day with an event, creating it if new")
    ap.add_argument("--splits", nargs="*", type=float, metavar="M")
    ap.add_argument("--track", metavar="ID")

    ap.add_argument("--merge", nargs="+", metavar="ID",
                    help="first id is kept, the rest are folded into it")
    ap.add_argument("--split", metavar="TRACK_ID")
    ap.add_argument("--sessions", nargs="+", metavar="ID")
    ap.add_argument("--name")

    ap.add_argument("--select", action="store_true")
    ap.add_argument("--day", metavar="ID")
    ap.add_argument("--session", metavar="ID")
    ap.add_argument("--slower-than", type=float, metavar="SECONDS")
    ap.add_argument("--non-representative", action="store_true")
    ap.add_argument("--keep-best", type=int, metavar="N")
    ap.add_argument("--no-video", action="store_true",
                    help="select laps that have no video")
    ap.add_argument("--has-video", action="store_true",
                    help="select laps that do have video")
    ap.add_argument("--gap-over", type=float, metavar="M",
                    help="select laps with a GPS hole bigger than this")
    ap.add_argument("--slower-than-best", type=float, metavar="PCT",
                    help="select laps this %% off the track's best")
    ap.add_argument("--delete", action="store_true",
                    help="with --select: remove the laps entirely")
    ap.add_argument("--delete-video", action="store_true",
                    help="with --select: remove only the video, keep the data")
    args = ap.parse_args(argv)

    lib = library_mod.Library(ROOT / args.library)
    if lib.migrated:
        print("migrated the library to schema v%d" % library_mod.SCHEMA_VERSION)

    if args.rename:
        kind, rid, name = args.rename
        lib.rename(kind, rid, name)
        print("renamed %s %s to %r" % (kind, rid, name))
        return 0
    if args.notes:
        kind, rid, text = args.notes
        lib.set_notes(kind, rid, text)
        print("noted on %s %s" % (kind, rid))
        return 0
    if args.event:
        day_id, name = args.event
        found = next((e for e in lib.events() if e["name"] == name), None)
        eid = found["id"] if found else lib.add_event(name)
        lib.set_event(day_id, eid)
        print("tagged day %s as %r%s"
              % (day_id, name, "" if found else " (new event)"))
        return 0
    if args.splits is not None:
        if not args.track:
            raise SystemExit("--splits needs --track <id>")
        lib.set_splits(args.track, args.splits)
        print("%d split(s) on %s" % (len(args.splits), args.track))
        return 0
    if args.merge:
        if len(args.merge) < 2:
            raise SystemExit("--merge needs the id to keep and at least one more")
        got = lib.merge_tracks(args.merge[0], *args.merge[1:])
        print("kept %s, absorbed %d track(s), moved %d day(s)"
              % (got["kept"], len(got["absorbed"]), got["days"]))
        return 0
    if args.split:
        if not args.sessions:
            raise SystemExit("--split needs --sessions <id> [...]")
        got = lib.split_track(args.split, args.sessions, name=args.name)
        print("moved %d session(s) onto new track %s (no map yet)"
              % (got["sessions"], got["track"]))
        return 0
    if args.reset:
        before = lib.summary()
        if not args.yes:
            print("this would remove %d lap(s) and %s from %s"
                  % (before["laps"], library_mod.human(before["on_disk"]),
                     ROOT / args.library))
            print("the source CSVs and footage are not touched -- everything "
                  "here was derived from them")
            print("add --yes to do it")
            return 0
        lib.reset()
        after = lib.summary()
        print("cleared %d lap(s) and %s; the library is now empty"
              % (before["laps"], library_mod.human(before["on_disk"])))
        print("%d track(s), %d lap(s), %s on disk"
              % (after["tracks"], after["laps"],
                 library_mod.human(after["on_disk"])))
        return 0
    if args.verify:
        return 1 if show_verify(lib.verify(repair=args.repair)) and \
            not args.repair else 0
    if args.vacuum:
        before = lib.paths.db.stat().st_size
        lib.vacuum()
        print("database %s -> %s" % (library_mod.human(before),
                                     library_mod.human(lib.paths.db.stat().st_size)))
        return 0
    if args.select:
        sel = lib.select_laps(
            track_id=args.track, day_id=args.day, session_id=args.session,
            slower_than=args.slower_than,
            non_representative=args.non_representative,
            keep_best=args.keep_best, no_video=args.no_video,
            has_video=args.has_video, gap_over_m=args.gap_over,
            slower_than_best_pct=args.slower_than_best)
        size = sum(l["video_bytes"] + l["data_bytes"] for l in sel)
        print("%d lap(s), %s" % (len(sel), library_mod.human(size)))
        for lap in sel:
            print("  %-16s %-4s %-3d %8.3f s  %10s%s"
                  % (lap["id"], lap["kind"], lap["number"], lap["duration_s"],
                     library_mod.human(lap["video_bytes"] + lap["data_bytes"]),
                     "" if lap["representative"] else "  out/in"))
        if args.delete_video:
            got = lib.delete_videos([l["id"] for l in sel])
            print()
            print("removed the video from %d lap(s), freed %s; the telemetry "
                  "stays" % (got["count"], library_mod.human(got["freed_bytes"])))
            return 0
        if not args.delete:
            print()
            print("nothing removed; add --delete-video to drop the video and "
                  "keep the data, or --delete to remove the laps entirely")
            return 0
        got = lib.delete_laps([l["id"] for l in sel])
        print()
        print("deleted %d lap(s), freed %s" % (got["count"],
                                               library_mod.human(got["freed_bytes"])))
        for kind, ids in got["pruned"].items():
            if ids:
                print("  pruned %d empty %s" % (len(ids), kind))
        return 0

    show_tree(lib, deep=not args.tracks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
