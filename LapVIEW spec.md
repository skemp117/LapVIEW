# LapVIEW — Build Specification

## 1. Purpose

Regenerate RaceChrono-style overlaid track videos on the desktop, from **CSV telemetry + raw video only**. No RaceChrono app involvement, no VBO, no `.rcz`. Everything the overlay needs must be derivable from the CSV plus the video files themselves.

Two deliverables:

1. **Render pipeline** — for each run, composite a data overlay onto that run's footage and write a finished MP4.
2. **Map generator** — a separate utility that builds a reusable track map by averaging lap geometry across multiple runs.

---

## 2. Repository layout

```
LapVIEW/
├── main.py                  # entry point; holds the user-editable CONFIG
├── config.py                # dataclasses + defaults + validation
├── racechrono/
│   ├── __init__.py
│   ├── csv_io.py            # CSV v3 loading, column disambiguation, resampling
│   ├── geo.py               # lat/lon → local metres, PCA orient, fit-to-box
│   ├── laps.py              # lap slicing, start/finish recovery, crossings
│   ├── trackmap.py          # map file load/save, GPS → map-space transform
│   ├── video.py             # ffprobe, clip discovery, ordering, timeline
│   ├── sync.py              # data ↔ video alignment (3 strategies)
│   ├── render.py            # frame loop, ffmpeg pipe, compositing
│   └── gauges/
│       ├── base.py          # Gauge ABC: static layer cache + per-frame draw
│       ├── map_gauge.py
│       ├── gmeter.py
│       ├── speedo.py
│       └── tacho.py
├── tools/
│   └── generate_map.py      # multi-run map averaging utility
├── Raw Data/                # INPUT: one CSV per run
├── Raw Footage/             # INPUT: one or more video clips per run
├── Maps/                    # generated track maps (.json + .svg preview)
├── Output/                  # finished overlaid videos
└── cache/                   # sync offsets, ffprobe results, static gauge layers
```

**Dependencies:** `numpy`, `pandas`, `pycairo`, `Pillow`, `scipy` (optional, for filtering). `ffmpeg`/`ffprobe` binaries must be on PATH. Do not add a GUI framework.

Cairo over PIL for gauge drawing — anti-aliased arcs, dashes, and stroke-under-stroke outlines are needed and PIL's are poor.

---

## 3. Input data — CSV format and its traps

Source is RaceChrono Pro v10.2.x "CSV v3", whole-session scope. The file opens with several metadata lines, then a **column header row**, then a **units row**, then data.

Real example header (channel set varies per run — never hardcode positions without validating):

```
timestamp,fragment_id,lap_number,elapsed_time,distance_traveled,accuracy,altitude,bearing,
device_update_rate,fix_type,latitude,longitude,speed,combined_acc,device_update_rate,
lateral_acc,lean_angle,longitudinal_acc,speed,coolant_temp,intake_temp,rpm,speed,throttle_pos
unix time,,,s,m,m,m,deg,Hz,,deg,deg,m/s,G,Hz,G,deg,G,m/s,.C,.C,rpm,m/s,%
```

### 3.1 Loader requirements (`csv_io.py`)

**Find the header row programmatically.** Scan for the first row whose first cell is `timestamp`. Do not assume a fixed skiprows.

**The second row is units, not data.** Read it separately, keep it, drop it from the frame.

**Column names repeat.** `speed` appears three times, `device_update_rate` twice. Pandas silently renames to `speed.1`, `speed.2`. Channels are grouped by source device in order: GPS block → calculated block → OBD block. Disambiguate **positionally, validated against the units row**:

```python
def disambiguate(cols, units):
    seen, out = {}, []
    block = ["gps", "calc", "obd"]
    for i, c in enumerate(cols):
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{block[min(seen[c], 2)]}")
        else:
            seen[c] = 0
            out.append(c)
    return out
```

Then **assert** the units match expectation for every channel you rely on (`speed*` → `m/s`, `rpm` → `rpm`, `*_acc` → `G`, `latitude` → `deg`). A mismatch must raise, not warn. A silently mislabelled channel produces a plausible-looking wrong video, which is the worst failure mode here.

**Speed is m/s.** Convert to mph at the gauge, not at load. Keep SI internally throughout.

**`timestamp` is unix time** (float seconds, GPS-derived). This is the master clock for the whole project.

**Channels log at different rates.** Slower channels (`coolant_temp`, `rpm` on some adapters) repeat their previous value across rows. Provide `resample(df, fps)` returning a frame on a uniform grid at the output frame rate:
- Continuous channels (`speed*`, `rpm`, `*_acc`, `bearing`, `throttle_pos`, `lat`, `lon`) → linear interpolation. **`bearing` must be interpolated on the unit circle** (interp sin and cos separately, then `atan2`), or it will sweep the wrong way through the 359°→1° wrap.
- Step channels (`lap_number`, `fragment_id`, `fix_type`) → forward fill / nearest.

**`fragment_id`** marks session pause/resume. Treat fragment boundaries as hard discontinuities: never interpolate across them.

**Quality gate:** rows with `fix_type` indicating no fix, or `accuracy` above a configurable threshold (default 10 m), get flagged. The map generator excludes them. The renderer holds the last good position and dims the map marker.

---

## 4. Track map utility (`tools/generate_map.py`)

Standalone CLI. Selects **multiple** CSVs and produces one averaged map.

```bash
python tools/generate_map.py \
    --runs "Raw Data/*.csv" \
    --name "AMP Full" \
    --out Maps/amp_full.json \
    --exclude-out-laps \
    --points 720
```

Interactive mode when `--runs` is omitted: list every CSV in `Raw Data/`, let the user tick which to include.

### 4.1 Algorithm

1. Load each run, apply the accuracy gate.
2. Slice into laps on `lap_number`. **Drop the first and last lap of every fragment** (out-lap / in-lap) unless `--keep-edge-laps`.
3. Reject laps whose time deviates from the session median by more than a configurable factor (default 1.25×) — catches cool-down and off-track laps.
4. Project every remaining lap to local metres with a **single shared origin** across all runs (the mean lat/lon of the first accepted run; store it).
5. Resample each lap to `N` points by **normalised cumulative distance**, not by time.
6. **Median** across laps per point index — not mean. Median rejects the one lap where someone went off.
7. Close the loop (append point 0 to the end).
8. Optional light smoothing: Savitzky-Golay, window 9, order 2. Keep it mild; over-smoothing eats the hairpin.
9. Orient via PCA so the long axis is horizontal, then normalise to a unit bounding box.

```python
def orient(x, y):
    p = np.c_[x - x.mean(), y - y.mean()]
    _, _, vt = np.linalg.svd(p, full_matrices=False)
    r = p @ vt.T
    return r[:, 0], r[:, 1], vt
```

### 4.2 Map file format

Store enough to place a live GPS point on the map at render time. This is the part that is easy to get wrong — **the runtime must reuse the map's exact transform, never recompute one from the current run's own extent**, or the marker will drift.

```json
{
  "name": "AMP Full",
  "created": "2026-08-20T14:22:00Z",
  "source_runs": ["run_01.csv", "run_02.csv"],
  "lap_count": 23,
  "origin": {"lat": 34.4262, "lon": -84.4331},
  "rotation": [[0.8123, -0.5832], [0.5832, 0.8123]],
  "extent_m": {"x": [-412.5, 398.1], "y": [-201.3, 244.7]},
  "points_unit": [[0.5012, 0.0], [0.5104, 0.0031], "..."],
  "start_finish": {"point": [0.501, 0.002], "normal": [0.0, 1.0]},
  "length_m": 3218.7
}
```

`points_unit` are in a 0–1 box with Y already flipped for screen space. Provide `trackmap.project(lat, lon, map)` returning unit coordinates via origin → metres → rotation → extent normalisation.

Also emit an SVG preview next to the JSON so the user can eyeball the result without running a render.

### 4.3 Start/finish from CSV only

The CSV has no trap geometry. Recover it from `lap_number` increments:

```python
def start_finish(df):
    i = np.flatnonzero(np.diff(df.lap_number.values) != 0)
    x, y = geo.to_xy(df.latitude.values, df.longitude.values)
    mid = np.c_[(x[i] + x[i+1]) / 2, (y[i] + y[i+1]) / 2]
    hdg = circular_mean(df.bearing.values[i])
    return mid.mean(0), np.array([np.sin(hdg), np.cos(hdg)])
```

Averaging across every crossing in every selected run tightens the centroid well below single-sample resolution (10 Hz at 100 mph is ~4.5 m/sample). If `distance_traveled` resets per lap, interpolate the exact crossing instead of taking midpoints — better, use it.

---

## 5. Run pairing and video timeline

### 5.1 Pairing CSVs to clips

Each CSV in `Raw Data/` is one run. Each run has **one or more** clips in `Raw Footage/`.

Pairing strategy, in order:
1. **Explicit manifest** — `runs.json` mapping CSV stem → ordered clip list. Always wins if present.
2. **Filename prefix** — clips whose stem starts with the CSV stem.
3. **Timestamp overlap** — `ffprobe` `format.tags.creation_time` falling inside the CSV's `timestamp` span (with the drift margin from §6).

Log the chosen strategy per run. If a run matches zero clips, skip it with a clear message; do not fail the whole batch.

### 5.2 Clip ordering and gaps

Order by `creation_time`. For each clip record `(path, start_unix, duration, fps, width, height)`.

Clips are usually chaptered recordings with **small gaps** between files (camera close/open time). Build an explicit timeline: for each clip, the data-time window it covers. Gaps are not rendered — output is per-clip by default, with `--concat` to join finished clips afterwards. Do not attempt to paper over a gap by stretching data.

Handle: mismatched clip resolutions (fail loudly), variable frame rate (force CFR on decode), and rotation metadata (respect it; overlay must land on the *displayed* orientation).

---

## 6. Data ↔ video sync (`sync.py`)

Three strategies, config-selectable, cached per clip in `cache/sync.json` so a re-render is instant.

**`container`** (default first pass) — `ffprobe` `creation_time`, UTC, matched against CSV `timestamp`. Fast, no decode. Expect ±1–3 s: some cameras stamp end-of-recording rather than start, and phone clock drift against GPS time is real. Not good enough alone.

**`audio_rpm`** (default refinement) — cross-correlate the audio envelope against the `rpm` channel. Engine note is a direct physical proxy for a channel already in the data.

```python
sr, hz = 8000, 50
raw = ffmpeg_pcm(clip, sr)                      # mono s16le via pipe
a = np.frombuffer(raw, "<i2").astype(float)
win = sr // hz
env = np.abs(a[:len(a)//win*win].reshape(-1, win)).mean(1)
grid = np.arange(t0, t1, 1/hz)
rpm = np.interp(grid, df.timestamp, df.rpm_obd)
z = lambda v: (v - v.mean()) / v.std()
c = np.correlate(z(env), z(rpm), mode="full")
lag = (c.argmax() - (len(rpm) - 1)) / hz
```

Constrain the search window to `container ± 10 s` rather than correlating the whole session. Slice both signals to a span containing several full laps at speed — the envelope tracks RPM well under load and poorly at idle, so pit lane and cool-down are noise. **Validate the peak**: compute peak-to-second-peak ratio; below a threshold (default 1.5) the lock is untrustworthy — fall back to `container` and warn. A broad or doubled peak usually means it locked onto gear-shift periodicity.

Fallback correlation signal if the exhaust is quiet: `longitudinal_acc` against audio energy.

**`manual`** — per-clip `offset_seconds` in the manifest or via `--offset`. Always available as an override; write refined offsets back to the cache in the same format so a bad auto-sync can be hand-corrected once and stays corrected.

Provide `--sync-check` mode: writes a single PNG contact sheet of ~6 frames with the overlay burned in at wide spacing, so alignment can be verified in seconds without a full render.

---

## 7. Rendering (`render.py`)

### 7.1 Pipeline

One ffmpeg process per clip. Decode source, receive an RGBA overlay stream on a pipe, composite, encode, copy audio:

```
ffmpeg -i <clip> \
       -f rawvideo -pix_fmt rgba -s WxH -r <fps> -i - \
       -filter_complex "[0:v][1:v]overlay=0:0:format=auto" \
       -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p \
       -c:a copy -movflags +faststart <out>
```

Python writes one full-frame RGBA buffer per frame to stdin. Frame *n* maps to data time `clip.start_unix + offset + n / fps`; look up the pre-resampled row by integer index — no per-frame searching.

### 7.2 Performance

Non-negotiable, this is the difference between a 4-minute render and a 40-minute one:

- **Cache static layers.** Gauge faces, tick marks, numerals, the map polyline — draw once to a cairo `ImageSurface` at startup, blit per frame. Only needles, the G dot, the map marker, and numeric text redraw.
- **Render only the dirty regions** where practical: each gauge owns a bounding box; composite gauge surfaces onto a persistent full-frame surface rather than clearing everything.
- **Parallelise across clips**, not within a clip (`multiprocessing`, worker per clip, default `min(cpu_count-1, n_clips)`).
- Text is the usual hidden cost — cache rendered digit glyphs; do not re-shape text every frame.

Target: ≥2× realtime for 1080p30 on a modern desktop CPU.

### 7.3 Preview mode

`--preview N` renders N seconds from a configurable start offset. Every layout iteration should go through this, never a full render.

---

## 8. Overlay elements

All geometry in **fractions of frame height** (except horizontal anchors, fraction of width) so layout survives resolution changes. Origin top-left. Defaults below reproduce the reference screenshot.

### 8.1 Track map — top-left

- Anchor: top-left. Default `x = 0.03` (of width), `y = 0.02` (of height).
- Height: `0.25` of frame height. Width follows the map's aspect ratio.
- **White path with a thin black outline.** Draw the black stroke first at `width + 2 * outline`, then white on top. Defaults: `stroke = 0.004 * H`, `outline = 0.0015 * H`. Round joins and caps — the AMP layout has tight radii and mitre joins will spike.
- No fill, no basemap, no drop shadow.
- **Position marker:** filled red arrow (triangle), rotated to `bearing`, apex forward. Size ~`0.018 * H`. Hold last position and reduce alpha to 40% when the accuracy gate fails.
- Map loaded from `Maps/*.json`; selectable in config, with auto-selection by nearest `origin` to the run's mean position when unset.

### 8.2 Bottom gauge row

Reference layout, left to right: **G-meter (half size) — speedometer (frame-centred) — tachometer**.

- `D = 0.26 * H` — diameter of speedo and tacho.
- G-meter diameter `0.5 * D`.
- Baseline: all three share a bottom edge at `1.0 - 0.02` of height.
- Speedo centre at `x = 0.5` of width, **centred on the frame**, per the reference.
- Tacho centre at speedo centre `+ 1.30 * D`.
- G-meter centre at speedo centre `- 0.95 * D`.

All three offsets configurable; those are defaults.

**Common dial style** (matches reference): open-ended arc of fine tick marks around the perimeter, no full ring; sparse numeric labels; a thin straight **red needle** from centre; large white value text below centre; small white unit label above centre. Semi-transparent dark backing disc, default alpha `0.35`.

**Speedometer** — `speed_gps` converted to mph. Range 0–120 default (configurable). Major ticks every 20, labelled; minor every 5. Needle sweep ~270°, gap at bottom.

**Tachometer** — `rpm_obd`. Range 0–7000 default, labelled in thousands (`1`, `2`, … `7`) as in the reference. Large numeric readout shows full RPM (`5810`). Optional redline arc segment, off by default.

**G-meter** — scatter/dot style, not a needle. Concentric rings at 1G and 2G with labels, dashed crosshair axes, filled red dot positioned by `(lateral_acc, longitudinal_acc)`. Numeric readout below shows **combined magnitude** to one decimal (`0.8`). Sign convention: +lateral → dot right, +longitudinal (acceleration) → dot up; make both invertible in config since adapter mounting varies. Optional short fading trail of the last ~0.5 s, off by default.

### 8.3 Element registry

Every element implements the same interface so config can enable, disable, move, and resize any of them without touching render code:

```python
class Gauge(ABC):
    def __init__(self, cfg, frame_size): ...
    def build_static(self) -> cairo.ImageSurface: ...   # called once
    def draw(self, ctx, row) -> None: ...               # per frame
    @property
    def bbox(self) -> tuple: ...
```

Registered by name in a dict so `CONFIG["elements"]` is data, not code. Stub (disabled by default, unimplemented) entries for future use: `lap_timer`, `sector_delta`, `speed_bar`, `throttle_bar`, `coolant_temp`. The reference image shows lap-timer boxes across the top — leave the slot, do not build it yet.

---

## 9. Configuration

`main.py` holds a single readable `CONFIG` dict at the top — the user edits this file directly, no YAML, no CLI flags for layout. Validated into dataclasses by `config.py` on load, with clear errors on unknown keys and out-of-range values.

```python
CONFIG = {
    "input_data":    "Raw Data",
    "input_footage": "Raw Footage",
    "maps_dir":      "Maps",
    "output_dir":    "Output",

    "map_file": None,              # None = auto-select by location
    "sync":     {"strategy": "audio_rpm", "search_window_s": 10.0,
                 "min_peak_ratio": 1.5},
    "quality":  {"max_accuracy_m": 10.0},
    "encode":   {"crf": 18, "preset": "medium", "copy_audio": True},

    "elements": {
        "track_map": {
            "enabled": True, "anchor": "top-left",
            "x": 0.03, "y": 0.02, "height": 0.25,
            "stroke": 0.004, "outline": 0.0015,
            "color": (1, 1, 1, 1), "outline_color": (0, 0, 0, 1),
            "marker": {"color": (0.9, 0.1, 0.1, 1), "size": 0.018},
        },
        "speedo": {
            "enabled": True, "anchor": "bottom-center",
            "x": 0.50, "y": 0.98, "diameter": 0.26,
            "channel": "speed_gps", "unit": "mph",
            "min": 0, "max": 120, "major": 20, "minor": 5,
        },
        "tacho": {
            "enabled": True, "anchor": "bottom-center",
            "x": None, "offset_d": 1.30,     # relative to speedo, in diameters
            "y": 0.98, "diameter": 0.26,
            "channel": "rpm_obd", "unit": "rpm",
            "min": 0, "max": 7000, "label_divisor": 1000,
        },
        "gmeter": {
            "enabled": True, "anchor": "bottom-center",
            "x": None, "offset_d": -0.95,
            "y": 0.98, "diameter": 0.13,
            "rings": [1, 2], "invert_lateral": False,
            "invert_longitudinal": False, "trail_s": 0.0,
        },
    },
}
```

---

## 10. CLI

```bash
python main.py                          # process every run found
python main.py --run run_03             # one run
python main.py --preview 20 --at 300    # 20 s starting 300 s into the data
python main.py --sync-check             # contact sheet, no render
python main.py --concat                 # join a run's finished clips
python main.py --dry-run                # pair, sync, report; render nothing
```

`--dry-run` should print, per run: CSV path, detected channels with units, lap count, matched clips with resolved offsets and sync confidence, selected map, estimated output duration. This is the first thing to build and the fastest way to catch a bad pairing.

---

## 11. Acceptance criteria

1. `--dry-run` correctly pairs every CSV with its clips and reports plausible offsets.
2. A generated map visually matches the reference screenshot's track shape (AMP Full: long right-hand loop, tight hairpin complex at the lower right).
3. A live position marker traverses the map without drift over a full lap, and returns to within a marker-width of its start.
4. `--sync-check` frames show gauge values consistent with the footage: needle rises as the car accelerates onto a straight, G dot swings the correct direction through a known corner.
5. Full render of a multi-clip run completes with audio intact and no A/V drift at the end of the longest clip.
6. Disabling any element in `CONFIG` removes it cleanly; moving one does not disturb the others.
7. A CSV with a different OBD channel set loads without crashing, or fails with a specific message naming the missing channel.

## 12. Explicit non-goals

No GUI. No VBO or `.rcz` parsing. No lap timing display in v1. No multi-camera PIP. No video stabilisation. No cloud anything.

## 13. Known ambiguities to resolve during build

- Whether `distance_traveled` resets per lap or accumulates across the session — check on real data; it determines whether sub-sample crossing interpolation is available.
- G-meter axis polarity for this specific adapter/mounting — verify against a known corner direction before finalising the default.
- Whether `lean_angle` is meaningful in this car's data (likely zero/absent for a four-wheeled vehicle) — ignore if constant.