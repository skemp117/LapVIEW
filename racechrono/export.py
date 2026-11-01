"""Render an overlaid video from a lap the library holds.

The renderer in `render.py` works from a folder of sessions and their original
footage. This works from the library, which is the only thing that stays
current: the originals are the user's to delete, and the import says when it is
safe to. A lap in the library is everything an export needs --

  * `data.csv`, a real CSV slice that stands alone (its own header, its own
    device rows, the whole timed lap plus its padding),
  * `video.mp4`, the trimmed clip, clean, with no overlay burned into it,
  * `video_t0`, the data time of that clip's first frame, measured at import
    rather than assumed, and
  * the track map, held per track in the library.

So the only work here is putting those four things into the shape
`render_segment` already takes, and naming the output something a person can
find later.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import csv_io, render, trackmap as tm_mod, video, window

# What a lap is called on disk once it leaves the library. The library's own
# names are ids because ids do not collide; an export lands in a folder someone
# browses, so it gets the names they gave things.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text, fallback="lap"):
    out = _UNSAFE.sub("-", str(text or "")).strip(" .")
    out = re.sub(r"\s+", " ", out)
    return out[:120] or fallback


def describe(library, lap_id):
    """Everything the interface needs to offer this export, and nothing more."""
    lap = library.lap(lap_id)
    if lap is None:
        raise KeyError(lap_id)
    sess = library.session(lap["session_id"])
    day = library.day(sess["day_id"]) if sess else None
    track_id = library.track_of_lap(lap_id)
    track = library.track(track_id) if track_id else None
    event = ""
    if day and day["event_id"]:
        event = next((e["name"] for e in library.events()
                      if e["id"] == day["event_id"]), "")
    stem = sess["name"] if sess else ""
    m = re.search(r"_(\d{2})(\d{2})(\d{2})_", stem or "")
    clock = "%s-%s" % (m.group(1), m.group(2)) if m else ""
    bits = [track["name"] if track else "lap",
            event if event and (not track or event != track["name"]) else "",
            day["date"] if day else "",
            clock,
            "%s %d" % (lap["kind"], lap["number"])]
    return {
        "id": lap_id,
        "name": safe_name(" ".join(b for b in bits if b)),
        "label": window.format_lap_time(lap["duration_s"]),
        "duration_s": float(lap["duration_s"]),
        "video_s": float(lap["video_s"]),
        "has_video": bool(lap["video_path"]),
        "track": track["name"] if track else "",
        "track_id": track_id,
    }


def _map_path(cfg, library, track_id):
    """The library's map for this track, as a file, because `render_segment`
    runs from paths so that it can also run in a worker process."""
    tm = library.map_of(track_id) if track_id else None
    if tm is None:
        return None
    out = Path(cfg.cache_dir) / "export-maps" / ("%s.json" % track_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tm.save(out)
    return out


def export_lap(cfg, library, lap_id, out_dir=None, overwrite=False,
               preview_s=None, on_frame=None):
    """One lap in, one overlaid file out.

    `preview_s` renders only the first few seconds, which is how anyone checks a
    layout change without paying for the whole lap.
    """
    got = describe(library, lap_id)
    lap = library.lap(lap_id)
    if not lap["video_path"]:
        raise ValueError("%s has no footage in the library, so there is "
                         "nothing to overlay" % got["name"])

    run_path = library.paths.root / lap["data_path"]
    clip_path = library.paths.root / lap["video_path"]
    if not run_path.is_file():
        raise ValueError("%s has no telemetry file" % got["name"])
    if not clip_path.is_file():
        raise ValueError("%s has no video file" % got["name"])

    clip = video.probe(clip_path, cfg.ffmpeg.ffprobe)
    # The container timestamp on a stored clip is when the import re-encoded it,
    # not when it was recorded. The data time of its first frame was measured at
    # import and is the only trustworthy answer, so it becomes the offset.
    clip.offset = float(lap["video_t0"]) - clip.start_unix
    clip.sync_method = "library"
    clip.sync_confidence = float("inf")

    duration = float(lap["video_s"]) or clip.duration
    if preview_s:
        duration = min(duration, float(preview_s))
    seg = video.Segment(clip=clip, seek=0.0, duration=duration,
                        data_t0=clip.data_t0)

    name = got["name"] + ("_preview" if preview_s else "")
    out_dir = Path(out_dir or cfg.output_dir)
    out_path = out_dir / (name + ".mp4")
    if out_path.exists() and not overwrite:
        return {**got, "out": str(out_path), "skipped": "already exported",
                "bytes": out_path.stat().st_size}

    tm_path = _map_path(cfg, library, got["track_id"])
    res = render.render_segment(cfg, run_path, seg, out_path, tm_path,
                                quiet=True, label=name, on_frame=on_frame)
    return {**got, "out": str(out_path), "bytes": out_path.stat().st_size,
            "seconds": res.get("seconds", duration),
            "speed": res.get("speed", 0.0), "elapsed": res.get("elapsed", 0.0),
            "no_map": tm_path is None}


def export(cfg, library, lap_ids, out_dir=None, overwrite=False,
           preview_s=None, on_lap=None, on_frame=None):
    """Several laps, one after another, reporting as it goes."""
    made = []
    for i, lap_id in enumerate(lap_ids):
        if on_lap:
            on_lap(i, len(lap_ids), describe(library, lap_id))
        try:
            made.append(export_lap(cfg, library, lap_id, out_dir=out_dir,
                                   overwrite=overwrite, preview_s=preview_s,
                                   on_frame=on_frame))
        except Exception as exc:
            made.append({**describe(library, lap_id),
                         "error": "%s: %s" % (type(exc).__name__, exc)})
    return made
