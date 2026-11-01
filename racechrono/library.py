"""The lap library: SQLite index, files on disk, stable ids, editable names.

## Ids, not names, and not filenames

Every record has an opaque id that nothing ever displays, and a `name` that is
only ever displayed. Laps point at sessions by id, sessions at days, days at
tracks. Renaming anything is an `UPDATE` on one row and detaches nothing.

This is not architectural taste. The current code keys a session by its CSV's
stem, a traversal by `"<stem>#<n>"` and a map by its filename, and every one of
those breaks the first time a file is renamed or a track is given its real name
-- which is a stated intention, not a hypothetical. "SCCA Autocross" is going to
become the actual event name.

The name RaceChrono exported is kept in two places, for two different reasons:

- `sessions.source_track_name` -- provenance. What this file said when it
  arrived. Never used to decide anything.
- `track_source_names` -- a set per track, used by the matcher as a shortlist.
  It grows as new names are seen for a track and is untouched by renaming,
  because RaceChrono will keep exporting the old string after the user renames
  the track here.

## What is measured and what is a label

Lap times come from the session's own timing and are not editable. Split times,
deltas and every insight are derived on demand from the stored lap data, so they
are not columns here at all -- refining a map or moving a split changes them
everywhere at once, and there is no cached copy to go stale.

## Deleting a lap

Deletes the row, the video and the data file. Two things deliberately do not
happen:

- **A track map is not rebuilt or altered.** If the lap was one of the traversals
  the map was averaged from, `map_sources` records it as no longer present rather
  than forgetting it, so "built from 12 laps, 3 since deleted" stays visible. A
  later refine uses whatever still exists.
- **A track is not removed when its last lap goes**, as long as it has a map.
  The map is expensive to build and the next event at that course should reuse
  it. Empty sessions and days are removed, because nothing is left to describe.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3

# Applied in order to a library older than `SCHEMA_VERSION`. Each entry is the
# statements that take the schema from `n-1` to `n`.
#
# This exists because the first version of this module shipped without
# `laps.video_t0`, and `CREATE TABLE IF NOT EXISTS` does not add a column to a
# table that already exists -- so an older library opened fine and then failed on
# the next insert, which is worse than refusing to open. A store people keep data
# in needs a way forward, not a version check.
MIGRATIONS = {
    2: [
        "ALTER TABLE laps ADD COLUMN video_t0 REAL",
        "ALTER TABLE laps ADD COLUMN video_s REAL NOT NULL DEFAULT 0",
    ],
    3: [
        "ALTER TABLE laps ADD COLUMN gap_m REAL NOT NULL DEFAULT 0",
        "ALTER TABLE laps ADD COLUMN measured_frac REAL NOT NULL DEFAULT 1",
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    notes    TEXT NOT NULL DEFAULT '',
    closed   INTEGER NOT NULL,          -- 1 circuit, 0 open course
    map_json TEXT,                      -- the TrackMap, or NULL until built
    created  TEXT NOT NULL
);

-- Names this track has been seen under in an import. Used for matching only.
CREATE TABLE IF NOT EXISTS track_source_names (
    track_id    TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    PRIMARY KEY (track_id, source_name)
);

-- Where a track's map came from, kept even when a source lap is deleted.
CREATE TABLE IF NOT EXISTS map_sources (
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    lap_id   TEXT,                      -- NULL when it was not a stored lap
    present  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS splits (
    track_id   TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    distance_m REAL NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (track_id, idx)
);

CREATE TABLE IF NOT EXISTS events (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS days (
    id       TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    date     TEXT NOT NULL,             -- YYYY-MM-DD
    event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    name     TEXT NOT NULL DEFAULT '',
    notes    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    day_id            TEXT NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT '',
    source_file       TEXT NOT NULL DEFAULT '',
    source_track_name TEXT NOT NULL DEFAULT '',
    t0_unix           REAL NOT NULL,
    duration_s        REAL NOT NULL,
    sample_hz         REAL NOT NULL DEFAULT 0,
    calibration       TEXT NOT NULL DEFAULT '{}',
    channels          TEXT NOT NULL DEFAULT '[]',
    shape             TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS laps (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'lap',   -- 'lap' or 'run'
    name        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    t0_unix     REAL NOT NULL,
    t1_unix     REAL NOT NULL,
    duration_s  REAL NOT NULL,
    distance_m  REAL NOT NULL DEFAULT 0,
    pad_s       REAL NOT NULL DEFAULT 0,
    -- Data time of the video's first frame. Not simply t0 - pad_s: padding is
    -- clamped to the footage that exists, so a lap at the end of a clip starts
    -- later than its padding asked for. A player that assumed t0 - pad_s would
    -- be up to 1.8 s out on the laps measured here, and the overlay with it.
    video_t0    REAL,
    video_s     REAL NOT NULL DEFAULT 0,
    video_path  TEXT NOT NULL DEFAULT '',
    data_path   TEXT NOT NULL DEFAULT '',
    video_bytes INTEGER NOT NULL DEFAULT 0,
    data_bytes  INTEGER NOT NULL DEFAULT 0,
    representative INTEGER NOT NULL DEFAULT 1,
    -- How much of the track's centreline this lap actually has fixes for.
    -- At AMP fourteen of twenty-seven laps have a 250-300 m hole in the same
    -- place, where the GPS goes under trees. Recorded at ingest so that
    -- "leave out the laps with holes in them" is a cheap question later.
    gap_m         REAL NOT NULL DEFAULT 0,
    measured_frac REAL NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS laps_by_session ON laps(session_id);
CREATE INDEX IF NOT EXISTS sessions_by_day ON sessions(day_id);
CREATE INDEX IF NOT EXISTS days_by_track ON days(track_id);
"""


def new_id():
    return uuid.uuid4().hex[:16]


def human(n):
    """Bytes as something readable. Here rather than in a CLI: both of them
    want it and a library reports sizes constantly."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


@dataclass
class Paths:
    root: Path

    @property
    def db(self):
        return self.root / "library.db"

    def track_dir(self, track_id):
        return self.root / "tracks" / track_id

    def lap_dir(self, lap_id):
        return self.root / "laps" / lap_id


class Library:
    """A library rooted at a directory. Cheap to open, safe to reopen."""

    def __init__(self, root):
        self.paths = Paths(Path(root))
        self.paths.root.mkdir(parents=True, exist_ok=True)
        (self.paths.root / "laps").mkdir(exist_ok=True)
        (self.paths.root / "tracks").mkdir(exist_ok=True)
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema', ?)",
                            (str(SCHEMA_VERSION),))
            self.migrated = []
        else:
            self.migrated = self._migrate(int(row["value"]))
        self.db.commit()

    def _migrate(self, have):
        """Bring an older library up to `SCHEMA_VERSION`. Returns what ran."""
        if have > SCHEMA_VERSION:
            raise RuntimeError(
                "this library is schema v%d and this code understands v%d; "
                "it was written by a newer version" % (have, SCHEMA_VERSION))
        done = []
        for version in range(have + 1, SCHEMA_VERSION + 1):
            for sql in MIGRATIONS.get(version, []):
                try:
                    self.db.execute(sql)
                except sqlite3.OperationalError as exc:
                    # Re-running a migration on a library that already has the
                    # column is not an error; anything else is.
                    if "duplicate column" not in str(exc).lower():
                        raise
            done.append(version)
        if done:
            self.db.execute("UPDATE meta SET value=? WHERE key='schema'",
                            (str(SCHEMA_VERSION),))
        return done

    @property
    def db(self):
        """A connection belonging to the calling thread.

        SQLite objects cannot cross threads, and the web server answers every
        request on a different one -- so a library opened while serving one
        request failed on the next with "created in thread 21132, this is thread
        44676". The page swallowed the error and kept showing the previous plan,
        which read as a one-request lag rather than a failure.

        A connection per thread rather than one connection behind a lock: the
        reads are frequent and short, and this way they do not queue behind each
        other.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.paths.db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- tracks
    def add_track(self, name, closed, source_names=(), notes=""):
        tid = new_id()
        self.db.execute(
            "INSERT INTO tracks (id, name, notes, closed, created) "
            "VALUES (?,?,?,?,?)",
            (tid, name, notes, 1 if closed else 0,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        for s in source_names:
            self.observe_source_name(tid, s)
        self.db.commit()
        return tid

    def observe_source_name(self, track_id, source_name):
        """Record a name an import arrived under. Never removed by a rename."""
        if not source_name:
            return
        self.db.execute("INSERT OR IGNORE INTO track_source_names VALUES (?,?)",
                        (track_id, source_name))
        self.db.commit()

    def source_names(self, track_id):
        return [r["source_name"] for r in self.db.execute(
            "SELECT source_name FROM track_source_names WHERE track_id=?",
            (track_id,))]

    def tracks(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM tracks ORDER BY name")]

    def track(self, track_id):
        r = self.db.execute("SELECT * FROM tracks WHERE id=?",
                            (track_id,)).fetchone()
        return dict(r) if r else None

    def set_map(self, track_id, tm, sources=()):
        """Store or replace a track's map, recording what it was built from."""
        self.db.execute("UPDATE tracks SET map_json=?, closed=? WHERE id=?",
                        (json.dumps(tm.to_dict()), 1 if tm.closed else 0,
                         track_id))
        self.db.execute("DELETE FROM map_sources WHERE track_id=?", (track_id,))
        for label, lap_id in sources:
            self.db.execute(
                "INSERT INTO map_sources (track_id, label, lap_id, present) "
                "VALUES (?,?,?,1)", (track_id, label, lap_id))
        d = self.paths.track_dir(track_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "map.json").write_text(json.dumps(tm.to_dict(), indent=1),
                                    encoding="utf-8")
        self.db.commit()

    def map_of(self, track_id):
        from . import trackmap
        r = self.db.execute("SELECT map_json FROM tracks WHERE id=?",
                            (track_id,)).fetchone()
        if not r or not r["map_json"]:
            return None
        d = json.loads(r["map_json"])
        return trackmap.TrackMap(
            name=d.get("name", ""), origin=d["origin"], rotation=d["rotation"],
            extent_m=d["extent_m"], points_unit=d["points_unit"],
            start_finish=d.get("start_finish", {}),
            closed=bool(d.get("closed", True)), start=d.get("start", {}),
            finish=d.get("finish", {}), length_m=d.get("length_m", 0.0),
            lap_count=d.get("lap_count", 0),
            source_runs=d.get("source_runs", []), created=d.get("created", ""),
            orientation=d.get("orientation", "north"),
            version=d.get("version", 1))

    def map_provenance(self, track_id):
        return [dict(r) for r in self.db.execute(
            "SELECT label, lap_id, present FROM map_sources WHERE track_id=?",
            (track_id,))]

    def maps(self):
        """`{track_id: TrackMap}` for every track that has one."""
        out = {}
        for t in self.tracks():
            tm = self.map_of(t["id"])
            if tm is not None:
                # The matcher wants the names this track has been seen under.
                tm.source_names = self.source_names(t["id"])
                out[t["id"]] = tm
        return out

    # ---------------------------------------------------------------- splits
    def set_splits(self, track_id, distances, names=None):
        """Replace a track's splits. Distances along its centreline, metres."""
        self.db.execute("DELETE FROM splits WHERE track_id=?", (track_id,))
        for i, d in enumerate(sorted(float(x) for x in distances)):
            nm = "" if not names or i >= len(names) else names[i]
            self.db.execute("INSERT INTO splits VALUES (?,?,?,?)",
                            (track_id, i, d, nm))
        self.db.commit()

    def splits(self, track_id):
        return [dict(r) for r in self.db.execute(
            "SELECT idx, distance_m, name FROM splits WHERE track_id=? "
            "ORDER BY idx", (track_id,))]

    # ------------------------------------------------------- events and days
    def add_event(self, name, notes=""):
        eid = new_id()
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (eid, name, notes))
        self.db.commit()
        return eid

    def events(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events ORDER BY name")]

    def add_day(self, track_id, date, event_id=None, name="", notes=""):
        did = new_id()
        self.db.execute(
            "INSERT INTO days (id, track_id, date, event_id, name, notes) "
            "VALUES (?,?,?,?,?,?)", (did, track_id, date, event_id, name, notes))
        self.db.commit()
        return did

    def find_day(self, track_id, date):
        r = self.db.execute("SELECT * FROM days WHERE track_id=? AND date=?",
                            (track_id, date)).fetchone()
        return dict(r) if r else None

    def day_for(self, track_id, date, event_id=None):
        """The day record for this track and date, created if absent."""
        got = self.find_day(track_id, date)
        if got:
            return got["id"]
        return self.add_day(track_id, date, event_id=event_id)

    def day(self, day_id):
        row = self.db.execute("SELECT * FROM days WHERE id=?",
                              (day_id,)).fetchone()
        return dict(row) if row else None

    def days(self, track_id=None):
        if track_id:
            rows = self.db.execute(
                "SELECT * FROM days WHERE track_id=? ORDER BY date",
                (track_id,))
        else:
            rows = self.db.execute("SELECT * FROM days ORDER BY date")
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- sessions
    def add_session(self, day_id, name, t0_unix, duration_s, **kw):
        sid = new_id()
        self.db.execute(
            "INSERT INTO sessions (id, day_id, name, notes, source_file, "
            "source_track_name, t0_unix, duration_s, sample_hz, calibration, "
            "channels, shape) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, day_id, name, kw.get("notes", ""), kw.get("source_file", ""),
             kw.get("source_track_name", ""), float(t0_unix),
             float(duration_s), float(kw.get("sample_hz", 0.0)),
             json.dumps(kw.get("calibration") or {}),
             json.dumps(kw.get("channels") or []),
             json.dumps(kw.get("shape") or {})))
        self.db.commit()
        return sid

    def sessions(self, day_id=None):
        if day_id:
            rows = self.db.execute(
                "SELECT * FROM sessions WHERE day_id=? ORDER BY t0_unix",
                (day_id,))
        else:
            rows = self.db.execute("SELECT * FROM sessions ORDER BY t0_unix")
        return [dict(r) for r in rows]

    def session_recorded_at(self, t0_unix, duration_s, tol=0.25):
        """An already-imported session that recorded the same span, if any.

        Importing the same folder twice used to double the library: nothing
        compared a session about to be written against what was already there,
        and the within-import md5 check cannot see across imports. A recording
        is identified by when it started and how long it ran -- a phone cannot
        produce two different sessions from the same instant, and a re-export of
        the same session lands on the same pair.
        """
        row = self.db.execute(
            "SELECT * FROM sessions WHERE abs(t0_unix - ?) <= ? "
            "AND abs(duration_s - ?) <= ? ORDER BY t0_unix LIMIT 1",
            (float(t0_unix), float(tol), float(duration_s), float(tol))).fetchone()
        return dict(row) if row else None

    def session(self, session_id):
        r = self.db.execute("SELECT * FROM sessions WHERE id=?",
                            (session_id,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ laps
    def add_lap(self, session_id, number, t0_unix, t1_unix, **kw):
        lid = new_id()
        self.paths.lap_dir(lid).mkdir(parents=True, exist_ok=True)
        self.db.execute(
            "INSERT INTO laps (id, session_id, number, kind, name, notes, "
            "t0_unix, t1_unix, duration_s, distance_m, pad_s, video_t0, "
            "video_s, video_path, data_path, video_bytes, data_bytes, "
            "representative, gap_m, measured_frac) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, session_id, int(number), kw.get("kind", "lap"),
             kw.get("name", ""), kw.get("notes", ""),
             float(t0_unix), float(t1_unix),
             float(kw.get("duration_s", t1_unix - t0_unix)),
             float(kw.get("distance_m", 0.0)), float(kw.get("pad_s", 0.0)),
             kw.get("video_t0"), float(kw.get("video_s", 0.0)),
             kw.get("video_path", ""), kw.get("data_path", ""),
             int(kw.get("video_bytes", 0)), int(kw.get("data_bytes", 0)),
             1 if kw.get("representative", True) else 0,
             float(kw.get("gap_m", 0.0)), float(kw.get("measured_frac", 1.0))))
        self.db.commit()
        return lid

    def laps(self, session_id=None, day_id=None, track_id=None):
        sql = ("SELECT laps.* FROM laps "
               "JOIN sessions ON sessions.id = laps.session_id "
               "JOIN days ON days.id = sessions.day_id")
        where, args = [], []
        if session_id:
            where.append("laps.session_id=?")
            args.append(session_id)
        if day_id:
            where.append("sessions.day_id=?")
            args.append(day_id)
        if track_id:
            where.append("days.track_id=?")
            args.append(track_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY laps.t0_unix"
        return [dict(r) for r in self.db.execute(sql, args)]

    def lap(self, lap_id):
        r = self.db.execute("SELECT * FROM laps WHERE id=?", (lap_id,)).fetchone()
        return dict(r) if r else None

    def track_of_lap(self, lap_id):
        r = self.db.execute(
            "SELECT days.track_id AS tid FROM laps "
            "JOIN sessions ON sessions.id=laps.session_id "
            "JOIN days ON days.id=sessions.day_id WHERE laps.id=?",
            (lap_id,)).fetchone()
        return r["tid"] if r else None

    # -------------------------------------------------------------- deletion
    def delete_lap(self, lap_id, prune=True):
        """Remove a lap, its video and its data. Irreversible.

        Returns what was freed and what was pruned, so a caller can say so.
        """
        lap = self.lap(lap_id)
        if lap is None:
            raise KeyError(lap_id)
        session_id = lap["session_id"]
        freed = int(lap["video_bytes"]) + int(lap["data_bytes"])
        d = self.paths.lap_dir(lap_id)
        if d.exists():
            freed = max(freed, sum(p.stat().st_size for p in d.rglob("*")
                                   if p.is_file()))
            shutil.rmtree(d, ignore_errors=True)

        # A map built from this lap keeps its provenance and its geometry.
        self.db.execute("UPDATE map_sources SET present=0 WHERE lap_id=?",
                        (lap_id,))
        self.db.execute("DELETE FROM laps WHERE id=?", (lap_id,))

        pruned = {"sessions": [], "days": []}
        if prune:
            if not self.laps(session_id=session_id):
                sess = self.session(session_id)
                self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                pruned["sessions"].append(session_id)
                day_id = sess["day_id"] if sess else None
                if day_id and not self.sessions(day_id=day_id):
                    self.db.execute("DELETE FROM days WHERE id=?", (day_id,))
                    pruned["days"].append(day_id)
        self.db.commit()
        return {"freed_bytes": freed, "pruned": pruned}

    def delete_video(self, lap_id):
        """Drop a lap's video and keep its data.

        Video is 99% of a lap's size -- 276 MB against 2 MB on a typical AMP
        lap -- and the two are useful separately. A lap someone has finished
        watching is still a lap they want in a comparison, and there is no reason
        holding on to the telemetry should cost a quarter of a gigabyte.
        """
        lap = self.lap(lap_id)
        if lap is None:
            raise KeyError(lap_id)
        freed = int(lap["video_bytes"])
        if lap["video_path"]:
            path = self.paths.root / lap["video_path"]
            if path.is_file():
                freed = max(freed, path.stat().st_size)
                path.unlink()
        self.db.execute(
            "UPDATE laps SET video_path='', video_bytes=0, video_t0=NULL, "
            "video_s=0 WHERE id=?", (lap_id,))
        self.db.commit()
        return {"freed_bytes": freed}

    def delete_videos(self, lap_ids):
        total = 0
        ids = list(lap_ids)
        for lid in ids:
            total += self.delete_video(lid)["freed_bytes"]
        return {"freed_bytes": total, "count": len(ids)}

    def delete_laps(self, lap_ids):
        total, pruned = 0, {"sessions": [], "days": []}
        for lid in list(lap_ids):
            got = self.delete_lap(lid)
            total += got["freed_bytes"]
            for k in pruned:
                pruned[k].extend(got["pruned"][k])
        return {"freed_bytes": total, "pruned": pruned, "count": len(lap_ids)}

    def select_laps(self, track_id=None, day_id=None, session_id=None,
                    slower_than=None, non_representative=False,
                    keep_best=None, no_video=False, has_video=False,
                    gap_over_m=None, slower_than_best_pct=None):
        """Laps matching a rule, for review before deleting them.

        Reclaiming space is the practical reason to delete anything, and doing it
        one lap at a time is not how anyone would. Every argument narrows the
        set; nothing here deletes.

        - `slower_than` -- seconds, absolute
        - `non_representative` -- the out-laps and in-laps. Of 27 AMP laps, 19
          are representative and the rest include a 3m35 in-lap.
        - `keep_best` -- keep the N quickest *per track* and select the rest
        - `no_video` -- laps whose footage never existed, which are data only
        """
        rows = self.laps(session_id=session_id, day_id=day_id, track_id=track_id)
        if non_representative:
            rows = [r for r in rows if not r["representative"]]
        if no_video:
            rows = [r for r in rows if not r["video_path"]]
        if has_video:
            rows = [r for r in rows if r["video_path"]]
        if gap_over_m is not None:
            rows = [r for r in rows if r["gap_m"] > float(gap_over_m)]
        if slower_than is not None:
            rows = [r for r in rows if r["duration_s"] > float(slower_than)]
        if slower_than_best_pct is not None:
            # Relative to the best lap of the same track, which is the only
            # comparison that means anything across venues.
            best = {}
            for r in rows:
                tid = self.track_of_lap(r["id"])
                cur = best.get(tid)
                if cur is None or r["duration_s"] < cur:
                    best[tid] = r["duration_s"]
            limit = 1.0 + float(slower_than_best_pct) / 100.0
            rows = [r for r in rows
                    if r["duration_s"] > best[self.track_of_lap(r["id"])] * limit]
        if keep_best is not None:
            by_track = {}
            for r in rows:
                by_track.setdefault(self.track_of_lap(r["id"]), []).append(r)
            out = []
            for tid, group in by_track.items():
                group.sort(key=lambda r: r["duration_s"])
                out.extend(group[int(keep_best):])
            rows = out
        return rows

    def reset(self):
        """Empty the library completely, keeping the directory and the schema.

        A real operation rather than a note telling someone to delete a folder:
        it is the natural thing to want after a trial import, and doing it by
        hand invites deleting the wrong directory. The originals are untouched,
        because the library never held them -- everything here was derived from
        `Raw Data` and `Raw Footage` and can be built again.
        """
        before = self.summary()
        for table in ("laps", "sessions", "days", "splits", "map_sources",
                      "track_source_names", "events", "tracks"):
            self.db.execute("DELETE FROM %s" % table)
        self.db.commit()
        for sub in ("laps", "tracks"):
            root = self.paths.root / sub
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
        self.vacuum()
        return before

    def delete_track(self, track_id):
        """Explicit, and it takes everything under it."""
        for lap in self.laps(track_id=track_id):
            d = self.paths.lap_dir(lap["id"])
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        self.db.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        self.db.commit()
        td = self.paths.track_dir(track_id)
        if td.exists():
            shutil.rmtree(td, ignore_errors=True)

    # ------------------------------------------------------- merge and split
    def merge_tracks(self, keep_id, *other_ids):
        """Fold other tracks into this one.

        The identifier reports an ambiguity rather than guessing, and reporting it
        is only useful if the user can then act. Acting is either merging two
        tracks that turned out to be one, or splitting one that turned out to be
        two. Both need to move days without touching laps.

        The kept track absorbs the others' source names, so a later import
        arriving under either name matches. Maps are not merged: the kept track
        keeps its own, and refining it is a separate, explicit step.
        """
        keep = self.track(keep_id)
        if keep is None:
            raise KeyError(keep_id)
        # Every id is checked before anything moves. Checking inside the loop
        # meant a typo in the third id left the first two already merged, with a
        # write transaction open behind the error.
        for other in other_ids:
            if other != keep_id and self.track(other) is None:
                raise KeyError(other)
        moved = 0
        for other in other_ids:
            if other == keep_id:
                continue
            for name in self.source_names(other):
                self.observe_source_name(keep_id, name)
            for day in self.days(other):
                mine = self.find_day(keep_id, day["date"])
                if mine is None:
                    self.db.execute("UPDATE days SET track_id=? WHERE id=?",
                                    (keep_id, day["id"]))
                else:
                    self.db.execute(
                        "UPDATE sessions SET day_id=? WHERE day_id=?",
                        (mine["id"], day["id"]))
                    self.db.execute("DELETE FROM days WHERE id=?", (day["id"],))
                moved += 1
            # Provenance follows, marked as coming from a merged track.
            self.db.execute(
                "UPDATE map_sources SET track_id=?, label=label || ' (merged)' "
                "WHERE track_id=?", (keep_id, other))
            self.db.execute("DELETE FROM tracks WHERE id=?", (other,))
            td = self.paths.track_dir(other)
            if td.exists():
                shutil.rmtree(td, ignore_errors=True)
        self.db.commit()
        return {"kept": keep_id, "absorbed": list(other_ids), "days": moved}

    def split_track(self, track_id, session_ids, name=None):
        """Move some sessions of a track onto a new track of their own.

        The other half of resolving an ambiguity: one track that is really two
        courses in the same lot. The new track has no map until one is built,
        which is deliberate -- the old map describes the old shape.
        """
        old = self.track(track_id)
        if old is None:
            raise KeyError(track_id)
        wanted = set(session_ids)
        if not wanted:
            raise ValueError("split needs at least one session")
        new_id = self.add_track(name or (old["name"] + " (split)"),
                               closed=bool(old["closed"]),
                               source_names=self.source_names(track_id))
        moved = 0
        for sess in self.sessions():
            if sess["id"] not in wanted:
                continue
            day = self.db.execute("SELECT * FROM days WHERE id=?",
                                  (sess["day_id"],)).fetchone()
            if day is None or day["track_id"] != track_id:
                continue
            target = self.day_for(new_id, day["date"])
            self.db.execute("UPDATE sessions SET day_id=? WHERE id=?",
                            (target, sess["id"]))
            moved += 1
        for day in list(self.days(track_id)):
            if not self.sessions(day_id=day["id"]):
                self.db.execute("DELETE FROM days WHERE id=?", (day["id"],))
        self.db.commit()
        return {"track": new_id, "sessions": moved}

    # --------------------------------------------------------------- renaming
    _RENAMEABLE = {"tracks", "events", "days", "sessions", "laps"}

    def rename(self, table, row_id, name):
        """Change a display name. Never touches an id or a source name."""
        if table not in self._RENAMEABLE:
            raise ValueError("%s has no display name" % table)
        cur = self.db.execute("UPDATE %s SET name=? WHERE id=?" % table,
                              (name, row_id))
        if cur.rowcount == 0:
            # Roll back before raising. sqlite3 opens a write transaction on the
            # first UPDATE, and leaving it open held the database lock for the
            # life of this thread's connection: renaming a row that did not
            # exist made every later write from any other thread fail with
            # "database is locked", which reads as corruption rather than as a
            # typo three requests ago.
            self.db.rollback()
            raise KeyError(row_id)
        if table == "tracks":
            # The stored map carries a name of its own, which ends up on the SVG
            # preview and in an export. One track should not have two names, so
            # it follows the track rather than remembering what it was called
            # when it was built.
            self._retitle_map(row_id, name)
        self.db.commit()

    def _retitle_map(self, track_id, name):
        r = self.db.execute("SELECT map_json FROM tracks WHERE id=?",
                            (track_id,)).fetchone()
        if not r or not r["map_json"]:
            return
        d = json.loads(r["map_json"])
        d["name"] = name
        self.db.execute("UPDATE tracks SET map_json=? WHERE id=?",
                        (json.dumps(d), track_id))
        path = self.paths.track_dir(track_id) / "map.json"
        if path.exists():
            path.write_text(json.dumps(d, indent=1), encoding="utf-8")

    def set_notes(self, table, row_id, notes):
        if table not in self._RENAMEABLE:
            raise ValueError("%s has no notes" % table)
        self.db.execute("UPDATE %s SET notes=? WHERE id=?" % table,
                        (notes, row_id))
        self.db.commit()

    def set_event(self, day_id, event_id):
        self.db.execute("UPDATE days SET event_id=? WHERE id=?",
                        (event_id, day_id))
        self.db.commit()

    def move_session(self, session_id, track_id, date=None):
        """Reassign a session to another track, creating the day if needed."""
        sess = self.session(session_id)
        if sess is None:
            raise KeyError(session_id)
        old_day = self.db.execute("SELECT * FROM days WHERE id=?",
                                  (sess["day_id"],)).fetchone()
        when = date or (dict(old_day)["date"] if old_day else "")
        day_id = self.day_for(track_id, when)
        self.db.execute("UPDATE sessions SET day_id=? WHERE id=?",
                        (day_id, session_id))
        if old_day and not self.sessions(day_id=old_day["id"]):
            self.db.execute("DELETE FROM days WHERE id=?", (old_day["id"],))
        self.db.commit()
        return day_id

    # ----------------------------------------------------------------- verify
    def verify(self, repair=False):
        """Look for the ways a library and its files can drift apart.

        Editing and deleting over months is what makes this necessary: an
        interrupted ingest leaves a lap directory with no row, a hand-deleted
        file leaves a row with no lap, and a byte count recorded at write time
        stops being true if anything touches the file. None of it is fatal and
        all of it is worth knowing about.

        With `repair`, the safe half is fixed: byte counts are re-read from the
        files, rows pointing at absent files have their paths cleared, and
        orphaned directories are removed. Rows are never invented and files are
        never re-encoded.
        """
        report = {"missing_files": [], "orphan_dirs": [], "byte_drift": [],
                  "orphan_rows": [], "repaired": 0}
        seen_dirs = set()
        for lap in self.laps():
            d = self.paths.lap_dir(lap["id"])
            seen_dirs.add(d.name)
            for kind, rel, col in (("video", lap["video_path"], "video_bytes"),
                                   ("data", lap["data_path"], "data_bytes")):
                if not rel:
                    continue
                path = self.paths.root / rel
                if not path.is_file():
                    report["missing_files"].append((lap["id"], kind, rel))
                    if repair:
                        self.db.execute(
                            "UPDATE laps SET %s='', %s=0 WHERE id=?"
                            % ({"video": "video_path",
                                "data": "data_path"}[kind], col),
                            (lap["id"],))
                        report["repaired"] += 1
                    continue
                real = path.stat().st_size
                if real != lap[col]:
                    report["byte_drift"].append((lap["id"], kind, lap[col], real))
                    if repair:
                        self.db.execute("UPDATE laps SET %s=? WHERE id=?" % col,
                                        (real, lap["id"]))
                        report["repaired"] += 1

        laps_root = self.paths.root / "laps"
        if laps_root.is_dir():
            for d in laps_root.iterdir():
                if d.is_dir() and d.name not in seen_dirs:
                    size = sum(f.stat().st_size for f in d.rglob("*")
                               if f.is_file())
                    report["orphan_dirs"].append((d.name, size))
                    if repair:
                        shutil.rmtree(d, ignore_errors=True)
                        report["repaired"] += 1

        # Rows whose parent went missing. Foreign keys should prevent it, but a
        # library edited by hand or copied mid-write can still show it.
        for sql, label in (
                ("SELECT laps.id FROM laps LEFT JOIN sessions "
                 "ON sessions.id=laps.session_id WHERE sessions.id IS NULL",
                 "lap without a session"),
                ("SELECT sessions.id FROM sessions LEFT JOIN days "
                 "ON days.id=sessions.day_id WHERE days.id IS NULL",
                 "session without a day"),
                ("SELECT days.id FROM days LEFT JOIN tracks "
                 "ON tracks.id=days.track_id WHERE tracks.id IS NULL",
                 "day without a track")):
            for r in self.db.execute(sql):
                report["orphan_rows"].append((label, r[0]))
        if repair:
            self.db.commit()
        return report

    def vacuum(self):
        self.db.commit()
        self.db.execute("VACUUM")

    # ----------------------------------------------------------------- stats
    def summary(self):
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "tracks": q("SELECT COUNT(*) FROM tracks"),
            "days": q("SELECT COUNT(*) FROM days"),
            "sessions": q("SELECT COUNT(*) FROM sessions"),
            "laps": q("SELECT COUNT(*) FROM laps"),
            "bytes": q("SELECT COALESCE(SUM(video_bytes+data_bytes),0) FROM laps"),
            "on_disk": sum(f.stat().st_size
                           for f in self.paths.root.rglob("*") if f.is_file()),
        }
