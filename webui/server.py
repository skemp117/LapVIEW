"""The localhost session view: routing and bytes.

    python -m webui.server                       # then open the printed URL
    python -m webui.server --library "D:/laps" --maps Maps
    python -m webui.server --port 8770 --no-open

Routes
    GET  /                              the app shell
    GET  /static/<name>                 page assets
    GET  /api/library                   tracks, days and laps: the whole tree
    GET  /api/laps?track=&day=          laps, filtered
    GET  /api/lap/<id>                  one lap: channels, map, driven line, video
    GET  /api/trace/<id>?ch=&n=         one channel of a lap, decimated
    GET  /api/layout/<id>               overlay frame size and region geometry
    GET  /api/overlay/<id>?t=           the overlay at that moment, packed BGRA
    GET  /video/<id>                    that lap's video, range-served
    GET  /api/comparable/<id>           every lap at the same track
    GET  /api/compare?laps=&ref=        delta traces and a sector table
    GET  /api/export?laps=              what exporting those laps would write
    GET  /api/export/progress           how the render is going
    GET  /api/survey?path=              how many files a folder holds
    GET  /api/import/review             what the last import produced
    GET  /api/browse?path=              folders under a path, for the picker
    GET  /api/import?source=&...        what importing that folder would do
    GET  /api/import/progress           how the running import is going
    GET  /api/config                    paths, server mode, upload limits
    GET  /api/health                    ffmpeg, encoder, writable data dirs
    GET  /api/exports                   files in the export folder
    GET  /download/<name>               one of them, as an attachment, ranged
    GET  /api/inbox                     upload folders and what they hold
    GET  /api/upload/status?id=         how much of an upload has arrived
    POST /api/rename                    {kind, id, name}: rename a track, day,
                                        session or lap
    POST /api/notes                     {kind, id, text}: tags/notes on one
    POST /api/event                     {day, name}: put a day under an event
    POST /api/delete                    {what, ids}: delete laps, their video,
                                        or tracks
    POST /api/import/run                {source, event, track|track_id, ...}:
                                        start an import on a worker thread
    POST /api/export/run                {laps, preview, overwrite}: render them,
                                        overlay burned in
    POST /api/reveal                    {path}: show an export in the file browser
    POST /api/pick                      {}: open the OS folder chooser
    POST /api/library/reset             {confirm: "yes"}: empty the library
    POST /api/rescan                    {}: drop loaded laps and overlays
    POST /api/upload/folder             {name}: a new inbox folder
    POST /api/upload/begin              {folder, name, size}: start or resume
    POST /api/upload/chunk?id=&offset=  raw bytes, written at that offset
    POST /api/upload/finish             {id}: check it is whole, move it in
    POST /api/upload/abort              {id}: forget a partial upload
    POST /api/inbox/delete              {folder}: remove an upload folder

Anything that changes something is a POST, takes a JSON body, and needs an
`X-LapVIEW: 1` header (see `do_POST`); a GET or HEAD of one answers 405 and does
nothing. The JSON names are the query parameters these routes used to take,
with the same meanings. Uploads are in `webui/uploads.py`.

Why stdlib only: this has to run for someone who unzipped a folder and typed one
command. A build step, a package manager or a framework is a support burden
before the tool has done anything useful. `ThreadingHTTPServer` handles the one
browser this serves, and the expensive work (loading a run, correlating audio) is
done once per session and cached in memory.

Everything served comes from the lap library. There is no `Raw Data` and no
`Raw Footage`: a folder is imported once and the laps it produced are what the
tool has, so a lap survives its source files being deleted.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as config_mod                                       # noqa: E402
import numpy as np                                                # noqa: E402

from racechrono import (compare as compare_mod, csv_io,           # noqa: E402
                        library as library_mod, render, video, window)
from webui import session as session_mod                          # noqa: E402
from webui import uploads as uploads_mod                          # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"


def headless_default(environ=None, platform=None):
    """Whether this is a server rather than someone's desktop.

    LAPVIEW_HEADLESS decides when it is set. Otherwise a data dir in the
    environment means a container, and Linux with no display means a server;
    either way there is no desktop to open a folder chooser or a file browser
    on, and the filesystem is not the browser's to wander.
    """
    env = os.environ if environ is None else environ
    v = str(env.get("LAPVIEW_HEADLESS") or "").strip()
    if v:
        return config_mod.env_flag(v, "LAPVIEW_HEADLESS")
    if str(env.get("LAPVIEW_DATA_DIR") or "").strip():
        return True
    plat = platform or sys.platform
    return plat.startswith("linux") and not (env.get("DISPLAY")
                                             or env.get("WAYLAND_DISPLAY"))


class Library:
    """The lap library, as the app sees it.

    Everything served comes from `library_dir`. There is no `Raw Data` and no
    `Raw Footage`: a folder is imported once, the laps it produced are what the
    tool has, and the originals can be deleted. A lap carries its own telemetry,
    its own video and the calibration of the session it came from, so nothing
    here needs the source file that produced it.

    The unit is the lap. It was the session, because that is what a folder of
    CSVs contains, and a session is not a thing anyone watches -- it is a
    recording that happens to hold one run, or eight laps, or an evening.

    One instance is shared by every request thread. The sqlite connection under
    `racechrono.library.Library` is per-thread; the caches here are guarded by a
    lock.
    """

    def __init__(self, cfg, headless=False, browse_roots=()):
        self.cfg = cfg
        # Server mode: no desktop integrations, and browsing and importing
        # confined to the data folders (`roots`) instead of the whole disk.
        self.headless = bool(headless)
        self.extra_roots = [Path(p).expanduser().resolve()
                            for p in browse_roots if str(p).strip()]
        self.encoder = None          # what encoders.resolve settled on
        self.inbox = uploads_mod.Inbox(self.inbox_dir())
        self._lock = threading.RLock()
        self._db = None
        self._runs = {}              # lap id -> loaded Run
        self._overlays = {}          # lap id -> compositor and row lookup
        self._plan = None
        self._plan_source = None
        self._job = None
        self._xjob = None            # the export, which is its own job
        # Caches the library reset and the import worker clear. They were only
        # ever cleared, never made, so a reset wiped the library and then
        # answered 500, and every import ended in an AttributeError in its
        # `finally` -- harmless only because the thread died right after.
        self._summaries = None
        self._listings = {}
        self._sessions = {}
        self._telemetry = {}

    # ---------------------------------------------------------------- basics
    #
    # Relative directories are read against the code, as they always were; the
    # environment makes them absolute, which `ROOT / p` leaves alone.
    def lib_dir(self):
        return (ROOT / self.cfg.library_dir).resolve()

    def out_dir(self):
        return (ROOT / self.cfg.output_dir).resolve()

    def inbox_dir(self):
        return (ROOT / self.cfg.inbox_dir).resolve()

    def cache_dir(self):
        return Path(self.cfg.cache_dir).resolve()

    def roots(self):
        """Where a server lets the browser look: the inbox, the library, and
        any LAPVIEW_BROWSE_ROOTS."""
        out = []
        for p in [self.inbox_dir(), self.lib_dir()] + self.extra_roots:
            if p not in out:
                out.append(p)
        return out

    def allowed(self, path):
        """`path`, resolved, if this server may read it; PermissionError if not.

        On a desktop that is anywhere -- the person at the browser owns the
        disk. On a server it is the data roots only, and a relative path is
        read against the inbox, so `2026-11-03_141502` names an upload.
        Resolving first means `..` and symlinks are judged by where they land.
        """
        p = Path(str(path)).expanduser()
        if self.headless and not p.is_absolute():
            p = self.inbox_dir() / p
        try:
            p = p.resolve()
        except OSError:
            pass
        if not self.headless:
            return p
        for root in self.roots():
            if p == root or root in p.parents:
                return p
        raise PermissionError(
            "%s is outside the folders this server reads (%s)"
            % (p, ", ".join(str(r) for r in self.roots())))

    def library(self):
        with self._lock:
            if self._db is None:
                self._db = library_mod.Library(ROOT / self.cfg.library_dir)
            return self._db

    def close(self):
        """Let go of the sqlite handle. Windows will not delete an open file,
        so a test that builds a library in a temporary directory needs this."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def rescan(self):
        with self._lock:
            self._runs.clear()
            self._overlays.clear()
            self._plan = None
            self._plan_source = None

    def _imu_opts(self):
        return self.cfg.imu.as_kwargs() if self.cfg.imu.enabled else None

    # ------------------------------------------------------------------ tree
    def tree(self):
        """Tracks, their days, and the laps under each -- the whole library."""
        lib = self.library()
        events = {e["id"]: e["name"] for e in lib.events()}
        tracks = []
        for t in lib.tracks():
            tm = lib.map_of(t["id"])
            days = []
            for day in lib.days(t["id"]):
                laps = [self._lap_row(l) for l in lib.laps(day_id=day["id"])]
                sessions = {}
                for sess in lib.sessions(day_id=day["id"]):
                    sessions[sess["id"]] = sess["name"]
                days.append({
                    "id": day["id"], "date": day["date"],
                    "event": events.get(day["event_id"], ""),
                    "event_id": day["event_id"] or "",
                    "notes": day["notes"],
                    "sessions": sessions,
                    "laps": laps,
                })
            tracks.append({
                "id": t["id"], "name": t["name"], "notes": t["notes"],
                "closed": bool(t["closed"]),
                "has_map": tm is not None,
                "length_m": round(tm.length_m, 1) if tm else 0,
                "source_names": lib.source_names(t["id"]),
                "splits": [s["distance_m"] for s in lib.splits(t["id"])],
                "days": days,
            })
        return {"library_dir": str(self.cfg.library_dir),
                "summary": lib.summary(), "tracks": tracks}

    def _lap_row(self, lap):
        return {
            "id": lap["id"], "kind": lap["kind"], "number": lap["number"],
            "name": lap["name"], "session_id": lap["session_id"],
            "duration": round(lap["duration_s"], 3),
            "label": window.format_lap_time(lap["duration_s"]),
            "t0_unix": lap["t0_unix"],
            "representative": bool(lap["representative"]),
            "has_video": bool(lap["video_path"]),
            "notes": lap["notes"],
            "bytes": lap["video_bytes"] + lap["data_bytes"],
            "gap_m": lap["gap_m"], "measured": lap["measured_frac"],
        }

    def laps(self, track_id=None, day_id=None):
        lib = self.library()
        return [self._lap_row(l) for l in lib.laps(track_id=track_id,
                                                   day_id=day_id)]

    def track_of(self, lap_id):
        return self.library().track_of_lap(lap_id)

    # -------------------------------------------------------------- one lap
    def run_of(self, lap_id):
        """The lap's telemetry, loaded once.

        From the lap's own file. The session that produced it is long gone, and
        the calibration it was solved with travelled with the lap.
        """
        with self._lock:
            if lap_id in self._runs:
                return self._runs[lap_id]
        lib = self.library()
        lap = lib.lap(lap_id)
        if lap is None:
            raise KeyError(lap_id)
        path = lib.paths.root / lap["data_path"]
        if not path.is_file():
            raise FileNotFoundError("lap %s has no telemetry file" % lap_id)
        run = csv_io.read_run(path, imu_opts=self._imu_opts(),
                              derive=self.cfg.imu.enabled,
                              validate_units=False)
        with self._lock:
            self._runs[lap_id] = run
        return run

    def map_of(self, lap_id):
        tid = self.track_of(lap_id)
        return self.library().map_of(tid) if tid else None

    def describe_lap(self, lap_id):
        lib = self.library()
        lap = lib.lap(lap_id)
        if lap is None:
            raise KeyError(lap_id)
        run = self.run_of(lap_id)
        tm = self.map_of(lap_id)
        cfg = config_mod.bind(self.cfg, run)
        sess = lib.session(lap["session_id"])
        tid = self.track_of(lap_id)
        track = lib.track(tid) if tid else None

        file_t0 = float(run.t0)
        d = {
            "id": lap_id,
            "kind": lap["kind"], "number": lap["number"],
            "name": lap["name"] or "%s %d" % (lap["kind"], lap["number"]),
            "label": window.format_lap_time(lap["duration_s"]),
            "duration": round(lap["duration_s"], 3),
            "representative": bool(lap["representative"]),
            "gap_m": lap["gap_m"], "measured": lap["measured_frac"],
            "track": {"id": tid, "name": track["name"] if track else "",
                      "closed": bool(track["closed"]) if track else True},
            "session": {"id": lap["session_id"],
                        "name": sess["name"] if sess else "",
                        "calibration": json.loads(sess["calibration"])
                                       if sess else {}},
            # One clock: seconds from the first row of this lap's file. The
            # padding either side is part of the file, so the timed lap starts
            # partway in and the video may start later still where the padding
            # ran past the end of the footage.
            "t0_unix": round(file_t0, 3),
            "file_s": round(float(run.duration), 3),
            "lap_start": round(float(lap["t0_unix"]) - file_t0, 3),
            "lap_end": round(float(lap["t1_unix"]) - file_t0, 3),
            "rows": int(run.df.shape[0]),
            "sample_hz": round(run.df.shape[0] / max(run.duration, 1e-6), 1),
            "channels": session_mod.channel_list(run),
            "map": session_mod.map_payload(tm),
            "track_line": (session_mod.position_track(run, tm,
                                                      cfg.quality.max_accuracy_m)
                           if tm else None),
            "video": None,
        }
        if lap["video_path"]:
            d["video"] = {
                "url": "/video/%s" % lap_id,
                "offset": round(float(lap["video_t0"]) - file_t0, 3),
                "duration": round(float(lap["video_s"]), 3),
                "bytes": lap["video_bytes"],
            }
        default = next((c["name"] for c in d["channels"] if c["preferred"]),
                       d["channels"][0]["name"] if d["channels"] else None)
        d["default_channel"] = default
        d["traces"] = ({default: session_mod.trace(run, default, 1200)}
                       if default else {})
        return d

    def trace(self, lap_id, channel, n=1200):
        return session_mod.trace(self.run_of(lap_id), channel, n)

    def video_path(self, lap_id):
        lib = self.library()
        lap = lib.lap(lap_id)
        if lap is None or not lap["video_path"]:
            raise KeyError(lap_id)
        return lib.paths.root / lap["video_path"]

    # ------------------------------------------------------------- comparing
    def comparable(self, lap_id):
        """Every lap at this lap's track."""
        tid = self.track_of(lap_id)
        if tid is None:
            return {"track": None, "laps": [],
                    "note": "this lap's track has no map"}
        lib = self.library()
        track = lib.track(tid)
        rows = self.laps(track_id=tid)
        for r in rows:
            sess = lib.session(r["session_id"])
            r["session"] = sess["name"] if sess else ""
        rows.sort(key=lambda r: r["duration"])
        return {"track": {"id": tid, "name": track["name"],
                          "closed": bool(track["closed"])},
                "laps": rows}

    def compare(self, lap_ids, ref=None, points=800, n_sectors=8,
                channels=("speed_gps",)):
        """Compare stored laps. Each lap is already a traversal."""
        lib = self.library()
        ids = [i for i in lap_ids if i]
        if not ids:
            raise ValueError("compare needs at least one lap")
        tracks = {self.track_of(i) for i in ids}
        tracks.discard(None)
        if len(tracks) > 1:
            names = sorted(lib.track(t)["name"] for t in tracks)
            raise ValueError("those laps are at different tracks (%s); a "
                             "comparison needs one shared centreline"
                             % ", ".join(names))
        if not tracks:
            raise ValueError("those laps have no track")
        tid = tracks.pop()
        tm = lib.map_of(tid)
        if tm is None:
            raise ValueError("%s has no map yet" % lib.track(tid)["name"])

        tvs, meta = [], {}
        for lap_id in ids:
            run = self.run_of(lap_id)
            lap = lib.lap(lap_id)
            have = [c for c in channels if run.has(c)]
            ts = run.values("timestamp")
            i0 = int(np.searchsorted(ts, lap["t0_unix"]))
            i1 = int(np.searchsorted(ts, lap["t1_unix"])) + 1
            sess = lib.session(lap["session_id"])
            label = "%s %d" % (lap["kind"], lap["number"])
            tv = compare_mod.traversal(
                run, tm, i0, i1, key=lap_id, label=label,
                number=lap["number"], channels=tuple(have),
                max_accuracy_m=self.cfg.quality.max_accuracy_m)
            if tv is None:
                continue
            tv.duration = float(lap["duration_s"])
            tvs.append(tv)
            meta[lap_id] = {
                "session": sess["name"] if sess else "",
                "date": (lib.db.execute(
                    "SELECT date FROM days JOIN sessions ON sessions.day_id=days.id"
                    " WHERE sessions.id=?", (lap["session_id"],)).fetchone()
                    or {"date": ""})["date"],
                "has_video": bool(lap["video_path"]),
                "file_t0": float(run.t0),
                "lap_start": float(lap["t0_unix"]) - float(run.t0),
            }
        have = [c for c in channels
                if all(c in tv.channels for tv in tvs)] if tvs else []
        c = compare_mod.assemble(tvs, tm, ref=ref, channels=tuple(have),
                                 points=points, n_sectors=n_sectors)
        c["track"] = {"id": tid, "name": lib.track(tid)["name"]}
        for row in c.get("traversals", []):
            row.update(meta.get(row["key"], {}))
        return c

    # ------------------------------------------------------------- overlay
    def overlay(self, lap_id):
        with self._lock:
            if lap_id in self._overlays:
                return self._overlays[lap_id]
        run = self.run_of(lap_id)
        tm = self.map_of(lap_id)
        cfg = config_mod.bind(self.cfg, run)
        lap = self.library().lap(lap_id)
        w, h, fps = 1920, 1080, float(cfg.encode.fps or 60.0)
        if lap["video_path"]:
            try:
                clip = video.probe(self.video_path(lap_id), cfg.ffmpeg.ffprobe)
                w, h = clip.display_size
                fps = float(cfg.encode.fps or clip.fps)
            except Exception:
                pass
        comp = render.make_compositor(cfg, w, h, fps, tm, for_preview=True)
        lookup, grid_t0, n = render.row_source(run, cfg, fps, run.t0, run.t1,
                                               comp.channels)
        got = {"comp": comp, "lookup": lookup, "grid_t0": grid_t0, "n": n,
               "fps": fps, "w": w, "h": h}
        with self._lock:
            self._overlays[lap_id] = got
        return got

    def overlay_frame(self, lap_id, t):
        ov = self.overlay(lap_id)
        run = self.run_of(lap_id)
        i = int(round((run.t0 + float(t) - ov["grid_t0"]) * ov["fps"]))
        i = max(0, min(ov["n"] - 1, i))
        with self._lock:
            ov["comp"].draw(ov["lookup"](i))
            views = ov["comp"].region_views()
            head = struct.pack("<i", len(views))
            for r, _buf, _stride in views:
                head += struct.pack("<4i", r.x, r.y, r.w, r.h)
            return head + b"".join(buf for _r, buf, _s in views)

    def layout(self, lap_id):
        ov = self.overlay(lap_id)
        comp = ov["comp"]
        return {"frame": [ov["w"], ov["h"]], "fps": ov["fps"],
                "regions": [{"x": r.x, "y": r.y, "w": r.w, "h": r.h}
                            for r in comp.regions],
                "atlas": [comp.atlas_w, comp.atlas_h],
                "bytes_per_frame": comp.bytes_per_frame,
                "coverage": round(comp.coverage, 4),
                "gauges": [g.name for g in comp.gauges]}

    def browse(self, path=None):
        """List folders under `path`, with a count of what is importable.

        A picker rather than an upload. The server and the browser are the same
        machine, so pointing at a folder reads it where it is -- no copy of 16 GB
        of footage, and nothing to clean up afterwards.
        """
        from racechrono import ingest as ingest_mod
        if self.headless:
            # A server's disk is not the browser's to walk: the data roots
            # only, starting at the inbox, and no way up out of them.
            target = self.allowed(path) if path else self.inbox_dir()
        else:
            target = Path(path).expanduser() if path else Path.home()
            try:
                target = target.resolve()
            except OSError:
                pass
        up = str(target.parent)
        if self.headless:
            try:
                self.allowed(target.parent)
                if target in self.roots():
                    up = ""
            except PermissionError:
                up = ""
        if not target.is_dir():
            return {"path": str(target), "exists": False, "entries": [],
                    "parent": up, "roots": self.browse_roots()}
        entries = []
        try:
            for child in sorted(target.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                try:
                    shallow = {"csvs": 0, "videos": 0}
                    for f in child.iterdir():
                        if not f.is_file():
                            continue
                        if f.suffix.lower() == ".csv":
                            shallow["csvs"] += 1
                        elif f.suffix.lower() in video.VIDEO_EXTS:
                            shallow["videos"] += 1
                except OSError:
                    shallow = {"csvs": 0, "videos": 0}
                entries.append({"name": child.name, "path": str(child),
                                **shallow})
        except OSError as exc:
            return {"path": str(target), "exists": True, "entries": [],
                    "parent": up, "error": str(exc)}
        here = ingest_mod.survey(target)
        return {"path": str(target), "exists": True, "parent": up,
                "entries": entries, "here": here,
                "roots": self.browse_roots()}

    def browse_roots(self):
        if self.headless:
            names = {self.inbox_dir(): "inbox", self.lib_dir(): "library"}
            return [{"name": names.get(r, r.name), "path": str(r)}
                    for r in self.roots()]
        return _roots()

    def import_plan(self, source, options=None):
        """Plan an import of one folder, and cache it.

        Held between requests so that moving a checkbox re-filters rather than
        re-reads: classifying and identifying 19 sessions is about 12 s, and
        `apply_filters` on the result is instant. The import screen is a lot of
        checkboxes, and a 12 s pause on each one would make it unusable.
        """
        from racechrono import ingest as ingest_mod
        src = str(Path(source).expanduser())
        with self._lock:
            if self._plan is None or self._plan_source != src:
                self._plan = ingest_mod.plan(
                    self.cfg, self.library(), source=src,
                    options=options or ingest_mod.Options(), verbose=False)
                self._plan_source = src
            elif options is not None:
                self._plan.options = options
                ingest_mod.apply_filters(self._plan)
            return self._plan

    def import_payload(self, source, options=None):
        """The plan as JSON, for the import screen."""
        p = self.import_plan(str(self.allowed(source)), options)
        est = p.estimate()
        tracks = []
        for tp in p.tracks:
            tracks.append({
                "key": tp.key,
                "name": tp.name,
                "closed": tp.closed,
                "new": tp.existing_id is None,
                "needs_map": tp.needs_map,
                "source_names": sorted(tp.source_names),
                "sessions": [sp.stem for sp in tp.sessions],
            })
        sessions = []
        for sp in p.sessions:
            sessions.append({
                "stem": sp.stem,
                "file": sp.path.name,
                "date": sp.date,
                "track_key": sp.track_key,
                "track_name": sp.track_name,
                "verdict": sp.verdict,
                "note": sp.note,
                "problems": list(sp.problems),
                "shape": sp.shape.label() if sp.shape else "",
                "kind": "lap" if (sp.shape and sp.shape.laps_usable) else "run",
                "laps": [{
                    "number": lp.number,
                    "kind": lp.kind,
                    "duration": round(lp.duration, 3),
                    "label": window.format_lap_time(lp.duration),
                    "representative": lp.representative,
                    "has_video": lp.has_video,
                    "split": len(lp.segments) > 1,
                    "video_s": round(lp.video_s, 1),
                    "gap_m": lp.gap_m,
                    "measured": lp.measured_frac,
                    "dropped": lp.dropped,
                } for lp in sp.laps],
            })
        from racechrono import ingest as ingest_mod
        return {
            "source": str(p.source),
            "survey": ingest_mod.survey(p.source),
            "library_dir": str(self.cfg.library_dir),
            "duplicates": [{"file": a, "same_as": b} for a, b in p.duplicates],
            "already": [{"file": a, "session": h["name"], "id": h["id"]}
                        for a, h in p.already],
            "skipped": [{"file": a, "why": b} for a, b in p.skipped],
            "tracks": tracks,
            "sessions": sessions,
            "estimate": est,
            "options": _options_dict(p.options),
            "library": self.library().summary(),
        }

    def survey(self, source):
        """What is in a folder, cheaply. No parsing, no probing, no planning."""
        from racechrono import ingest as ingest_mod
        got = ingest_mod.survey(self.allowed(source))
        got["library"] = self.library().summary()
        return got

    def import_start(self, source, options=None):
        """Begin an import on a worker thread and return immediately.

        Importing the working set is about thirteen minutes of encoding. Holding
        an HTTP request open for that long asks the browser not to time out,
        gives no progress, and turns a lost connection into an unknown state --
        so the request starts the job and the page asks how it is going.
        """
        from racechrono import ingest as ingest_mod
        source = str(self.allowed(source))
        with self._lock:
            if self._job and self._job.get("state") == "running":
                return dict(self._job)
        job = {"state": "running", "phase": "reading the folder", "done": 0,
               "total": 0, "lap": "", "bytes": 0, "started": time.time(),
               "source": str(source), "error": "", "result": None}
        with self._lock:
            self._job = job

        def progress(sp, lp, lid, vbytes):
            job["done"] += 1
            job["bytes"] += vbytes
            job["lap"] = "%s %s %d" % (sp.stem, lp.kind, lp.number)

        def phase(name):
            job["phase"] = name

        def work():
            try:
                plan = self.import_plan(source, options)
                job["total"] = plan.estimate()["laps"]
                job["sessions"] = len(plan.sessions)
                job["tracks"] = len(plan.tracks)
                job["already"] = len(plan.already)
                # A session put on a track by hand can be the wrong shape for
                # it. The plan says so; the review has to show it rather than
                # filing it quietly.
                job["warnings"] = [
                    {"session": sp.stem, "why": why}
                    for sp in plan.sessions for why in sp.problems]
                made = ingest_mod.apply(plan, self.library(), self.cfg,
                                        encode=True, verbose=False,
                                        on_progress=progress, on_phase=phase)
                job["phase"] = "checking what was written"
                check = ingest_mod.verify(made, self.library(), plan)
                job["result"] = {
                    "tracks": len(made["tracks"]), "days": len(made["days"]),
                    "sessions": len(made["sessions"]),
                    "laps": len(made["laps"]), "bytes": made["bytes"],
                    "verified": check,
                    "already": len(plan.already),
                    "dropped": len(plan.dropped()),
                    "warnings": job["warnings"],
                    "track_ids": list(made["tracks"]),
                    "lap_ids": list(made["laps"])}
                job["phase"] = "done"
                job["state"] = "done" if check["ok"] else "unverified"
            except Exception as exc:
                job["error"] = "%s: %s" % (type(exc).__name__, exc)
                job["state"] = "failed"
            finally:
                with self._lock:
                    self._plan = None
                    self._plan_source = None
                    self._summaries = None
                    self._listings.clear()
        threading.Thread(target=work, daemon=True).start()
        return dict(job)

    # --------------------------------------------------------------- export
    #
    # The renderer works from a folder of sessions and their original footage;
    # this works from the library, which is the only thing that stays current.
    # The originals are the user's to delete and the import says when it is safe
    # to, so an export that needed them would be an export that stops working.

    def export_offer(self, lap_ids):
        """What exporting this selection would produce, before committing."""
        from racechrono import export as export_mod
        lib = self.library()
        out = []
        for lid in lap_ids:
            try:
                out.append(export_mod.describe(lib, lid))
            except KeyError:
                continue
        return {"laps": out,
                "with_video": sum(1 for x in out if x["has_video"]),
                "seconds": round(sum(x["video_s"] for x in out
                                     if x["has_video"]), 1),
                "out_dir": str(self.out_dir())}

    def export_start(self, lap_ids, preview_s=None, overwrite=False):
        """Render on a worker thread; the page asks how it is going.

        One lap is about half its own length in wall time, so even a single
        export outlives an HTTP request worth holding open.
        """
        from racechrono import export as export_mod
        with self._lock:
            if self._xjob and self._xjob.get("state") == "running":
                return dict(self._xjob)
        ids = [i for i in lap_ids if i]
        if not ids:
            raise ValueError("nothing to export")
        offer = self.export_offer(ids)
        ids = [x["id"] for x in offer["laps"] if x["has_video"]]
        if not ids:
            raise ValueError("none of those laps has footage in the library, "
                             "so there is nothing to overlay")
        job = {"state": "running", "done": 0, "total": len(ids),
               "lap": "", "frame": 0, "frames": 0, "files": [],
               "out_dir": offer["out_dir"], "started": time.time(),
               "error": "", "preview_s": preview_s}
        with self._lock:
            self._xjob = job

        def on_lap(i, n, got):
            job["done"] = i
            job["lap"] = got["name"]
            job["frame"] = 0
            job["frames"] = 0

        def on_frame(f, n):
            job["frame"] = f
            job["frames"] = n

        def work():
            try:
                made = export_mod.export(
                    self.cfg, self.library(), ids,
                    out_dir=self.out_dir(),
                    overwrite=overwrite, preview_s=preview_s,
                    on_lap=on_lap, on_frame=on_frame)
                job["files"] = made
                job["done"] = len(ids)
                job["state"] = "done"
            except Exception as exc:
                job["error"] = "%s: %s" % (type(exc).__name__, exc)
                job["state"] = "failed"

        threading.Thread(target=work, daemon=True).start()
        return dict(job)

    def export_progress(self):
        with self._lock:
            job = dict(self._xjob) if self._xjob else {"state": "idle",
                                                       "files": []}
        if job.get("state") == "running":
            frames = job.get("frames") or 0
            done = job.get("done", 0) + (job.get("frame", 0) / frames
                                         if frames else 0)
            job["fraction"] = round(done / max(1, job.get("total", 1)), 4)
            elapsed = time.time() - job["started"]
            if done > 0.05:
                job["eta_s"] = round(elapsed * (job["total"] - done) / done, 1)
        job["files"] = [dict(f, url=self.download_url(f["out"]))
                        if f.get("out") and not f.get("error") else f
                        for f in job.get("files", [])]
        job["headless"] = self.headless
        return job

    # ------------------------------------------------------------ downloads
    #
    # An export on a server is a file on someone else's disk. "Show in folder"
    # cannot help with that; a download link can, and it is the same link on a
    # phone and on a desktop.

    def download_url(self, path):
        """`/download/<name>` for a file in the export folder, else None."""
        try:
            rel = Path(path).resolve().relative_to(self.out_dir())
        except (ValueError, OSError):
            return None
        return "/download/" + quote(rel.as_posix())

    def export_file(self, rel):
        """A file in the export folder by its path there; KeyError otherwise.

        Resolved before it is judged, so `..`, an absolute path or a symlink
        that leads out of the folder is simply not there.
        """
        out = self.out_dir()
        if not rel or "\x00" in rel:
            raise KeyError(rel)
        target = (out / rel).resolve()
        if out not in target.parents or not target.is_file():
            raise KeyError(rel)
        return target

    def exports(self):
        """Everything in the export folder, newest first, with its link."""
        out = self.out_dir()
        rows = []
        if out.is_dir():
            for p in out.iterdir():
                if p.is_file() and not p.name.startswith("."):
                    st = p.stat()
                    rows.append({"name": p.name, "bytes": st.st_size,
                                 "mtime": st.st_mtime, "out": str(p),
                                 "url": self.download_url(p)})
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return {"out_dir": str(out), "files": rows, "headless": self.headless}

    # --------------------------------------------------------------- health
    def health(self):
        """(ok, report): ffmpeg found, every data folder writable, the encoder.

        Cheap enough to poll: two PATH lookups (cached), a temporary file in
        each folder, a count from sqlite.
        """
        ff = {}
        for name in ("ffmpeg", "ffprobe"):
            try:
                ff[name] = video.find_binary(name, getattr(self.cfg.ffmpeg, name))
            except FileNotFoundError:
                ff[name] = None
        dirs = {}
        for key, p in (("library", self.lib_dir()), ("output", self.out_dir()),
                       ("cache", self.cache_dir()), ("inbox", self.inbox_dir())):
            dirs[key] = {"path": str(p), "writable": _writable(p)}
        try:
            free = shutil.disk_usage(self.inbox_dir()).free
        except OSError:
            free = None
        enc = dict(self.encoder or {"encoder": self.cfg.encode.encoder,
                                    "tested": False})
        from racechrono import encoders
        enc["summary"] = encoders.describe(self.cfg.encode, self.encoder)
        try:
            summary = self.library().summary()
        except Exception as exc:
            summary = {"error": "%s: %s" % (type(exc).__name__, exc)}
        ok = (all(ff.values()) and all(d["writable"] for d in dirs.values())
              and "error" not in summary)
        return ok, {"ok": ok, "headless": self.headless, "ffmpeg": ff,
                    "encoder": enc, "dirs": dirs, "free_bytes": free,
                    "library": summary}

    def client_config(self):
        """What the page needs to know about where it is running."""
        from racechrono import encoders
        return {
            "library_dir": str(self.cfg.library_dir),
            "maps_dir": str(self.cfg.maps_dir),
            "output_dir": str(self.cfg.output_dir),
            "inbox_dir": str(self.inbox_dir()),
            "headless": self.headless,
            "encoder": encoders.describe(self.cfg.encode, self.encoder),
            "upload": {"chunk": uploads_mod.CHUNK,
                       "max_chunk": uploads_mod.MAX_CHUNK,
                       "max_file": self.inbox.max_file,
                       "extensions": sorted(uploads_mod.ALLOWED_EXTS)},
            "summary": self.library().summary(),
        }

    def reveal(self, path):
        """Show a finished file in the desktop's own file browser.

        The alternative is printing a path for someone to paste somewhere, which
        is not a control. Nothing is opened outside the export folder.
        """
        if self.headless:
            raise ValueError("this server has no desktop to show files on; "
                             "download the file instead")
        target = Path(path).resolve()
        out_dir = self.out_dir()
        if target != out_dir and out_dir not in target.parents:
            raise ValueError("only files in %s can be revealed" % out_dir)
        if not target.exists():
            raise ValueError("%s is not there any more" % target.name)
        if sys.platform == "win32":
            if target.is_dir():
                subprocess.Popen(["explorer", str(target)])
            else:
                subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if target.is_file() else "",
                              str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir()
                                             else target.parent)])
        return {"ok": True, "revealed": str(target)}

    def import_progress(self):
        with self._lock:
            job = dict(self._job) if self._job else {"state": "idle"}
        job["library"] = self.library().summary()
        if job.get("state") == "running" and job.get("done"):
            elapsed = time.time() - job["started"]
            per = elapsed / job["done"]
            job["eta_s"] = round(per * max(0, job["total"] - job["done"]), 1)
        return job

    def import_review(self):
        """What the last import produced, for the screen that reviews it."""
        with self._lock:
            job = dict(self._job) if self._job else {}
        result = job.get("result") or {}
        lap_ids = set(result.get("lap_ids", []))
        track_ids = set(result.get("track_ids", []))
        lib = self.library()
        tracks = []
        for t in lib.tracks():
            laps = [l for l in lib.laps(track_id=t["id"]) if l["id"] in lap_ids]
            if not laps and t["id"] not in track_ids:
                continue
            tm = lib.map_of(t["id"])
            rows = []
            for l in sorted(laps, key=lambda r: r["duration_s"]):
                sess = lib.session(l["session_id"])
                rows.append({**self._lap_row(l),
                             "session": sess["name"] if sess else ""})
            tracks.append({
                "id": t["id"], "name": t["name"],
                "closed": bool(t["closed"]),
                "new": t["id"] in track_ids,
                "map": session_mod.map_payload(tm),
                "laps": rows,
            })
        return {"result": result, "source": job.get("source", ""),
                "tracks": tracks, "library": lib.summary()}

    # ------------------------------------------------------------- managing
    #
    # Names and tags are the point of a library: a folder of files already
    # exists, and what it lacks is anything that says what it is. Every level can
    # be renamed and annotated, and none of it touches an id or a source name.

    KINDS = {"track": "tracks", "day": "days", "session": "sessions",
             "lap": "laps", "event": "events"}

    def _table(self, kind):
        table = self.KINDS.get(kind)
        if table is None:
            raise ValueError("nothing called %r can be named" % kind)
        return table

    def rename(self, kind, row_id, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("a name cannot be empty")
        self.library().rename(self._table(kind), row_id, name)
        return {"ok": True, "kind": kind, "id": row_id, "name": name}

    def set_notes(self, kind, row_id, text):
        self.library().set_notes(self._table(kind), row_id, (text or "").strip())
        return {"ok": True, "kind": kind, "id": row_id}

    def tag_event(self, day_id, name):
        """Put a day under an event, reusing one of that name if it exists."""
        lib = self.library()
        name = (name or "").strip()
        if not name:
            lib.set_event(day_id, None)
            return {"ok": True, "day": day_id, "event": ""}
        found = next((e for e in lib.events() if e["name"] == name), None)
        eid = found["id"] if found else lib.add_event(name)
        lib.set_event(day_id, eid)
        return {"ok": True, "day": day_id, "event": name,
                "created": found is None}

    def delete(self, what, ids):
        """Delete laps, their video, or whole tracks. Never a whole library."""
        lib = self.library()
        ids = [i for i in ids if i]
        if not ids:
            raise ValueError("nothing to delete")
        if what == "lap":
            got = lib.delete_laps(ids)
        elif what == "video":
            got = lib.delete_videos(ids)
        elif what == "track":
            # delete_track returns nothing, so the size has to be read before it
            # goes: this is the number the interface reports as freed.
            freed = 0
            for tid in ids:
                freed += sum(l["video_bytes"] + l["data_bytes"]
                             for l in lib.laps(track_id=tid))
                lib.delete_track(tid)
            got = {"count": len(ids), "freed_bytes": freed}
        else:
            raise ValueError("cannot delete %r" % what)
        got["what"] = what
        got["library"] = lib.summary()
        return got

    def reset_library(self):
        before = self.library().reset()
        self.rescan()                 # loaded laps and overlays are gone too
        with self._lock:
            self._plan = None
            self._plan_source = None
            self._summaries = None
            self._listings.clear()
            self._sessions.clear()
            self._telemetry.clear()
            self._job = None
        return {"cleared": before, "library": self.library().summary()}





_PICKER = """
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
path = filedialog.askdirectory(title='Choose a folder of sessions and footage')
root.destroy()
sys.stdout.write(path or '')
"""


def pick_folder(timeout=600):
    """Open the operating system's own folder chooser and return what it gave.

    A browser will not hand over a path -- that is a security boundary, not an
    oversight -- so the picker has to be opened by the server. Which is fine:
    the server is on the same machine as the person clicking, and this is the
    dialog they already know.

    In a throwaway process, because a GUI toolkit started inside a request
    thread of a long-running server is a good way to lose the server.
    """
    try:
        out = subprocess.run([sys.executable, "-c", _PICKER],
                             capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"error": "no python to open a folder chooser with"}
    except subprocess.TimeoutExpired:
        return {"error": "the folder chooser was left open"}
    if out.returncode != 0:
        why = (out.stderr or "").strip().splitlines()
        return {"error": "could not open a folder chooser"
                         + (": " + why[-1] if why else ""),
                "detail": "paste the path instead"}
    path = out.stdout.strip()
    if not path:
        return {"cancelled": True}
    return {"path": str(Path(path))}



def _writable(path):
    """Can a file actually be made here? `os.access` lies on bind mounts."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=str(path)):
            pass
        return True
    except OSError:
        return False


def _roots():
    """Somewhere for a folder picker to start.

    The user's home, and on Windows every drive that answers -- someone's footage
    is as likely to be on an SD card as in Documents.
    """
    out = [{"name": "home", "path": str(Path.home())}]
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path("%s:\\" % letter)
            if drive.exists():
                out.append({"name": "%s:" % letter, "path": str(drive)})
    else:
        out.append({"name": "/", "path": "/"})
    return out



def _options_dict(o):
    return {"pad_s": o.pad_s, "cq": o.cq, "height": o.height, "fps": o.fps,
            "audio": o.audio, "import_video": o.import_video,
            "skip_edge_laps": o.skip_edge_laps, "keep_best": o.keep_best,
            "slower_than_best_pct": o.slower_than_best_pct,
            "max_gap_m": o.max_gap_m, "require_video": o.require_video,
            "event": o.event, "track_name": o.track_name,
            "track_id": o.track_id, "date_override": o.date_override,
            "estimated_mbps": round(o.estimated_mbps(), 2)}


def _options_from(q):
    """Build `ingest.Options` from a query string, ignoring anything absent."""
    from racechrono import ingest as ingest_mod

    def num(key, cast=float):
        v = q.get(key, [None])[0]
        if v is None or v == "":
            return None
        return cast(v)

    def flag(key, default=False):
        v = q.get(key, [None])[0]
        if v is None:
            return default
        return v not in ("0", "false", "no", "")

    o = ingest_mod.Options()
    o.pad_s = num("pad") if num("pad") is not None else o.pad_s
    o.cq = int(num("cq")) if num("cq") is not None else o.cq
    o.height = num("height", int)
    o.fps = num("fps", int)
    o.audio = flag("audio", True)
    o.import_video = flag("video", True)
    o.skip_edge_laps = flag("skip_edge")
    o.keep_best = num("keep_best", int)
    o.slower_than_best_pct = num("within_pct")
    o.max_gap_m = num("max_gap")
    o.require_video = flag("require_video")
    o.event = q.get("event", [""])[0].strip()
    o.track_name = q.get("track", [""])[0].strip()
    o.track_id = q.get("track_id", [""])[0].strip()
    o.date_override = q.get("date", [""])[0]
    return o


# The routes that change something. They are POSTs, so a request for one has to
# carry the X-LapVIEW header (see `Handler.do_POST`); a GET or HEAD of one is
# answered 405 and does nothing. They were GETs once, and a GET is what any page
# on any site can make a browser send -- a link, an `<img src>`, a redirect -- so
# any page open in the same browser could have deleted the library's laps.
ACTIONS = ("/api/rename", "/api/notes", "/api/event", "/api/delete",
           "/api/import/run", "/api/export/run", "/api/reveal", "/api/pick",
           "/api/library/reset", "/api/rescan")
UPLOAD_ROUTES = ("/api/upload/folder", "/api/upload/begin",
                 "/api/upload/chunk", "/api/upload/finish",
                 "/api/upload/abort", "/api/inbox/delete")
POST_ONLY = frozenset(ACTIONS + UPLOAD_ROUTES)


def _text(v):
    """A JSON value as the string its query parameter would have been."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (list, dict)):
        raise ValueError("expected one value, got a %s" % type(v).__name__)
    return str(v)


def _id_list(v):
    """Ids as a JSON list, a comma-separated string, or a list of those."""
    if v is None:
        return []
    parts = v if isinstance(v, list) else [v]
    return [i for part in parts for i in _text(part).split(",") if i]


def _truthy(v):
    return _text(v).strip().lower() in ("1", "yes", "true")


def _as_query(body):
    """A JSON body in the shape `parse_qs` gives, for `_options_from`."""
    return {k: [_text(v)] for k, v in body.items() if v is not None}



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LapVIEW"
    library = None
    # A phone that loses signal mid-chunk would otherwise hold a thread open
    # forever. Five minutes is far longer than any one read or write should
    # take, and an idle keep-alive connection is simply reopened.
    timeout = 300

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _fail(self, code, msg):
        self._json({"error": msg}, code)

    def _q(self):
        return parse_qs(urlparse(self.path).query)

    def _qf(self, q, key, default=None):
        v = q.get(key, [None])[0]
        if v is None or v == "":
            return default
        return float(v)

    def _refuse(self, code, msg):
        """An error after which this connection is not reused.

        A refused upload chunk may still be on its way in; answering and then
        reading the next request out of the middle of it would be nonsense.
        """
        self.close_connection = True
        self._send(code, json.dumps({"error": msg}),
                   "application/json; charset=utf-8", {"Connection": "close"})

    def _trace(self):
        if getattr(self.server, "verbose", False) or \
                getattr(self.server, "headless", False):
            traceback.print_exc()

    # -- routing ---------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = unquote(urlparse(self.path).path)
        try:
            self._route(route)
        except BrokenPipeError:
            pass                        # the browser seeked away mid-response
        except ConnectionResetError:
            pass
        except uploads_mod.UploadError as exc:
            self._fail(exc.code, str(exc))
        except PermissionError as exc:
            self._fail(403, str(exc))
        except KeyError as exc:
            self._fail(404, "no such session: %s" % exc)
        except Exception as exc:
            self._trace()
            self._fail(500, "%s: %s" % (type(exc).__name__, exc))

    def do_POST(self):
        """Everything that changes something: `ACTIONS` and the uploads.

        Every POST must carry `X-LapVIEW: 1`. A page on another site can make a
        browser send a plain POST here, but not one with a custom header -- that
        needs a CORS preflight this server never answers -- so nothing outside
        this app can rename, delete, import, export, open a dialog, or push
        files into the inbox.

        Never answer that preflight. There is no `do_OPTIONS`, so an OPTIONS
        request gets the stdlib's 501, and no response anywhere carries an
        `Access-Control-Allow-*` header; either would hand the header check to
        any site that asked.
        """
        route = unquote(urlparse(self.path).path)
        try:
            if self.headers.get("X-LapVIEW") != "1":
                return self._refuse(403, "POST needs the X-LapVIEW: 1 header")
            self._post(route)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except uploads_mod.UploadError as exc:
            self._refuse(exc.code, str(exc))
        except PermissionError as exc:
            self._refuse(403, str(exc))
        except OSError as exc:
            # A socket timeout mid-chunk lands here too: the page retries it.
            self._trace()
            self._refuse(500, "%s: %s" % (type(exc).__name__, exc))
        except Exception as exc:
            self._trace()
            self._refuse(500, "%s: %s" % (type(exc).__name__, exc))

    def _length(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise uploads_mod.UploadError("send a Content-Length, not a chunked "
                                          "body", 411)
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise uploads_mod.UploadError("Content-Length is required", 411)
        try:
            n = int(raw)
        except ValueError:
            raise uploads_mod.UploadError("bad Content-Length %r" % raw) from None
        if n < 0:
            raise uploads_mod.UploadError("bad Content-Length %r" % raw)
        return n

    def _body_json(self, limit=65536):
        n = self._length()
        if n > limit:
            raise uploads_mod.UploadError("that request body is too big", 413)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise uploads_mod.UploadError("the body is not JSON") from None
        if not isinstance(body, dict):
            raise uploads_mod.UploadError("the body has to be a JSON object")
        return body

    def _drain(self, n):
        """Read and discard a body we are refusing, so the answer arrives."""
        left = n if n <= uploads_mod.MAX_CHUNK else 0
        while left > 0:
            got = self.rfile.read(min(1 << 20, left))
            if not got:
                break
            left -= len(got)

    def _post(self, route):
        inbox = self.library.inbox
        if route == "/api/upload/chunk":
            q = self._q()
            uid = q.get("id", [""])[0]
            try:
                offset = int(q.get("offset", ["-1"])[0])
            except ValueError:
                offset = -1
            n = self._length()
            try:
                inbox.check_chunk(uid, offset, n)
            except uploads_mod.UploadError:
                self._drain(n)
                raise
            return self._json(inbox.write(uid, offset, n, self.rfile))
        body = self._body_json()
        if route == "/api/upload/folder":
            return self._json(inbox.new_folder(body.get("name", "")))
        if route == "/api/upload/begin":
            return self._json(inbox.begin(body.get("folder"), body.get("name"),
                                          body.get("size")))
        if route == "/api/upload/finish":
            return self._json(inbox.finish(body.get("id")))
        if route == "/api/upload/abort":
            return self._json(inbox.abort(body.get("id")))
        if route == "/api/inbox/delete":
            return self._json(inbox.delete_folder(body.get("folder")))
        if route in ACTIONS:
            return self._act(route, body)
        return self._refuse(404, "no POST route for %s" % route)

    def _act(self, route, p):
        """The routes in `ACTIONS`, from a JSON body.

        The names are the query parameters these took as GETs, with the same
        meanings: `{"kind": "lap", "id": "...", "name": "..."}` is what
        `?kind=lap&id=...&name=...` was. A bad kind or an empty id is the
        caller's mistake, not a server fault, and the page shows whatever comes
        back: 400 with a sentence beats 500 with a stack trace.
        """
        lib = self.library

        def s(key):
            return _text(p.get(key))

        missing = "lap" if route in ("/api/export/run", "/api/reveal") else "row"
        try:
            if route == "/api/rename":
                return self._json(lib.rename(s("kind"), s("id"), s("name")))
            if route == "/api/notes":
                return self._json(lib.set_notes(s("kind"), s("id"), s("text")))
            if route == "/api/event":
                return self._json(lib.tag_event(s("day"), s("name")))
            if route == "/api/delete":
                return self._json(lib.delete(s("what"), _id_list(p.get("ids"))))
            if route == "/api/export/run":
                preview = s("preview")
                return self._json(lib.export_start(
                    _id_list(p.get("laps")),
                    preview_s=float(preview) if preview else None,
                    overwrite=_truthy(p.get("overwrite"))))
            if route == "/api/reveal":
                return self._json(lib.reveal(s("path")))
            if route == "/api/import/run":
                src = s("source")
                if not src:
                    return self._fail(400, "import needs a source folder")
                opts = _options_from(_as_query(p))
                # Naming is required, and the route enforces it rather than
                # trusting the screen: this is what writes to the library. A
                # library is worth keeping because its names mean something,
                # and the name a phone puts in a CSV does not -- "Grip 8-22"
                # for a season's sixth event.
                if not opts.event:
                    return self._fail(400, "import needs an event name (event)")
                if not opts.track_id and not opts.track_name:
                    return self._fail(
                        400, "import needs a track: track_id (an existing one) "
                             "or track (a name for a new one)")
                return self._json(lib.import_start(src, opts))
            if route == "/api/library/reset":
                if s("confirm") != "yes":
                    return self._fail(400, 'resetting the library needs '
                                           '{"confirm": "yes"}; it cannot be '
                                           'undone')
                return self._json(lib.reset_library())
            if route == "/api/pick":
                if lib.headless:
                    return self._json({
                        "error": "this server has no desktop to open a folder "
                                 "chooser on",
                        "detail": "upload the files, or pick a folder under %s"
                                  % lib.inbox_dir(), "headless": True})
                return self._json(pick_folder())
            if route == "/api/rescan":
                lib.rescan()
                return self._json(lib.tree())
        except PermissionError as exc:
            return self._fail(403, str(exc))
        except ValueError as exc:
            return self._fail(400, str(exc))
        except KeyError as exc:
            return self._fail(404, "no such %s: %s" % (missing, exc))
        return self._refuse(404, "no POST route for %s" % route)

    def _post_only(self, route):
        """A GET or HEAD of a route that changes something: refused, not done."""
        self._send(405, json.dumps({
            "error": "%s changes something, so it takes a POST with an "
                     "X-LapVIEW: 1 header" % route}),
            "application/json; charset=utf-8", {"Allow": "POST"})

    def _route(self, route):
        lib = self.library
        if route in POST_ONLY:
            return self._post_only(route)
        if route == "/" or route == "/index.html":
            return self._static("index.html")
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])

        if route == "/api/config":
            return self._json(lib.client_config())
        if route == "/api/health":
            ok, report = lib.health()
            return self._json(report, 200 if ok else 503)

        # -- uploads and downloads ---------------------------------------
        if route == "/api/inbox":
            try:
                free = shutil.disk_usage(lib.inbox_dir()).free
            except OSError:
                free = None
            return self._json({"root": str(lib.inbox_dir()), "free_bytes": free,
                               "folders": lib.inbox.folders()})
        if route == "/api/upload/status":
            return self._json(lib.inbox.status(self._q().get("id", [""])[0]))
        if route == "/api/exports":
            return self._json(lib.exports())
        if route.startswith("/download/"):
            try:
                target = lib.export_file(route[len("/download/"):])
            except KeyError:
                return self._fail(404, "no such export")
            return self._serve_file(target, download=True)

        # -- importing ---------------------------------------------------
        if route == "/api/survey":
            src = self._q().get("path", [None])[0]
            if not src:
                return self._fail(400, "survey needs ?path=<folder>")
            return self._json(lib.survey(src))
        if route == "/api/import/review":
            return self._json(lib.import_review())
        if route == "/api/browse":
            return self._json(lib.browse(self._q().get("path", [None])[0]))
        if route == "/api/import":
            src = self._q().get("source", [None])[0]
            if not src:
                return self._fail(400, "import needs ?source=<folder>")
            try:
                return self._json(lib.import_payload(src,
                                                     _options_from(self._q())))
            except FileNotFoundError as exc:
                return self._fail(400, str(exc))
        if route == "/api/import/progress":
            return self._json(lib.import_progress())

        # -- the library -------------------------------------------------
        if route == "/api/library":
            return self._json(lib.tree())
        if route == "/api/export/progress":
            return self._json(lib.export_progress())
        if route == "/api/export":
            q = self._q()
            ids = [i for part in q.get("laps", []) for i in part.split(",")]
            try:
                return self._json(lib.export_offer(ids))
            except ValueError as exc:
                return self._fail(400, str(exc))
        if route == "/api/laps":
            q = self._q()
            return self._json({"laps": lib.laps(
                track_id=q.get("track", [None])[0],
                day_id=q.get("day", [None])[0])})
        if route == "/api/compare":
            q = self._q()
            ids = [i for part in q.get("laps", []) for i in part.split(",") if i]
            chans = [c for part in q.get("channels", ["speed_gps"])
                     for c in part.split(",") if c]
            try:
                return self._json(lib.compare(
                    ids, ref=(q.get("ref", [None])[0] or None),
                    points=int(self._qf(q, "points", 800)),
                    n_sectors=int(self._qf(q, "sectors", 8)),
                    channels=tuple(chans)))
            except ValueError as exc:
                return self._fail(400, str(exc))

        parts = [p for p in route.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api":
            kind, lap_id = parts[1], parts[2]
            if kind == "lap":
                return self._json(lib.describe_lap(lap_id))
            if kind == "comparable":
                return self._json(lib.comparable(lap_id))
            if kind == "trace":
                q = self._q()
                ch = q.get("ch", [None])[0]
                if not ch:
                    return self._fail(400, "trace needs ?ch=<channel>")
                return self._json(lib.trace(lap_id, ch,
                                            int(self._qf(q, "n", 1200))))
            if kind == "layout":
                return self._json(lib.layout(lap_id))
            if kind == "overlay":
                t = self._qf(self._q(), "t", 0.0)
                return self._send(200, lib.overlay_frame(lap_id, t),
                                  "application/octet-stream")

        if len(parts) >= 2 and parts[0] == "video":
            try:
                return self._serve_file(lib.video_path(parts[1]))
            except KeyError:
                return self._fail(404, "lap %s has no video" % parts[1])

        self._fail(404, "no route for %s" % route)

    def _static(self, name):
        # Only files that actually sit in the static directory. `..` in a URL is
        # already normalised away by most clients, but not by all of them.
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._fail(404, "no such asset: %s" % name)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or target.suffix in (".js", ".json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _serve_file(self, path, download=False):
        """Serve a file with byte-range support.

        A `<video>` element will not seek without it: Chrome asks for
        `bytes=0-`, reads the moov atom, then jumps straight to the byte offset
        it wants. Answering 200 with the whole file makes every seek re-download
        a 2 GB clip from the start. The same ranges let a phone resume a
        download that the network dropped.

        `download` adds `Content-Disposition: attachment`, so the browser saves
        the file under its own name instead of playing it in the tab.
        """
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = False
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            lo, _, hi = spec.partition("-")
            try:
                if lo == "":                      # bytes=-500, the last 500
                    start = max(0, size - int(hi))
                else:
                    start = int(lo)
                    if hi:
                        end = min(size - 1, int(hi))
                partial = True
            except ValueError:
                partial = False
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        if download:
            # Headers are latin-1: an ASCII name for old clients, the real one
            # (RFC 6266 / 5987) for everything else.
            plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_"
                            for c in path.name)
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                             % (plain, quote(path.name, safe="")))
            self.send_header("Last-Modified",
                             self.date_time_string(int(path.stat().st_mtime)))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def build_config(args):
    """CONFIG, then the LAPVIEW_* environment, then the command line."""
    import main as main_mod
    raw = config_mod.apply_env(main_mod.CONFIG)
    if args.maps:
        raw["maps_dir"] = args.maps
    if args.library:
        raw["library_dir"] = args.library
    return config_mod.load(raw)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("LAPVIEW_PORT") or 8770),
                    help="default 8770, or LAPVIEW_PORT")
    ap.add_argument("--host", default=os.environ.get("LAPVIEW_HOST")
                    or "127.0.0.1",
                    help="default 127.0.0.1, or LAPVIEW_HOST; 0.0.0.0 to "
                         "serve other machines")
    ap.add_argument("--maps", help="override CONFIG['maps_dir']")
    ap.add_argument("--library", help="override CONFIG['library_dir'] -- the "
                                      "import screen writes here")
    ap.add_argument("--no-open", action="store_true",
                    help="do not launch a browser")
    ap.add_argument("--headless", action="store_true",
                    help="server mode: no folder chooser or file browser, and "
                         "browsing confined to the data folders (also "
                         "LAPVIEW_HEADLESS=1, and the default on Linux with no "
                         "display or when LAPVIEW_DATA_DIR is set)")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    args = ap.parse_args(argv)

    cfg = build_config(args)
    headless = args.headless or headless_default()
    roots = [p for p in (os.environ.get("LAPVIEW_BROWSE_ROOTS") or "")
             .split(os.pathsep) if p.strip()]
    lib = Library(cfg, headless=headless, browse_roots=roots)
    Handler.library = lib
    for d in (lib.lib_dir(), lib.out_dir(), lib.cache_dir()):
        d.mkdir(parents=True, exist_ok=True)
    lib.inbox.ensure()
    swept = lib.inbox.sweep()
    got = lib.library().summary()

    from racechrono import encoders
    lib.encoder = encoders.resolve(cfg, log=print)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.verbose = not args.quiet
    srv.headless = headless
    url = "http://%s:%d/" % (args.host, args.port)
    print("library  %s  (%d track(s), %d lap(s), %s)"
          % (lib.lib_dir(), got["tracks"], got["laps"],
             library_mod.human(got["bytes"])))
    print("exports  %s" % lib.out_dir())
    print("uploads  %s%s" % (lib.inbox_dir(),
                             "  (%d stale partial upload(s) removed)" % swept
                             if swept else ""))
    if headless:
        print("mode     server: browsing limited to %s"
              % ", ".join(str(r) for r in lib.roots()))
    if not got["laps"]:
        print("         empty -- open the library tab and import a folder")
    print("serving  %s   (ctrl-c to stop)" % url)
    if not args.no_open and not headless:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
